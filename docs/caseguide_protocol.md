# YaoBi-CaseGuide Skill Protocol

YaoBi-CaseGuide Skill 是自动导引患者形成标准化腰痹医案的轻量化 Hermes 智能体。它以红旗筛查为安全底线，以中西医问诊为信息骨架，以沈钦荣腰痹经验规则为导引逻辑，将患者口语化描述转化为可供医生复核、科研标注和规则匹配的医案。

## 严格边界

用户若要求“诊断和处方”，系统只能输出：

- 候选诊断/证型假设：标注为“待医生复核”。
- 方剂路线信号：标注为“经验规则线索，非处方”。
- 药物模块解释：不生成患者可执行剂量和服药方法。
- 标准化医案、结构化标签、风险提示、医生复核清单。

系统不得输出最终诊断、临床处方、患者自服剂量或替代医生治疗建议。

## 12 个问诊 Skill

1. `consent_privacy_skill`：知情与脱敏。
2. `red_flag_screen_skill`：红旗症状筛查，优先级最高。
3. `chief_complaint_skill`：主诉生成。
4. `pain_profile_skill`：疼痛部位、放射、性质、评分、诱因、缓解因素。
5. `neuro_ortho_screen_skill`：麻木、无力、间歇性跛行、影像、既往诊断。
6. `tcm_four_diagnosis_skill`：寒热、湿重、气血、肝肾、少阳情志、睡眠、胃纳、汗出二便、月经、舌象。
7. `shen_rule_signal_skill`：当归四逆、寒湿、补肾强骨、柴胡、顾护中焦、气血痹阻夹湿等信号捕捉。
8. `comorbidity_medication_skill`：合并病、NSAIDs、肌松药、抗凝/激素/降糖药、过敏史。
9. `adaptive_question_planner_skill`：按安全权重、规则信息增益、缺失字段、证型不确定性、方剂路线不确定性、患者疲劳度动态补问。
10. `case_structuring_skill`：生成标准医案草稿。
11. `case_quality_check_skill`：按基本信息、疼痛、红旗、中医特征、沈老信号评分。
12. `clinician_handoff_skill`：生成医生复核摘要。

## 状态机

`CaseGuideSession` 按以下状态运行：`S0_CONSENT → S1_REDFLAG → S2_BASIC → S3_PAIN_PROFILE → S4_NEURO_ORTHO → S5_TCM_CORE → S6_SHEN_SIGNAL → S7_COMORBIDITY → S8_ADAPTIVE_REPAIR → S9_CASE_SUMMARY → S10_FINAL_REPORT`。若红旗为 urgent，进入 `S_EMERGENCY_NOTICE` 并停止后续中医问诊。

## “诊断和处方”需求的安全实现

`clinician_review_package_skill` 是处理“请给出诊断和处方”这类需求的唯一出口。它不会生成最终诊断或完整处方，而是生成：

- 西医鉴别方向：如神经根受压、椎管狭窄、骨质疏松相关压缩骨折风险等，全部标注“待医生结合查体和影像复核”。
- 中医候选证型：来自确定性规则评分，标注“候选证型，待医生复核”。
- 方剂路线信号：如独活寄生汤、当归四逆汤、桂枝芍药知母汤等路线，仅作经验规则线索。
- 药物模块解释：只列代表药物和触发证据，不合成患者可执行处方，不给服用剂量。

这样既满足医生/科研场景下对“诊断假设与处方经验”的复盘需要，又避免患者端自动诊疗和自动开方风险。

## 为什么不能“标注医师审核后仍给患者可执行剂量”

`patient_request_guard_skill` 会拦截最终诊断、完整处方、患者可执行剂量和自服方案请求。原因是“标注需医师审核”不能消除患者端自动诊疗风险：一旦系统输出完整药味、剂量、煎服法或疗程，患者可能直接执行，尤其涉及附片、细辛、虫类药、乌头类药物时风险更高。

因此系统允许的替代输出是：标准化医案、鉴别方向、候选证型、方剂路线信号、代表性药物模块、安全风险和医生复核清单。若需要最终诊断或处方，必须由具有资质的医生在面诊、查体、影像和用药安全审查后完成。

