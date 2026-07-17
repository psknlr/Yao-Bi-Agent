# 研究依据（Research Grounding）

本文档记录 v0.6「研究方法层」四个模块的科学出处、与原方法的对应关系、以及本实现的**诚实差异声明**。原则：只借用经过顶级期刊/会议检验的方法骨架，所有简化都显式声明，不夸大等价性。

## 1. 共形证型预测集（`backend/engine/conformal.py`）

**方法出处**
- 分裂共形预测（split conformal prediction）：Vovk 等的共形预测框架；Angelopoulos & Bates, *Conformal Prediction: A Gentle Introduction*（Foundations and Trends in ML, 2023）。
- 临床应用综述：*Conformal Prediction in Clinical Artificial Intelligence*, CHEST (2025)，指出"预测集在医学诊断中即鉴别诊断（differential diagnosis）的统计形式化"。
- 小样本警示：*A Critical Perspective on Finite Sample Conformal Prediction Theory in Medical Applications* (arXiv:2512.14727)——有限样本保证的实际意义高度依赖校准集规模。

**本实现**：以金标准病例（`evaluation/golden_cases.yaml`，排除 known_gap）为校准集；非一致性分数取 score-ratio `α = 1 − score(标注证型)/score(最高候选)`；有限样本分位数 `⌈(n+1)(1−α)⌉/n`；输出"90% 目标覆盖下不可排除的证型集合"，随报告呈现；基准中以留一法（LOO）报告经验覆盖率与平均集合大小。

**诚实差异**：(a) 校准集 n≈12–15，远小于文献建议规模——分位数在此规模下保守（集合偏宽、绝不偏窄，保证方向正确），输出中明示；(b) 覆盖保证是边际的（marginal），非按证型条件化；(c) 保证相对于**项目内标注分布**，标签未经独立专家盲审；(d) 当前引擎在校准集上零误差 → q̂=0、集合退化为并列最高分候选，这是数据使然而非方法失效——引擎一旦在新校准病例上出错，重新校准会自动加宽集合。

## 2. 期望信息增益自适应问诊（`backend/skills/active_questioning.py`）

**方法出处**
- BED-LLM: *Intelligent Information Gathering with LLMs and Bayesian Experimental Design* (arXiv:2508.21184, Apple ML Research, 2025)——逐轮选择使目标变量期望信息增益（EIG）最大的问题。
- 对话式诊断 AI 的问诊质量维度：AMIE, *Towards conversational diagnostic artificial intelligence*, **Nature** 641 (2025), s41586-025-08866-7（历史采集/诊断准确性等评估轴）。

**本实现**：证型后验来自规则得分的归一化；答案似然 `P(答=有 | 证型)` 由规则结构直接导出（槽位标签 ∈ 该证型触发集 → 0.8，否则 0.15）——**结构先验而非拟合参数**，完全可审计；`EIG(槽位) = H(后验) − E_答案[H(后验|答案)]`（bits）；只对**鉴别性追问**重排，红旗/必填槽位保持硬优先（临床安全优先于信息论最优）。EIG 排序随 `/api/interview` 载荷输出（`question_selection`），构成"为什么问这个问题"的审计链。

**诚实差异**：(a) 似然是结构启发式（0.8/0.15），非 BED-LLM 的模型内估计——换来的是零训练数据、确定性、可解释；(b) 答案空间简化为二元（有/无）；(c) 候选问题池来自规则的 DISCRIMINATIVE 映射，不做开放式问题生成（生成式追问由 `tao_followup_probe_skill` 单独负责且受守卫）。

## 3. 语义自一致性（`tao_consultation_skill._semantic_consistency`）

**方法出处**
- Farquhar, Kossen, Kuhn & Gal, *Detecting hallucinations in large language models using semantic entropy*, **Nature** 630:625–630 (2024), s41586-024-07421-0——多次采样→按语义等价聚类→在聚类上计算熵，度量"意义层面"的不确定性，无需真值标签即可标记虚构（confabulation）。

**本实现（semantic-entropy-lite）**：`TAO_SELF_CONSISTENCY=N`（默认关闭）时对同一会诊上下文采样 N 次；每次生成的"临床结论签名"= 文中提交的证型+方剂实体集合；按 Jaccard ≥ 0.5 贪心聚类；报告聚类熵（bits）与最大簇一致率；一致率 < 60% 时在答案尾部追加"结论不稳定，医师复核请格外谨慎"警示（非阻断）。

