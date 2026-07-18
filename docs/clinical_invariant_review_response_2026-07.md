# 临床安全不变量评审整改对照（2026-07，v0.14）

针对第七轮外部评审（v0.13.0 增量审计：7 个 P0 + 治理/后端/测试建议）。处置分三类：
**采纳**、**改进后采纳**（评审方向对但细节需修正）、**暂缓**（记录理由）。所有 P0 主张
在动手前均经对抗用例**实证复现**——本轮全部复现成功，评审判断准确。

## P0 逐条处置

### P0-3/P0-4（先修——最严重的临床假阴性）：同名历史事件掩盖当前急症 + 组合红旗跨人/跨时污染 — 采纳

**复现确认**：`十年前车祸后腰痛，今天再次车祸后不能站立` → A3 正常流程（应 A0）；
`一周前发热已退，今天再次发热并腰痛` → A3；`十年前骨折制动时小腿肿痛，今天感冒气短`
→ 误报 A0 肺栓塞组合。

- `clinical_entity_skill.scan_term` 聚合排序新增**时态优先级**：affirmed+patient 事件中
  current > historical > resolved——当前事件永远不被首次出现的历史事件折叠掉；同时输出
  `events` 列表（逐次出现的极性/时态/经历者/原文 span），事件级记录供医师审计。
- 组合规则（PE/胸痛/免疫抑制）全部改为 `_patient_current()`（患者本人 + 当前时态）
  组合；慢性背景线索（长期激素）用 `_patient_affirmed()`。**家属事实与患者事实永不组合；
  历史事实与当前事实永不组合**。`剧痛`/`夜间痛` 升级判据同步收紧。
- 灵敏度反向哨兵：真实的当前 PE 组合（卧床制动+小腿肿+气短）仍必须 A0（有测试）。
- **改进说明**：评审建议的完整事件 ID/episode 图谱数据结构未整体引入——零依赖前提下
  以"聚合安全优先 + events 明细"达成同一安全性质；episode 级 Clinical Fact 模型维持
  路线图（见暂缓）。

### P0-1：跨入口范围控制不统一 — 采纳

**复现确认**：骨折术后案例主管线拒绝出方、chat/autonomous 照常输出补肾类方；无锚点
方剂问题默认按腰痹放行；旧腰痹 scope 粘连到新膝关节主诉。

`question_scope_gate` 重写为与完整路由器同优先级：
1. 当前问题含骨折/术后/创伤锚点 → **优先于腰锚点**直接阻断（与管线
   `FRACTURE_POSTOPERATIVE_PRIORITY` 同语义）；
2. 当前问题含域外部位锚点且无腰锚点 → 阻断（`DOMAIN_SHIFT_DETECTED`——粘连的
   `scope.in_scope` 不再能救活域外主诉）;
3. 临床意图（证型/方药/用药/推理/经验）且问题与病例都无腰痹证据 → 阻断
   （`NO_LUMBAR_ANCHOR`——"无部位默认腰痹"仅对含腰痹证据的问卷病例成立）。
腰痹证据 = 问卷标签 ∪ 主诉文本 ∪ 服务端计算的 scope（客户端 scope 已被剥离，见 P0-2）。
autonomous 入口：域冲突整单拒绝；仅无锚点时仍可规划，临床子意图逐个被意图级门控拦截
（数据集统计/安全类子任务不受牵连）。

### P0-2：能力令牌默认放行 + 客户端 scope 注入 — 采纳

- `Blackboard.capabilities`：`None`=全权 已删除 → **默认 `frozenset()`（default-deny）**；
  `capability_allowed` 只认成员关系；类型为 frozenset（`.add()` 直接 AttributeError）；
  授予只能经 `grant_capabilities(caps, issuer)`，issuer 白名单目前仅 `ScopeGateAgent`——
  智能体自授权抛 `BlackboardOwnershipError`（有测试）。
- 能力检查扩展到此前未检查的智能体：`TcmSyndromeAgent`（syndrome_reasoning）、
  `ReasoningAgent`/`ExperienceAgent`（经验叙述同属临床内容）、`PhysicianReviewAgent`
  （review_packaging）。范围外病例现在连证型/病机叙述也不会产生。
- `server._case_state` 不再透传客户端 `scope`：仅接受 `_enrich_with_question` 盖章
  （`_scope_source="server"`）的服务端决策，客户端声明被丢弃并审计
  （`client_scope_claim_dropped`）。fail-closed 标记方向性安全（只会更保守），保留。

### P0-5：聊天安全从属于意图分类 — 改进后采纳

**复现确认**：`你能做什么？另外我父亲小腿肿痛，我今天气短胸痛` → 只显示功能菜单。

