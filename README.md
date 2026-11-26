# Deep Loopminder · PM Agent 协作工作台

> 当前分支 `pm-continue` 以 **PM Agent 及其专属本地工作台** 为唯一主入口；仓库内保留的历史多 Agent 代码仅作参考，不在默认 LangGraph 图中注册。

Deep Loopminder 是一个基于 **LangGraph + DeepAgents + ACP（Agent Client Protocol）** 的本地项目经理（PM）与多编程 Agent 协作开发工作台。

它把“项目管理与决策”和“代码实现与核验”拆开：
- **你（用户）** 负责定目标、拍板关键取舍、完成最终人工验收与批准；
- **PM Agent** 负责澄清范围、拆解任务简报、按双轨策略派工（一般直接委派 / 商讨门禁）、领取交付、按需发起独立审查并汇报证据；
- **ACP Workers**（如 Claude Code 作为开发负责人 `developer`、Codex 作为独立审查者 `reviewer`）在绑定的项目目录中执行真实的技术调查、开发与只读审查。

---

## 核心特性

### 1. 双轨协作开发链路（一般委派 + 商讨门禁）
- **一般模式（默认）**：适合需求明确的任务，PM 可直接调用 `spawn_agent`、`followup_task`、`send_message` 委派开发与实时跟进；底层依然保持项目目录隔离、单项目 Worker 互斥与进程组退出核验。
- **商讨模式**：支持 3 种入口触发——**工作台输入框一键开启“商讨模式”开关**、用户口头要求先商讨、或 PM 根据任务复杂度主动发起 `begin_discussion`。
  - 进入商讨模式后，直接委派（`spawn_agent`）会被禁用，商讨与审查阶段禁止中途插话；
  - PM 与开发负责人先以只读方式讨论目标、范围、方案与风险，达成共同结论后提交 **Cloudflare Workers AI（`@cf/cloudflare/clef-flash`）** 独立门禁判断；
  - 门禁通过后，续接同一开发负责人会话转入受监管的可写开发阶段。

### 2. 独立只读审查与人工验收闭环
- 开发结果返回原发起会话；若 PM 空闲，宿主 `ClaimWatcher` 会自动唤醒原会话领取结果。
- PM 可按需调用 `begin_review` 派发独立只读审查者（如 Codex）对照任务简报、验收标准与实际项目目录核验交付。
- **人工验收与人工批准分离**：无论是否经过独立审查，均由用户在工作台逐项勾选验收，再单独确认批准或拒绝；若项目目录在验收后发生变化，批准确认窗口会明确提醒用户。

### 3. 100% 静态 System Prompt 与中文工具体系
- **最大化 Prefix Cache 命中**：项目状态、历史记忆与后台会话列表全部移出 System Prompt，固定提示词保持 100% 静态不变；商讨模式与模型切换提示仅追加到本轮新输入消息（前端自动渲染为清爽徽章，不污染历史消息）。
- **21 个统一中文工具**：
  - 项目状态（4）：`project_read`（聚合实时项目快照、任务阶段、商讨门禁、验收/独立审查事实与未领取运行）、`project_command`、`project_create_artifact`、`project_read_artifact`；
  - 商讨与审查（5）：`begin_discussion`、`assess_discussion`、`discussion_status`、`begin_review`、`recover_dispatched_session`；
  - 人工决策（1）：`request_human_decision`（基于 LangChain 官方 `HumanInTheLoopMiddleware`）；
  - 历史记忆（5）：`memory_search`、`pm_read_memory`、`pm_read_bookmark`（按轮次与轮内条目倒序游标分页、默认折叠工具输出、强制证据非指令边界声明）、`pm_investigate_memory`、`submit_pm_memory`；
  - ACP 协作（6）：`spawn_agent`、`followup_task`、`send_message`、`wait_agent`、`list_agents`、`interrupt_agent`。

