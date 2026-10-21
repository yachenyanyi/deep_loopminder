# Claude ACP 子代理实战审查

日期：2026-10-13。实际调用：Claude Code ACP 0.89.1，沿用 `openrouter/free` 路由；没有固定或切换到底层其他指定模型。

可以作为当前 Codex 的外部子代理使用。此次由 Codex 决定任务和追问，经临时桥接入口调用项目中间件；父 `create_agent` 使用确定性工具转发，不是 PM 模型自主调度，也没有把外部 Worker 注册为 Codex 内置 collaboration provider。

## 实际审查

启动两个独立原生会话：`project_review` 审查 ACP、PM 装配及 Router；`pm_memory_review` 审查 PM 记忆和上下文。提供当前 79 个文件的源码快照，不含 `.env` 与认证文件。原生通知证明实际读取 14 个不同文件，22 个工具 ID 完成，主要为 Read 和 Glob。

两个会话都返回带文件和行号的审查意见，能够同会话讨论先前结果。当前模型路由的质量存在明显限制：首轮主审查的 5 条意见全部未采纳，PM 侧的 4 条原意见中 3 条明确误报，1 条经纠正后收窄为错误处理边界。不能把“读取过代码”或 Worker 自称“source-proven”当作结论正确的证明。

## 操作结果

- 创建：主会话约 0.03 秒、第二会话约 0.02 秒返回，后台继续执行。
- 短等待：200ms 与后续长等待超时没有取消 Worker；目标组可等待两个会话。
- 运行中发送消息：返回原生 `injected`；空闲时返回 `promptRequired/noRunningTurn`，提示 `followup_task`，没有隐式开启任务。
- 同会话追问：可排队，列表同时展示 running 与 queued Run，旧回答仍按原 Run 领取。
- 真正中断：一个 running 和一个 queued 同时存在时，约 0.56 秒完成取消，真实状态均为 interrupted，`interruption_complete=True`。已完成轮次的中断明确为 no-op。
- 中断后继续：保持原生 session ID `c09da2d6-1e4c-4178-850e-db58f4ef1ecc`，后续 Run 成功，能继续讨论先前意见。
- 结果重读：默认等待不重复交付；指定旧 Run 重读后，再次普通等待仍为空，已有领取标记未丢失。
- 权限：原生 plan 模式尝试写计划文件及切换模式，两次都 failed；计划文件不存在。要求只读没有阻止模型“尝试”，实际权限策略拒绝了请求。
- 清理：79 个源文件 hash 均未变化；桥接进程 exit 0，官方服务器与 SDK 资源正常关闭。

## 审查意见核对

主会话声称 `acp_delivered` 被覆盖：不成立。`middleware.py:41` 声明了 `_merge` reducer，Command 只传增量；真实空等待与旧 Run 重读后再次空等待也反驳了该结论。

主会话声称显式重读产生重复是缺陷：不成立。`run_id/include_completed` 正是用户要求重读的接口，不是普通等待重复交付。

主会话声称追问时间全部陈旧：不成立。`_updated` 刷新检查与更新时间；`created_at` 表示同一个子代理的首次创建，未承诺表示每个 Run 的创建时间。

主会话声称取消会跳过 finally：不成立。Python 的 finally 在 CancelledError 下仍执行；它在纠正后撤回了这条。`url=None` 不支持 HTTP 控制的说法也未采纳：该调用发生在服务内，官方 ASGI transport 已在真实 HTTP 控制中成功运行。

PM 会话误判 `durability != "durable"`、source_id 幂等返回、变化证据检查：均撤回；这些是正确判断或已有保护逻辑。它还把实际位于 memory/middleware.py 的逻辑错标到 context.py。

## 留下的可复现边界

位置：`src/deep_agents/agents/pm_agent/memory/jobs.py:244`。

真实调用 `PMMemory.capture()` 保存 source，但没有调用 `submit()`，随后调用 `status(project_id, source_id)`：source 存在，Run 引用不存在，访问 `ref.value` 抛出 `AttributeError: 'NoneType' object has no attribute 'value'`，且尚未调用远端 API。

这是未提交 source 的状态查询错误处理边界，建议明确返回“没有已提交的运行”或抛出有业务含义的异常。没有证据支持原意见所说的“ref 字典被并发写坏”或正常已提交任务的数据丢失。本轮仅复现和记录，未修改此源码。

## 使用判断与证据

调用通道、异步协调和会话控制可用于真实工作；本次免费路由模型适合辅助搜集代码证据和提供初稿，审查结论需要上层核对后才能转为修复。部分原生回答片段还被重复拼接，增加父上下文成本，结果保留原样供追溯。

[脱敏运行证据](evidence/acp-review-20261013.json)包含实际控制响应、原生工具摘要、各轮原始回答及 PM 状态边界复现。其首轮意见不是已确认缺陷列表。[阶段记录](acp-tools.md)保存运行与交接 checkpoint。
