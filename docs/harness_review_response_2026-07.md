# Harness 架构评审整改对照（2026-07，v0.9）

针对第三轮外部评审（"顶级 Harness"视角，20 项发现 + P0/P1/P2 路线图）。处置分三类：
**采纳**、**改进后采纳**（方向对但细节需修正）、**暂缓**（记录为路线图并说明理由）。
项目约束不变：核心运行时零第三方依赖（仅 pyyaml），因此所有采纳项均以 stdlib 实现，
Pydantic/FastAPI 类建议以等价 stdlib 机制落地。

## 逐条处置

### 1. hermes_tools.json 是静态文档而非运行时注册表 — 采纳（评审判断完全正确）

实测漂移比评审发现的更严重：除 `conflict_checker_skill` 缺 `medications/conditions`
外，`consent_privacy_skill` 真实签名是 `(user_role, raw_input)`、`chief_complaint_skill`
是 `(main_symptom, duration, …)`，而 JSON 里写的都是 `(case_state, answers)` —— 纯虚构。

- **实现**（`backend/tools/`）：`ToolRegistry` + `ToolSpec`（名称/描述/schema/角色/
  风险级/幂等/超时/执行方式）。关键设计：**schema 由真实函数签名自动生成**
  （`schema_from_callable`），漂移在结构上不可能发生。
- 统一执行入口 `invoke()`：existence → role authorization → input validation →
  execute（计时）→ output validation → audit span，返回标准 ToolResult 信封
  （status/output/error_type/retryable/duration_ms/span_id/warnings）。
- 错误分类：`ToolNotFound / ToolPolicyDenied / ToolInputError /
  ToolOutputValidationError / ToolExecutionError(retryable)`。
- `run_case_pipeline` 全链路改经 `registry.call()`（校验+span 后返回原始输出，
  失败抛分类异常——确定性管线没有 planner 可重规划，raise 是正确语义）。
- `config/hermes_tools.json` 现在**由注册表生成**（含 `x_governance` 元数据），
  `tests/test_version_consistency.py` 强制文件与注册表一致、schema 与真实签名一致。
- **改进点（timeout）**：进程内确定性技能无法安全强杀线程，注册表实现**软超时**
  （计时+`timeout_exceeded` warning）而不伪装硬取消；真正的硬超时/重试在模型调用层
  （DaoClient HTTP 路径）已存在。诚实的软超时优于假的硬超时。

### 2. 缺统一 RunContext / Run 生命周期 — 采纳

- **实现**（`backend/runtime/run_context.py`）：`RunStatus` 9 态枚举 + 合法迁移表
  （非法迁移抛 `IllegalRunTransition`）、`StopReason` 8 种停止原因、`RunBudget`
  （iterations/tool_calls/model_calls/wall_time 统一扣减）、`AgentRun`（run_id、
  事件轨迹、terminal 判定）。
- `AutonomousQAAgent` 与 `AgentOrchestrator` 输出 `run` 块：guard 拦截 →
  `POLICY_DENIED`、红旗门控 → `SAFETY_HALTED`、引擎弃权 → `INSUFFICIENT_EVIDENCE`、
  预算耗尽 → `BUDGET_EXHAUSTED`（计划截断但已完成步骤仍综合输出）。
- **未做**：把 interview FSM 的状态也并入同一对象——interview 已有自己的显式 FSM
  （YaoBiState），强行合并两套状态机反而增加非法组合面；以 run 块逐步覆盖为路线图。

### 3. 无 Durable Execution / 断点恢复 — 暂缓（有依据）

评审自己说"先不必直接上复杂框架"。当前部署形态是单进程研究原型（Colab / 本地演示），
SQLite event store + replay + exactly-once 语义是真实工程量，且在没有多实例部署与长
时审核流程前收益有限。**已落地的替代**：审计日志哈希链化（见 §17）提供事件级可重建
基础；`AgentRun.events` 已是事件轨迹雏形。SQLite event store + snapshot/replay 列为
P2 路线图（docs 本节即为记录）。

