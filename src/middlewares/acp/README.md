# 本地 Claude / Codex ACP 接入（Issue #35）

本目录集中保存 ACP 中间件、官方后台执行图和 SDK 会话控制，可供 PM、其他 Agent 或图复用。

## 文件职责

- `control.py`：官方 SDK 的连接、会话控制、原生通知和权限回调。
- `middleware.py`：子代理格式的六个工具，继承官方 AsyncSubAgentMiddleware，复用 `async_tasks`。
- `graph.py`：官方后台 Run 与 ACP 会话之间的执行、控制图。
- `server.py`：通过官方应用 lifespan 托管 SDK 连接；按可信配置提供角色候选。
- `capabilities.py`：协商能力与通用 Worker 候选映射。
- `correlation.py`：项目、任务、线程与原生会话关联引用。
- `__main__.py`：独立调用与排查入口，使用 `python -m src.middlewares.acp`。
- `__init__.py`：公共接口与 `ACPWorker` 按需导入。

项目任务和路由规则继续使用 `src/middlewares/project_state/`、`src/runtime/routing/`；通用授权和审批继续使用公共中间件模块。

本模块使用官方 `agent-client-protocol` Python SDK 托管 ACP 适配器进程、调用原生会话接口、接收通知和回答权限请求。Codex 使用 `@agentclientprotocol/codex-acp`；Claude 使用 `@agentclientprotocol/claude-agent-acp`，底层是官方 Claude Agent SDK。

PM 通过 `ACP_SUBAGENTS_JSON` 显式启用中间件。未配置时，保留原有 PM 装配；其他旧 Agent 没有自动增加工具。

## 子代理工具

- `spawn_agent(task_name, message, agent_type=None)`：立即返回 `agent_id`（官方线程 ID），后台执行。只有一个角色时默认使用该角色；多个角色时指定角色，或由部署方设置 `default_agent_type`。任务名使用小写 snake_case，当前父线程内不能重复使用；同一模型轮次并行同名创建会在发请求前拒绝。
- `followup_task(target, message)`：同一会话开启下一轮；通过官方 Run 的 `enqueue` 排队。
- `send_message(target, message)`：普通 ACP 会话使用已协商的原生 steer；受 PM 商讨门禁治理的会话目前拒绝在途自由消息，需等待本轮结束后继续商讨或续问。
- `wait_agent(timeout_ms=30000, targets=None, include_completed=False, run_id=None)`：默认等待所有子代理的新结果；`targets` 可选 1～8 个名称或 ID，`timeout_ms=0` 立即查询。默认不再返回已领取的 Run；历史重读须指定单个 target 和 `run_id`，`include_completed=True` 未指定 Run 时兼容归一化为未读模式，并返回 notice，不无差别重读旧结果。每次至多返回 8 轮；`has_more_results` 表示尚有未返回结果。超时只取消等待，不取消 Worker。

角色描述应包含执行端实际能力（包括是否支持原生子代理）、职责和交接方式；运行时按已有官方状态注入负责人别名、角色、最新 Run、最后观察状态和结果是否领取，不将其描述为实时探测。默认中文沟通，续问只补变化。Worker 原生工具集独立于 PM，通过会话交付；项目级 Worker 对自己的内部子代理负责。Claude Code 原生子代理能力由部署角色说明声明，ACP 不新增控制内部子代理的第二层调度。
- `list_agents(path_prefix=None, status=None)`：按任务名前缀和最新观测状态筛选。返回当前运行、排队的 Run ID、未读结果数和观测时间；刷新失败显示 `status_fresh=False`，并附错误。
- `interrupt_agent(target)`：先取消队列，再取消在途 Run；有界等待后读取实际状态。已完成时重复调用是无副作用操作；`interruption_complete=False` 表示尚未确认停止，需查询状态。原生会话保留。

`target` 接受返回的 `agent_id` 或当前父线程内的任务名。所有响应使用 JSON；正常返回 `ok=True`，无效名称、角色、空白消息、网络失败等可恢复错误返回 `ok=False` 和 `error.code/message/retryable/next_action`。列表或等待的局部故障放在 `errors`，健康子代理的结果仍返回。状态为 `success` 只代表执行结束，不代表项目验收通过。