改进点：评审建议 A0/A1 完全抢占返回。本实现区分两类意图——临床推理类意图维持
**整体替换**（原有红旗门控）；非临床意图（capabilities/数据统计/剂量分布）在 urgent 期间
**强制前置急诊横幅**（确定性文本在正文之前）而非吞掉原答案：紧急提示永远第一屏，
而数据集统计等"急症病人也合法需要"的信息不被误杀（与既往轮次"mining 类意图保留"的
裁决一致）。安全状态本就在意图路由前由 `absorb_question_facts` 分级——顺序不变量成立。

### P0-6：A1 仍生成完整方药对象 — 改进后采纳

**复现确认**：GC012 类病例 A1 时 `primary_route=独活寄生汤加减`、`action_card.blocked=[]`。

与金标准的冲突点如实处理：**GC012 的专家期望明确要求 A1 情境性紧急保留医师复盘
方路分析**（v0.10 轮已裁决"按类别分级硬停"而非"urgent 一律停"）。因此不照搬
"A1 禁止生成方药对象"，而是落地**机器可读的发布契约**：
- 新增 `ACTION_LEVEL_POLICY`（A0–A3 能力矩阵，safety_guard_skill 单一事实源）；
- `action_card.blocked` 不再为空：A1 明确列出「患者端方药建议 / 可执行治疗与常规康复
  内容 / LLM 临床扩写」；
- 管线结果新增 `capability_policy` 块：`patient_facing_formula=false`、
  `clinical_chain=clinician_review_only`、`clinician_review_only_keys=[formula_routes,
  primary_route, matched_modules, syndrome_candidates]`——下游消费方（含患者端过滤器）
  依据契约执行，而不是前端约定。A0/A1-halt 类别照旧硬停（对象为空）。

### P0-7：急症文本先发送给 LLM — 采纳

**复现确认**：interview 开启 LLM 时调用顺序为 `extract_slots →确定性识别→
generate_emergency_referral`，急症响应等待两次模型调用。

`run_turn` 重排：**确定性红旗扫描 + 共享安全内核先于一切模型调用**；判定 emergency 的
轮次（a）跳过本轮槽位抽取——急症原文不出本机，（b）转诊内容（警告+医师端临床指引）
全部来自本地确定性模板（`deterministic_emergency_guidance`，与 mock 同源单一实现），
不等待、不依赖网络。advisory（high）级保留可选 LLM 指引叠加（紧迫度已由规则先行确定）。
有测试断言急症轮 **LLM 调用数 = 0**。

### （评审第三节）Orchestrator stop_reason 可被覆盖 — 采纳（确凿 bug）

**复现确认**：`finish(BUDGET_EXHAUSTED)` 后再 `finish(GOAL_COMPLETED)` 把 stop_reason
改写为正常完成（transition 对同状态提前返回，赋值先于迁移）。修复：终态 run 再次
finish 抛 `IllegalRunTransition`；迁移先于赋值（非法迁移不改 reason）；orchestrator 的
尾部 finish 加终态守卫（预算耗尽路径正是评审所指的双 finish 现场）。

## 预算/审计/持久化（评审 十一/十二/十三）

- **HTTP 重试逐次计费**：`_generate_http` 每次重试补记一次 model_call（首个请求在
  dispatch 漏斗计费），重试中预算耗尽即停止——"3 次真实请求记 1 次"的口径修复（有测试
  断言网络尝试次数 ≤ 预算）。
- **审计链写后推进**：`record()` 只有文件 append 成功后才推进内存 `_seq`/`_prev_hash`——
  写失败不再留下引用幽灵事件的链头（有注入测试：失败事件后链仍连续）。
- **审批绑定事实版本**：ApprovalRequest 新增 `case_digest`（红旗集合+安全级别+轮数），
  confirm 时比对当前摘要，不一致 → `invalidated_by_new_facts`（审计+持久化），覆盖不
  执行，医师须基于新事实重新申请（有测试：申请与确认之间新增发热 → 审批失效）。
- **可选双人复核**：`YAOBI_OVERRIDE_DISTINCT_REVIEWER=1` 时 critical 审批要求确认人
  ≠ 申请人（four-eyes）；默认保持同人二次确认并在文档中如实称为"再确认"而非独立复核
  （单医生部署场景）。
- **持久化必需模式**：`YAOBI_STATE_DB_REQUIRED=1` 时事件存储初始化失败抛
  `EventStoreUnavailableError` 而非静默降级；`/api/health` 新增 `persistence` 块，
  `required_but_unavailable` 时 `ok=false`——内存兜底不再与正常持久化不可区分。

## 模型后端（评审 十五）