### 4. 零构建原生工作台（`/workbench`）
- 纯原生 HTML / CSS + ES Modules（`src/workbench/app.js` + `modules/*.js`），无前端打包构建依赖，适配桌面端与移动端。
- **项目与多会话管理**：选择本机文件夹创建并绑定项目，单项目支持多会话并行、一键新建、首句自动命名、对话内切换模型与思考等级。
- **实时协作视图**：对话顶部任务状态条、后台运行状态提示、Markdown 表格与代码块一键复制、宿主通知自动折叠、权限审批摘要卡片、任务卡片直接展示商讨/审查/验收状态及 **Worker 上下文用量（已用 / 总量 / 剩余 / 压缩状态）**，并内嵌运行轨迹看板（`/workbench/trajectory`）。
- **全网页可视化配置**：模型服务管理与远程拉取/搜索/启用置顶、默认模型、Clef 凭据、开发负责人与独立审查者配置（含单轮超时 `timeout`）、项目默认商讨模式、PM 运行参数、自定义 Worker/角色 JSON 及轨迹与上下文压缩阈值，全部可在设置弹窗配置。

---

## 协作架构

```text
用户（本地工作台 http://127.0.0.1:2031/workbench）
  │  可一键切换：一般模式 / 商讨模式、自由切换模型与思考等级
  ▼
PM Agent（LangGraph + DeepAgents，100% 静态 System Prompt + 21 个中文工具）
  ├── 项目状态与制品（ProjectStateMiddleware + PostgreSQL Store）
  ├── 后台记忆与书签（PMAgentMemoryMiddleware + 官方 SummarizationMiddleware）
  ├── 人工决策中断（HumanInTheLoopMiddleware）
  └── 派工与门禁
        ├── 一般模式 ──▶ spawn_agent / followup_task / send_message ──▶ 受监管开发执行
        └── 商讨模式 ──▶ begin_discussion（只读商讨）
                           ──▶ assess_discussion（Cloudflare Clef 门禁）
                           ──▶ followup_task（续接同一会话转可写开发）
                                         │
                                         ▼
                        ACP Worker Pool（Windows 原生进程组监管 + 目录隔离）
                          ├── developer（如 Claude Code ACP）
                          └── reviewer （如 Codex ACP，独立只读审查）
                                         │
                                         ▼
                        工作台人工逐项验收 ──▶ 本机二次确认人工批准 ──▶ 任务完成
```

---

## 快速开始

### 环境要求
- **Python >= 3.12**（推荐使用项目虚拟环境 `.venv` 或 Conda Python 3.12）
- **PostgreSQL**（推荐，用于持久化项目状态、记忆与会话检查点）
- **Node.js**（用于运行本地 ACP 适配器，如 `@agentclientprotocol/claude-agent-acp`、`@agentclientprotocol/codex-acp`）

### 安装依赖

```powershell
git clone https://github.com/yachenyanyi/deep_loopminder.git
cd deep_loopminder
pip install -e .
```

### 启动 PM 工作台

- **方式 1（Windows 推荐）**：直接双击根目录的 `start_pm.bat`（自动启用 `PYTHONUTF8=1`、关闭热重载、就绪后自动用默认浏览器打开工作台）。
- **方式 2（命令行）**：

```powershell
$env:PYTHONUTF8 = "1"
python scripts/run_pm_server.py --open-workbench
# 默认地址：http://127.0.0.1:2031/workbench
```

> 请使用 `scripts/run_pm_server.py` 或 `start_pm.bat` 启动 PM 服务。直接运行 `langgraph dev` 存在单并发槽位死锁和未转发自定义 checkpointer 的限制，不建议替代 PM 启动脚本。

### 首次配置流程
1. 打开工作台后点击左下角 **设置**。
2. 在 **运行环境** 中配置 PostgreSQL 连接地址、端口、轨迹与 PM 压缩阈值（或自定义 Worker JSON）。
3. 在 **模型** 中新增模型服务地址与凭据，点击 **获取模型** 并启用所需模型，设置默认模型。
4. 在 **协作与审查** 中配置 Clef 凭据、开发负责人与独立审查者（含适配器目录、模型与单轮超时）及项目商讨模式。
5. 保存后重启 PM 服务使数据库/Worker 配置生效，随后点击 **新建项目** 绑定一个独立本机文件夹即可开始使用。

