# 临床安全档案（Clinical Safety Case）— YaoBi-Skill 研究型 CDSS 原型

> 本文档参照 NHS DCB0129（制造方临床风险管理）/ DCB0160（部署方临床风险管理）的**思路**编写危害日志与安全论证，
> 但**不声明**符合任何法规或标准认证。本系统是研究/教学原型，不是已注册或已认证的医疗器械软件。

- 文档版本：v1.0（2026-07）
- 适用系统版本：`APP_VERSION = "0.5.0"`（`backend/provenance.py`）
- 相关文档：[`docs/safety_policy.md`](safety_policy.md)、[`docs/feature_review_2026-07.md`](feature_review_2026-07.md)、[`docs/final_functionality_audit.md`](final_functionality_audit.md)

---

## 1. 文档目的与范围

本文档为 YaoBi-Skill（沈钦荣腰痹经验规则智能体）建立**危害日志（Hazard Log）与安全论证**，目的是：

1. 系统性列举本 CDSS 原型在预期用途内的可预见危害；
2. 对每项危害给出**指向真实代码与真实测试**的缓解措施与验证证据（而非愿景式描述）；
3. 诚实标注残余风险与"已规划但尚未验证"的缓解项，作为后续治理工作的输入。

**预期用途（claim boundary）**：本系统仅用于名老中医经验研究、医案复盘、教学训练与科研标注。README 的用途边界声明原文：

> "本项目仅用于名老中医经验研究、医案复盘、教学训练、处方经验挖掘与科研标注，不构成诊断、处方或治疗建议，不提供患者自用方案。"（`README.md`）

**明确排除的用途**：直接面向患者的诊疗、自动处方、剂量医嘱、替代执业医师判断、任何未经医师复核即对外发布的临床内容。

**范围内的软件构件**：`backend/`（规则引擎、技能、多智能体编排、LLM 运行时与守卫、审计与溯源）、`rules/`（YAML 规则库）、`backend/server.py`（HTTP 服务）、`frontend/`（静态 UI）、`backend/mining/`（脱敏挖掘）。硬件、部署基础设施、网络与鉴权体系不在本档案范围内（见第 5 节部署前置条件）。

---

## 2. 系统安全声明

**顶层安全声明（Top-level Safety Claim）**：

> **系统不产出患者可执行的诊断、处方或剂量；所有临床内容均为 `draft_for_clinician_review`（医师复核草案），最终诊断/处方/剂量/煎服法/疗程只能由执业医师手工录入并签名锁定。**

该声明由以下架构不变量支撑（均有代码与测试背书，详见第 3 节危害日志）：

| # | 子声明 | 主要实现 |
|---|---|---|
| S1 | Rule-first：临床结论以确定性规则引擎输出为事实来源，语言模型只做叠加解释，失败/违规即回退 | `backend/skills/pipeline.py`、`backend/skills/tao_report_generation_skill.py` |
| S2 | 红旗优先于一切中医规则：马尾综合征等急诊线索命中即硬停问诊/中止下游智能体 | `backend/agents/yaobi_interview.py`、`backend/agents/orchestrator.py` |
| S3 | 角色感知输出守卫：患者端严格地板（无诊断/处方/剂量），医师端草案禁止断言式结论与可执行医嘱 | `backend/llm/output_guard.py` |
| S4 | 医师签名门控：模型生成内容不得被标记为最终诊断/处方，签名记录强制医师身份与执业证号 | `backend/skills/physician_review_skill.py` |
| S5 | 患者请求拦截：即使用户要求"标注需医师审核"，也不生成最终诊断、完整处方或可执行剂量（`docs/safety_policy.md`） | `backend/skills/patient_request_guard_skill.py` |

---

## 3. 危害日志（Hazard Log）

**残余风险评级定义**（针对本系统**预期用途 = 研究/教学**的定性评级）：

