# 与顶级下一代智慧 CDSS 智能体的差距分析与改进路线（v0.15）

> 本文件是一次深度代码审查 + 顶级设计调研的产物。第 1 部分给出本轮已完成的
> 缺陷修复（均经实测复现 + 回归测试固化）；第 2 部分对照 2025–2026 年顶级
> CDSS/医疗 Agent 研究给出结构化差距分析与优先级路线图。原则不变：**Rule-first、
> Evidence-traceable、Doctor-reviewable、Non-prescriptive**——所有建议都在这个治理
> 底座上做加法，不牺牲确定性内核与安全回退。

---

## 第 1 部分：本轮已修复的缺陷（v0.15）

审查以多智能体工作流展开（8 维度并行深读 + 逐条对抗验证），因会话额度中断，
规则引擎与 LLM 集成两个维度完成并返回 17 条带代码证据的发现。每一条都在本仓库
直接运行复现后修复，并补充回归护栏。

### 1.1 安全关键

| 级别 | 问题 | 根因 | 修复 |
|---|---|---|---|
| CRITICAL | 冠心病/心衰/肝硬化/尿毒症/肾功能异常/地塞米松/艾瑞昔布/依托考昔 等**已采集**的高危合并症与用药，触发不了 interruptive 禁忌告警 | `06_conflict_rules` 的 drugs/conditions 词表与抽取/问诊词表系统性脱节，`_terms_match` 只做子串匹配无法桥接同义词 | 补齐同义词使高危项命中 `remove_or_replace + physician_signoff` 硬门；新增**词表可达性 lint**（抽取词表/问卷 C001/C002 每个高危项必须能命中≥1 条相互作用规则或显式豁免） |
| HIGH | 患者端最严 guard 可被「毫升/丸/片 计量 + 早晚分服/顿服/温服/送服/冲服」整句绕过 | `dose_instruction` 单位集合仅 g/克/mg/钱；频次正则要求 次/服/回 收尾 | 补齐汤剂/成药单位与无数字收尾的服法动词；同步收紧 clinician_draft 的 executable_regimen，保留经验剂量区间软放行；新增 guard 绕过语料 + 不误杀科普语料回归 |

### 1.2 正确性

| 级别 | 问题 | 修复 |
|---|---|---|
| HIGH | 问诊 `tag_mapping` 死标签：冷痛→cold_pain、阴雨天→damp_weather_aggravation、热敷不舒服→possible_heat_pattern 在 FSM 路径下无任何规则消费，证型证据被静默丢弃 | 改为 deep_cold_pain(R004) / cold_aggravation+dampness(R008) / heat_aggravation(R007)；胀痛→distending_pain 接入 R002；新增**问诊 tag_mapping 消费者 lint** + 证型可达行为测试 |
| MEDIUM | `02.effect.modules` 与 `08.followup` 引用 4+ 个 `04_module_rules` 中不存在的模块名 | 对齐到真实模块；新增**模块引用存在性 lint** |
| MEDIUM | 随访规则合取条件被按析取求值（`pain_reduced` 单独即触发「痛减而麻存」，向无麻木患者建议虫类药） | `08` 改用显式 `all/any` 结构并复用 `engine.trigger_matches` |

### 1.3 LLM 集成安全与鲁棒性