### 4. "自主智能体"实为受限 planner workflow — 采纳定位，按医疗域拒绝无限自主

评审的"有限自主"设计（强制节点/可选节点/禁止节点）与本系统现状高度一致，且我们
**有意**保持这种形态：红旗门控、角色策略、输出守卫、医师终审是模型不可触碰的
确定性安全面（评审第五节"三层架构"的第一层——本系统已经是这样分层的）。
本轮把"禁止节点"从惯例升级为机制：`physician_review_skill` 在注册表中
`allowed_roles={"clinician"}`（连 system 角色都不可调用）、覆盖红旗必须走两阶段
人工审批（§16）。动态子任务生成/替代工具选择保持路线图——在证据层（结构化抽取）
夯实之前扩大自主性是给弱证据加自由度。

### 5. Blackboard 宽松字典 — 采纳（stdlib 实现）

- **实现**（`backend/agents/base.py`）：`BLACKBOARD_KEY_OWNERS` 字段所有权表
  （routed→TcmSyndromeAgent、safety→ConflictSafetyAgent、review_package→
  PhysicianReviewAgent…），非所有者写入抛 `BlackboardOwnershipError`；每次写入记录
  `{producer, seq, written_at, status:"draft"}` 元数据——黑板上的一切在医师审核前
  都是草案，这一点现在是数据结构自身携带的事实。
- **改进点**：不引入 Pydantic BaseModel（违反零依赖约束）；所有权+元数据用 15 行
  stdlib 达到评审要求的核心性质（错键即错、producer 可归责、草案状态显式）。

### 6. 缺统一 Tool Result Contract — 采纳（envelope 版）

注册表 `invoke()` 信封即统一契约（status/output/error/error_type/retryable/warnings/
duration_ms/span_id/role）。**改进点（state_patch）**：评审建议工具返回 JSON Patch
由 harness 审查后提交——对当前全部为纯函数（输入→输出、不改共享状态）的技能而言，
patch 机制解决的是一个本系统结构上已避免的问题（技能本来就不写共享状态；黑板写入
已由所有权表约束）。故 state_patch 列为出现"会修改共享状态的工具"时的准入条件，
而非现在引入的间接层。

### 7. 预算与停止原因分散 — 采纳

`RunBudget` 统一扣减 + `StopReason` 8 类明确停止原因（见 §2）。停止原因随 `run` 块
返回给 UI/审计——"为什么停、下一步做什么"可回答。

### 8. 工具级错误治理缺失 — 采纳核心、暂缓重型件

错误分类 + 分类→动作映射已实现（InputError→上游修正参数；OutputValidationError→
丢弃结果；ExecutionError→retryable 标记）。**暂缓**：circuit breaker / fallback
chain / tool health 面板——对全部为本地纯函数、无外部依赖的工具集，熔断器保护的
故障模式（下游服务抖动）不存在；模型调用层已有重试+退避+超时。外部工具接入时按需补。

### 9. Prompt/模型配置未入 provenance — 采纳

`runtime_fingerprint()`：prompt_bundle_hash（prompt_templates.py）、guard_version
（output_guard.py）、policy_bundle_hash（safety_config.yaml + hermes_agent.yaml）、
case_schema_hash、tool_registry_hash（注册表导出的规范化 JSON）、git_commit
（.git plumbing，无 subprocess）。另：`DaoGenerationConfig` 新增 `model_revision`
（`TAO_MODEL_REVISION`），transformers 加载与 provenance 均携带——生产 pin 版本的
机制已就位（研究原型默认 latest，已在 provenance 注明）。

### 10. 版本漂移 — 采纳（评审完全正确）

