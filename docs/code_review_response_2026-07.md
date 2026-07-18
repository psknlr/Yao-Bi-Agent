# 外部深度评审整改对照（2026-07，v0.8）

本文档逐条回应第二轮外部智能体评审。处置分三类：**采纳**（按建议实现）、
**改进后采纳**（建议方向正确但细节需修正，按修正后的方案实现）、**部分采纳/暂缓**
（记录为路线图，说明原因）。所有已实现项均有对应回归测试。

## P0 项

### 1. 医生端 `guard_consultation` 过松 — 采纳（改进实现细节）

评审正确：consultation 是"模型为主推理者"的最宽出口，却只拦"患者自行用药"类措辞，
比 `guard_clinician_draft` 松，属守卫倒挂。

- **实现**：`guard_consultation` 对 clinician/researcher 直接委托 `guard_clinician_draft`
  （`backend/llm/output_guard.py`），patient 角色维持严格底线不变。
- **改进点**：不能照搬旧模式串。旧断言模式 `最终诊断|明确诊断` 会误杀两类合规文本——
  强制免责声明"最终诊断与处方须医师面诊后确定"，和临床建议"完善影像以明确诊断"。
  已把断言模式收敛为 `最终诊断[为是：:] / 明确诊断为 / 确诊为 / 可确诊`，
  即只拦"作出断言"，不拦"提及概念"。基准对抗集（ADV08/09/10、BEN01/02）与
  `tests/test_safety_gates.py` 双向回归。

### 2. 公网医生模式必须强制 token — 采纳

- **实现**（`backend/server.py`）：`_resolve_role` 三层裁决——配置了
  `YAOBI_CLINICIAN_TOKEN` 则必须校验通过；未配置时仅当服务绑定回环地址（或库内直调，
  无网络暴露）才承认 `doctor_mode`；公网绑定（`0.0.0.0` 等）未配置 token 时一律降级
  patient，`make_server` 启动时打印告警。
- **附加**：角色来源（`token_verified / local_demo / public_no_token_denied /
  token_mismatch`）写入响应与审计（评审建议第 4 小点），拒绝消息明确指引配置方式。

### 3. 红旗 urgent 全局前置硬中止 — 改进后采纳

评审方向正确（红旗门控必须是所有入口共享的不变式），但"urgent 一律中止"与金标准
病例集自身矛盾：GC012（骨质疏松跌倒）预期 **urgent + 保留证型/方剂复盘分析**——
对回顾性医案研究，情境性 urgent（脆性外伤转诊）中止全部中医分析反而是错的。

- **实现**：按红旗**类别**分级中止。`safety_guard_skill.emergency_halt_required`
  为共享门控谓词：确认马尾/进行性无力/感染类（always-emergency 类别）→ 硬中止；
  情境性 urgent（脆性外伤、肿瘤史+夜间痛）→ 标记 urgent、保留医师复盘分析。
- **覆盖入口**：`run_case_pipeline`（抽取后、辨证前先分级，中止分支不调用模型）、
  `ConversationSession._dispatch`（urgent 时证型/方剂/用药/推理/经验/剂量/证据类
  intent 统一返回转诊提示，安全/红旗/元信息类 intent 保留）、`AutonomousQAAgent.run`
  （规划前替换全部计划为红旗排查）、多智能体 orchestrator（原有 halt 机制不变）、
  interview FSM（原有 SAFETY_REFERRAL 硬停不变）。

### 4. `_enrich_with_question` 红旗只设 caution — 采纳

- **实现**：自由文本红旗改走与管线相同的 `safety_guard_skill` 类别分层分级器；
  状态合并 escalate-only（只升不降，客户端声明的 urgent 不会被后续轮次降级）；
  graded "safe" 不覆盖 None（一句话不构成完成筛查）；`need_further_inquiry` 一并透传。
  "会阴麻木、尿不出来"入 chat 即 urgent 并触发门控（`tests/test_safety_gates.py`）。

### 5. 规则死标签 + tag lint — 采纳（范围扩大）

评审点名的 `ganshen_buzu` / `kidney_yang_deficiency` / `young_patient` 实际**有**生成
路径（证候派生 / 年龄计算），并非死标签；但 `qi_deficiency / limb_weakness /
cold_damp_obstruction / joint_stiffness` 确属死条件。lint 落地后又发现模块规则里还有
13 个评审未点名的死触发标签（dampness、spasm_pain、wind_damp_obstruction、
refractory_pain、severe_numbness、phlegm_heat、stasis_pattern、sprain/strain 等）。

