# 全面功能审核与加固记录（2026-07）

本轮审核目标：对照 README 逐条核对功能实现的真实性与完整度，重点检验**模型接入、自主对话智能问诊、规则软约束下的模型意见输出**三大主线，并使模型在"自身中医知识 + 沈老经验规则模板"结合下发挥最佳性能。审核方式：四路并行深度代码审查（LLM 接入层 / 自主问诊 / 规则引擎与融合 / 服务端与前端与多智能体）+ 全量测试 + mock 后端端到端冒烟。

## 一、总体结论

**README 声明的功能全部有真实实现，无空壳/假实现。** 116 项既有测试全部通过；`/api/chat`、`/api/interview`、`/api/followup_probe`、`/api/collaboration`、`/api/autonomous` 在 `TAO_BACKEND=mock` 下端到端可用；前端对"模型真实路由 vs 关键词回退 vs 离线"的标注诚实；多智能体协作含真实的基于内容的控制流（红旗命中即中止下游）。

但审核发现一批影响"模型发挥最佳性能"与安全性的实质缺陷（下表），本轮已全部修复，并新增 20 项回归测试（合计 136 项，全绿）。

## 二、发现与修复对照

### A. 模型接入层（backend/llm/）

| 级别 | 发现 | 修复 |
|---|---|---|
| 高 | `_generate_http` 不包裹网络异常：http 后端超时/连接失败会穿透为 HTTP 500，违背"失败自动回退确定性规则"的核心承诺 | 所有网络/解析失败转为 `DaoRuntimeError`；5xx/超时有限重试（指数退避），4xx 立即失败；全部消费者恢复回退保证 |
| 高 | http 路径把本地手工拼的 Qwen `<|im_start|>` 模板串塞进 `messages[user]`，且 system 提示重复两次——OpenAI 兼容端点会二次套模板，严重损害输出质量 | http 路径改发原生 system+user messages（含多轮 history），Qwen 模板仅在本地 transformers 路径应用 |
| 中 | `config/model_config.yaml` 的 `inference_profiles` 从未被加载：结构化 JSON 任务（路由/规划/抽取）在 temp=0.3 采样下跑，稳定性差；长文会诊被 2048 token 截断 | 真正加载 profiles：结构化任务 `do_sample=false, temp=0.1, 1024 token`；教学/会诊 `3072 token`；显式 `TAO_*` 环境变量优先级最高 |
| 中 | 流式推理子线程异常被吞，返回空串/半截文本 | 子线程异常捕获回传，join 后抛 `DaoRuntimeError` |
| 中 | 教学报告/推理/经验总结要求"长 Markdown 塞进 JSON 字符串"，换行/引号易破坏 JSON 导致静默回退 | 提示模板补 `\n`/`\"` 转义指引 + token 预算提升至 3072 |
| 高（并发） | 共享 HF 模型实例的 `generate()` 无锁，ThreadingHTTPServer 多请求并发会竞争 KV cache/CUDA 状态 | `_generate_lock` 进程级串行化推理 |

### B. 输出守卫（软约束落地，backend/llm/output_guard.py）

原实现的核心矛盾：会诊路径用角色感知软守卫（医师草案可含方剂/方义/经验剂量范围），但三个**同为医师端**的叠加技能（教学报告/经验推理/经验按语）用硬关键词守卫——而这三个技能的提示词**主动要求**模型写"经验剂量区间""疗程要点""先煎后下"。结果是**模型照要求发挥就会被守卫整段误杀回退**，"软约束"名不副实；同时患者端守卫只匹配阿拉伯数字，"细辛三克，一日三次"可完整绕过。

| 级别 | 发现 | 修复 |
|---|---|---|
| 高 | 医师端叠加用硬守卫误杀 prompt 主动邀请的内容（`\d+g`/`疗程`/`每次` 一票否决），Tao 叠加在真实医教场景近乎恒回退 | 新增 `guard_clinician_draft` 软守卫：允许方义、先煎/后下安全提示、"经验剂量区间（医师审核）"；拦截断言式最终诊断（最终诊断/明确诊断/确诊为）、可执行完整医嘱（处方如下/水煎服/每日X次/分X次服）、患者自服指令、结构化违禁字段非空。三个叠加技能按角色选守卫（患者仍走严格守卫） |
| 中 | 患者端守卫可被中文数字剂量/频次绕过（三克、一日三次、分两次服） | `FORBIDDEN_PATTERNS` 补中文数字剂量（`[一二三...两半]+[克钱]`）与频次（`[每一]?[日天]X[次服]`、`分X次`）模式 |
| 中 | `每次`/`疗程`/`\d+\s*g`（IGNORECASE 连 IgG 都命中）过宽，问诊追问"每次发作持续多久"被误杀 | 收紧为剂量语境正则：`每次` 需接数量/服用词；`疗程` 需带数字；`g` 排除字母连写 |

