# ACP 子代理工具优化

日期：2026-10-13。状态：已完成、已验证。

## 基线

已存在官方 AsyncSubAgentMiddleware 派生中间件、六个工具、ACP Worker 执行图与控制图、PM 可选装配。前轮 21 项相关测试与真实 Claude ACP 创建、等待、追问、消息注入、中断、正常停机恢复通过。前轮证据：`C:/Users/User/AppData/Local/Temp/acp-middleware-live-iov9jd6t/results.json`（强制终止恢复失败，其他调用成功）和 `C:/Users/User/AppData/Local/Temp/acp-middleware-live-42zxhrf6/results.json`（正常重启恢复成功）。本轮代码改变后仅重验受影响行为。

## 设计与验收

目标：让调用者使用任务名或 ID 操作子代理，稳定领取每轮结果，准确区别超时、远端错误和已完成状态。

方案一：增加自有代理注册表、消息队列与事件存储；增加与官方 Run 的同步责任，拒绝采用。

方案二（采用）：保留官方 `async_tasks` 与 Run 排队，用官方 checkpoint history 按 `run_id` 读取回答。父 checkpoint 只增加领取标记与业务名称。共享的寻址、错误转换、状态读取由中间件内部负责，不增加通用层。

验收项：

- 已完成、已验证：任务名/ID 寻址、名称冲突检查、默认角色与模型可读参数约束。
- 已完成、已验证：指定目标等待、未读轮次结果、超时不取消、单个远端故障不吞掉其他结果。
- 已完成、已验证：排队 followup 的旧回答按 Run 读取，结构化错误与恢复提示。
- 已完成、已验证：中断幂等、取消排队与当前 Run、读取真实状态、有界请求。
- 已验证：12 项中间件行为测试与 5 项 PM 装配测试通过（17 passed），ACP 源码及行为测试 Ruff 通过。覆盖名称/ID、默认角色、无效输入、并行同名拒绝、逐 Run 回答、结果领取、单轮重读、超时、队列可见与取消顺序、重复中断、服务部分离线和身份隔离。命令：`uv run --no-project --with deepagents==0.7.21 --with langchain==1.4.3 --with 'langgraph-cli[inmem]' --with 'agent-client-protocol>=0.12.1,<0.13' --with pytest python -m pytest -q tests/test_acp_middleware.py tests/test_pm_middleware_assembly.py`。
- 已验证：真实 Claude ACP 0.89.1，默认 `openrouter/free`。两轮在首次领取前排队，分别获取 `ACP_FIRST` 与 `ACP_MATURE_20261013`，不同 `run_id` 和 checkpoint；重复等待无重复结果，指定旧 Run 精确重读；名称寻址、默认角色、队列查询、空闲 steer 提示 `followup_task`、完成后的中断无副作用通过。证据：`C:/Users/User/AppData/Local/Temp/acp-middleware-live-e7bpwmci/results.json`，测试入口 `C:/Users/User/AppData/Local/Temp/acp_middleware_mature_smoke.py`。父模型是确定性工具驱动；未测 PM 自主调度、未请求 Worker 修改文件或执行命令。验证服务器已正常停止。
- 已完成、已验证：创建响应将官方工具的合成状态标为 `status_fresh=False`，查询后才标为新鲜观测；初始化失败清理使用官方 AsyncExitStack，在关闭 SDK 资源后清除引用。新增相关用例后合计 18 passed（13 中间件/生命周期 + 5 PM 装配）。本轮真实证据已保存到 [项目证据文件](evidence/acp-tools-20261013.json)。

## 当前 checkpoint

本轮实现、相关回归与真实专项已完成。修改：`src/middlewares/acp/middleware.py`、`graph.py`、`server.py`、`README.md`、`tests/test_acp_middleware.py` 和本进度/证据目录。首次真实运行因隔离环境缺少 Windows 日志所需 `colorama` 在调用 Worker 前失败；补充隔离依赖后通过，不改变产品模型配置。前轮已验证且本轮未改变的正常重启可靠性不重复测试。

后续独立范围：真实 PM 模型自主调度、第二个 provider/部署的契约验证、生产持久化与强制终止恢复。当前并发读取仍由调用方按 `run_id` 去重；结果领取标记需持久父 checkpoint。本轮未实现业务任务验收或新调度器。没有后台验证服务遗留，也未提交/推送代码。

最终检查：Ruff 与 `git diff --check` 通过；`.gitignore` 对本主题的两份进度文件和一份脱敏证据增加精确例外，其他本地文档继续忽略。