- **低**：在预期用途内，现有缓解措施可信地把危害限制在可接受范围，且缓解有自动化回归测试锚定。
- **中**：缓解措施真实存在且大多有测试，但存在已知盲区（黑名单式守卫、启发式否定识别、无临床验证集等）。
- **高**：缓解措施只能部分覆盖危害面，剩余部分依赖尚不存在的验证（临床数据集、外部专家盲审、人因研究）；该项应视为**部署前置条件**（第 5 节）。

> 引用格式约定：缓解措施引用 `文件 — 机制`；验证证据引用 `测试文件::测试函数`。标注"**状态：本轮实现**"的机制属于本轮并行治理工作流的目标架构，其中尚无专项测试的项会**显式注明**。

| 危害ID | 危害描述 | 潜在临床后果 | 缓解措施（代码引用） | 验证证据（测试引用） | 残余风险评级与理由 |
|---|---|---|---|---|---|
| **H1** | 红旗漏检（尤其马尾综合征）：问诊未识别大小便失禁/会阴麻木等急诊线索，继续常规辨证 | 马尾综合征延误急诊减压，可致永久性大小便功能障碍、下肢瘫痪；骨折/感染/肿瘤延误诊治 | ① `backend/agents/yaobi_interview.py` — 红旗**双通道**：确定性关键词扫描 `_scan_red_flag_text`（`RED_FLAG_TEXT_KEYWORDS`，不依赖 LLM）∪ LLM 槽位抽取，`_detect_red_flags` 中 `bowel_bladder_dysfunction`/`saddle_anesthesia` 置 `safety_level="emergency"`，`run_turn` 立即转 `SAFETY_REFERRAL` 并 `done=True` 硬停；② `backend/agents/orchestrator.py` — `RedFlagAgent` 命中后 `halt_pipeline=True`，下游临床智能体记为 skipped，仅急诊通知智能体续跑；③ `backend/skills/caseguide_state_machine.py` — 红旗问题未答完不允许离开红旗筛查状态，urgent 硬停后续问诊；④ `backend/skills/red_flag_screen_skill.py` — urgent → `stop_and_refer`；⑤ `backend/skills/safety_guard_skill.py` — `cauda_equina_symptoms` 等映射 `safety_status="urgent"`（`rules/07_safety_rules.yaml`） | `tests/test_review_hardening.py::test_interview_detects_cauda_equina_without_llm`（LLM 离线仍拦截）；`tests/test_server.py::test_interview_red_flag_hard_stop`；`tests/test_agents.py::test_red_flag_agent_autonomously_halts_downstream`；`tests/test_caseguide.py::test_red_flag_screen_stops_on_urgent`、`::test_run_scripted_interview_hard_stops_on_urgent_red_flag`、`::test_caseguide_cannot_manually_skip_unanswered_red_flags`；`tests/test_pipeline.py::test_safety_flags_raw_red_flag` | **中**：关键词表覆盖有限（方言、罕见表述可漏），否定窗口仅 6 字符启发式；LLM 抽取召回未在专家标注集上评测过灵敏度。在"研究/教学、非患者自用"的用途边界内可接受，临床部署前必须做红旗灵敏度验证 |
| **H2** | 红旗假阳性导致过度急诊：否认句（"没有大小便失禁"）或字符串槽位"否"/"正常"被当作阳性，触发急诊硬停 | 不必要的急诊转诊、患者焦虑、医疗资源占用；正常问诊被错误终止 | ① `backend/agents/yaobi_interview.py` — `_slot_positive` 把否认字符串（`_NEGATIVE_SLOT_STRINGS`：否/没有/正常/阴性…）归一为 False；`_scan_red_flag_text` 的 `_NEGATION_MARKERS` 否定窗口跳过被否认关键词；② 同文件 — 红旗分级：仅马尾线索为 `emergency` 硬停，其余为 `high`（advisory，用户可澄清）；③ 同文件 `run_review(action="override")` — 医师可覆盖红旗评估并恢复 FSM（覆盖理由留痕于 `physician_review`） | `tests/test_review_hardening.py::test_interview_negated_red_flag_does_not_trigger`、`::test_interview_string_denial_slot_does_not_trigger_emergency`；`tests/test_server.py::test_interview_negation_does_not_trigger_red_flag`、`::test_interview_physician_override_resumes_fsm` | **低**：误报方向是"故障-安全"（宁可过度转诊），且有带留痕的医师覆盖通道；残余仅为启发式否定识别的边角误差 |
| **H3** | 面向患者泄漏可执行处方/剂量：模型输出"水煎服/每日X次/细辛三克"等患者可执行内容 | 患者自行服用高风险药物（附片、细辛、虫类药、乌头类），中毒、药害、延误正规诊疗 | ① `backend/llm/output_guard.py` — 患者端严格守卫 `guard_tao_output`：`FORBIDDEN_PATTERNS` 覆盖诊断断言、处方短语、阿拉伯数字**与中文数字**剂量/频次（"三克""一日三次"）、先煎/后下、自行购药，命中即 `fallback_required` 回退确定性模板；结构化 JSON 禁字段 `_FORBIDDEN_STRUCTURED_KEYS`（final_diagnosis/complete_prescription/patient_executable_dose/administration_instruction 必须为空）；② 同文件 `guard_consultation(user_role="patient")` — 患者角色强制走严格地板；③ `backend/skills/patient_request_guard_skill.py` — 患者索取诊断/处方/剂量的**请求侧**拦截；④ `backend/skills/cdss_recommendation_skill.py` — CDSS 草案拒绝患者角色；⑤ `backend/skills/physician_review_skill.py` — `patient_visible_until_signed=False`，未签名内容不患者可见 | `tests/test_review_hardening.py::test_patient_guard_blocks_chinese_numeral_doses`；`tests/test_tao_runtime.py::test_guard_detects_diagnosis_prescription_and_dose_patterns`、`::test_tao_overlay_rejects_prescriptive_output_and_falls_back`；`tests/test_conversation.py::test_patient_request_for_prescription_is_blocked`；`tests/test_caseguide.py::test_patient_request_guard_blocks_final_diagnosis_prescription_and_dose`、`::test_cdss_recommendation_blocks_patient_role`；`tests/test_tao_reasoning.py::test_guard_consultation_is_role_aware` | **中**：守卫本质是黑名单正则，改写式表达（隐喻、拼音、非常见剂量单位、跨句拆分）理论上可绕过；尚无对抗性红队评测。缓解链多层（请求侧+输出侧+角色门控+签名门控）故不评"高" |
| **H4** | 药物相互作用漏报：中西药合用（抗凝药×活血/虫类药）、十八反（附片×半夏）、妊娠/肝肾禁忌未被提示 | 出血事件、乌头碱心脏/神经毒性、妊娠用药损害、肝肾功能恶化 | ① `rules/06_conflict_rules.yaml` — `herb_drug_interactions`（抗凝×活血、抗凝×虫类、NSAID×甘草/活血、糖皮质激素×甘草等）+ 十八反 `shibafan_wutou_banxia` + 合并病/妊娠禁忌，逐条 `alert_level`；② `backend/engine/conflict_resolver.py` — `check_interactions` 子串双向容错匹配（"华法林片"仍命中），空串永不匹配；③ `backend/skills/conflict_checker_skill.py` — 方剂路线核心模块并入检查池，`alert_summary.requires_dual_signoff`。**状态：本轮实现** | `tests/test_interactions.py`（本轮新增）：`::test_anticoagulant_x_huoxue_fires_interruptive`、`::test_anticoagulant_x_insect_herbs_fires_interruptive`、`::test_fupian_x_banxia_shibafan_fires_interruptive`、`::test_pregnancy_contraindication_fires_interruptive`、`::test_drug_matching_is_substring_tolerant`、`::test_skill_flattens_formula_route_into_interaction_pool`、`::test_toxic_herbs_x_hepatorenal_and_cardiac` | **高**：知识库仅十余条高频规则，远非药典级完整相互作用库，漏报（假阴性）风险本质存在；未接入权威相互作用数据库、无药师审校记录。**部署前置条件** |
| **H5** | 模型幻觉超出规则证据：LLM 在教学报告/会诊/推理叠加中编造规则层不存在的证型、方药、机理或"经验" | 医师被错误内容误导，错误教学内容扩散，虚构的"沈老经验"污染知识库 | ① `backend/skills/pipeline.py` — Rule-first：确定性管线是事实来源，LLM 仅可选叠加；② `backend/skills/tao_report_generation_skill.py`、`tao_consultation_skill.py` — JSON 合约 + `backend/llm/json_repair.py` + 输出守卫，失败/违规自动回退确定性模板；③ `backend/agents/skill_router.py`、`autonomous_agent.py` — 意图/计划只能取自 `ALLOWED_INTENTS` 白名单，越界回退关键词路由；④ `backend/skills/tao_question_planner_skill.py` — Tao 只能重排/改写既有问题 id，新增 id 即拒绝；⑤ `backend/agents/yaobi_interview.py::_build_report` — 会诊证据包注入规则引擎的证型/方剂/药物模块/安全审查/不确定性，约束模型论述；⑥ `backend/skills/physician_reasoning_skill.py` — 推理链由规则确定性构建，Tao 仅"语言化"，不得新增证型/方药 | `tests/test_tao_runtime.py::test_tao_overlay_rejects_prescriptive_output_and_falls_back`、`::test_dao_mock_question_plan_preserves_candidate_ids`；`tests/test_conversation.py::test_route_intent_llm_overlay_stays_within_allowlist`、`::test_route_intent_falls_back_when_llm_returns_invalid_intent`；`tests/test_autonomous_agent.py::test_plan_question_llm_overlay_stays_in_allowlist_and_falls_back`；`tests/test_caseguide.py::test_tao_question_overlay_rejects_new_ids_and_prescriptive_text`；`tests/test_tao_reasoning.py::test_physician_reasoning_falls_back_on_unsafe_tao_output` | **中**：守卫能拦截结构化越界与模式化违规，但**无法验证自由文本叙述内部的临床事实正确性**——长篇会诊中的细节性幻觉（错误机理、伪造引文）正则不可捕获，最终依赖医师复核（S4）与反馈闭环（第 4 节） |
| **H6** | 规则内容错误/过时：YAML 规则库中的证型判据、方剂路线、剂量经验区间本身错误或随认识更新而过时 | 系统性地向所有医师复核者输出同一错误倾向，教学内容失真 | ① `rules/01–11_*.yaml` — 规则外置为可读 YAML，独立于代码可审；② `backend/mining/xlsx_case_miner.py` — 挖掘候选规则强制 `status: pending_expert_review`、`clinician_only: true`，不参与自动决策；③ `evaluation/expert_review_template.md` — 专家评审表（规则忠实度/理论合理性/是否过度推断/是否误导临床）；④ `backend/provenance.py` — `rules_fingerprint` 对每个规则文件做内容哈希，规则改动即版本可见（**状态：本轮实现**）；⑤ 第 6 节变更管理流程 | `tests/test_mining.py::test_rule_candidates_are_pending_review_and_clinician_only`；`tests/test_interactions.py::test_rule_file_schema_and_review_phrasing`（本轮新增：规则 schema 与"需医师审核"措辞校验）；`tests/test_pipeline.py::test_pipeline_matches_expected_rule_signals`（核心规则行为回归锚点） | **高**：规则源自单一专家（沈钦荣）经验，**无外部专家盲审记录、无临床验证数据集**；16 例带标注预期的黄金病例回归集已建立（`evaluation/golden_cases.yaml` + `backend/evaluation/benchmark.py`，CI 阈值断言见 `tests/test_benchmark.py`；预期标签为项目内标注，尚未经独立专家盲审确认）。**部署前置条件** |
| **H7** | 隐私泄漏：医案挖掘或审计日志泄露患者姓名、病案号等 PII，或原始就诊叙述外流 | 患者隐私侵害、法律责任、研究数据合规风险 | ① `backend/mining/xlsx_case_miner.py` — `PII_COLUMNS`（姓名/病案号/地址/医师工号/就诊序号/就诊日期/医师姓名）在内存中即被丢弃，自由文本仅关键词扫描后丢弃，产物只含聚合统计与行号；② `.gitignore` — `data/private/` 与 `*.xlsx` 全局禁入仓库；③ `backend/audit/audit_log.py` — 审计日志对**患者叙述不存原文**，只存 `text_digest`（SHA-256 前 16 位 + 字符数）；医师撰写的反馈原因（`/api/feedback` reason，≤500字）按设计以明文留存以支撑规则策展，UI 明确提示勿粘贴患者身份信息（**状态：本轮实现**）；④ `backend/server.py` — 500 响应只回错误类型+消息，堆栈仅落服务端日志；⑤ `backend/skills/consent_privacy_skill.py` — 问诊侧知情与脱敏 | `tests/test_mining.py::test_loader_deidentifies_rows`、`::test_write_outputs_contain_no_pii_and_load_back`；`tests/test_caseguide.py::test_consent_desensitizes_and_preserves_boundary`。审计日志脱敏（③）由 `tests/test_cdss_governance.py::test_text_digest_never_stores_content`、`::test_audit_log_appends_jsonl_records`、`::test_chat_decision_is_audited` 专项验证 | **中**：会话内存中的对话原文仍以明文存在（`server.py` `_INTERVIEWS`）；服务无鉴权与传输加密（由部署方负责）；关键词扫描不是严格的 NLP 去标识。研究场景（本地/私有部署 + gitignore）下可接受 |
| **H8** | 自动化偏倚：医师过度信任系统草案，把"候选证型/方剂路线"当作结论直接采用 | 医师复核流于形式，错误草案（H4/H5/H6 的下游）被直接转化为临床行为 | ① 全链路输出状态固定 `draft_for_clinician_review` + 强制免责声明（`backend/skills/safety_guard_skill.py` `required_disclaimer`，文案在 `rules/07_safety_rules.yaml`）；② `backend/skills/uncertainty_skill.py` — 弃权阈值与鉴别缺口显式发声（低分即 `abstain`、说明"补什么信息才能改变判断"），注入 `yaobi_interview._build_report` 证据包（**状态：本轮实现**）；③ 前端诚实标注"模型真实路由 vs 关键词回退 vs 离线"，不夸大模型在环；④ `backend/skills/physician_review_skill.py` — 最终内容必须医师**手工录入**并签名，系统拒绝模型生成的最终诊断/处方；⑤ `backend/server.py::handle_feedback` — 医师对每条输出可标 确认/需修订/不采纳，`/api/metrics` 暴露采纳率（**状态：本轮实现**） | `tests/test_caseguide.py::test_physician_review_allows_signed_manual_content_and_rejects_model_generated`；`tests/test_frontend_static.py::test_frontend_calls_backend_for_genuine_llm`（前端不冒充模型在环）。uncertainty_skill（②）由 `tests/test_cdss_governance.py::test_uncertainty_abstains_when_no_candidates`、`::test_uncertainty_reports_narrow_separation_with_differential_gaps` 等 5 项专项验证；/api/feedback（⑤）由 `::test_feedback_endpoint_validates_and_counts` 专项验证 | **中**（预期用途内）/ 临床部署视为**高**：自动化偏倚是人因风险，标注、弃权与签名门控是必要非充分条件，未做任何使用者研究；在研究/教学用途内风险可控，临床使用前需人因评估 |
| **H9** | 模型服务不可用/加载失败：Tao 后端超时、网络故障、显存不足、加载崩溃 | 若失败穿透，问诊中断或返回残缺内容；最坏情况是安全检查随模型一起失效 | ① `backend/llm/dao_client.py` — 所有网络/解析/子线程失败统一转 `DaoRuntimeError`，5xx/超时有限重试、4xx 立即失败，**所有消费者失败即回退确定性规则**；② `backend/agents/yaobi_interview.py` — 红旗检测的确定性通道不依赖 LLM（`use_llm=False` 时安全网全量生效）；③ `backend/server.py` — 模型后台预加载失败被捕获不崩溃，`/api/health` 暴露 `load_state`；④ 默认 `TAO_BACKEND=disabled`，模型是可选叠加而非依赖 | `tests/test_review_hardening.py::test_http_backend_network_failure_raises_dao_runtime_error`、`::test_report_skill_falls_back_when_http_backend_unreachable`；`tests/test_tao_runtime.py::test_tao_disabled_runtime_falls_back_to_deterministic_report`、`::test_dao_preload_surfaces_transformers_load_failure_without_crashing`；`tests/test_server.py::test_disabled_backend_is_offline`；`tests/test_review_hardening.py::test_interview_detects_cauda_equina_without_llm`（离线红旗） | **低**：确定性功能（规则、红旗、报告模板）在模型完全不可用时全量可用，损失的只是叠加解释质量且 UI 如实标注 |
| **H10** | 告警疲劳：安全提示过多/过泛（或守卫误杀正常教学内容触发无谓回退），医师习惯性忽略告警 | 真正关键的 interruptive 告警（十八反、抗凝相互作用）被淹没或跳过 | ① `rules/06_conflict_rules.yaml` + `backend/skills/conflict_checker_skill.py` — 分层告警：`interruptive`（需医师显式确认，置 `alert_summary.requires_dual_signoff=true`）与 `advisory`（随草案展示的被动提示）分级，仅高危组合为 interruptive（**状态：本轮实现**）；② `backend/llm/output_guard.py` — 守卫正则收紧（"每次发作持续多久""IgG"不再误杀），减少无谓回退；③ `backend/agents/yaobi_interview.py` — 红旗分 `emergency`（硬停）与 `high`（可澄清），不一刀切 | `tests/test_interactions.py::test_alert_summary_counts_across_tiers`、`::test_advisory_only_does_not_require_dual_signoff`、`::test_existing_three_conflicts_still_fire_with_alert_level`（本轮新增）；`tests/test_review_hardening.py::test_patient_guard_no_longer_false_kills_teaching_phrases`、`::test_probe_guard_allows_frequency_questions` | **中**：分层阈值未经真实医师工作流校准；`requires_dual_signoff` 目前只是服务端输出的标志，**实际阻断依赖 UI/流程约定**，服务端未强制拦截未确认的 interruptive 告警 |
| **H11** | 会话串号/数据混淆：多客户端问诊会话共用状态，患者 A 的红旗/槽位污染患者 B 的会话 | 错误的红旗归属（漏报或误报到他人）、医案数据交叉污染 | ① `backend/server.py` — 缺失 `session_id` 时服务端生成 `srv-<uuid>` 并随响应返回（不再共用 `"default"` 键）；② 同文件 — 会话字典 LRU 淘汰（上限 256），按 session 加锁串行化单会话回合；③ 同文件 — 请求体上限 1 MiB、畸形 JSON 返回 400（防御性输入处理） | `tests/test_review_hardening.py::test_interview_without_session_id_gets_server_generated_isolated_sessions`；`tests/test_server.py::test_interview_physician_confirm` 等问诊多轮测试隐式覆盖单会话状态持续性 | **中**：无鉴权体系，session_id 可被猜测/冒用；会话为内存态，进程重启即丢失，LRU 淘汰会静默丢弃闲置会话（对研究场景可接受，临床场景需持久化+鉴权） |
| **H12** | 版本漂移：规则/应用/模型配置被修改后，历史报告无法追溯"当时是哪个版本给出的建议"，规则改动不可审计 | 事故回溯不能定位责任版本；未经评审的规则修改静默上线 | ① `backend/provenance.py` — `rules_fingerprint()` 对 `rules/*.yaml` 逐文件内容哈希 + 库级组合哈希，`get_provenance()` 附带 `APP_VERSION` 与模型运行时指纹（model_id/backend/dtype/量化）；② `backend/skills/pipeline.py` + `report_generation_skill.py` — 每份报告尾注"规则库版本/应用版本"溯源行；③ `backend/server.py` — `/api/health` 与 `/api/metrics` 暴露 provenance，审计记录可与之对齐；④ 第 6 节变更管理：规则改动必须伴随版本号变化（哈希自动变化）+ git PR 评审。**状态：本轮实现** | `tests/test_cdss_governance.py::test_rules_fingerprint_is_stable_and_covers_all_rule_files`、`::test_provenance_includes_model_runtime_when_config_given`、`::test_pipeline_report_carries_provenance_and_uncertainty`、`::test_health_exposes_provenance` 专项验证 | **中**：指纹只能证明"用了哪个版本"，**不能阻止**未经评审的修改被部署——防线是流程性的（第 6 节）而非技术强制；需配合 git 权限与 CI 门禁 |