**诚实差异**：(a) 原方法用 NLI 双向蕴含判定语义等价，本实现用**临床实体集合的 Jaccard 相似**作为语义等价的领域代理——对"结论层面"的分歧敏感，对措辞分歧免疫，但不能捕捉实体相同而逻辑相反的罕见情形；(b) 采样成本随 N 线性增长（30B 模型上昂贵），故默认关闭、显式 opt-in；(c) mock 后端确定性输出恒判 stable，真实验证需 http/transformers 后端。

## 4. 声明级实体接地（`backend/skills/groundedness_skill.py`）

**方法出处**
- RAG 忠实性与归因：*Measuring and Enhancing Trustworthiness of LLMs in RAG through Grounded Attributions and Learning to Refuse* (arXiv:2409.11242, ICLR 2025)；claim-level grounding（eTracer, arXiv:2601.03669）；FaithJudge 幻觉评测（arXiv:2505.04847）。核心思想：模型输出中的每个可核对声明应能归因到检索证据，无法归因者需被标记而非默认可信。

**本实现**：从模型会诊文本中抽取三类临床实体（证型/方剂/药物；词表来自规则库 02/03/04/05/06/07 + 经典方剂表，最长优先匹配、方剂名掩蔽防止内含药名重复计数），对照本案证据包（规则候选证型、方剂路线、药物模块）逐一核对；输出接地率与"模型自身知识实体"清单，以中文按语标注在会诊元数据中（`groundedness`），供医师定点复核。**设计为非阻断透明层**：会诊的价值正在于模型补充规则之外的知识，接地检查的职责是让"哪些话有规则出处、哪些话只有模型出处"对医师可见。

**诚实差异**：(a) 词表法只覆盖三类命名实体，不核对病机论述、治法陈述等自由声明（claim-level 全覆盖需 NLI/LLM 判定器，成本与可靠性权衡后未采用）；(b) 子串匹配无消歧（同名异物风险低但存在）；(c) 接地率是透明度指标，不是正确性指标——模型自身知识可能完全正确，只是需要医师而非规则来背书。

## 与 TCM-LLM 研究现状的关系

- npj Digital Medicine (2025) *Evaluating the role of large language models in traditional Chinese medicine diagnosis and treatment recommendations*（s41746-025-01845-2）与 JMIR Medical Informatics (2025) 辨证思维评测均指出：通用 LLM 在 TCM 辨证上与专家仍有差距、且缺乏不确定性表达。本项目的"规则接地 + 共形集合 + 弃权 + 接地标注"正面回应这两个缺口。
- JingFang (arXiv:2502.04345) 等 TCM 多智能体系统与本项目同构（专科智能体 + 会诊），本项目的差异化在于**治理层**：确定性规则事实源、守卫回退、审计出处、金标准回归——这些在现有 TCM-LLM 系统中普遍缺失。
- AMIE 系列（Nature 2025 诊断对话、Nature 2026 疾病管理）验证了"结构化推理 + 指南对齐"的路线；本项目以沈氏经验规则库充当"指南"角色，用 EIG 主动问诊对应其历史采集优化目标。

## 参考文献

1. Tu, T. et al. Towards conversational diagnostic artificial intelligence. *Nature* 641 (2025). https://www.nature.com/articles/s41586-025-08866-7
2. Palepu, A. et al. Towards conversational AI for disease management. *Nature* (2026). https://www.nature.com/articles/s41586-026-10764-5
3. Farquhar, S., Kossen, J., Kuhn, L. & Gal, Y. Detecting hallucinations in large language models using semantic entropy. *Nature* 630, 625–630 (2024). https://www.nature.com/articles/s41586-024-07421-0
4. Conformal Prediction in Clinical Artificial Intelligence. *CHEST* (2025). https://journal.chestnet.org/article/S0012-3692(25)05184-0/fulltext
5. A Critical Perspective on Finite Sample Conformal Prediction Theory in Medical Applications. arXiv:2512.14727.
6. BED-LLM: Intelligent Information Gathering with LLMs and Bayesian Experimental Design. arXiv:2508.21184. https://machinelearning.apple.com/research/bed-llm
7. Measuring and Enhancing Trustworthiness of LLMs in RAG through Grounded Attributions and Learning to Refuse. arXiv:2409.11242.
8. Evaluating the role of large language models in traditional Chinese medicine diagnosis and treatment recommendations. *npj Digital Medicine* (2025). https://www.nature.com/articles/s41746-025-01845-2
9. Jingfang: An LLM-Based Multi-Agent System for Precise Medical Consultation and Syndrome Differentiation in TCM. arXiv:2502.04345.
10. Evaluating and Improving Syndrome Differentiation Thinking Ability in LLMs. *JMIR Medical Informatics* (2025). https://medinform.jmir.org/2025/1/e75103