### C. 规则引擎与规则覆盖（backend/engine/ + rules/ + 抽取归一）

| 级别 | 发现 | 修复 |
|---|---|---|
| 高 | `rules/01_tags.yaml` 别名词表是**死配置**（全项目无一处加载）；抽取/归一化硬编码小词表导致 R002 气滞血瘀、R006 脾虚不运**永不可能从自由文本触发**，`cold_damp_signal` 恒 False | `case_normalize_skill` 以 01_tags.yaml 为单一事实源做别名扫描（最长别名优先）；补齐 `strain_or_sprain`/`emotional_constraint`/`teeth_marks`/`weak_stomach`/`deep_cold_pain`/`nausea` 等缺失别名；抽取关键词扩充刺痛/固定痛/纳差/齿痕/受凉加重/热敷缓解等。R002、R006、cold_damp_signal 现均可从真实病案文本触发 |
| 中 | `high` 置信度为死代码：每个证型/方剂仅一条规则，单规则最高 5 分，永远达不到 8 分阈值 | 引入证据富集加成（每条命中规则超过 2 个证据标签的部分逐一加分），阈值调至 7；证据充分的肝肾不足案例实测达 `high` |
| 低 | `physician_reasoning` 治法映射缺 `少阳气郁证` 键（规则实际产出名），治法步骤被跳过 | 补齐键名 |

### D. 自主对话智能问诊（backend/agents/yaobi_interview.py + caseguide）

| 级别 | 发现 | 修复 |
|---|---|---|
| 高 | 红旗（含马尾综合征）识别**完全依赖 LLM 槽位抽取**：`use_llm=False` 或模型漏抽即红旗逃逸；且槽位为字符串"否"/"正常"时按真值处理会**假阳性触发急诊硬停** | 双通道红旗：新增确定性关键词扫描（含否定语义窗口"没有大小便失禁"不触发）与 LLM 抽取取并集，Tao 离线安全网仍生效；`_slot_positive` 对否认字符串（否/没有/正常/阴性…）归一为 False |
| 中 | `/api/interview` 会诊报告证据包只含证型候选，**缺方剂路线/药物模块/安全审查**——报告 scope 声称"证型→治法→方药→随访"，模型只能凭自身知识补方药，规则对方药的约束失效 | `_build_report` 补全证据包：formula_base_selector + herb_module_composer + safety_guard 全部注入，与 conversation 路径的 `_evidence_bundle` 同级 |
| 中 | freeform 追问上下文过薄（只有上轮答案+标签），README 声称的"整轮作废"实为逐条丢弃 | 追问上下文注入 `rule_context`（证型/方剂线索）；freeform 任一问题泄漏诊断/处方/剂量即**整轮作废**，与声明一致 |
| 文档 | README"每轮 1–3 个问题"未计入 Tao probe 附加数 | README 口径修正 |

### E. 服务端工程（backend/server.py）

| 级别 | 发现 | 修复 |
|---|---|---|
| 中 | `_INTERVIEWS` 会话字典无上限无淘汰（内存泄漏）；未传 session_id 统一落 `"default"` 键（跨客户端会话串号/红旗状态互相污染）；同 session 并发无锁 | LRU 淘汰（上限 256）；缺失 session_id 服务端生成 `srv-<uuid>` 并随响应返回；按 session 加锁串行化单会话回合 |
| 中 | 无请求体大小限制；畸形 JSON 静默变 `{}`；500 响应回传 traceback 尾部（信息泄露） | 请求体上限 1 MiB；畸形 JSON 返回 400；500 只回错误类型+消息，堆栈仅落服务端日志 |

## 三、README 逐条声明核对结果（要点）