---

## 4. 治理机制映射（临床治理闭环）

以下六项机制属于本轮 CDSS-governance 升级的目标架构（**状态：本轮实现**，由并行工作流落地；各项的测试覆盖状态在第 3 节对应危害行中如实标注）。它们串成一个可审计的临床治理闭环：

```text
决策产出（规则+守卫下的草案）
  → ① 溯源指纹：这份草案由哪个规则库/应用/模型版本产出
  → ② 追加式审计日志：为什么系统这么说（路由、守卫判定、回退、红旗等级）
  → ③ 分层告警：哪些风险必须医师显式确认（interruptive），哪些随案提示（advisory）
  → ④ 医师签名终审：最终诊断/处方只能人工录入并签名锁定
  → ⑤ 医师反馈闭环：确认/需修订/不采纳 回流到审计与指标
  → ⑥ 黄金病例回归：规则修订不破坏既往正确行为 → 版本号变化 → 回到 ①
```

1. **溯源指纹（Provenance）** — `backend/provenance.py`：`rules_fingerprint` + `APP_VERSION` + 模型运行时指纹；随 `final_report`（`backend/skills/pipeline.py`）、报告尾注（`report_generation_skill.py`）、`/api/health`、`/api/metrics` 与审计记录发布。对应危害 H12。
2. **追加式审计日志（Append-only Audit Log）** — `backend/audit/audit_log.py`：每个 API 决策以 JSON line 追加到按日文件；`backend/server.py` 的 `_AUDITED_ENDPOINTS`（/api/chat、/api/autonomous、/api/interview、/api/collaboration、/api/followup_probe）经 `_decision_summary` 提取决策事实（意图、路由方式、守卫判定、回退、红旗等级、review_action），**不存患者原文**（`text_digest`）。写失败绝不阻断临床请求（IO 错误吞掉并计数）。对应危害 H7、H12。
3. **医师反馈闭环（Physician Feedback Loop）** — `backend/server.py::handle_feedback`：医师对任一输出提交 `confirmed / revised / rejected`（+ 原因），落审计日志并在 `/api/metrics` 汇总 `acceptance_rate`；规则维护者由此看到哪些推荐不被信任，作为规则修订输入。对应危害 H8、H6。
4. **黄金病例回归（Golden-case Regression）** — 状态：本轮实现。`evaluation/golden_cases.yaml` 提供 16 例覆盖全部 6 个规则证型（各≥2例）、3 例红旗、良性与模糊病例的标注集；`python -m backend.evaluation.benchmark` 输出 top-1/top-2 证型准确率、方剂召回、红旗召回、安全等级准确率与守卫对抗集捕获/误杀率；`tests/test_benchmark.py` 在 CI 强制红旗召回=100%、守卫捕获=100%、误杀=0%。剩余欠账是预期标签的独立专家确认，而非病例集本身的存在。
5. **分层告警（Tiered Alerts）** — `rules/06_conflict_rules.yaml` 逐条 `alert_level` + `backend/skills/conflict_checker_skill.py` 的 `alert_summary.requires_dual_signoff`：interruptive 需医师显式确认，advisory 被动展示，限制告警疲劳。对应危害 H4、H10。
6. **医师签名终审（Physician Sign-off）** — `backend/skills/physician_review_skill.py`：强制医师身份三要素（physician_id/physician_name/license_id）+ `role=licensed_physician` + `signed=True`；拒绝 `source == "model_generated"` 的最终诊断/处方；高风险药物命中自动加警示；签名记录含审计块。此为闭环的**人类终审节点**，早于本轮存在且有测试（`tests/test_caseguide.py::test_physician_review_allows_signed_manual_content_and_rejects_model_generated`）。对应危害 H3、H8。