- **MiniMax 默认端点更新**为 OpenAI 兼容面 `https://api.minimax.io/v1/chat/completions`
  （旧 `api.minimax.chat/v1/text/chatcompletion_v2` 已弃用；大陆 `api.minimaxi.com` 经
  `TAO_ENDPOINT_URL` 配置）；HTTP 200 + `base_resp.status_code!=0` 错误面继续识别
  （两种端点通吃）。
- **Azure v1 API 支持**：`TAO_AZURE_API_VERSION=v1`（或 `preview`）→
  `{endpoint}/openai/v1/chat/completions`，无日期版本参数，deployment 经 payload
  `model` 传递；日期式 GA 版本仍为默认（生产存量），不再是唯一形态。
- **响应健壮性**：空 `choices`、`content=null`、`refusal`、`finish_reason=content_filter`
  各自成为显式 `DaoRuntimeError`（过滤/空补全不再被当成功文本进下游）；响应体
  8MB 上限（恶意/异常端点不能 OOM 服务）。
- **出站策略（最小闭环）**：非本地端点强制 HTTPS（`YAOBI_ALLOW_INSECURE_EGRESS=1`
  显式豁免实验网络）；`YAOBI_EGRESS_ALLOWED_HOSTS` 主机白名单——配置后错误端点
  fail-fast，患者文本与密钥不会发往任意主机（有测试）。

## CI（评审 十九）— 采纳

新增 `.github/workflows/ci.yml`：push/PR 全量 pytest（Python 3.11/3.12 矩阵）——金标准、
规则变异哨兵、入口一致性矩阵、守卫对抗集、import 棘轮、版本一致性、后端合约全部成为
合并门槛，而非本地习惯。

## 暂缓项（理由）

- **完整 Clinical Fact/episode 模型 + 单一 `resolve_current_episode` 网关**（三/二十）：
  本轮以"事件级聚合 + 入口同语义门控 + 服务端权威字段"覆盖了评审列举的全部具体
  失败场景（有 23 项不变量测试锁定）；事实 ID/episode 图谱是正确的长期形态，但需要
  全链路数据结构迁移，在零依赖单管线规模下一次性引入的回归风险大于收益，列 v0.15+
  分阶段实施（评审自己的分期第一阶段也是事件级修复）。
- **不可变 `ClinicalExecutionContext` dataclass**（十四/二十）：frozenset 能力 + 授权
  网关 + 服务端权威字段已达成其核心性质（default-deny/不可自增/来源可审计）；完整的
  签名上下文对象（integrity_hash/expires_at）与硬门控矩阵全量下沉列为下一轮。
- **硬超时/可取消请求/熔断器**（十一）：维持前轮分级判断——进程内纯函数无法安全强杀，
  模型路径已有真实 socket 超时+重试+逐次计费+响应上限；worker 隔离属生产化项。
- **审批 TTL/时钟语义**（十）：事实版本失效已覆盖"病例变了旧审批失效"的主风险；
  墙钟过期需要持久时钟与过期扫描器，列 P1。
- **interview 会话本体持久化**（十）：维持路线图（评审同判）；本轮先落地"持久化必需
  模式 + 健康可见性"，防止静默降级。
- **Egress Policy Engine 全量**（十五）：本轮落地 HTTPS 强制+主机白名单+密钥 fail-fast；
  去标识化引擎、区域驻留、同意管理属合规工程，超出零依赖原型边界，列生产化清单。
- **trust_remote_code 供应链**（十六）：已支持 `TAO_MODEL_REVISION` pin；仓库白名单/
  哈希校验/隔离推理进程列生产化清单（文档已注明医院部署不建议 Web 进程直载）。
- **Grounded/探索双 API 拆分**（十七）：现有 guard 分层（患者严格/医师草案级）+
  groundedness 标注 + 无证据弃权已控制主要风险面；正式 API 拆分与 claim-level 断言
  校验列 v0.15 讨论（需要语义解析基础设施，与 Claim graph 同批）。
- **覆盖率分层门槛/变异测试全套**（十八/十九）：CI 已建，阈值另行校准后加入（先避免
  "为数字而测试"）；规则变异哨兵（L2）已在既有测试中。

## 回归口径

- 全量测试 **371 通过 / 1 跳过**（新增 `tests/test_clinical_invariants.py` 23 项：评审
  建议清单的适用子集全部落地，含跨入口骨折术后阻断、无锚点方剂阻断、域切换失效、
  历史/复发事件、家属/跨时组合隔离、安全抢占、A1 发布契约、default-deny、急症零
  LLM 调用、终态保护、审计链尾部失败、重试计费、审批事实绑定、双人复核、出站
  策略、持久化就绪共 16 类不变量）；
- 金标准 **31/31**（GC012 复盘语义保持）；守卫对抗 16/16 拦截、4/4 良性放行；
- 版本 0.14.0 四处一致（CI 断言）。
