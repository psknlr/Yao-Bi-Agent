# Colab 一键复现 · 真·Tao 在环 UI（含 ngrok 公网）

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/pariskang/Yao-Bi-Agent/blob/claude/focused-planck-3dv9we/colab/YaoBi_Skill_Colab.ipynb)

[`YaoBi_Skill_Colab.ipynb`](YaoBi_Skill_Colab.ipynb) 在 Google Colab 上一键复现全部功能，**前端 UI 通过后端 API 真正调用语言模型（Tao）自主选择并调用 skill、自主问诊**，并经 **ngrok** 暴露为公网链接。

## 它和"纯静态"版的区别

纯静态前端把路由/规划/追问都用浏览器端关键词规则模拟——"Tao 选择 / Tao 在环"只是标签。本笔记本启动 **`backend.server`**（零额外依赖的 stdlib HTTP 服务，同源提供 UI + `/api/*`），前端改为调用真实端点：

| UI 模块 | 端点 | 语言模型真实职责 |
|---|---|---|
| 智能问答 | `POST /api/chat` | `route_skill` 在受限技能集内**真实选择** skill（JSON 修复 + 越界回退） |
| 自主多步 | `POST /api/autonomous` | `plan_skills` **真实规划**多步并委派子智能体 |
| Tao 自动追问 | `POST /api/followup_probe` | 规则约束内**真实生成**澄清式追问（经 Output Guard） |
| 对话式智能问诊 | `POST /api/interview` | Tao 抽取槽位→FSM 判阶段/红旗→**模型自主追问**→会诊报告 |
| 智能体协作 | `POST /api/collaboration` | `ReasoningAgent`/`ExperienceAgent` **真实调用 Tao** |
| 经验推理/总结 | `POST /api/reasoning` `…/summary` | `physician_reasoning_skill` / `case_experience_summary_skill` 真实润色 |

UI 右上角显示**真实运行时徽章**（在线后端 + 模型名）；只有模型真正路由时才标 `Tao 选择 ✓`，否则如实标 `关键词回退`/`离线`。

## 用法

1. 点上方 **Open In Colab**。建议先在「运行时 → 更改运行时类型」选 **GPU（A100/L4）**。
2. 依次运行：
   - **①** 克隆 + 基础依赖
   - **②** 选 Tao 运行时（默认 **Dao1-30B 本地全量 FP16**，推荐 A100 80GB / H100；备选：小模型免费 T4 / 外部 HTTP / mock）
   - **③** ngrok authtoken
   - **④** ★ 启动服务并取公网链接
   - **⑤** 预热 30B 模型（首次下载较慢）
   - **⑥** 验证 `method=llm`（模型真实路由）
   - **⑦–⑩** 规则 CLI、全量测试、脱敏挖掘、清理

## Tao 运行时（通过环境变量切换，`DaoClient` 消费）

| 变量 | 说明 |
|---|---|
| `TAO_BACKEND` | `transformers`（本地）/ `http`（OpenAI 兼容）/ `mock` / `disabled` |
| `TAO_MODEL_ID` | 默认 `CMLM/Dao1-30b-a3b`；可换 `Qwen/Qwen2.5-3B-Instruct` 等小模型 |
| `TAO_TORCH_DTYPE` / `TAO_ATTN_IMPLEMENTATION` / `TAO_DEVICE_MAP` | 默认 `float16` / `eager` / `auto`——官方模型卡推荐的全量 FP16 推理路径 |
| `TAO_LOAD_IN_4BIT` / `TAO_LOAD_IN_8BIT` | 可选量化加载（默认 `false`；30B-MoE + 单卡 < 60GB 与 `device_map=auto` 同用易触发 `bitsandbytes` 的 CPU offload 报错） |
| `TAO_ENDPOINT_URL` / `TAO_API_KEY` | `http` 后端的接口与密钥 |

## 安全边界（服务端强制，不变）

语言模型只负责**选择/编排/改写技能**；最终诊断、完整处方、患者可执行剂量由 `patient_request_guard` 与 Output Guard 拦截，违规回退确定性规则。仅供研究/教学/医师复核。

## 预热失败 / `Connection refused` 排查

`<urlopen error [Errno 111] Connection refused>` 本质是**后端进程没在监听**（不是模型本身的报错）。第 ④ 步会**轮询 `/api/health` 等待后端就绪**后再映射 ngrok，并把后端全部输出写入 `yaobi_server.log`；第 ⑤ 步**轮询 `/api/health` 的 `load_state`**，可区分三种情形：

| 现象 | 含义 | 处理 |
|---|---|---|
| `load_state=loading` | 仍在下载/加载大权重（30B 约 60GB，首次较慢） | 继续等待；或先在 UI 对话触发 |
| `load_state=ready` / `model_loaded=true` | 模型就绪 | 正常使用 |
| `load_state=error` + `load_error` | **可捕获**的加载错误（依赖版本/网络下载/权重不匹配等） | 按 `load_error` 处理；如缺依赖回第 ② 步重装（注意 `transformers>=4.51` 才支持 Qwen3-MoE） |
| 第 ⑤ 步报「后端进程已退出」并打印日志 | 进程被杀（最常见：**30B FP16 显存/内存不足被 OOM kill**） | 换 A100 80GB / H100；或第 ② 步改用 `TAO_LOAD_IN_4BIT=true` 量化 / 小模型 / `mock` / `http` 备选，再重跑 ②④⑤ |

要点：模型加载已与 HTTP 请求**解耦**（后端启动即在后台线程预加载，可用 `--no-preload` / `TAO_PRELOAD=0` 关闭），因此**可捕获**的加载失败不会再让进程崩溃——后端会保持在线，`/api/health` 与 `/api/warmup` 直接回报真实原因，而不是只剩 `Connection refused`。

## 没有 GPU？

在 **②** 改用 `TAO_BACKEND=mock`（验证 UI 与管线）或小模型（`Qwen/Qwen2.5-3B-Instruct`，免费 T4 可跑），或 `http` 指向你自己的推理服务。前端会如实显示当前运行时。