---

## 配置与安全边界

- **配置分离（不入库）**：公开运行配置保存在根目录 `settings.json`，API 密钥、Clef 令牌和 PostgreSQL 连接串保存在 `.env`（界面不回显明文）。请勿将 `settings.json` 或 `.env` 提交到 Git 仓库。
- **PM 职责硬边界**：PM 只通过项目状态工具管理项目，不直接编写产品代码、不执行命令或测试、不替代人工验收与批准。
- **项目目录与进程隔离**：每个项目绑定独立本机目录（防与其他项目重叠）；同一项目受监管执行期间保持单活跃 Worker 互斥，并在 Windows 下通过 Job Object 核验进程组退出；项目外操作或破坏性操作默认需本机用户审批。
- **多会话共享规则**：同一项目的多个会话各自保存对话、模型选择、商讨模式开关与人工决策，同时共享项目任务、制品与绑定目录。

---

## 目录结构（`pm-continue` 核心模块）

```text
re_build-pm-continue/
├── start_pm.bat                              # Windows 一键启动工作台脚本
├── scripts/
│   └── run_pm_server.py                      # PM 专属服务启动入口
├── src/
│   ├── deep_agents/agents/pm_agent/
│   │   ├── agent.py                          # PM 角色配置、中间件装配与主图工厂
│   │   ├── prompts.py                        # 100% 静态中文系统提示词
│   │   ├── discussion.py                     # 商讨门禁、独立审查与异常恢复工具
│   │   ├── collaboration.py                  # Worker 执行授权、互斥隔离与退出核验
│   │   ├── clef.py                           # Cloudflare Clef 判断模型调用
│   │   └── server.py                         # Starlette 挂载工作台与后台领取循环
│   ├── middlewares/
│   │   ├── acp/                              # ACP 连接池、Worker 图、控制图与协作工具
│   │   ├── project_state/                    # 项目快照、状态机、制品、文件夹绑定与工具
│   │   ├── memory/pm_agent_memory/           # 后台记忆归档、官方压缩与 Codex 式书签翻阅
│   │   ├── agent/                            # 统一配置、模型目录与会话模型切换中间件
│   │   ├── approval/                         # 风险分析、策略审批与官方 HITL 人工决策
│   │   ├── trajectory/                       # 运行轨迹记录与独立轨迹看板
│   │   └── persistence/                      # PostgreSQL Checkpointer 与 Store 运行时
│   ├── runtime/                              # 角色能力路由与运行时规则
│   └── workbench/
│       ├── app.py / context.py / threads.py  # 工作台本地 HTTP API、会话与 SSE 流式对话
│       ├── settings_routes.py / claims.py    # 全量设置接口与空闲 PM 自动领取观察器
│       ├── decisions.py / worker_reviews.py  # 人工决策恢复与 Worker 实时权限审批
│       ├── index.html / workbench.css        # 零构建静态页面与响应式样式
│       ├── app.js                            # 前端入口与恢复观察器
│       └── modules/                          # 原生 ES Modules（state/sidebar/chat/tasks/settings）
├── tests/                                    # 单元测试与契约回归套件
└── docs/progress/PM协作开发链路.md            # 实施进度与技术细节记录
```

---

## 开发与验证

```powershell
# 1. 运行完整单元测试（当前基线：77 passed）
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m pytest -q

# 2. 检查前端原生 ES Modules 语法
node --check src/workbench/app.js
Get-ChildItem src/workbench/modules/*.js | ForEach-Object { node --check $_.FullName }
```

---

## 后续演进方向

- [ ] 真实长周期项目实战打磨与后台 Worker 实时耗时/活动步骤展示
- [ ] 多项目全局 PM 指挥舱（全局态势看板、跨项目待决策/待验收聚合收件箱、总控会话统筹多项目）

---

## License

MIT
