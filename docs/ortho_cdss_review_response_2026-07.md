# 骨伤科临床安全评审整改对照（2026-07，v0.10）

针对第四轮外部评审（骨伤科 CDSS 临床安全视角）。处置分三类：**采纳**、
**改进后采纳**（方向对、细节按临床/工程事实修正）、**暂缓**（记录为路线图并说明理由）。
评审的核心判断——"当前安全测试全部通过只能证明现有测试定义内通过，不能证明骨伤科
临床安全"——完全成立：其给出的 8 个对抗场景在 v0.9 下确实全部漏判或误判，本轮已
全部修复并纳入 CI。

## P0 项

### P0-1：安全红旗体系不适合完整骨伤科 — 采纳（本体扩展 + 组合升级）

- **7 个新增急诊硬停类别**（`safety_guard_skill.EMERGENCY_HALT_CATEGORIES`，
  与 `hermes_agent.yaml` halt_categories CI 绑定）：
  `major_trauma`（车祸/高处坠落/挤压伤/多发伤——与低能量跌倒分离，后者保持
  脆性背景升级 + 医师复盘保留的 GC012 语义）、`open_fracture_dislocation`
  （伤口见骨/骨外露/明显畸形/脱位/张力性水疱）、`neurovascular_deficit`
  （动脉搏动消失/肢体苍白发凉/无脉）、`compartment_syndrome`（被动牵伸痛/
  张力性肿胀/骨筋膜室）、`vascular_emergency`（撕裂样痛/腹部搏动感/搏动性包块）、
  `cardiopulmonary_emergency`（胸痛/呼吸困难/咯血）、`cervical_myelopathy`
  （踩棉花/双手笨拙/持物不稳）。
- **组合升级**（单关键词捕不到的多征象模式，`_combination_flags`）：
  小腿肿痛+气短/心慌/呼吸困难 → 疑似 PE（急诊硬停）；免疫抑制（生物制剂/长期激素，
  新注册标签 `immunosuppressant_use`）+夜间痛或感染线索 → `immunosuppressed_risk`
  （情境性 urgent：当日紧急专科评估，非急救现场，不硬停——评审表格该行也未要求硬停）。
- **验收**：评审 8 场景全部达到"应有结果"（对抗表 10 场景测试
  `tests/test_ortho_safety.py` + 金标准 GC022–GC030），高能量创伤/开放骨折/
  神经血管损伤漏报率 0，主动脉/PE 场景零方药输出。

### P0-2：方药路线可脱离证型候选直接产生 — 采纳（门控落地，具体规则改法有修正）

- **实现**：`formula_base_selector_skill` 路线门控——无证型候选或 top 候选仅
  低置信 → `route_gate.allowed=False`，`primary_route=None`，输出弃权理由。
  结合上游门控构成评审要求的四重条件：in-scope（范围路由器）× 非急症（红旗门控，
  急症根本到不了本技能）× 证型候选存在 × 候选置信过弃权线。
  "72岁+撕裂样痛"案例现在在 `vascular_emergency` 处硬停，任何方药代码路径不可达。
- **修正评审的具体改法**：评审建议 F001 改为 `requires syndrome_any:[肝肾不足证,
  肾阳不足证] + all:[chronic_yabi]` ——该改法会推翻金标准 GC011（55 岁脾虚不运
  兼久病腰痛，沈氏流派预期独活寄生汤为底顾护中焦；患者非高龄，肝肾不足不成候选）。
  流派专家标注的预期不应被工程侧静默改写；选择器级门控在不推翻专家判读的前提下
  达成同一安全性质（无证型接地必弃权）。
- **连带修正**：GC021（肿瘤史+夜间痛+体重下降）旧预期"无证型候选仍给经验路线"
  正是评审点名的自动化偏差（恶性未排除场景更危险），已按新门控废止该预期
  （`formula_route_any: []`）。

### P0-3：缺少临床行动分层 — 采纳

- `safety_guard_skill` 输出 `action_level`（A0 立即急救硬停 / A1 当日紧急专科评估 /
  A2 尽快面诊检查 / A3 常规门诊决策支持）+ `action_meaning`，并把 `drivers` 拆为
  `clinical_urgency` / `medication_review_required` / `evidence_insufficient` 三轴——
  "车祸后不能站立"（A0，clinical_urgency）与"当归四逆汤含细辛需审核"
  （A2，medication_review）不再共享一个 caution。
- **行动卡**：pipeline 输出 `action_card`（级别 → 依据 → 下一步 → 当前禁止 →
  证据缺口 → 驱动因素），临床行动先于长报告——对应评审第五节"临床行动卡优先"。
  A4（健康教育/随访）属患者端内容层级，不由病例安全分级产生，未纳入本技能。

### P0-4：时间语义抽取了但安全层没消费 — 采纳（评审的断链诊断准确）

- `clinical_entity_skill` 时态扩展为 `current | historical | resolved`：
  resolved 通过同句尾及**紧邻短下句**的"已痊愈/已缓解/已退/已恢复"判定
  （中文叙述以逗号分句，"一周前感冒发热，现已痊愈"的关键线索在下一分句）。
  "一周前"类时间短语本身**不**降级——一周前未愈的发热仍是当前感染筛查对象。
- `safety_guard_skill` 消费时态：affirmed 但 historical/resolved 的红旗进入
  `historical_red_flags`（医师可见记录，永不报警）；`cancer_history` 例外——
  病史本身就是该红旗的意义。
- **修复的断链根因**：`case_normalize` 的别名扫描把"发热"直接写成
  `fever_or_infection` 标签（标签路径绕过实体层时态）——红旗类标签已从叙事
  归一化中移除（`_ALIAS_SKIP_TAGS`），统一走极性+时态已解析的实体路径；
  问卷直填标签路径不受影响。