pyproject / provenance.APP_VERSION / hermes_agent.yaml（两处）统一 0.9.0；
`tests/test_version_consistency.py` 四处断言一致，不一致即 CI 失败。README 的
0.7.0/0.8.0 表述改为明确的历史版本记录。**改进点**：不做构建期注入（无构建系统），
以常量+CI 断言达到"单一版本事实"。

### 11. hermes_agent.yaml 与运行逻辑漂移 — 采纳

default_sequence 修正为真实 v0.8+ 顺序（红旗门控在辨证**之前**，模块后全量复扫，
补 uncertainty_skill）；`dynamic_logic.red_flag_gate` 记录 halt_categories 与
contextual_urgent 行为。测试强制：safety 在 syndrome 之前、sequence 中每个工具都在
注册表中、halt_categories 与代码常量一致。**改进点**：评审建议的"manifest 编译驱动
代码"（workflow DSL）反向依赖——对一个安全关键管线，代码为真、manifest 被测试锁定
到代码，比"YAML 驱动执行"少一个解释层的攻击/漂移面。

### 12. Guard 正则黑名单语义绕过 — 采纳增量、认同分层定位

- NFKC 归一化（全角数字"３克"、兼容形不再绕过 ASCII 锚定模式）；
- 新增口语化服法模式："早晚各服一回""照此执行/煎服""依上述比例""按常规量"
  "三指撮"；对抗集扩至 16 例全拦截、4 例良性零误杀。
- 评审"四层 guard"中的其余三层本系统已有：角色/意图策略（patient_request_guard +
  intent 白名单）、结构化字段约束（_FORBIDDEN_STRUCTURED_KEYS 强制 null）、实体级
  语义检测（groundedness + 本轮新增断言强度层）。正则是第四层兜底，不是唯一层。
- **暂缓**：医生端五级输出分级（教育/候选/策略草案/完整处方/已签医嘱）——当前系统
  结构上只存在前三级（后两级只能由医师人工录入，模型路径不可达），细分收益递延。

### 13. Prompt injection 信任边界 — 部分采纳

不可信输入在本系统只进入 user 消息区、只作为数据消费（抽取/路由输出必须落在受限
枚举/JSON 合约内，越界即回退），system prompt 恒定且已哈希入 provenance。已有注入
对抗测试（test_negation_safety 的越权/注入集）。**暂缓**：显式 trust-level 标签包裹
与间接注入全套测试矩阵（工具描述污染、Unicode 隐写、二次注入）——列为 P1 路线图；
当前挖掘数据（xlsx→YAML 统计）不含自由文本回灌路径，间接注入面有限。

### 14. 接地性仅实体级、非 claim verification — 部分采纳（增量），claim graph 暂缓

新增 **claim-modality 层**：断言强度检测（必须/确定为/即属/毫无疑问… 且无 倾向/
供审 对冲）× 证型/方剂实体 → overstatement 清单 + 医师复核注记。规则证据永远是
草案级，因此"实体有据但语气确定"现在会被点名——这正是评审举的"附片必须重用"类
过度自信问题的可检测子集。完整 claim graph（主谓宾+modality+support_level+矛盾边）
需要语义解析基础设施，列为路线图；先以 modality 层覆盖最高风险的过度自信模式。

### 15. Critic 不独立 — 采纳

`backend/agents/critics.py`：contradiction critic（寒热并见、湿浊/津伤并见两条反证
轴，源自规则库 contra 列表——主动找反证，且**不读取**候选方剂结论，不被锚定）、
policy critic（只看角色×intent）、evidence critic（只看证据引用）。各批判者互不读取
对方结论，由执行器合成。反证轴命中会出现在 critique 与综合答案的"反证批判者"注记中。

### 16. Human-in-the-loop 缺 approval object — 采纳（并修复评审未发现的真实漏洞）

实测发现比评审说的更糟：`/api/interview` 的 `review_action` **没有任何角色校验**，
匿名调用者可直接 override 清除已确认红旗。修复：