## 医师审核模块

新增 `create_physician_review_task` 与 `physician_review_skill`，用于医生端闭环：

1. 系统先生成标准化医案、鉴别方向、候选证型、方剂路线信号和药物模块解释。
2. `create_physician_review_task` 创建仅医生可见的审核任务。
3. 具备资质的 `licensed_physician` 可以在医生端**手工录入**最终诊断、处方、剂量、煎服法、疗程和复诊计划。
4. `physician_review_skill` 要求医师身份、执业证号和签名，拒绝模型生成的最终诊断或处方。
5. 签名记录进入审计日志，患者端只能看到医生签名后的内容。

这满足“需要医师审核模块”的需求，但仍保持模型不自动诊断、不自动开方、不自动给患者剂量的安全边界。

## CDSS 自动草案模块

`cdss_recommendation_skill` 面向医生端 CDSS 场景，可以由模型/规则自动生成：

- 西医候选诊断/鉴别方向；
- 中医候选证型；
- 方剂路线草案；
- 药物模块组合草案；
- 高风险药物与复核清单。

但这些内容的状态始终是 `draft_for_clinician_review`，不是已签名诊断、不是最终处方、不是患者可见医嘱，也不包含患者可执行剂量。最终诊断、完整处方、剂量、煎服法和疗程必须经 `physician_review_skill` 由 licensed physician 手工录入并签名。

## FSM v0.2：状态内动态深化追问

CaseGuide 现在采用“有限状态机 + 状态内最多三轮追问”模式：

1. 每个状态先根据本状态核心目标给出最多 3 个问题。
2. 用户回答后，系统把上一轮答案写入 `case_state.fsm.last_answers`，刷新 `normalized_tags`、沈老经验信号、候选证型和方剂路线信号。
3. 下一轮问题会叠加“当前规则上下文 + 上一轮答案 + 未补齐字段”进行深化，而不是机械展示完整题库。
4. 每个状态最多追问 3 轮；到达上限会自动进入下一状态。
5. 前端/调用方可以触发 `end_current_state()` 手动结束当前状态，直接进入下一状态。
6. 红旗状态仍然最高优先级；若命中 urgent，不允许通过手动结束绕过线下/急诊提示。

每轮响应都会返回：

```json
{
  "state": "S3_PAIN_PROFILE",
  "next_questions": ["最多3个深化问题"],
  "fsm": {
    "state_goal": "采集疼痛部位、性质、程度、诱因、缓解因素",
    "turn_index": 1,
    "max_followups_per_state": 3,
    "remaining_followups": 2,
    "can_end_state": true,
    "rule_context": {
      "normalized_tags": ["elderly", "chronic_yabi"],
      "top_syndrome_candidates": [],
      "top_formula_routes": []
    },
    "last_answers": {}
  }
}
```


## FSM v0.3：规则选题 + Tao 问诊推理叠加

问诊 Agent 现在不是单纯规则驱动：

1. **确定性规则层**先根据当前 state、上一轮答案、标准化标签、沈老规则信号、候选证型和方剂路线，生成最多 3 个候选问题 id。
2. **Tao 叠加层**在 `use_llm_questions=True` 时接收候选问题和规则上下文，只能执行三类动作：重排已有问题、患者友好化改写问题、解释为什么此刻追问。
3. Tao 输出必须是 JSON object，并且只能引用候选问题中的既有 id；系统会丢弃新增 id。
4. Tao 输出会经过 JSON repair 与 forbidden-output guard；如果出现最终诊断、完整处方、剂量、煎服法或患者自用建议，立即回退确定性规则问题。
5. 因此问诊路径是“规则决定边界和候选问题，Tao 做安全的语义深化与表达优化”，符合 Hermes-style agent 编排，而不是让模型自由问诊或自由开方。

## FSM v0.4：可配置追问预算 + 自主问诊驱动器

1. **追问预算可配置**：`CaseGuideSession(max_followups_per_state=N, questions_per_turn=M)`，
   也可在运行中调用 `set_max_followups(N)` / `set_questions_per_turn(M)`（均有下限 1 保护）。
   默认仍是每状态最多 3 轮、每轮最多 3 问；`fsm` 元数据新增 `questions_per_turn` 字段。