`followup_task` 总是保留原会话的项目、任务、角色与所有者；父调用后续改变 `project_id/task_id` 不会改变已有会话。等待按官方 checkpoint history 的 `run_id` 领取回答，因此连续排队的旧回答不会被最新回答覆盖；结果包含 `run_id`、`checkpoint` 和原生 `stop_reason`。中断或失败轮次返回终止状态与错误，不把前一轮回答当成它的结果。

领取标记保存在父 Agent 的官方 checkpoint；父线程需要持久化，才可跨调用/重启保留“已读”。并发或重试场景调用方应按 `run_id` 去重，这不是跨进程的 exactly-once 消息投递服务。每个父线程通过官方 Run 串行执行；无需复制 Worker 的完整会话历史。

可信构造参数 `request_timeout=15` 限制单次服务请求及中断收敛等待；`timeout_ms` 限制等待 Worker 完成的阶段，状态读取另受请求期限约束。写操作超时返回 `outcome_unknown=True` 且不建议直接重试，先查看服务状态与日志。中间件不自动重复启动 Worker。

典型调用顺序：

```python
spawn_agent(task_name="inspect", message="检查项目结构", agent_type="researcher")
followup_task(target="inspect", message="继续检查测试入口")
wait_agent(targets=["inspect"], timeout_ms=30000)
list_agents(path_prefix="ins")
```

以上是模型看到的工具调用格式；Python 应通过装配中间件后的 Agent 调用它们，隐藏的 `ToolRuntime` 由官方工具运行器注入。

每次 Agent 调用必须提供 `config.configurable.thread_id`。可同时提供 `project_id`、`task_id`、`required_capabilities`，关联项目与任务。目标只从当前调用者的官方 `async_tasks` 解析；模型不能指定另一个父线程、启动命令、环境变量或权限。

## 后台部署配置

安装项目的 ACP 依赖和官方开发服务器：`pip install -e '.[acp]' 'langgraph-cli[inmem]'`。
`langgraph.json` 注册 `acp_worker`、`acp_control` 并装配官方 Starlette lifespan。模块路径保证图工厂与 lifespan 复用同一份 SDK 连接。

`ACP_WORKERS_JSON` 是部署方配置的 JSON 数组，例如：

```json
[
  {
    "provider_id": "claude",
    "roles": ["developer", "researcher", "reviewer"],
    "capabilities": ["repo.read"],
    "command": ["node", "D:/deep_loopminder/.local-acp/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js"],
    "cwd": "D:/deep_loopminder",
    "env": {"CLAUDE_CODE_EXECUTABLE": "C:/Users/User/.local/bin/claude.exe"},
    "timeout": 180,
    "grants": ["worker.connect", "worker.new", "worker.ask", "worker.cancel", "worker.resume", "worker.observe", "worker.steer"]
  }
]
```

路径必须换成实际安装位置。`roles` 与 `capabilities` 是选择要求，授权仍由 `grants` 和原有 ApprovalProvider 决定。可选 `approval` 直接使用已有 `ApprovalProviderConfig` 的 `use/config` 格式；未配置策略时拒绝 ACP 权限请求，需人工批准但未提供人工回调的请求也拒绝。原生工具不一定逐次请求 ACP 权限，工作目录不构成 OS 沙箱。

PM 的 `ACP_SUBAGENTS_JSON` 只描述角色与官方图端点：

```json
[
  {"name": "developer", "description": "实现有明确验收条件的开发任务", "graph_id": "acp_worker"},
  {"name": "researcher", "description": "只读调查项目与官方资料", "graph_id": "acp_worker"}
]
```

同部署省略 `url`，使用官方 ASGI transport。其他进程或远程部署增加 `url`；需要认证时使用官方 spec 的 `headers`。

```python
from langchain.agents import create_agent
from src.middlewares.acp import ACPMiddleware

agent = create_agent(
    model,
    middleware=[
        ACPMiddleware(
            async_subagents=[
                {
                    "name": "researcher",
                    "description": "只读调查项目",
                    "graph_id": "acp_worker",
                    "url": "http://localhost:2024",
                }
            ]
        )
    ],
    checkpointer=checkpointer,
)
await agent.ainvoke(
    {"messages": [("human", "创建一个 researcher 子代理检查项目")]},
    {"configurable": {"thread_id": "parent-thread", "project_id": "demo"}},
)
```

