# Harness 治理深化评审整改对照（2026-07，v0.12）

针对第六轮外部评审（27 项发现 + P0/P1/P2 路线图）。处置分三类：**采纳**、
**改进后采纳**、**暂缓**（记录理由）。零第三方依赖约束不变（仅 pyyaml + stdlib
sqlite3），Pydantic 类建议以等价 stdlib 机制落地。

## P0 项（评审第十六节清单，逐条状态）

### 1. Orchestrator 忽略预算耗尽返回值 — 采纳（确凿 bug）

`AgentOrchestrator.run` 现在在每个智能体执行**前**检查 `charge()` 返回值，耗尽即
记录 BudgetManager halt 步骤、`run.finish(BUDGET_EXHAUSTED)` 并中断；编排器预算按
花名册规模配置（固定链而非开放循环）。

### 2. 所有 Agent/Session 路径统一经过 Tool Registry — 采纳（大头落地 + 棘轮）

- `ConversationSession`、`clinical_agents`、`AutonomousQAAgent` 的全部**确定性**
  技能调用改为 `registry.call(role="system", ...)`——角色检查、schema 校验、
  执行点预算扣减、审计 span 现在覆盖 chat / autonomous / collaboration 的每次
  底层工具执行。
- 新增 **import 棘轮 CI**（`tests/test_import_ratchet.py`）：`backend/agents/`、
  `backend/runtime/`、`backend/server.py` 对 `backend.skills.*` 的直接 import 只允许
  白名单内条目（运行时绑定 LLM overlay、纯 helper/gate 函数、interview 引擎——
  逐条注明归类），**新增直接 import 即 CI 失败**；白名单只减不增。
- **有意保留直连**（白名单注明理由）：tao_consultation / physician_reasoning /
  case_experience（持有 DaoClient 的运行时绑定 overlay，host 层已有 guard +
  provenance）；interview 引擎的 FSM 槽位技能（转换列为路线图）。

### 3. reviewer_id 从认证身份派生 — 采纳

- 新增 `YAOBI_CLINICIAN_TOKENS="dr-001:tokenA,dr-002:tokenB"` 多身份令牌映射
  （单 token 部署回退 `YAOBI_CLINICIAN_ID`；本地 demo 主体为
  `local-demo-clinician`）。`_resolve_auth` 返回 (role, source, subject)。
- 审批 reviewer 一律取**认证主体**；请求体 reviewer_id 不再决定身份——伪造值被
  忽略并作为 `reviewer_identity_claim_mismatch` 记入审计。两阶段确认的"同一
  reviewer"约束现在比较认证主体：**另一位真实医生（另一 token）无法确认他人的
  覆盖申请**（有测试）。
- **confirm/revise 同等要求**（评审 #7）：匿名确认被拒
  （`authenticated_reviewer_required`），revise 必须附修订理由。
- **诚实声明**：token→subject 映射证明"持有该 token 的主体"，不证明执业资质；
  OIDC/医院 SSO 仍是 P2（与评审一致）。

### 4/8. red_flags_overridden 全局永久绕过 — 采纳（评审判断完全正确）

`red_flags_overridden: bool` 已删除，替换为 **范围化 override 记录**
（`red_flag_overrides: list`，含被覆盖 flag 集合、approval_id、reviewer、
审批时 turn 计数、scope=existing_evidence_only）。红旗检测**持续运行**：
只抑制被医师审阅过的具体 flag，覆盖后新发红旗（新尿潴留/发热寒战/新外伤）
**重新报警**——有专门测试（覆盖马尾后再报发热 → emergency 再触发）。
时间窗口过期机制列为后续（需要持久时钟语义）。

### 5. Blackboard owned key 强制 producer — 采纳

受控键匿名写入现在直接抛 `BlackboardOwnershipError`（此前 producer=None 可绕过）。

### 6. 高风险审批 audit 失败即拒绝 — 采纳

审批 create/decide 的审计写入是**前置条件**：audit 启用但写失败 →
`AuditWriteError` → 覆盖不执行、审批不创建（interview 返回
`audit_unavailable_high_risk_denied`）。普通教育类请求保持 fail-open（分级正确）。

### 7. runtime-bound LLM 工具绕过 Runtime — 部分采纳