| 级别 | 问题 | 修复 |
|---|---|---|
| MEDIUM(可叠加) | transformers `build_prompt` 与 http messages 未中和 ChatML 控制记号，患者文本可伪造 system/assistant 轮次（角色注入），与 guard 绕过形成叠加链 | 剥离 `<\|im_start\|/im_end/endoftext\|>`，history role 白名单限定 {user, assistant} |
| MEDIUM | `_check_egress` 只校验初始 URL，urllib 默认跟随 3xx 重定向并重发 Authorization/api-key，可把凭据+PHI 泄漏到 allowlist 外主机 | 改用禁止跨源重定向的自定义 opener |
| MEDIUM | HTTP 429/408 被当作 4xx 立即放弃重试，托管后端限流瞬间击穿重试预算 | 纳入可重试集合 + 读取 Retry-After 退避；400/401/403 仍立即失败 |
| MEDIUM | 采样档在 http 后端只发 temperature/top_p，`do_sample=False` 未映射为 `temperature=0`，structured_json 的贪心/可复现承诺失效 | greedy→temp=0 且省略 top_p，repetition_penalty→frequency_penalty |
| MEDIUM | transformers 本地推理无超时；卡死生成持类级锁无限阻塞所有 LLM 路径 | 加 `max_time` 与带超时的锁获取，超时即回退确定性规则 |
| MEDIUM | 流式 callback 抛异常（客户端断连）跳过 `thread.join`，在 generate 仍运行时释放推理锁 | `try/finally` 必 join，callback 异常转 DaoRuntimeError |
| LOW | `json_repair` 全局正则静默删除字符串值内部的 `, }` 序列篡改临床文本 | 改为字符串状态感知扫描，仅删结构性尾逗号 |

### 1.4 统计置信层（共形预测集）

| 级别 | 问题 | 修复 |
|---|---|---|
| HIGH | 成员判定只在引擎正候选上进行：真证型未被召回时，即使 q̂=1 也进不了集合，**覆盖保证方向在最需要它的场景恰好失效** | 改为在全证型标签空间上判定（非候选 nonconformity=1.0，q̂≥1 退化为全集） |
| MEDIUM | q̂ 用 `round(…,4)` 可向下取整窄化集合，违反「never anti-conservative」；缓存无失效键，规则热编辑后仍用旧 q̂ | 成员判定用全精度 q̂；缓存键并入 rules+golden 的 stat 指纹 |
| MEDIUM | q̂=0 源于校准集与规则开发集同源（交换性假设被违反），LOO=1.0 制造虚假安心 | 新增 `dev_set_contaminated` 标记 + 中文按语，明示覆盖声明尚未在留出集验证 |

**测试**：378 → 403 全绿，新增 25 项回归（含 4 类 lint 护栏 + guard 语料 + HTTP 契约 + 共形方向）。

---

## 第 2 部分：与顶级下一代 CDSS 智能体的差距分析

对标系统：Google **AMIE**（Nature 2025 诊断对话 / Nature 2026 疾病管理）、
**JingFang** 等 TCM 多智能体、CDS Hooks/SMART-on-FHIR 工程标准、Anthropic
effective-agents 模式、MedAgentBench/AgentClinic/HealthBench 评测、FDA 2026 CDS 指南。

### 差距总览（按战略优先级）

