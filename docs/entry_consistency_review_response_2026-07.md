# 入口一致性与统一安全内核评审整改对照（2026-07，v0.11）

针对第五轮外部评审。其核心发现（P0-1：安全与范围控制未覆盖所有智能体入口）经实证
**完全成立**：同一膝关节主诉，主流水线正确拒绝，而 chat / autonomous / collaboration
三个入口全部输出了当归四逆汤路线——"同一患者、同一输入，因 API 不同得到不同安全
处理"。本轮已将该病例与其余矩阵病例固定为 CI（`tests/test_entry_consistency.py`）。

## P0 项

### P0-1：安全/范围控制未覆盖所有入口 — 采纳（本轮核心）

- **chat**（`ConversationSession._dispatch`）：临床 intent 在红旗门控之后增加
  **scope gate**（`question_scope_gate`）——问句含域外锚点（膝/肩/骨折/术后…）且
  会话无腰痹证据（问句锚点、腰痹上下文标签、既往轮次建立的 sticky scope）→ 拒绝
  辨证/方药并给转诊话术。
- **autonomous**（`AutonomousQAAgent.run`）：规划**之前**同款 scope gate——域外主诉
  直接生成 triage/转诊 turn，`run.stop_reason = policy_denied`，不委派任何临床子
  智能体。
- **collaboration**：新增 **ScopeGateAgent**（能力签发者，排在 RedFlagAgent 之后）：
  case_state 携带的服务端 scope 判定为域外 → halt 协作、只授 triage 能力；
  同时落地评审的**能力令牌模式**——`Blackboard.capabilities` + 工具内自检：
  `FormulaReasoningAgent`/`HerbModuleAgent` 在**自身内部**校验 `formula_draft`/
  `herb_module` 能力，未授权即 blocked（纵深防御：即使未来编排器忘了 halt，
  方药智能体自己也会拒绝——有专门测试）。
- **server**：`_enrich_with_question` 对问句计算 scope 判定并随 case_state 下发，
  chat/autonomous/collaboration 三个入口消费同一份判定。
- **改进点（对"不可绕过的统一执行上下文"）**：评审建议的 ClinicalExecutionContext
  全量重构，本轮以"scope 判定随 case_state 传播 + 智能体内能力自检 + 入口一致性
  矩阵测试"达成其安全性质；完整的不可伪造上下文对象（含 patient_id/facts_version）
  依赖持久化身份体系（P2），先以矩阵测试锁住行为等价。

### P0-2：interview 使用旧版安全体系 — 采纳

- `_detect_red_flags` 现在**叠加共享安全内核**：累计患者叙事经
  `case_extract → case_normalize → safety_guard_skill`（与 pipeline 完全同源，含
  时态、经历者、组合升级、脆性背景），`emergency_halt_required` → emergency 硬停；
  内核 urgent（情境性）→ high 提示。原槽位通道保留为**附加**通道，不再是唯一来源。
  内核异常 → **fail closed**（按高风险处理并记录）。
- 行为统一实证：活动性发热+腰背痛在 interview 从"high 可继续"变为与 pipeline
  GC014 相同的 emergency 硬停；脆性跌倒保持 high 咨询级（GC012 语义）——两者均有
  测试。
- **暂缓（方向认同）**：单一 `safety_ontology.yaml` 生成全部消费方——当前以
  `RED_FLAG_CATEGORY`+`EMERGENCY_HALT_CATEGORIES` 为单一代码事实源、manifest 与
  CI 绑定；YAML 本体化列为规则治理工作台的一部分（需专家维护界面支撑）。

### P0-3：范围路由优先级误判 — 采纳

- 新优先级：急症内核 > **骨折/脱位/术后随访**（骨折/脱位/术后/内固定/椎体成形/
  钢板/螺钉/置换术锚点）> 腰痹 > 其他关节 > unknown。"腰椎压缩性骨折术后复查"
  现在路由为 `spine_fracture_followup`、`in_scope=False`、reason_code
  `FRACTURE_POSTOPERATIVE_PRIORITY`、blocked `lumbar_bi_formula_route`。
- 路由输出升级为评审建议的结构：`reason_codes` + `allowed_capabilities` +
  `blocked_capabilities`（不再是裸二元 in_scope）。
- 锚点判定改为 `_active()`：**affirmed + current + patient** 三条件——"十年前车祸"
  "肩关节脱位已复位""父亲车祸"都不再牵引域判定。

### P0-4：方药门控只到表面 — 采纳

- **方证兼容约束**：`03_formula_rules.yaml` 每条规则新增 `compatible_syndromes`；
  选择器只允许与 **top-1 或 medium+ 候选**相交的方路入选，低置信尾部候选不再
  贡献派生标签。评审三个对抗全部封死：少阳+高龄→仅柴胡类方；湿热+骨松→仅四妙丸；
  高龄无久病→无独活寄生汤。
