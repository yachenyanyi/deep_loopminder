# PM Memory：官方执行机制优先

设计来源：[Notion PM Memory](https://app.notion.com/p/3e2ee7753a908007bb24f749e769b696)。ProjectStore 拥有当前事实；记忆保存历史原因、证据、每日观察、长期主题与压缩交接。

## 执行与入口

PM 与记忆功能通过 `langgraph.json` 分别注册入口：

- `pm_agent`：`src/deep_agents/agents/pm_agent/agent.py` 中的 PM 主图，读取记忆并提交任务。
- `pm_memory_agent`：本目录 `graph.py` 中的后台记忆图，统一处理普通快照与定期整理。

后台运行依赖官方 Agent Server。启动 `langgraph dev --n-jobs-per-worker 10` 后，通过官方 SDK 调用 `pm_agent`，并在 config.configurable 提供 project_id 和 thread_id。Project State 先由 #36 的 ProjectStore 写入。

PMMemory 只保存固定来源快照及官方 run_id 引用：

- `runs.create(..., multitask_strategy="enqueue")` 提交后台任务；同项目使用同一官方线程，串行写入。
- `runs.get` 读取真实状态，`runs.join` 等待压缩交接。
- 失败重试使用相同快照创建新的官方 Run。
- 不维护自己的 pending/running/completed 状态、asyncio 任务集合、锁、重启扫描、drain 或调度器。

默认 `get_client(url=None)` 使用同部署 ASGI transport。连接远程部署时配置 `LANGGRAPH_API_URL`。没有服务的普通 Python 调用不能异步提交；可直接构造官方 create_agent 调用读取工具，或连接已运行的 Server。不提供一套本地后台 fallback。

## 周期整理

使用官方 cron，在同一项目记忆线程上排队；没有在每个 PM turn 中维护周期计时器。

```python
from langgraph_sdk import get_client
from src.middlewares.memory.pm_agent_memory import PMMemory

client = get_client(url="http://localhost:2024")
thread_id = PMMemory.thread("demo")
await client.threads.create(thread_id=thread_id, if_exists="do_nothing")
await client.crons.create_for_thread(
    thread_id,
    "pm_memory_agent",
    schedule="0 19 * * *",  # UTC 19:00，即北京时间次日 03:00
    input={"project_id": "demo", "source_id": None},
    multitask_strategy="enqueue",
)
```

代码只提供执行图，不替部署创建 cron。整理读取近期来源和既有每日/主题记录，合并、修订、失效或晋升；周期应按项目实际活动配置，避免空转。

## 官方能力与项目职责

Agent 使用 LangChain create_agent；文件工具、路由与持久化使用 Deep Agents FilesystemMiddleware / CompositeBackend / StoreBackend。结构化结果使用 LangChain ToolStrategy(MemoryOutcome)，区分 ignore / candidate / merge / promote；无价值内容可忽略。

有索引时通过官方 Store asearch 查找主题摘要；没有索引时用 Backend grep 做字面搜索，不再维护中文切词、评分或自建检索索引。grep 查询应使用短的原文关键词。PM Skill 可通过 `## Memory questions` / `## 历史问题` 声明要核查的问题，或提供状态字段 pm_memory_questions。

项目自己保留的逻辑：项目 scope、固定证据快照、最新状态优先、写入前核对新证据、主题来源和版本、有限读取预算，以及压缩前等待可读交接。当前事实不从历史检索猜测。

## 读取与写入

每次异步模型调用读取最新 Project State，附加有预算的最新已完成交接、Memory Map、近期每日摘要、候选主题和书签目录。Memory Map 从已有文档生成。每日默认自动可见三天；过期只停止注入，不删除或判定失效。

主 PM 工具：project_context、memory_search、submit_pm_memory、pm_memory_status（retry=True 可显式重试）、pm_read_memory、pm_read_bookmark、pm_investigate_memory。主 PM 没有记忆文件写工具；调查 Agent 也只有只读工具。来源书签无需查询运行状态即可读取。

后台 Agent 使用固定来源，加上写入时最新 Project State 与用户观察。只允许修改本次 destination 和项目 topics；无 shell、普通委派或 ProjectCommand。失败由 Server 记录，原始快照继续保留。整篇旧主题用 `<!-- pm-memory: superseded -->` 标示，从默认检索与 Map 中排除；各观点的确定程度由正文区分。

虚拟文件：topics/<主题>.md、daily/<日期>.md、handoffs/<线程>/<交接>.md、bookmarks/<来源ID>.json，均在 /memories/ 下，按项目 namespace 隔离。统一 provider 复用现有 PostgreSQL → InMemoryStore；内存降级明确提示 ephemeral。

## 压缩与生命周期

PMMemorySummarizationMiddleware 只在官方 before-model hook 中增加交接等待，随后调用官方 SummarizationMiddleware。交接失败时停止压缩，保留来源；成功后下一轮加载可读交接与最新 Project State。

主动提交、正式 pm_memory_events、Project State 变化及 token 水位仍可触发后台运行。会话结束/任务切换事件生成交接；正式验收事件由应用提供，不把模型完成声明当成验收。

旧开发版 pm_memory_jobs 数据没有自动迁移到新 pm_memory_sources namespace。多文件编辑不是原子事务。真实模型质量、PostgreSQL 和完整 Server 部署仍需实际环境验证。

## 官方依据

- [后台记忆整理](https://docs.langchain.com/oss/python/deepagents/memory#background-consolidation)：独立整理 Agent + 官方 cron。
- [异步子 Agent](https://docs.langchain.com/oss/python/deepagents/async-subagents)：通过 Agent Protocol / SDK 管理后台运行，同部署优先 ASGI。
- [并发排队](https://docs.langchain.com/langsmith/enqueue-concurrent)：使用 multitask_strategy="enqueue"。
- [结构化输出](https://docs.langchain.com/oss/python/langchain/structured-output)：使用官方 schema 与 ToolStrategy。

2026-10-08：仅保留官方排队/来源重试、交接失败阻止压缩/成功恢复、官方文件工具/结构化输出三项关键行为检查，配合原有读取与装配检查。SDK 交互使用测试替身，未声称真实部署已验证。


## 模型指导与成果确认

整理模型使用中文步骤指导：真实读取、写入、读回，再通过官方 MemoryOutcome 报告结果。
普通文本中的工具名称或 JSON 不会被执行。调用保持串行，单行 Markdown 写入会返回格式错误，要求模型修正。

缺少结构化结果时只追加一次指导。若仍缺失，只有当前依据未变化、目标文档（综合整理也包括主题）真实新增、含实际换行且引用本次来源书签时，代码才保守确认 candidate 并完成索引；返回和来源记录标记 `outcome_origin=verified_document`。
模型正常报告时标记 `outcome_origin=model`。代码确认不会晋升为长期事实，也不会改变项目任务状态。
没有真实文档、文档未变化或来源不符时仍然失败；这不是把文字工具提议当成执行成功。


## Server 的统一 Store 接入

`langgraph.json` 的 `store.path` 指向 `provider.py:generate_pm_store`，由官方 Server 管理连接生命周期。
配置 `LANGGRAPH_POSTGRES_URI` 时使用官方 PostgresStore，其异步接口兼容 Windows 后台循环；未配置时提供 InMemoryStore。
图工厂优先通过官方 get_store 复用 Server 的同一对象，普通独立 Python 命令仍复用项目原有数据库提供方。
不能只在 compile/create_agent 中传入应用 Store 就假设 Server 会沿用它，也不能关闭记忆中间件的 Store 一致性检查。
参考：https://docs.langchain.com/langsmith/custom-store 。


## 定时整理的无变化判断

无 source_id 的官方 Run 调用 PMMemory.consolidate。成功整理后，在同一个 Store 的 pm_memory_consolidations 项目命名空间保存最新输入凭据：项目状态、最新用户观察身份、普通来源身份及每日/主题/交接正文哈希。来源扫描使用官方分页查询。

下一次完全相同时直接返回 ignore，outcome_origin=unchanged_inputs，不调用模型、不创建额外来源。失败不会保存凭据；执行期间出现新来源也不推进凭据。整理自身的来源与书签不触发重复整理；凭据记录整理后的正文，所以自身写入不会造成空转。新增或变化的业务依据仍进入正常模型整理。此记录不维护 Run 状态，不代替官方 Cron 或队列。

综合整理允许仅更新 /memories/topics/；普通记录与交接仍要求其目标文档实际变化。缺少结果报告时，主题也必须满足多行、实际新增且含本次书签，才能保守确认为 candidate。所有调用须在同一项目的官方记忆线程排队，跨线程直接并行修改记忆不保证原子性。


## 状态查询的可恢复错误

来源快照可以先于官方 Run 保存。只有来源、没有 Run 引用时，状态查询和重试会明确拒绝；不会伪造 pending 等运行状态，也不会自动重新提交。来源书签继续可读，需先核实原提交结果。未知来源和无效重试由官方 ToolException/handle_tool_error 转为 status=error 的工具结果，PM 可继续处理问题。网络、数据库等基础设施异常仍传播给官方 Run，不包装成成功。

读取工具也使用同一官方错误机制：pm_read_memory 的非法路径、缺失文档和分页范围错误，以及 pm_read_bookmark 的未知来源/外项目来源和分页错误，均返回 status=error，允许 Agent 修正后继续读取。跨项目来源与未知来源使用相同提示，不暴露外项目内容；连接、存储等基础设施故障仍传播给官方 Run。

memory_search 的关键词必须非空白，limit 必须在 1～20。错误输入同样返回官方 status=error 工具结果，PM 修正参数后可继续搜索；检索后端连接故障不会被转换成“无匹配”。

模型配置与PM主图、真实命令测试共用src/models/llm.py的get_default_model：minimax/minimax-m3.1-flash:free、32768输出token；不再单独选择Command Code或覆盖后台记忆模型参数。