| # | 维度 | 本项目现状 | 顶级标准 | 差距 | 优先级 |
|---|---|---|---|---|---|
| G1 | 相似医案检索 / 案例推理 | 209 例仅产出**关联规则统计**（`11_mined_rule_candidates`），`mined_evidence_skill` 按全局 lift/support 过滤展示，不做**案例条件化排序**，也不核对"规则引擎的方路是否与经验队列一致" | JingFang 双阶段检索 + KG；AMIE 结合 in-context retrieval | 队列证据未按本案证型/症状做相关性排序；规则引擎推荐与名老中医真实用方之间无一致性核对 | ★★★★★ |
| G2 | 学习闭环 | 医师反馈（确认/修订/不采纳）只进审计日志与计数器 | Learning Health System：反馈→规则候选→专家审核→上线 | 反馈"只写不学"，闭环未合上 | ★★★★★ |
| G3 | 纵向患者模型 / 多次就诊 | 问诊会话一次性、纯内存、256 上限 LRU、重启即失忆；`08` 随访规则无会话级载体 | AMIE 疾病管理（Nature 2026）多次就诊、长上下文检索、指南对齐 | 无跨就诊时间线、无复诊对比与随访调方闭环 | ★★★★☆ |
| G4 | 知识图谱 / 结构化知识 | 规则 = YAML 平表，方剂/药物/证型/病机无图结构 | KG-LLM（TCM-DiffRAG、KG+CoT）是 2025 TCM 主流方向 | 无可推理的中医知识图谱，配伍/归经/君臣佐使等关系无法图上推理 | ★★★★☆ |
| G5 | 标准化互操作 (FHIR/CDS Hooks) | 独立 stdlib http server + 自有 JSON 契约 | CDS Hooks/SMART-on-FHIR 工作流触发、<500ms、厂商中立 | 无法嵌入 EHR 工作流，`capability_policy` 已是好雏形但非标准 schema | ★★★☆☆ |
| G6 | 主动问诊的信息论最优 | EIG 主动问诊已落地（`active_questioning`，BED-LLM 思路），但似然是 0.8/0.15 结构启发式 | AMIE self-play 优化 history-taking；BED-LLM 模型内似然估计 | 似然未标定、答案空间二元、无生成式追问与信息增益联合优化 | ★★★☆☆ |
| G7 | 评测严谨度 | 自有 golden 31 例 + 变异哨兵 + 对抗 guard 语料（同类项目少见） | AMIE 32 轴双盲交叉 RCT；MedAgentBench/AgentClinic 标准环境 | 无标准化 agent 评测、无专家盲审标注、共形校准集与开发集同源 | ★★★★☆ |
| G8 | Agent 编排的自主深度 | orchestrator + 独立 critics（evaluator）+ 受限 route/plan | evaluator-optimizer 迭代精炼、reflection 驱动改进、agentic memory | critics 是单轮只读校验，无迭代精炼与反思→改进回路；记忆无持久层 | ★★★☆☆ |
| G9 | 多模态 | 纯文本医案 | AMIE-vision 多模态诊断对话（舌象/影像图片） | 无舌象/影像/脉图输入（中医四诊客观化的核心缺口） | ★★☆☆☆ |
| G10 | 校准与选择性预测 | 有 conformal + 弃权（abstain）+ 语义自一致性 | selective prediction / learn-to-defer、calibration、幻觉检测 | 已达同类前沿；conformal 需真正的留出校准集才成立（见 G7） | ★★★☆☆（基础好） |

### 项目已经领先的地方（应保持）

诚实地说，本项目在**治理层**已经领先绝大多数 TCM-LLM 研究原型，这些是顶级系统
普遍缺失、而这里已具备的：

- **确定性规则事实源 + LLM 仅解释叠加 + 守卫回退**：正面回应 npj Digital Medicine
  (2025) 指出的"通用 LLM 在 TCM 辨证上缺乏可靠性与不确定性表达"。
- **红旗前置硬门控 + action level 分级 + interruptive/advisory 告警分层**：与
  CDS Hooks 最佳实践的"alert fatigue 对策"同构。
- **conformal 预测集 + 弃权 + 声明级实体接地 + 语义自一致性**：把 Nature 2024
  (semantic entropy)、conformal (CHEST 2025)、BED-LLM (Apple 2025) 的方法骨架
  真正落到了代码。
- **审计出处指纹 + 金标准回归 + 变异哨兵 + CI 全量门禁**：符合 FDA 2026 CDS 指南对
  "独立 HCP 复核 + 算法开发/验证的可解释描述"的非器械豁免要件。
- **非处方、医师签名闭环**：与 FDA 非器械 CDS 的四要件（用途/人群/输入/算法说明 +
  可独立复核）方向一致。

### 优先级路线图

**P0（本轮已完成）**：安全词表可达性、guard 绕过、死标签、共形覆盖方向、LLM 韧性 —— 见第 1 部分。

**P1（下一步，最高杠杆，已开始）**：
- **G1 案例队列证据检索 + 方路一致性核对**：受限于仓库内只持久化了脱敏**聚合**
  （无案例级语料），本轮实现的是 case-based reasoning 的可行最小闭环——把 209 例
  挖掘出的方路/剂量/关联信号按**本案证型排名 + 症状重叠 + 队列支持度**做相关性
  排序，返回带出处（脱敏行号、n/183）的经验证据包；并新增**引擎—队列一致性核对**：
  规则引擎选出的方路是否被名老中医在相似队列中的高频用方所印证（corroborated /
  supplementary / divergent），作为非阻断的医师复核信号。→ 本轮已实现 v1
  （见 `backend/skills/case_retrieval_skill.py`）。真正的"个案级相似检索"需要持久化
  脱敏案例级语料（P2，需数据治理审批）。