已按仓库经验闭环约定保存 [Notion 候选经验](https://app.notion.com/p/3f8ee7753a9081918553f53f75593cb9)，保持项目内验证级别，未提升为全局规则。

## 本阶段：Claude 作为当前 Codex 的审查子代理

日期：2026-10-13。状态：已完成、已验证。

目标：由当前 Codex 直接决定 ACP 工具调用，让真实 Claude Code Worker 阅读当前项目代码并审查；测试创建、短等待、查询、在途消息、同会话追问、排队与中断恢复。保持 `openrouter/free`，不复跑已完成的纯功能套件。

范围：ACP/PM/项目与路由的当前源码、对应测试与装配配置。采用不含密钥或 `.env` 的当前文件快照，按文件 hash 核对源文件未变化。原生工具仅批准 read/search，快照中配置 plan 模式与禁止执行/修改工具。此测试是当前 Codex 经桥接调用中间件工具，不是 PM 模型自主调度，也不是原生 Codex collaboration 工具已注册外部 provider。

验收：真实原生读取事件可见；审查指出文件位置与触发路径；Codex 核对意见可信度；多种操作返回实际状态；全部进程清理，留下脱敏审查与运行证据。

当前 checkpoint：只读快照包含 79 个当前文件，测试桥接入口为 `C:/Users/User/AppData/Local/Temp/acp_project_review_bridge.py`。第一次交互因 Windows PTY 长输入截断在 Worker 创建前失败，服务已清理；已改为短指令引用参数文件，准备启动真实审查。失败证据 `C:/Users/User/AppData/Local/Temp/acp-review-live-s2btav1l/results.json`。

已启动：运行目录 `C:/Users/User/AppData/Local/Temp/acp-review-live-vx214b9b`。审查 `agent_id=01a1342e-fc12-7e33-b35e-0e4687fd0a6f`，原生会话 `c09da2d6-1e4c-4178-850e-db58f4ef1ecc`。创建 0.03 秒返回；200ms 等待真实超时且任务继续；在途 `send_message` 返回 `injected`；状态查询显示 running。原生事件已证明读取 `middleware.py` 与 `tests/test_acp_middleware.py`，不是仅回答文字。正在等待审查，已发送排队追问。

已验证：排队追问返回 pending，列表同时展示原 Run running 和新 Run queued。原生审查已读取 ACP 7 个模块、PM 装配、测试与 Router，使用 Read/Glob。另启动 `pm_memory_review`（`agent_id=01a13431-8c50-7af1-af72-835b3520805a`），独立审查 PM memory/context，验证多个原生会话并发与目标组等待。审查结果质量仍待核对。

审查已返回：主审查提出 5 项意见，PM 记忆两轮提出 4 项意见。核对发现主审查忽略 `Annotated[..., _merge]`、错误声称 CancelledError 会跳过 finally、未看到 `_updated` 的时间刷新，且误判 `url=None` 的官方 ASGI；PM 审查误判正确的 ephemeral 表达式并把幂等/证据变化保护当成缺陷。这些意见不能直接采纳。已发出针对性复核指令，尚待回复。

已验证真正中断：初次两次请求碰到已完成轮次，返回 no-op，未冒称取消成功。随后主审查处于一个 running + 一个 queued 时，中断 0.56 秒完成，取消 Run `01a13435-865a-77b1-984c-11ab51b70132` 与 `01a13434-0d24-74c0-89ee-f99aa8b65945`，实际状态 interrupted，`interruption_complete=True`。原生 session ID 保持 `c09da2d6-1e4c-4178-850e-db58f4ef1ecc`，已发送中断后的同会话复核追问。

已验证恢复：中断后新 Run `01a13437-aef1-7bf0-98db-d0f5c2dc6055` 成功，能继续讨论先前意见。两轮 interrupted 的领取响应只有终止错误，没有冒充前一轮答案。主审查经纠正仍坚持 3 条误报；PM 复核撤回 ephemeral、幂等和证据保护 3 条，剩下缺少运行引用的错误处理边界。实测再次等待为空、精确重读旧 Run 后再次等待仍为空，直接反驳交付标记被清空的说法。

已验证源文件未变化：快照的 79 个来源文件 hash 检查 `changed_files=[]`。服务已发送正常停机指令，等待清理确认。正在做一次有意义的 `capture()` 后未 `submit()` 即调用 `status()` 的独立复现；初次在构造对象前因隔离环境缺少 Windows `tzdata` 失败，补充测试依赖后重试，不改变产品依赖。

已验证边界：使用真实 `PMMemory.capture()` 与 `InMemoryStore` 保存独立证据后，不执行 submit，再调用真实 `status()`，复现 `AttributeError: 'NoneType' object has no attribute 'value'`；source 存在、run ref 不存在、远端请求次数为 0。这是未提交 source 的错误处理边界，不是原审查声称的字典被并发破坏，也未证明正常 submit 流程的数据丢失。证据 `C:/Users/User/AppData/Local/Temp/acp-review-live-vx214b9b/pm-status-repro.json`。

原生工具审计补充：共 98 条工具通知、22 个 completed tool ID。主会话实际读取 11 个不同文件，PM 会话还读取 3 个文件（路径以 rawInput 的相对路径呈现）。原生 plan 模式另尝试写计划文件和退出模式，两个请求均 failed，未获得 edit/switch_mode 授权；不能把“要求只读”误说为“模型从未尝试修改”。需要在交付报告中区别只读限制执行结果与模型行为。

清理确认：测试桥接进程已 exit 0，官方服务与 SDK 资源正常关闭。本阶段不修改被审查源码；正在保存报告和脱敏证据。

本阶段最终 checkpoint：已完成两个真实 Claude ACP 会话的审查与协作实战。报告 [claude-review-20261013.md](claude-review-20261013.md)，持久证据 [acp-review-20261013.json](evidence/acp-review-20261013.json)。功能可用，审查意见高误报且部分回复重复，不能直接采纳；唯一经独立复现的候选是 capture-only source 调用 status 的错误处理边界。源文件未修改，本轮仅增加进度、报告、脱敏证据与精确忽略例外。没有遗留服务。下一步若用户要求修复，再处理该边界或回复流去重，不在本次审查中擅自修改。

最终差异检查通过；按仓库闭环要求保存 [ACP 审查验证候选经验](https://app.notion.com/p/3f8ee7753a908108a93fe5589c01066e?pvs=204)，仍为单项目候选，不推广为全部 Claude 或免费模型的结论。