- **F001 再收紧**：`all:[chronic_yabi]`（独活寄生汤主治久痹，久病为必要条件）+
  虚证佐证 any 列表（补入 fatigue/thin_pulse——方义中"气血不足"的佐证），金标准
  31/31 保持（GC011 脾虚兼久病的专家判读不被推翻）。
- 安全状态/范围/行动等级不直接传入选择器的原因（对评审"工具没接收上下文"的回应）：
  急症与域外在上游结构性不可达（halt/out-of-scope 分支根本不调用本技能），
  智能体层另有能力令牌内检；矩阵测试锁定该组合性质。

### P0-5：时态与经历者假阴/假阳 — 采纳

- **缓解词绑定收紧**：跨逗号 look-ahead 只接受**纯缓解短句**
  （`^现?已(经)?(痊愈|缓解|好转|恢复|消退|复位|退)$` 类）——"发热，腰痛已缓解"
  不再把发热标记为已缓解（危险假阴性封死）；"发热伴咳嗽，咳嗽已经好转"同理。
- **已复位**入缓解词表："肩关节脱位已复位"不再按当前脱位触发 A0。
- **N年前 → historical**："十年前车祸"不再触发当前重大创伤（近日"三天前跌倒"
  保持 current）。
- **经历者识别**：家属称谓前缀（父亲/母亲/家属/同事…）→ `experiencer="other"`；
  安全层将非患者本人所历红旗归入 `other_experiencer_flags`（记录、永不报警）；
  聚合时患者本人的 affirmed 出现优先。
- **暂缓**：完整事件级表示（event/status/certainty 对象）与症状-缓解词的实体级
  依存绑定——需要依存解析或标注数据，列入 Clinical Fact 模型路线图（前两轮已定级）。

## P1 项

### P1-1/P1-2：A0 过度分诊 — 采纳

- **胸痛/呼吸困难单独出现**降为 `cardiopulmonary_symptom`（情境性 urgent，A1 心肺
  追问，不硬停）；与气短/心慌组合才升级 `cardiopulmonary_emergency`（A0 硬停）。
  "胸壁拉伤按压可复现、无气短无咯血"现在是 A1 而非 A0（有测试）。咯血保留直接
  A0（特异性足够）。撕裂样痛保留 A0（主动脉高特异性描述，安全优先，已记录）。
- **颈髓病拆出 A0**：仍硬停中医推理（TCM 输出对疑似脊髓压迫无益），但
  `action_level=A1`（当日紧急专科评估，非急救现场）——halt 语义与行动语义解耦。
- **certainty 维度**：确认旗携带 `certainty`（单关键词 `reported`，组合升级
  `highly_suspected`）。完整四级 certainty 分级列为后续。

### P1-3：自行服药混入临床红旗 — 采纳

"自服/自己买药/开方"移出 `confirmed_red_flags`，进入独立 `policy_flags` 轴
（drivers 同步携带）——不再抬升临床安全等级；请求守卫与医师复核行为不变。

### P1-4：A1/A2 仍生成方药内容 — 部分采纳

pipeline 输出显式 `clinical_mode`（emergency_halt / out_of_scope_triage /
urgent_workup_priority / standard_support），行动卡在 A1 下声明患者端方药阻断。
情境性 urgent（GC012 脆性跌倒）保留医师复盘分析是金标准专家判读的既定语义，
不改；下游若需完全裁剪，`clinical_mode` 即是消费开关。

## 其余章节

- **五（Clinical Fact 数据模型）/七（多智能体重构其余部分）**：与前三轮定级一致
  （Case Schema v2 / 专家工作台 / 持久化），本轮新增的能力令牌与 ScopeGateAgent
  是其中"能力控制"部分的落地。
- **六（测试盲区）**：**入口一致性矩阵已落地**（`test_entry_consistency.py`：
  膝关节/开放骨折/历史车祸 × pipeline/chat/autonomous/collaboration/interview）；
  **安全失败关闭已落地**（`_enrich_with_question` 与会话吸收层异常 → 标记
  fail-closed、审计、临床 intent 弃权——不再静默放行）；评审建议的其余变异
  （删除入口路由/伪造 safe 状态等）由矩阵测试+能力内检天然覆盖其行为面。
- **八（生产级）**：`/api/metrics` 与 `/api/warmup` 公网绑定下需有效令牌（未配置
  令牌即锁定）；前端 XSS 修复（输入框改 DOM `.value` 赋值、协作时间轴 agent
  name/role/summary/handoff 统一 escapeHtml）。OIDC/持久化/CSP 强化保持 P2。

## 回归口径

- 全量测试 **321 项通过**（新增 `test_entry_consistency.py` 10 项，含运维端点
  锁定与 fail-closed 断言）；金标准 **31/31**；守卫对抗 16/16 拦截、4/4 良性放行。
- 入口一致性矩阵：开放骨折在五个入口全部硬停；膝关节主诉在五个入口全部无方药；
  历史车祸在全部入口均不按当前急症处理。