- **G2 学习闭环第一步**：医师"修订/不采纳"反馈聚合成**规则候选偏差报告**，
  进入 `pending_expert_review` 队列（复用 mined_evidence 的 governance 通道）。

**P2（结构性升级）**：
- **G3 纵向患者模型**：会话状态加持久层（SQLite/JSONL），引入 `patient_timeline`，
  让 `08` 随访规则在复诊时真正驱动"调方对比"。
- **G4 中医知识图谱**：从规则库物化一个轻量 KG（证型—病机—治法—方—药—配伍—归经），
  让 conflict/formula 推理走图路径（可先离线构建，stdlib 内存图）。
- **G7 评测**：采一批**未参与调规**的标注病例作独立校准/测试集，让 conformal
  覆盖声明真正成立；对齐 AgentClinic 式对话评测轴。

**P3（生态与前沿）**：
- **G5 FHIR/CDS Hooks 适配层**（把 `capability_policy` 映射为 CDS Hooks card）。
- **G6/G8** 生成式追问与 EIG 联合、evaluator-optimizer 迭代精炼、agentic memory。
- **G9** 舌象/影像多模态（四诊客观化）。

### 一句话结论

当前代码**不是**一个"套壳大模型"的 TCM demo，而是一个**治理扎实、方法有出处、
测试有护栏**的 CDSS 研究底座——它与顶级下一代智能体的差距，主要不在"安全与可信"
（这方面已领先），而在**知识的组织形态（案例库/知识图谱）、学习的闭环（反馈→规则）、
就诊的纵深（单次→纵向管理）**这三个"从可信走向持续进化"的维度。本轮先把安全与
正确性的地基夯实（P0 全部完成），并落地了 P1 的相似医案检索最小闭环，指明了通往
"顶级下一代"的可执行路径。

---

## 参考来源（本轮调研，2025–2026）

- Tu et al. *Towards conversational diagnostic artificial intelligence.* Nature 641 (2025). https://www.nature.com/articles/s41586-025-08866-7
- Palepu et al. *Towards conversational AI for disease management.* Nature (2026). https://www.nature.com/articles/s41586-026-10764-5
- Google Research. *AMIE gains vision: multimodal diagnostic dialogue.* https://research.google/blog/amie-gains-vision-a-research-ai-agent-for-multi-modal-diagnostic-dialogue/
- Anthropic. *Building Effective Agents.* https://www.anthropic.com/research/building-effective-agents
- CDS Hooks. *Best Practices.* https://cds-hooks.org/best-practices/
- Jiang et al. *MedAgentBench: A Realistic Virtual EHR Environment to Benchmark Medical LLM Agents.* arXiv:2501.14654. https://stanfordmlgroup.github.io/projects/medagentbench/
- *JingFang: A TCM LLM of Expert-Level Diagnosis and Syndrome Differentiation-Based Treatment.* arXiv:2502.04345. https://arxiv.org/html/2502.04345v2
- *TCM-DiffRAG: Personalized Syndrome Differentiation via Knowledge Graph and Chain of Thought.* arXiv:2602.22828. https://arxiv.org/pdf/2602.22828
- *MTCMB: Multi-Task Benchmark for Knowledge, Reasoning, and Safety in TCM.* arXiv:2506.01252. https://arxiv.org/html/2506.01252v1
- FDA. *Clinical Decision Support Software — Guidance for Industry and FDA Staff* (2026). https://www.fda.gov/regulatory-information/search-fda-guidance-documents/clinical-decision-support-software
- 既有依据见 `docs/research_grounding.md`（conformal / semantic entropy / BED-LLM / RAG grounding 的方法出处与诚实差异声明）。