- `review_action` 全部动作需 clinician 角色（服务端 RBAC，与 token 机制联动）；
- override 升级为**两阶段 ApprovalRequest**（`backend/runtime/approvals.py`）：
  阶段一必须携带 reviewer_id + 具体理由，创建 pending 审批，**临床状态不变**；
  阶段二同一 reviewer 携审批号 + confirm_override 二次确认才执行；确认人与申请人
  不一致直接拒绝；被覆盖的红旗清单、审批号、reviewer_id 全部入 physician_review
  记录与哈希链审计。前端配合改造（工号必填 + 二次确认对话框）。

### 17. 审计非防篡改 — 采纳（哈希链），数据库暂缓

每条审计记录携带 `prev_event_hash` + `event_hash`（规范化 JSON 的 SHA-256 链），
`verify_chain()` 可检出链内任何篡改/删除（含定位首个断点 seq）。**暂缓并注明**：
链为进程内（重启开新链）；跨重启持久链、独立时间戳服务、审计访问控制属生产部署件。

### 18. 评估集"设计集污染" — 采纳诊断、如实声明而非伪拆分

评审诊断正确：21 例金标准同时充当开发集/回归集/共形校准集。**不做的事**：把 21 例
拆成 dev/cal/test——每份不足 10 例，拆分产生的是统计幻觉而非无偏估计。**做的事**：
共形输出已明确声明"仅相对项目内标注分布"（v0.8）；本文档正式记录五集拆分协议
（开发/校准/锁定测试/外部专家/前瞻 silent）为规则库扩容时的准入条件——新增病例
必须先进入锁定集，不得用于调规则。

### 19. 测试是代码级非任务级 — 部分采纳

本轮新增故障路径测试（schema 校验失败、角色拒绝、未知工具、审批人不匹配、审计链
篡改、非法状态迁移、预算耗尽截断）。**暂缓**：真实 Dao1 模型评测（需 GPU，评测
脚本与指标定义已在 benchmark 中就位）、10–30 步长程任务集、并发故障注入——列为
P1/P2 路线图。mock 后端的可预测性是单测的特性；模型行为评测是另一层，不假装单测
覆盖了它。

### 20. 无任务完成判定器 — 改进后采纳（以 stop_reason 落地）

评审的 CompletionCriteria 本质是"不能只看有没有输出"。已落地的等价机制：run 必须
以显式 `StopReason` 终结（goal_completed / insufficient_evidence / safety_halt /
policy_denied / budget_exhausted…），"引擎弃权"不再与"正常完成"混同；红旗未排除
时 run 是 SAFETY_HALTED 而非 COMPLETED。字段级 required_artifacts 清单（复核包必须
含红旗筛查+候选或弃权+相互作用检查…）列为医师工作台（P2）的验收契约。

## 五 / 六 / 七 / 八 / 九节（目标架构、状态图、目录、评估体系、路线图）

- 三层隔离（确定性安全面 / 临床工作流面 / LLM 推理面）：本系统现状即此分层，本轮
  以注册表角色表、黑板所有权、run 状态机把隔离从惯例变为机制。
- 建议目录结构已按零依赖比例落地：`backend/runtime/`（run_context, approvals）、
  `backend/tools/`（registry, builtin）；`policy/`、`observability/` 独立目录在现有
  文件规模下过早，对应职责分别位于 output_guard/safety_config 与 audit/provenance。
- 评估指标体系（tool-use / agent-loop / 医学安全 / 接地性 / 医生协作 / 稳定性）：
  医学安全与接地性指标已在 benchmark 中（红旗召回、守卫拦截/误杀、接地率、
  overstatement）；其余随真实模型评测一并列为路线图。

## 回归口径

- 全量测试 287 项通过（新增 `test_harness_runtime.py` 18 项、
  `test_version_consistency.py` 6 项；`test_server.py` 审批流按新契约重写）；
- 金标准 21/21，对抗守卫 16/16 拦截、4/4 良性放行，共形 LOO 覆盖达标。