模型调用治理已通过**环境执行上下文**统一（见下），模型预算/输出量在
`DaoClient._dispatch`（所有生成任务的单一漏斗）扣减；完整的 handler 注入签名
（`handler(arguments, context)`）列为 P1 继续项——当前 direct 工具的 schema 仍由
注册表单源、host 层 guard/provenance 完整。

## 预算与执行上下文（评审 3/4/5/23）

- 新增 `backend/runtime/execution_context.py`（stdlib contextvars）：活动 AgentRun
  随调用栈传播。`ToolRegistry.invoke` 与 `DaoClient._dispatch`/`chat` 在**真实执行点**
  扣减 tool_call / model_call——"planner 记 1 次实际执行 5–8 个工具"的口径错误
  已消除（有测试断言逐工具计数与耗尽阻断）。
- `RunBudget` 新增 `model_output_chars` 维度（零依赖的 token 预算替身：字符数
  免分词器、与 token 单调、约束失控生成同一风险面）；真实 token/成本核算列为
  P2（需 pin 分词器与计价模型）。

## Registry 强类型化（评审 9/10/11）

- **additionalProperties: false** 默认：未知参数（`"ignore_safety": true`）是
  schema 违规即时拦截，不再依赖深层 TypeError。
- **enum 约束**：`user_role` 等封闭词表参数收敛为枚举（越权值
  `user_role="admin"` 直接 input_error）。
- **输出契约**：safety_guard（safety_status/action_level 枚举 + 必需字段）、
  syndrome_router（候选必须携带 name/score/confidence）、formula_selector
  （route_gate 必需）、scope_router（domain 枚举）四个最高价值工具定义输出
  schema——不合法输出被 discard（分类错误 → 审计 → 确定性 fallback/raise），
  永不进入下游（有恶意工具注入测试）。
- **暂缓（Pydantic）**：违反零依赖约束；上述 stdlib 校验已覆盖评审列举的核心
  性质（枚举/必需字段/未知字段拒绝/嵌套 items），Literal/discriminated-union
  级别的类型系统列为 Pydantic 引入时的 P2。

## 审计（评审 17/18/19）

- `event_hash` 使用**完整 SHA-256**（64 hex）。
- **链头持久化**（`.chain-head.json`：last_hash + seq + boot_id）：进程重启**续链**
  而非新 genesis——删除上一进程尾部记录会与持久化链头断裂（有跨"重启"续链测试）；
  每条记录携带 boot_id。外部锚定/WORM 保持部署件。
- 高风险动作 fail-closed（见 P0-6）。

## 持久化（评审 22）— 首步落地

`backend/runtime/event_store.py`（stdlib sqlite3）：`approvals` 与 `run_events`
两表；ApprovalManager 全量读写穿透存储——**重启后医生仍可确认此前的 pending
审批**（有 manager 重建恢复测试）。`YAOBI_STATE_DB` 配置路径（默认
logs/state.db，=0 禁用）。interview session 本体与 run checkpoint/replay 保持
路线图（评审自己的分期也是先 approvals）。

## 暂缓项（理由）

- **Manifest 编译执行图**（16）：维持前轮判断——代码为真、manifest 被 CI 锁定；
  执行图引擎在当前单管线规模下是新增解释层风险。
- **硬超时/进程隔离**（12）：维持前轮分级判断（进程内纯函数无法安全强杀；模型
  路径已有真实超时/重试）；worker-process 隔离列为生产化项。
- **五类新 Critic / Claim graph**（20/21/十一）：现有 contradiction critic 直读
  规则 contra 轴、safety critic 直读原文 span（非仅 tags）；关系级 claim 验证
  维持路线图（需语义解析基础设施）。
- **推理队列/优先级**（24）：急诊判断本就不依赖模型（确定性内核先行返回）；
  队列治理属多实例部署阶段。
- **真实模型矩阵/故障注入全套**（26/27）：本轮已含审计写失败、恶意工具输出、
  预算耗尽、审批重启恢复等注入用例；真 Dao1 矩阵需 GPU，评测脚本就位。
- **OIDC/PostgreSQL/OTel**（P2）：维持定级。

## 回归口径

- 全量测试 **331 项通过**（新增执行点预算、schema 枚举/未知字段、输出契约、
  黑板匿名写、审计续链、审批持久化恢复、audit-fail-closed、import 棘轮共 11 项）；
- 金标准 31/31；守卫对抗 16/16 拦截、4/4 良性放行；入口一致性矩阵全绿。