- Tao Runtime 三后端/默认关闭/JSON repair/守卫/回退：**属实**（本轮将 http 后端的回退保证补齐）。
- `DaoClient.chat()` 直接 Transformers + `TextIteratorStreamer` 流式、FP16 默认、后台预加载、`/api/health` 暴露 `load_state`：**属实**。
- `route_skill`/`plan_skills`/`generate_consultation`/`generate_probe_questions` 真实调用模型且受白名单约束、越界回退：**属实**。
- CaseGuide FSM 预算/红旗硬门控/`run_scripted_interview` 自主推进/urgent 硬停：**属实**，测试覆盖完整。
- 多智能体协作 11 智能体 + RedFlag 自主中止 + 协作轨迹：**属实**，为真实基于内容的控制流。
- 前端"只有模型真正路由才标 Tao 选择 ✓"：**属实**，离线镜像结构性无法冒充模型在环。
- xlsx 脱敏挖掘、CDSS 草案、医师签名闭环：**属实**。

## 四、遗留边界（诚实声明）

- mock 后端的"模型自主性"是模板模拟，真实自主问诊/会诊质量需在 http/transformers 后端上做提示回归评测（尚无专家标注基准集）。
- 抽取/归一化仍是轻量关键词+别名扫描（无否定检测的通用 NLP），复杂叙述下的标签召回有限；LLM 抽取路径缺 schema/枚举归一层。
- `mined_evidence_skill` 自身无 `user_role` 守卫，"仅医师端"依赖调用方隔离。
- 服务端聊天（`/api/chat`）每请求新建会话，无跨轮记忆；生产部署仍需鉴权、持久化、审计日志与容量规划（见 `final_functionality_audit.md`）。

## 五、验证

- `python -m pytest tests/`：**136 passed**（116 既有 + 20 新增 `tests/test_review_hardening.py`）。
- mock 后端端到端冒烟：确定性管线 CLI、`--use-llm` 叠加、`/api/health`、`/api/chat`（`method=llm`、`answer_source=tao_primary_grounded`）、`/api/interview` 多轮（槽位抽取→FSM 推进→候选证型更新）、`/api/followup_probe`（规则约束内生成追问）均正常。
- 红旗安全网：离线（`use_llm=False`）输入"小便失禁，会阴发麻"→ `emergency` 硬停；"没有大小便失禁"→ 不触发；字符串"否"/"正常"槽位→ 不触发。

---

# 第二轮：CDSS 治理层升级（同月晚些时候）

按顶级 CDSS 设计理念（CDS 五个正确、可追溯、审计问责、用药安全、告警分级、认知谦逊、持续验证、临床治理）做第二轮差距分析与实现，全程采用「并行侦察 → 并行实现 → 对抗性审查（4 视角 + 16 项对抗验证）→ 修复」的多智能体流程。

## 新增治理能力

1. **决策出处**：`backend/provenance.py` 规则库 SHA-256 指纹（按文件 mtime/size 自动失效重算）+ 应用/模型运行时指纹，注入所有报告、`/api/health`、`/api/metrics`。
2. **审计日志**：`backend/audit/` 追加式 JSONL，每次 API 决策记录意图/路由/守卫/回退/红旗级别/延迟；患者叙述仅存摘要哈希+长度；失败安全（磁盘故障不影响请求）。
3. **医师反馈闭环**：`POST /api/feedback`（确认/需修订/不采纳+原因，字段全部限长）+ `GET /api/metrics`（采纳率等）+ 前端反馈组件（聊天回答/问诊小结/表单报告三处）。
4. **用药安全深化**：`rules/06` 新增 1 条十八反（附片×半夏，interruptive）+ 5 条药-药 + 6 条药-病禁忌规则；`check_interactions` 双向子串匹配含否定窗口与短词防过匹配；`alert_summary.requires_dual_signoff` 分级告警；抽取器新增用药/合并病识别（含否定感知），管线/编排器/会诊/问诊四路全部接通。
5. **认知谦逊**：`uncertainty_skill` 输出弃权判定（无候选或证据弱）、top1-top2 区分度、鉴别信息缺口与强化证据建议；进入报告「判读可信度」节、caseguide `final_report.uncertainty`、会诊 prompt（要求模型如实呈现不确定性）。
6. **金标准回归**：`evaluation/golden_cases.yaml` 16 例（全部 6 证型×2、3 红旗、良性、模糊弃权、1 例诚实标注的已知缺口——湿热痹阻证规则缺失）；`python -m backend.evaluation.benchmark`；CI 强制红旗召回=100%、守卫对抗集捕获=100%、良性误杀=0%。
7. **临床安全个案**：`docs/clinical_safety_case.md` 12 项危害（H1-H12）→ 缓解（代码引用）→ 验证（测试引用）→ 残余风险评级。

