# 本地 Claude / Codex ACP 接入（Issue #35）

本模块使用官方 `agent-client-protocol` Python SDK 托管 ACP 适配器进程、调用原生会话接口、接收通知和回答权限请求。Codex 使用 `@agentclientprotocol/codex-acp`；Claude 使用 `@agentclientprotocol/claude-agent-acp`，底层是官方 Claude Agent SDK。

没有修改现有 Agent 的 middleware 列表，也没有把外部 Worker 接入 PM 自动委派流程。现在提供可独立运行的控制接口及本地验证入口，后续 PM/Router 装配可以直接调用。

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
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --action initialize --json
python -m src.runtime.external_agents --provider claude --cwd D:/work/example --action initialize --json
```

成功后会输出官方 initialize 结果，包括真实能力及可用认证方法。初始化能验证进程和协议连接，实际 prompt 才能验证模型访问及登录。如果需要通过协议登录，用初始化结果里的 auth method ID：

```powershell
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --action authenticate --auth-method METHOD_ID --timeout 600
```

## 第一轮任务与恢复

```powershell
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --project-id demo --task-id inspect-1 --thread-id pm-1 --role reviewer --permissions read --prompt "阅读仓库，说明结构和测试入口。" --ref-out .local-acp/codex-ref.json
python -m src.runtime.external_agents --provider claude --cwd D:/work/example --project-id demo --task-id inspect-2 --thread-id pm-1 --role reviewer --permissions read --prompt "阅读仓库，说明结构和测试入口。" --ref-out .local-acp/claude-ref.json
```

`--ref-out` 在会话创建后、执行任务前保存，所以任务失败也能保留恢复引用。文件仅保存 provider/session/project/task/thread/role/cwd/handoff 引用，原始会话历史继续由 provider 持有。

```powershell
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --permissions read --prompt "继续刚才的检查，列出下一步建议。"
python -m src.runtime.external_agents --provider claude --cwd D:/work/example --ref-in .local-acp/claude-ref.json --permissions read --prompt "继续刚才的检查，列出下一步建议。"
```

恢复实际调用 adapter 广告的 load/resume。如果版本未广告该能力，会明确报 unsupported；不会新建会话假装恢复。新进程和原生恢复的连接方式适合本地单次命令测试。

## 文件修改、事件和取消

- `--permissions deny`（默认）：拒绝 ACP 权限请求。
- `--permissions read`：仅允许经过本地示例策略批准的 read/search 请求。
- `--permissions ask`：在交互终端逐次确认原生工具请求。批准只选 `allow_once`；不选永久授权。非交互 stdin 无法确认时拒绝。

```powershell
python -m src.runtime.external_agents --provider claude --cwd D:/work/example --permissions ask --prompt "创建 acp-smoke.txt，内容为 ACP smoke test。完成后说明改动。" --ref-out .local-acp/claude-edit-ref.json
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --permissions ask --prompt "创建 acp-smoke.txt，内容为 ACP smoke test。完成后说明改动。" --ref-out .local-acp/codex-edit-ref.json
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --permissions read --prompt "详细检查整个仓库。" --cancel-after 5 --json
```

`--json` 输出原生 `session/update` JSON-RPC 通知，保留 update 类型及元数据；末行是官方方法响应。协议事件、diff、plan、usage 以适配器实际发送为准。诊断和权限询问写到 stderr。`--prompt-file UTF8文件` 可发送长 TaskBrief。

`--cancel-after` 在同一连接里取消正在执行的 prompt，适合验证取消；Ctrl+C 也会尝试发送 cancel 并交给 SDK 清理进程。单独 `--action cancel --ref-in ...` 需要 provider 能在重连后定位活跃轮次，不能保证取消另一个适配器进程里的工作。正式集成应保留同一 ACPWorker 连接。

## 模式和模型配置

先读取真实会话返回的 mode/config option IDs，再设置：

```powershell
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action observe --json
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action mode --mode-id ADVERTISED_MODE_ID
python -m src.runtime.external_agents --provider codex --cwd D:/work/example --ref-in .local-acp/codex-ref.json --action config --config-id ADVERTISED_CONFIG_ID --value-json '"ADVERTISED_VALUE"'
```

PowerShell 的原生命令参数转义因版本而异；复杂值建议直接用下面的 Python API。能力未广告时不模拟 steer、hold、compaction。set_mode/set_config 的配置变化由 provider 持有；跨重连能否保留以 adapter 行为为准。

自定义适配器或已有安装路径可用 `--command-json` 传入明确 argv，例如 `['node', 'D:/adapters/agent/dist/index.js']` 对应的合法 JSON 字符串。不要把 shell 命令文本放进单个参数。

## Python 控制接口

```python
import asyncio
from src.runtime.external_agents import ACPWorker
from src.middlewares.execution.security_policy import SecurityGrant