2. **自动终止追问**：当本状态没有可问问题、或追问轮数到达上限时，状态机自动进入下一状态；
   调用方也可通过 `answer_stage(..., end_state=True)` 或 `end_current_state()` 主动终止。
3. **红旗硬门控收紧**：`answer_red_flags(..., end_state=True)` 不再能跳过未回答的红旗问题，
   行为与 `end_current_state()` 一致；红旗未答完时任何方式都不能离开 S1_REDFLAG。
4. **自主问诊驱动器** `run_scripted_interview(answers)`：给定回答池后，状态机自主完成
   全流程问诊——逐轮取 next_questions（规则优先，开启 `use_llm_questions` 时叠加 Tao 改写）、
   提交可用回答、无可答问题时自动结束当前状态的追问，命中 urgent 红旗立即硬停止，
   到达 S9 自动生成最终报告。返回 `stopped_reason`（completed / red_flag_urgent /
   blocked_unanswered_red_flags / max_total_turns_reached）与完整 `transcript` 供审计回放。
5. **前端对齐**：静态原型新增"追问轮数上限（1–5）"设置与"答完自动进入下一状态"开关；
   单选题答完当前轮会自动深化追问或自动进入下一状态，红旗页在全部回答前禁止手动跳过。

## FSM v0.5：Tao 自动追问 + 经验推理 + 经验总结

在 v0.3/v0.4 的“规则决定边界、Tao 安全叠加”基础上，新增三项 Tao 能力，全部遵循
“确定性输出为准 → Tao 叠加 → JSON Repair → Output Guard → 失败/违规回退”的统一管线：

1. **规则约束内自动追问**（`tao_followup_probe_skill`）
   - 与 `tao_question_planner_skill`（只能重排/改写既有规则问题 id）不同，本技能允许 Tao
     **生成新的澄清式追问**，但施加硬约束：
     - 仅在临床内容状态启用（S3–S8）；红旗筛查 S1、知情 S0、人口学 S2 不开放生成式追问；
     - 每轮最多 `tao_probe_budget` 个（`CaseGuideSession` 默认 2，可设 0 关闭）；
     - `field_hint` 必须取自本状态允许字段或为 null，越界自动降级为纯文字线索；
     - 追问 **不驱动状态跳转**：答案以 `TAO_PROBE_*` 记入 `case_state.tao_probe_answers`
       与 `answer_evidence`（`source=tao_probe, advisory_only=true`），状态推进仍由规则问题决定；
     - 出现诊断/证型判定/处方/剂量或越主题，则整轮追问作废，回退为“不追问”。
   - `next_questions` 在确定性问题（可经 planner 改写）之后追加 Tao 追问；
     `fsm.tao_probe_runtime` 暴露其状态供审计与 UI 展示。
2. **医师经验辨证推理**（`physician_reasoning_skill`）
   - 规则先构建确定性推理链：四诊采集 → 辨证倾向 → 治法 → 方剂路线 → 药物模块 → 安全复核 → 沈老经验信号，
     每步带证据；Tao 仅把推理链语言化为教学解释，不得新增规则层没有的结论；患者角色拦截。
3. **案例经验总结自动生成**（`case_experience_summary_skill`）
   - `mode="case"` 生成单案医案按语；`mode="experience"` 基于脱敏挖掘统计生成经验规律总结；
     确定性总结为事实来源与回退，Tao 仅润色。

`final_report` 现额外返回 `physician_reasoning` 与 `case_experience_summary`，均为
`draft_for_clinician_review`、`patient_visible=false`。

## v0.6：多智能体自主协作编排

`backend/agents/` 将隐式的 skill 顺序调用升级为显式的多智能体协作，编排机制可审计、可视化：

- **共享黑板**（`backend/agents/base.py::Blackboard`）：智能体共享工作记忆；上游写结论、下游读取续接。
- **智能体**（`backend/agents/clinical_agents.py`）：每个智能体包装一个已测试 skill，声明
  name/role/kind(rule|llm)/handoff，并返回 `AgentResult`（status、confidence、evidence、used_llm、
  llm_runtime、halt_pipeline）。