- **暂缓部分**：recurrent/hypothetical 时态、experiencer 家属区分、否定跨并列
  范围、引述检查报告——需要标注数据支撑的语义解析，列入 Case Schema v2 路线图；
  当前 experiencer 固定 patient 的局限已在实体模块文档注明。

### P0-5：缺范围路由器 — 采纳

- 新技能 `clinical_scope_router_skill`（已注册进 ToolRegistry，
  `hermes_tools.json` 同步再生成）：输出评审建议的
  `domain / task / in_scope / scope_confidence / out_of_scope_reason /
  allowed_capabilities` 结构。
- 路由次序：**急症安全内核（红旗门控）先于范围路由**——胸痛气短先按急症硬停
  （比"域外"更正确），非急症再判域：腰痹锚点在 → in_scope；创伤/关节主诉 →
  相应 domain + 仅 safety_triage；无法识别 → unknown + 仅 safety_triage。
  膝关节肿痛等域外主诉不再进入腰痹辨证方药链（`_out_of_scope_result`）。

### P0 验收标准（评审第八节）逐条状态

| 验收项 | 状态 |
| --- | --- |
| 高能量创伤漏报率 0 | ✅ GC022/GC023 + 参数化测试 |
| 开放骨折及神经血管损伤漏报率 0 | ✅ GC024 |
| PE、主动脉病例不产生任何中医方药路线 | ✅ GC026/GC027（halt 路径方药代码不可达） |
| 历史已缓解发热不触发当前感染硬停止 | ✅ GC029 + 时态单测（活动性发热仍硬停） |
| 无证型候选时 primary_route 必须为 None | ✅ route_gate 单测 + GC021/GC030 |
| 紧急门控之后 LLM 调用次数必须为 0 | ✅ `_CountingDao` 计数断言 == 0 |
| 九个对抗病例加入 CI 全部通过 | ✅ GC022–GC030 + GC031（哨兵），金标准 31/31 |

## P1 项

### "100% 准确率"重新定义 — 采纳诊断，落地 L1/L2，如实声明 L3-L5

- 评审的定性正确：金标准是**规则回归测试**，非独立临床验证；共形校准与开发集
  同源。这些声明 v0.8 起已写入共形输出与 README 定位声明，本轮在本文档再次固定。
- **L2 规则变异测试已落地**（`tests/test_rule_mutation.py`）：对规则库注入有害
  变异并要求金标准捕获。落地过程本身验证了评审的担忧——"删除全部反证（contra）"
  变异在原 30 例金标准下**全部存活**（真实评测盲区），为此新增反证敏感哨兵病例
  GC031（寒凝血瘀为主兼尿黄，contra 罚分决定 top1），现两类变异（删反证、删
  at_least 阈值）均被捕获，且基线哨兵通过。
- **暂缓（需临床资源，非工程可自造）**：L3 千例级临床对抗集、L4 多中心盲法
  回顾验证、L5 前瞻静默试验及敏感度/特异度/PPV/NPV/警报负担指标——列为独立
  临床验证阶段的准入条件；工程侧不伪造临床数据。

### 其余 P1（挖掘规则治理 / 规则元数据 / 药物风险建模 / Clinical Fact 模型）— 部分采纳

- 挖掘候选规则状态分级：现有 `pending_expert_review` 即 draft_mined 语义，且
  已隔离在医生端研究证据、不入自动决策链（v0.5 起）；完整六级生命周期
  （draft_mined → … → approved_for_clinical_use）与规则治理元数据
  （evidence_sources/clinical_reviewer/valid_from…）需要专家审核工作台支撑，
  列入第二阶段路线图。
- 药物风险"药名交集"升级为剂量×炮制×肝肾功能×妊娠多因子建模、输出分级
  （绝对/相对禁忌/需监测/仅理论性）：需要正式药物知识库（品种/炮制/监测项），
  暂缓并注明现有 conflict_checker 的等级字段（interruptive/advisory）是其雏形。
- Clinical Fact 事件模型（code/body_site/laterality/observed_time…）与受伤机制、
  影像结构化等骨伤科 Schema 扩展：Case Schema v2 路线图；当前已有的
  polarity/temporality/experiencer/source_span 实体结构是其第一层。

## 四～七节（Harness/产品形态/互操作）— 与前轮口径一致

- 软超时→硬隔离、会话/审批持久化、OIDC/医院级身份、/api/metrics 与 /api/warmup
  的暴露收敛、FHIR/CDS Hooks 映射、七类专科智能体产品化拆分：均已在
  `docs/harness_review_response_2026-07.md` 中定级为 P2/部署件，本轮不重复。
- 行动卡优先的输出形态本轮已在数据层落地（`action_card`）；前端首屏改造属
  产品化阶段。
- 决策证据链 vs 模型思维过程：现有报告/trace 本就输出规则命中+证据标签而非模型
  自由思维流，行动卡进一步把"结论/支持事实/命中规则/缺失信息/未执行及原因"
  结构化——与评审建议的展示格式一致。

## 回归口径

- 全量测试 **310 项通过**（新增 `test_ortho_safety.py` 20 项、
  `test_rule_mutation.py` 3 项）；
- 金标准 **31/31**（21 原有 + 9 骨伤科对抗 + 1 反证哨兵；红旗召回率、安全等级
  准确率、top1/top2、方路召回均 100%）；
- 守卫对抗 16/16 拦截、4/4 良性放行；共形 LOO 覆盖达标（口径：项目内校准）。