- **实现**：
  - `rules/01_tags.yaml` 为可文本抽取的标签补齐临床别名（气虚/下肢无力/晨僵/拘挛/
    游走痛/顽固疼痛/麻木加重/痰热/烦躁 等 15 个新注册标签）；
  - 证候级概念不走文本别名而走派生：`formula_base_selector_skill.SYNDROME_DERIVED_TAGS`
    （肝肾不足证→ganshen_buzu、肾阳不足证→kidney_yang_deficiency、
    寒湿痹阻证→cold_damp_obstruction、气滞血瘀证→stasis_pattern）；
  - 模块规则中的 `sprain, strain` 归并为注册标签 `strain_or_sprain`；
  - 新增 `tests/test_rule_lint.py`（CI 门）：规则引用标签必须"注册 ∪ 派生"、规则必须有
    id/category/rationale/effect 且 id 唯一、方剂规则必须带 route+core_module、
    派生标签不得同时携带文本别名（单一生成源）。
- **验证**：21 例金标准基准全部通过（含新别名后的证型/方路排序无回归）。

## P1 项

### 6. LLM 证据不足时不得默认补齐方剂 — 采纳

- **实现**：mock 会诊在证据包无候选证型且无方路时输出弃权+追问（不再有
  "气血痹阻/独活寄生汤"默认值；方义段落也不再硬编码独活寄生汤方解）；真实后端由
  `tao_consultation_skill` 强制同一契约——无规则依据且文中出现**提问者未提及**的
  证型/方剂实体时整段降级回确定性规则回答（`status=ungrounded_no_rule_basis`）。
  用户自己点名方剂的教学性提问（"独活寄生汤的方义？"）不受影响。

### 7. 多轮记忆 — 采纳（第一阶段）

- **实现**：`ConversationSession.absorb_question_facts` 每轮把陈述的临床事实并入共享
  case_state：标签合并、红旗 escalate-only 重分级、`state_version` 递增、每轮返回
  `state_updates` diff；自主智能体共用同一入口。后续轮次辨证基于累计状态。
- **暂缓部分**：SQLite/PostgreSQL 会话持久化、可回放 transcript 版本树、医师确认后的
  finalized state 属产品化范围（见 P2）。

### 8. Conformal 表述 — 采纳

- **实现**：`coverage_note` 与报告措辞改为"项目内校准的候选证型集合（提示哪些证型尚
  不能排除）"，明确目标覆盖率仅相对项目内标注分布按边际意义成立、不代表真实临床人群
  诊断概率、不构成统计学临床正确性保证。`conformal.py` 的方法学注释（exchangeability、
  marginal、小样本保守）原本已诚实，保持不变。

### 9. 智能体自主性定位 — 采纳表述、暂缓架构重写

评审对定位的判断（"规则驱动 + 受限技能路由 + 固定编排 + 批判者闭环"，而非强自主
医疗智能体）与 README 既有定位声明一致；v0.8 在 README 继续沿用该定位。把固定管线
重写为完整 Planner–Tool–Observe–Reflect 闭环**有意暂缓**：在临床安全域，可预测的
确定性编排 + 批判者补救是特性而非缺陷；自主性升级应在证据层（结构化抽取）夯实后进行，
否则是给弱证据加自由度。

## P2 项（记录为路线图，本轮不实现）

以下各项方向认可，但依赖专家资源、真实数据或属产品化工程，超出本轮"安全治理加固"
范围（评审自己的结论也是"下一步不要继续堆功能"）：

- 抽取层升级为结构化槽位 + span evidence + temporality/experiencer/certainty
  （现有 polarity/时态识别是第一步；完整实现需标注数据支撑）；
- 规则规模扩充与证据权重分层、Evidence Card（每条结论→规则→原文→审核人→证据等级）
  ——需要沈氏流派专家参与的规则工程，不宜由模型代写临床规则；
- golden cases 扩至数百例、双/三医师独立标注、多中心样本——需真实临床资源；
- FastAPI/Pydantic/OpenAPI、Docker、数据库审计持久化、任务队列——生产化工程；
- `trust_remote_code=True` 与模型版本 pin：研究原型保留，生产部署须固定 revision
  并审计 remote code（已在本文档立此存照）。

## 本轮不采纳（含理由）

- **"urgent 一律中止所有分析"的字面实现**：与金标准 GC012 的临床预期冲突，
  改为类别分级中止（见 P0-3）。
- **guard 模式串直接照搬 `guard_clinician_draft` 旧断言模式**：会误杀强制免责声明，
  改为断言级精确模式（见 P0-1）。
- **评审所列"死标签"清单照单全收**：其中 3 个并非死标签（有派生/计算路径），
  以 lint 实测为准；同时 lint 发现了评审未覆盖的 13 个模块死触发标签（见 P0-5）。

## 回归口径

- 全量测试：`python -m pytest tests/`（v0.8：263 项，含新增 `test_safety_gates.py`
  18 项、`test_rule_lint.py` 6 项）；
- 金标准基准：21/21 通过，红旗召回率与安全等级准确率 100%，守卫对抗集无误杀；
- 共形 LOO 覆盖率满足目标（小样本保守口径见上文表述校准）。