- **编排器**（`backend/agents/orchestrator.py::AgentOrchestrator`）：
  - 顺序：CaseStructuring → RedFlag → OrthoRisk → TcmSyndrome → FormulaReasoning → HerbModule →
    ConflictSafety → EvidenceTrace → Reasoning(llm) → Experience(llm) → PhysicianReview；
  - **自主中止**：`RedFlagAgent` 命中 urgent 时 `halt_pipeline=True`，下游临床智能体记为 skipped，
    仅 `EmergencyNoticeAgent`（`runs_after_halt=True`）续跑；
  - 输出 `collaboration_trace`、`agent_roster`、`used_llm_agents`、`llm_in_loop`、`blackboard`。
- **集成**：`CaseGuideSession.run_agent_collaboration()` 为独立入口；`final_report()` 以编排器为
  唯一"大脑"，从 blackboard 还原全部既有返回键并附带 `agent_collaboration`。
- **安全不变量**：确定性输出为事实来源；仅 Reasoning/Experience 调用语言模型且必经 Output Guard；
  任何智能体都不得产出最终诊断/处方/可执行剂量；红旗为硬门控。

## v0.7：多轮智能问答 + 语言模型自主调用技能

`backend/agents/skill_router.py` 与 `backend/agents/conversation.py` 在多智能体编排之上叠加
对话式入口，实现“语言模型自主选择并调用不同 skill，再按用户提问挖掘数据”：

- **技能注册表** `INTENTS`：12 个意图，每个携带 description、keywords 与**示例问题**（用于引导用户）。
- **意图路由** `route_intent`：
  - 确定性关键词匹配始终可用、可回退；
  - 开启 Tao 时调用 `DaoClient.route_skill`，只能返回 `ALLOWED_INTENTS` 内的 intent，
    越界/解析失败回退关键词，全过程记录 `llm_runtime`；
  - 患者请求最终诊断/处方/剂量经 `patient_request_guard_skill` 拦截到 `safety_block`。
- **自主调用技能** `ConversationSession.ask`：路由命中后自主调用对应 skill（syndrome_router、
  formula_base_selector、herb_module_composer、safety_guard、physician_reasoning、
  case_experience_summary、mined_evidence 等），并返回调用了哪些 skill、路由方式、规则/语言模型来源。
- **按提问挖掘** `query_mined`：解析问题中的证型/方剂/症状/药物，实时查询
  `rules/11_mined_rule_candidates.yaml` 的统计与关联规律（support/confidence/lift、剂量分布）。
- **引导用户提问** `suggested_questions`：按能力分组给出示例问题，UI 渲染为可点击 chips。
- **安全不变量**：答案来自确定性规则/脱敏数据；语言模型仅做技能选择与措辞，不产出最终诊断/处方/剂量。

UI 新增「智能问答」模块：多轮对话气泡、示例问题 chips、每条回答标注意图/技能/路由方式/规则-语言模型来源。

## v0.8：自主多步智能体（Plan → Subagent 委派 → Synthesize）

`backend/agents/autonomous_agent.py` 把单意图问答升级为前沿 agent 范式（ReAct + Plan-and-Execute
+ subagent 委派），即“自由问答智能体”：

- **plan_question**：把问题分解为有序多步计划（每步=intent+理由）。确定性关键词规划始终可用；
  开启 Tao 时 `DaoClient.plan_skills` 可重排/扩展，但 intent 必须 ∈ ALLOWED_INTENTS，越界/解析失败回退。
- **AutonomousQAAgent.run**：逐步把计划委派给负责该 intent 的子智能体（`ConversationSession.invoke`），
  累积观察，支持一问多技能；输出 ReAct 轨迹（thought → action(delegate→subagent) → observation）。
- **_synthesize**：多步时综合各子智能体观察为一条回答（含计划概述与分步小结），单步时直接返回。
- **安全不变量**：子智能体只运行注册技能、基于确定性规则/脱敏数据；患者请求诊断/处方/剂量拦截；
  语言模型只规划与编排技能，不产出临床结论。
- **入口**：`python -m backend.main --ask "..." --autonomous`；UI「智能问答」模块「自主多步」开关
  展示计划链、子智能体委派与观察、综合结论。