---

## 5. 遗留风险与部署前置条件

以下事项使本系统**不得**在真实临床环境中作为决策依据部署。每一项都是明确的前置条件，而非"建议改进"：

1. **无临床验证数据集**：红旗灵敏度/特异度、证型判别准确率、方剂路线合理性均未在专家标注基准集上评测（`docs/feature_review_2026-07.md` 第四节已声明"尚无专家标注基准集"）。16 例黄金病例集已建立但预期标签为项目内标注、未经外部盲审，不构成临床验证基准。
2. **无法规注册/认证**：未做 NMPA/CE/FDA 任何医疗器械软件注册，本档案仅**参照** DCB0129/0160 思路，不构成合规声明。
3. **单一经验来源的泛化风险**：全部规则源于沈钦荣个人腰痹诊疗经验（单中心、单专家），未经其他流派/地域专家盲审；对不同人群、不同证候谱的泛化能力未知。挖掘侧数据质量问题已如实标注（门诊导出"中医四诊"栏多为模板文本，`data_quality.tongue_pulse_usable=false`，见 `README.md`）。
4. **模型许可边界**：`config/model_config.yaml` 明确 `license_boundary: non_commercial_non_medical_research`——Dao1-30b-a3b 的使用限于非商业、非医疗用途的科研；任何商业化或医疗场景部署需先解决模型许可。
5. **工程性前置**：无鉴权与用户体系、无持久化（会话/审计仅本地文件与内存）、无传输加密约定、`mined_evidence_skill` 自身无 `user_role` 守卫（"仅医师端"依赖调用方隔离，见 `docs/feature_review_2026-07.md`）、抽取/归一化为轻量关键词+别名扫描（复杂叙述召回有限）。
6. **本轮新机制的专项验证**：审计日志脱敏、`/api/feedback`、`uncertainty_skill`、provenance 指纹均已有专项回归测试（`tests/test_cdss_governance.py`，17 项）；黄金病例回归由 `tests/test_benchmark.py`（9 项）强制阈值。仍属欠账的是这些机制在真实医师工作流中的人因验证与长期运行数据。