async def main():
    # 这里只演示读取与发任务。正式应用使用当前有效 grant 回调、
    # 已有 ApprovalProvider 和 HumanReview；不要让模型创建 grant。
    grants = SecurityGrant(frozenset({
        "worker.connect", "worker.new", "worker.observe", "worker.ask", "worker.cancel",
    }))
    async with ACPWorker(
        "codex",
        ["node", "D:/deep_loopminder/.local-acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js"],
        "D:/work/example",
        grants=lambda ref: grants,
    ) as worker:
        ref = await worker.new(project_id="demo", task_id="task-1", thread_id="pm-1", role="developer")
        print(worker.observe(ref))
        result = await worker.control(ref, "ask", {"text": "说明当前目录结构。"})
        print(result.stop_reason)  # 执行停止原因，不是 Task DONE

asyncio.run(main())
```

可用 API：`new`、`resume`、`list_sessions`、`authenticate`、`observe`、`control`。control 提供 ask/cancel/interrupt，以及真实广告的 resume/set_mode/set_config/fork/close。ask 可接收文本或官方 ACP prompt block 对象；同一 session 的并发 ask 会拒绝，cancel 可以在另一协程调用。

`worker_candidate` 把已协商控制能力投影给既有 #37 Router；Python、测试等业务能力由可信配置提供。Worker 不固定绑定 Developer/Tester；角色随 task/ref 传入。

## 权限及边界

每次控制调用检查当前 `SecurityGrant`；回调可复用 #20 的 `effective_grant` 合并部署、项目和任务限制。支持的控制能力与 authorization 分开。`external_tool.<ACP kind>` 是权限回调的能力要求，ApprovalProvider 同时收到原生 rawInput、tool call、完整 WorkerRef，可据实际输入决策。

`request_permission` 经当前 grant → ApprovalProvider → 必要的人工回调 → 再检查 grant 和策略。无策略、未知 session、策略异常或缺少单次批准选项均拒绝。CLI 策略仅供显式本地 smoke；正式 PM 装配需注入实际策略。

这些检查只约束传入控制请求和 adapter 发出的权限请求。cwd 不是 OS sandbox；原生 Worker 可能在自身模式/设置下自动运行部分工具，不逐次询问 ACP client。必须使用原生 sandbox/approval 设置或隔离环境满足部署策略，不能把 SDK 回调宣称为全部 shell/file 操作的强制沙箱。设置宽权限模式属于显式控制授权。

session completion 不会修改 ProjectStore，也不把 Task 改为 DONE。compaction 可见性目前为 unknown；当前没有 provider native steer 或 compaction 扩展、PM 语义监督自动接入、MCP 业务服务器装配。可传官方 MCP server 配置给 new/resume，业务工具仍需显式 task/thread/message refs。

## 验证

```powershell
python -m pytest -q tests/test_external_acp.py
```

测试使用两个不同 capability profile 的独立 stdio Agent 进程，双方均使用官方 SDK，覆盖协议通信、原生通知、会话恢复、权限单次批准/拒绝、动态撤权、取消、超时、模式、配置、fork/close 和 CLI。模拟 Agent 的历史由它自己持有。

真实 Codex/Claude 登录、模型回答、文件修改和 Windows 执行，需要按上面的本地步骤验证。本环境未使用你的本地账号，也没有把模拟进程称为真实 provider 测试。

2026-10-05 验证结果：ACP 专项 12 项通过；`pytest -q --ignore=tests/test_pm_middleware_assembly.py` 共 200 项通过；本模块、新增测试的 Ruff、compileall 和 diff 检查通过。完整测试仍受原有 PM 装配导出缺失影响，相关模块本轮未修改，所以这不是完整套件全绿的声明。

参考：[官方 Python SDK](https://github.com/agentclientprotocol/python-sdk)、[Codex ACP](https://github.com/agentclientprotocol/codex-acp)、[Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp)、[acpx CLI](https://github.com/openclaw/acpx/blob/main/docs/CLI.md)。