Windows 启动官方开发服务器前设置 `$env:PYTHONUTF8 = '1'`，再运行 `langgraph dev --n-jobs-per-worker 4`。线程、Run、checkpoint 的可靠性取决于官方持久化部署；强制终止开发服务器可能丢失尚未落盘的 checkpoint。缺少原生会话引用时，followup 明确失败，不新建会话冒充恢复。

依据：[官方中间件工具与状态](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[AsyncSubAgent](https://docs.langchain.com/oss/python/deepagents/async-subagents)、[官方资源 lifespan](https://docs.langchain.com/langsmith/custom-lifespan)。

## 为什么首版直接使用 Python SDK

已经核对 SDK 0.12.1、两个适配器上游接口及 acpx 文档。SDK 的进程管理、初始化、会话 new/load/resume/list/fork/close、prompt/cancel、模式和配置接口已足够；不需要自行编写 transport、协议解析器或 provider 生命周期。

acpx 是可用的 headless 控制工具，本次评估的是其 CLI：非交互权限升级会拒绝当前请求，CLI 策略是预先配置。现有 Python ApprovalProvider 的实时决策与人工回调可直接接到 SDK `request_permission`，无需再增加 Node bridge。因此首版选官方 Python SDK；acpx 可继续作为人工排查工具，不并行维护第二套会话状态。

## 本地准备

在仓库根目录执行，使用 Python 3.12+ 和 Node 22+。已有项目环境只需补装 ACP Python 依赖；新环境可以安装 `.[acp,dev]`。

```powershell
git switch AI
git pull --ff-only
python -m pip install "agent-client-protocol>=0.12.1,<0.13"
npm install --prefix .local-acp @agentclientprotocol/codex-acp @agentclientprotocol/claude-agent-acp
```

CLI 默认直接执行 `node <本地包>/dist/index.js`，Windows 下也不经过 `.cmd` shim 或 shell。`.local-acp/` 已忽略，不提交依赖、运行引用或本地文件。

先确认你本地 Claude/Codex 的登录及环境配置正常。适配器使用本地原生登录或所支持的 API 环境变量；DeepLoopMinder 不提取或保存 token。Codex 适配器包含兼容的 Codex 依赖；若要指定已有二进制，可在启动前配置上游支持的 `CODEX_PATH`。Claude 适配器启动 SDK 工作会话，不接管既有终端窗口。

## 先检查连接，再发送任务

以下 `D:/work/example` 换成你准备测试的目录。建议用测试仓库完成首次文件修改验证。

```powershell
python -m src.middlewares.acp --provider codex --cwd D:/work/example --action initialize --json
python -m src.middlewares.acp --provider claude --cwd D:/work/example --action initialize --json
```

成功后会输出官方 initialize 结果，包括真实能力及可用认证方法。初始化能验证进程和协议连接，实际 prompt 才能验证模型访问及登录。如果需要通过协议登录，用初始化结果里的 auth method ID：

```powershell
python -m src.middlewares.acp --provider codex --cwd D:/work/example --action authenticate --auth-method METHOD_ID --timeout 600
```

## 第一轮任务与恢复

```powershell
python -m src.middlewares.acp --provider codex --cwd D:/work/example --project-id demo --task-id inspect-1 --thread-id pm-1 --role reviewer --permissions read --prompt "阅读仓库，说明结构和测试入口。" --ref-out .local-acp/codex-ref.json
python -m src.middlewares.acp --provider claude --cwd D:/work/example --project-id demo --task-id inspect-2 --thread-id pm-1 --role reviewer --permissions read --prompt "阅读仓库，说明结构和测试入口。" --ref-out .local-acp/claude-ref.json
```

`--ref-out` 在会话创建后、执行任务前保存，所以任务失败也能保留恢复引用。文件仅保存 provider/session/project/task/thread/role/cwd/handoff 引用，原始会话历史继续由 provider 持有。

```powershell
python -m src.middlewares.acp --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --permissions read --prompt "继续刚才的检查，列出下一步建议。"
python -m src.middlewares.acp --provider claude --cwd D:/work/example --ref-in .local-acp/claude-ref.json --permissions read --prompt "继续刚才的检查，列出下一步建议。"
```

恢复实际调用 adapter 广告的 load/resume。如果版本未广告该能力，会明确报 unsupported；不会新建会话假装恢复。新进程和原生恢复的连接方式适合本地单次命令测试。

## 文件修改、事件和取消

- `--permissions deny`（默认）：拒绝 ACP 权限请求。
- `--permissions read`：仅允许经过本地示例策略批准的 read/search 请求。
- `--permissions ask`：在交互终端逐次确认原生工具请求。批准只选 `allow_once`；不选永久授权。非交互 stdin 无法确认时拒绝。

```powershell
python -m src.middlewares.acp --provider claude --cwd D:/work/example --permissions ask --prompt "创建 acp-smoke.txt，内容为 ACP smoke test。完成后说明改动。" --ref-out .local-acp/claude-edit-ref.json
python -m src.middlewares.acp --provider codex --cwd D:/work/example --permissions ask --prompt "创建 acp-smoke.txt，内容为 ACP smoke test。完成后说明改动。" --ref-out .local-acp/codex-edit-ref.json
python -m src.middlewares.acp --provider codex --cwd D:/work/example --permissions read --prompt "详细检查整个仓库。" --cancel-after 5 --json
```

`--json` 输出原生 `session/update` JSON-RPC 通知，保留 update 类型及元数据；末行是官方方法响应。协议事件、diff、plan、usage 以适配器实际发送为准。诊断和权限询问写到 stderr。`--prompt-file UTF8文件` 可发送长 TaskBrief。

`--cancel-after` 在同一连接里取消正在执行的 prompt，适合验证取消；Ctrl+C 也会尝试发送 cancel 并交给 SDK 清理进程。单独 `--action cancel --ref-in ...` 需要 provider 能在重连后定位活跃轮次，不能保证取消另一个适配器进程里的工作。正式集成应保留同一 ACPWorker 连接。

## 模式和模型配置

### Codex 默认使用 GPT-6 Luna

当前本机按用户指定采用固定分工：`developer` 只由 Claude Code 承接；`reviewer`（按验收标准核验）、`tester`（测试和复现）、`researcher`（研究、调查、资料搜集）只由 Codex 承接。Codex 使用 `gpt-6-luna`，Claude 保持 `minimax/minimax-m3.1-flash:free`。PM 从 `ACP_SUBAGENTS_JSON` 的角色说明选择业务角色，由部署方的 Worker `roles` 完成唯一匹配，不让 PM 指定提供方。

测试和验收发现产品缺陷时交回 developer 修复；Worker 报告不会自动写成项目 passed、approved 或 done。角色分工用于选择执行者，不是按角色建立操作系统权限隔离。

现有 `ACPWorker` 已支持 Codex，不需要另建中间件或修改 PM 主模型。安装 `@agentclientprotocol/codex-acp@2.2.2` 后，在 `ACP_WORKERS_JSON` 中为该 Worker 设置：

```json
{
  "provider_id": "codex",
  "env": {
    "CODEX_CONFIG": "{\"model\":\"gpt-6-luna\"}",
    "INITIAL_AGENT_MODE": "workspace-write"
  }
}
```

以上仅展示 Worker 的模型配置片段；完整角色、启动命令、目录、grants 和审批示例见项目根目录 `.env.example`。`CODEX_CONFIG` 是适配器的会话配置覆盖，复用本机已有认证，不修改全局 `~/.codex/config.toml`。不设置 `CODEX_PATH` 时使用适配器捆绑的兼容 Codex。

`workspace-write` 保留原生工作区约束，ACP 权限请求继续通过现有 grants 和 ApprovalProvider；需要人工审批但未配置回调的请求仍拒绝，不启用全自动审批。新增提供方不能绕过这些限制。

PM 仍按角色和能力派工。若 Claude 与 Codex 都能满足相同角色，现有路由按可选能力得分、再按 provider_id 排序选取；并不自动负载均衡，也不保证每个任务都会调用 Codex。新增 Worker 时应合并原配置，不覆盖已有 Claude。修改环境配置后须重启 Agent Server，已有原生会话不保证切换模型。

2026-10-20 的一次真实连接检查已确认新会话报告 `gpt-6-luna`；完整模型回复结果与验证范围见 `docs/progress/codex-acp.md`。

先读取真实会话返回的 mode/config option IDs，再设置：

```powershell
python -m src.middlewares.acp --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action observe --json
python -m src.middlewares.acp --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action mode --mode-id ADVERTISED_MODE_ID
python -m src.middlewares.acp --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action config --config-id ADVERTISED_CONFIG_ID --value-json '"ADVERTISED_VALUE"'
```

PowerShell 的原生命令参数转义因版本而异；复杂值建议直接用下面的 Python API。能力未广告时不模拟 steer、hold、compaction。set_mode/set_config 的配置变化由 provider 持有；跨重连能否保留以 adapter 行为为准。

自定义适配器或已有安装路径可用 `--command-json` 传入明确 argv，例如 `['node', 'D:/adapters/agent/dist/index.js']` 对应的合法 JSON 字符串。不要把 shell 命令文本放进单个参数。

## Python 控制接口

```python
import asyncio
from src.middlewares.acp import ACPWorker
from src.middlewares.execution.security_policy import SecurityGrant


async def main():
    # 这里只演示读取与发任务。正式应用使用当前有效 grant 回调、
    # 已有 ApprovalProvider 和 HumanReview；不要让模型创建 grant。
    grants = SecurityGrant(
        frozenset(
            {
                "worker.connect",
                "worker.new",
                "worker.observe",
                "worker.ask",
                "worker.cancel",
            }
        )
    )
    async with ACPWorker(
        "codex",
        [
            "node",
            "D:/deep_loopminder/.local-acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js",
        ],
        "D:/work/example",
        grants=lambda ref: grants,
    ) as worker:
        ref = await worker.new(
            project_id="demo", task_id="task-1", thread_id="pm-1", role="developer"
        )
        print(worker.observe(ref))
        result = await worker.control(ref, "ask", {"text": "说明当前目录结构。"})
        print(result.stop_reason)  # 执行停止原因，不是 Task DONE


asyncio.run(main())
```

可用 API：`new`、`resume`、`list_sessions`、`authenticate`、`observe`、`control`。control 提供 ask/cancel/interrupt，以及真实广告的 resume/set_mode/set_config/fork/close/steer。ask 可接收文本或官方 ACP prompt block 对象；同一 session 的并发 ask 会拒绝，cancel 和 steer 可以在另一协程调用。steer 仅在初始化明确广告 `_meta.steering.supported` 时开放。

`worker_candidate` 把已协商控制能力投影给既有 #37 Router；Python、测试等业务能力由可信配置提供。Worker 不固定绑定 Developer/Tester；角色随 task/ref 传入。

## 权限及边界

每次控制调用检查当前 `SecurityGrant`；回调可复用 #20 的 `effective_grant` 合并部署、项目和任务限制。支持的控制能力与 authorization 分开。`external_tool.<ACP kind>` 是权限回调的能力要求，ApprovalProvider 同时收到原生 rawInput、tool call、完整 WorkerRef，可据实际输入决策。

`request_permission` 经当前 grant → ApprovalProvider → 必要的人工回调 → 再检查 grant 和策略。无策略、未知 session、策略异常或缺少单次批准选项均拒绝。CLI 策略仅供显式本地 smoke；正式 PM 装配需注入实际策略。

这些检查只约束传入控制请求和 adapter 发出的权限请求。cwd 不是 OS sandbox；原生 Worker 可能在自身模式/设置下自动运行部分工具，不逐次询问 ACP client。必须使用原生 sandbox/approval 设置或隔离环境满足部署策略，不能把 SDK 回调宣称为全部 shell/file 操作的强制沙箱。设置宽权限模式属于显式控制授权。

session completion 不会修改 ProjectStore，也不把 Task 改为 DONE。compaction 可见性目前为 unknown；通用 ACP 本身不接管业务验收、语义监督或 MCP 业务服务器装配。可传官方 MCP server 配置给 new/resume，业务工具仍需显式 task/thread/message refs。

## 验证

```powershell
python -m pytest -q tests/test_external_acp.py tests/test_acp_middleware.py tests/test_pm_middleware_assembly.py
```

测试使用两个不同 capability profile 的独立 stdio Agent 进程，双方均使用官方 SDK，覆盖协议通信、原生通知、会话恢复、权限单次批准/拒绝、动态撤权、取消、超时、模式、配置、fork/close 和 CLI。模拟 Agent 的历史由它自己持有。

2026-10-13 本轮验证：13 项中间件行为/资源生命周期测试、5 项 PM 装配测试通过（18 passed），Ruff 通过。行为测试通过真实 `create_agent` 图调用工具，覆盖寻址、角色选择、无效输入、并行名称冲突、逐轮回答及领取、精确重读、排队状态、中断顺序和幂等、局部故障、超时及恢复引用缺失；客户端请求在这些自动测试中使用 mock。前轮 12 项 ACP SDK 专项已通过，本轮未改变底层 SDK 控制实现，未重复运行。

另外使用 Windows 本机 Claude ACP 适配器 0.89.1、现有默认 `openrouter/free` 配置和官方 Agent Server 完成真实调用：创建立即返回（本次约 0.03 秒）、等待回答、同会话追问并回忆标记、状态查询、在途消息注入、中断及中断后继续。正常停止并重启服务器后，原生 session ID 保持一致，仍能回忆原标记。调用环境使用 deepagents 0.7.21、langchain 1.4.3、langgraph-api 0.16.0。

真实调用的父 Agent 使用确定性的工具驱动，尚未验证 PM 模型自主选择工具或业务验收；未请求 Worker 执行文件修改和命令。强制终止开发服务器的恢复测试发现未落盘 checkpoint 丢失，因此不能把正常停机恢复的结果视为强制终止可靠性保证。

工具优化后的真实专项又验证了首次领取前连续排队两轮、名称寻址、默认角色、分别按 Run 读取不同 checkpoint、已读不重复领取、旧轮次精确重读、空闲消息提示和完成后中断。两轮回答分别为 `ACP_FIRST` 与 `ACP_MATURE_20261013`。[持久验证证据](../../../docs/progress/evidence/acp-tools-20261013.json)与[进度记录](../../../docs/progress/acp-tools.md)保存在项目内。Windows 隔离验证环境需为官方开发服务器补充 `colorama` 或关闭彩色日志；这不改变 ACP 的模型或权限配置。

参考：[官方 Python SDK](https://github.com/agentclientprotocol/python-sdk)、[Codex ACP](https://github.com/agentclientprotocol/codex-acp)、[Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp)、[acpx CLI](https://github.com/openclaw/acpx/blob/main/docs/CLI.md)。

PM 的业务关联由 `project_state/ProjectStateMiddleware(workflow=...)` 包装现有六工具完成：任务绑定、TaskBrief 注入、Run 和交付制品写回均归项目状态模块，通用 ACP 工具格式保持不变。直接使用 ACPWorker 或独立 ACPMiddleware 不会自动写入项目状态。可信验收和人工批准接口见项目状态中间件 README。

2026-10-20：本地同部署入口通过 candidate_source 从服务注册表提供实时角色/精确能力目录，在模型调用前注入，并在创建 Run 前复用 route_worker 预检。远程端点未提供目录时维持兼容路径。执行失败且没有 worker_ref 时返回 session_not_created/fatal/stop_and_report，禁止向不存在的会话排队续问；已有原生会话时保留续问恢复建议。23 项中间件与 PM 装配测试通过。

Worker别名 task_name 现允许大写字母和连字符（字母开头、最长64字符、区分大小写），不再迫使业务任务另建小写副本。可选 task_id 绑定原业务任务，默认同 task_name；同一任务的开发、审查、测试可用不同 task_name 分别创建持久会话。项目角色/状态约束仍由 project_state 检查，通用 ACP 不把业务任务状态当成 Run 状态，也不自动批准验收。