## 对抗性审查确认并修复的缺陷（本轮内自查自纠）

- **高**：问诊路径把含否定的原始句子当合并病条件传入相互作用匹配（「没有高血压也没有心脏病」触发 2 条 interruptive 假告警）→ 只传结构化条件 + `_terms_match` 加否定窗口与短词下限。
- **高**：管线路径的药-药告警永远无法触发（抽取器不认药名）→ 抽取器补用药/合并病词表（含否定感知）。
- **高**：问诊路径算出 `requires_dual_signoff` 却只进 LLM prompt、不进 API 载荷 → 报告载荷显式携带 `interaction_alerts`/`alert_summary`/`uncertainty`。
- **高**：前端从不上送合并病数据，UI 全链路告警层失效 → `casePayload` 补 comorbidity。
- **中**：问诊不确定性误用归一化概率×10（弃权判定错误、显示虚构分数）→ 全程携带原始规则分。
- **中**：LLM 返回字符串型 comorbidities 使最终报告 500 → `_as_term_list` 归一。
- **中**：出处指纹永久缓存而规则引擎每请求重读 YAML（热改规则后指纹漂移）→ mtime/size 键控缓存。
- **中**：`/api/feedback` 客户端字段未限长直写审计 → 全字段限长；隐私声明在 README/安全个案中如实区分「患者叙述摘要化」与「医师反馈原文留存（设计使然）」。
- **文档**：安全个案中黄金病例集/专项测试的三处过时表述与本轮产物矛盾 → 全部校正。

## 最终验证

- `python -m pytest tests/`：**187 passed**（上轮 136 → 本轮 +51：相互作用 14、基准 9、治理 26、前端 2）。
- 基准：top1/top2/路线/红旗/安全等级全 100%（16 例，1 known_gap 诚实另计），守卫捕获 100%、误杀 0%。
- 端到端（mock）：`/api/health` 带 provenance、`/api/metrics` 带计数与采纳率、审计 JSONL 落盘且患者文本仅哈希、问诊否定条件不再假告警、抗凝药×活血中断级告警从叙述文本直达报告「需医师确认」区。

---

# 第三轮：研究方法层（对标最新顶级科研成果）

深度检索调研 2024–2026 顶级成果（AMIE，Nature 2025/2026；语义熵幻觉检测，Nature 2024；共形预测临床综述，CHEST 2025；BED-LLM，2025；RAG 忠实性归因，ICLR 2025；TCM-LLM 评测，npj Digital Medicine 2025），将四项方法工程化落地（出处映射与诚实差异声明见 `docs/research_grounding.md`）：

1. **共形证型预测集**（`backend/engine/conformal.py`）：金标准病例校准的分裂共形预测，报告输出"90% 目标覆盖下不可排除的证型"，基准报告 LOO 经验覆盖率（当前 100% ≥ 90% 目标）与平均集合大小；小样本保守性、边际覆盖、标注分布依赖全部明示。
2. **EIG 自适应问诊**（`backend/skills/active_questioning.py`）：BED 式期望信息增益（bits）对鉴别性追问重排，似然由规则结构导出（零训练、可审计），红旗/必填槽位硬优先不受重排影响；`/api/interview` 输出 `question_selection` 审计链。实现过程中发现并修复了 SLOT_TAGS 三处死标签映射（waist_knee_soreness/冷痛/怕冷 映射到规则不认识的标签名，问诊回答从未真正喂给 R003/R004 规则）。
3. **语义自一致性**（semantic-entropy-lite）：`TAO_SELF_CONSISTENCY=N` 多采样按临床结论实体集聚类，聚类熵+一致率，不稳定结论自动附加复核警示；默认关闭（采样成本），mock 恒稳定属预期。
4. **声明级实体接地**（`backend/skills/groundedness_skill.py`）：会诊文本中的证型/方剂/药物实体逐一对照本案规则证据，输出接地率与"模型自身知识"实体清单（如：右归丸未见于本案证据→标注需医师重点复核）；非阻断透明层，随 `/api/chat` 与 `/api/interview` 载荷返回。

验证：`python -m pytest tests/` **206 passed**（+19 `tests/test_research_methods.py`）；基准新增共形覆盖行；README 新增「研究方法层」节。
