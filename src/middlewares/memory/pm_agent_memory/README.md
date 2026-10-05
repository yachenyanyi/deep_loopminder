# PM 记忆中间件

本模块独立于通用记忆中间件。ProjectStore 是当前项目状态的权威来源；这里保存历史原因、观察、决策依据、每日记录、交接和原始上下文书签。

## 当前实现

- PM 用 `submit_pm_memory(purpose, observation, focus)` 提交观察重点，不直接写文件。
- 系统先固定消息快照和提交时读取的 Project State，再持久化任务，随后启动后台写入。消息和 Project State 的读取不是跨 Store 的原子事务。
- 后台 Agent 使用官方 `create_agent`、`FilesystemMiddleware`、`CompositeBackend` 和 `StoreBackend`。只有文件工具，没有 shell、普通任务委派和项目状态修改工具。
- 写入限制由工具调用中间件执行：仅允许本次 destination 和 `/memories/topics/`，不允许修改书签、其他日期的每日记录和其他交接文件。
- 同一 consumer 的同一项目串行写入；提交幂等任务时也在实例内串行检查。不同项目可以同时处理。
- 任务状态为 queued/running/completed/failed。失败保留快照；调用 `retry` 重试同一个来源。`resume_pending` 恢复被进程中断的 queued/running 任务。
- 文件版本元数据保存稳定 ID、项目、路径、版本、内容 hash、更新时间和来源书签；只给本任务实际修改的文档增加版本。

## 文件与来源

| 虚拟路径 | 用途 |
|---|---|
| `/memories/topics/<topic>.md` | 可复用的决定、原因、经验；确定程度写在具体陈述里 |
| `/memories/daily/YYYY-MM-DD.md` | 项目当天的观察；可提炼到主题记忆 |
| `/memories/handoffs/<thread-hash>/<time>-<job-id>.md` | 单次线程交接 |
| `/memories/bookmarks/<job-id>.json` | 指向持久化任务中的原始快照 |

文档使用 `("deep_loopminder", "pm_memory_files", project_id)` 命名空间；任务和版本元数据分别使用 pm_memory_jobs、pm_memory_documents 项目命名空间。全部使用同一个注入的 BaseStore。没有另建数据库或 checkpoint 实现。

`pm_read_memory(path, offset, limit)` 按行读取选中文档；`pm_read_bookmark(job_id, offset, limit)` 按字符读取完整来源 JSON，包含消息和提交时的 Project State，失败任务的来源也可读。两者均返回总长度，便于继续翻页。书签里的 `pm-memory-job://` 由这个工具解析，不是 HTTP 地址。

`memory_search` 优先使用 runtime.store，查询文件与已有历史记录；旧记录仍按 superseded 过滤。文件搜索目前是关键词匹配，不能自动判断文件内部某一句是否已被新结论替代，读取后须核对正文及 Project State。

## 读取与触发

每次异步模型调用都追加最新 Project State 和有限的历史上下文，保留已有系统提示词及 Skills 内容。默认加载当前线程最近一个 **completed** 交接、最多 30 个主题摘要，以及上海时区今天和前两天的每日摘要；旧日期、未来日期不自动加载。历史上下文默认最多约 8000 字符，全文由 PM 选择读取。每日窗口可设为 0。

写入支持三种入口：PM 主动工具调用、应用提供的项目事件、配置的上下文 token 阈值。自动触发在 `after_agent`，并不是实时监听外部项目数据库。

事件通过状态的 `pm_memory_events` 追加，必须包含唯一的 `id` 和受支持的 `kind`。例如：

```python
{"id": "task-17-failed-attempt-2", "kind": "task_failed", "evidence_ref": "test://17"}
```

支持 new_goal、goal_updated、plan_created、task_assigned、task_completed、task_failed、risk_raised、blocker_detected、requirement_changed、milestone_completed、verification_failed、project_closed。应用负责提供真实事件及证据，中间件不把模型声称的完成当作验收。

事件游标及 token 水位放在图状态里；配置官方 checkpointer 后可跨调用恢复。事件 ID 为线程内稳定身份，相同 ID 的重复提交保留首次快照。阈值使用官方近似 token 计数，需要调用方配置；首次达到阈值或之后又增长一个阈值才提交，压缩后水位可重置。它不是模型窗口比例，也不包含系统提示词的 token。

## 配置示例（尚未插入现有 Agent）

```python
from src.middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware, PMMemoryJobs

# store 使用 PM 主图已有的统一 provider，memory_model 由应用提供。
jobs = PMMemoryJobs(store, memory_model, timezone_name="Asia/Shanghai")
memory = PMAgentMemoryMiddleware(
    jobs,
    recent_daily_days=3,
    context_token_threshold=12_000,  # 示例参数，不是固定产品策略
)
# 后续装配时，主图的 store 必须是同一个实例。
# 调用 config.configurable 必须包含 project_id 和 thread_id。
```

不提供 jobs 时保留原有两个读取工具，不开放后台写入工具。当前仅实现异步钩子；使用 `ainvoke`，运行于持续存活的 asyncio 事件循环。正常关闭前可 `await jobs.drain()`。短暂调用 `asyncio.run` 后立即退出不会保证后台任务完成。

## 压缩与交接边界

`await jobs.prepare_handoff(project_id, thread_id, messages, focus=...)` 保存快照、等待后台写入完成并返回可读交接正文。写入失败时抛错，因此调用方可以停止压缩，保留上下文后重试。

这个显式接口 **尚未接入现有压缩中间件**。没有任何 Agent 装配变更，也不能声称目前自动压缩已经受交接门禁保护。普通自动记忆不能替代压缩前等待交接。

## 尚未完成及运行限制

- 目前是固定快照启动独立记忆 Agent，不是真正复用父图 checkpoint 的 fork。
- 单个项目只能有一个活跃 consumer；实例锁不能协调多个进程。持久化任务支持重启恢复，但不是分布式调度器。
- 多个文件的编辑与任务完成标记不是原子事务。失败时可能留下部分每日/主题编辑；正文来源仍可查，交接自动加载只采用 completed 任务。
- 长消息快照及长期文件保留尚无清理策略；日记合并/主题提炼由每次后台 Agent 判断，尚无周期性整理调度。
- 尚无按 Skill 的历史问题自动推荐、语义向量文件检索或自动压缩接入。
- 已有 provider 的 PostgreSQL fallback、durability 由原模块负责；不能把 InMemoryStore 的恢复能力当成跨进程持久化。
- 行为测试使用 InMemoryStore 和可调用工具的假模型；真实 PostgreSQL、真实模型质量及最低 deepagents 版本兼容性尚未验证。本轮测试环境为 deepagents 0.7.21、langchain 1.4.3。

官方接口参考：[文件中间件](https://reference.langchain.com/python/deepagents/middleware/filesystem)、[StoreBackend 源码](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/backends/store.py)。

## 本轮验证（2026-10-05）

- 新增 14 项行为测试，包含真实 LangChain 图调用及官方文件工具调用；模型响应由假模型提供。
- `python -m pytest -q --ignore=tests/test_pm_middleware_assembly.py`：188 passed。
- 本模块及新增测试的 Ruff、compileall、git diff --check 通过。
- 无忽略参数的全量测试在收集阶段失败：AI 分支原有 `src.runtime.middleware_assembly.pm` 从 `src.runtime.pm` 导入不存在的 `stable_pm_system_prompt`。这两个文件与基线一致，本轮未修改；因此不能把上面的结果称为完整测试套件通过。