---

## 6. 变更管理

规则库（`rules/*.yaml`）与安全相关代码的任何修改必须走以下流程；跳过任何一步的修改不得合入主干：

1. **专家评审**：临床内容改动（证型判据、方剂路线、剂量经验区间、相互作用条目）必须由具备资质的中医师按 `evaluation/expert_review_template.md` 评审（规则忠实度、理论合理性、是否过度推断、是否误导临床、安全提示充分性），评审记录随 PR 存档。挖掘产生的候选规则在评审通过前保持 `status: pending_expert_review`、`clinician_only: true`，不参与自动决策。
2. **黄金病例回归**：改动后全量运行 `python -m pytest tests/`（136 项既有 + 本轮并行治理工作流新增，须全绿）并通过黄金病例锚点（`tests/test_pipeline.py::test_pipeline_matches_expected_rule_signals` 等）；新增规则应同步新增至少一个正例与一个不触发反例测试（参照 `tests/test_interactions.py` 的模式）。行为预期变化必须在 PR 中逐条说明并更新对应测试，禁止静默改预期。
3. **版本号提升**：任何影响推荐行为的改动必须提升 `backend/provenance.py` 的 `APP_VERSION`（与 `pyproject.toml` 同步）；规则内容哈希（`rules_fingerprint`）随内容自动变化，使新旧报告的溯源行可区分。安全语义的改动（红旗、守卫、告警分层）需同步更新本文档对应危害行的缓解措施与验证证据。
4. **审计可回溯**：变更通过 git PR 合入（评审人留痕）；部署后首个请求起，审计日志与 `/api/health` 的 provenance 块即携带新版本指纹，使"哪一天起哪个版本在给建议"可事后重建。

---

*本文档由代码与测试证据支撑；引用如与代码不一致，以代码为准并应视为本文档的缺陷提出修订。*
