# 运行轨迹中间件

`TrajectoryMiddleware` 是官方异步 `AgentMiddleware`，放在中间件列表最前方，记录输入、每轮可见上下文、模型调用和最终工具返回。不修改提示词或成功结果，不维护另一份调度器。生产 PM 可通过宿主环境变量 `PM_TRAJECTORY_DIRECTORY` 启用；`PM_TRAJECTORY_STOP_ON_ERROR=true` 显式开启严格停止，默认只观察。其他 Agent 直接装配即可：

```python
from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

recorder = TrajectoryRecorder("workspace/my-run")
observer = TrajectoryMiddleware(recorder, stop_on_error=True)
agent = create_agent(model, tools=tools, middleware=[
    observer,
    *business_middleware,
    observer.request_capture(),
])
```

每个部署进程共享同一个 recorder；不要让多个进程同时追加同一事件文件。网页只读取该文件。正式部署的跨进程聚合应复用已有日志/遥测服务，不能把这个本地文件实现宣称为分布式事件存储。

## 文件与职责

- `middleware.py`：事件格式、脱敏、首错现场、官方模型/工具包装器，以及同一中间件的内层请求采集端。记录器的 `native(provider, notification, stop_on_error=...)` 接收 ACP 公开通知，外部调用轮次可通过 `emit` 接入。
- `web.py`：只监听 `127.0.0.1` 的独立本地 HTTP 服务、字节游标增量读取、状态/导出和停止请求。
- `dashboard.html`：完整网页功能集中在一个文件，包含事件投影、快照、DSH式30px连续账本、三泳道时间线及按需展开的记录检查器；支持真实会话侧栏、只读对话/轨迹切换、轮次/调用折叠、实际时长开关、历史加载、时间范围聚焦、复制、导出、暂停跟随、深浅色与停止按钮。角色/类型/错误筛选及用量列开关位于右上角更多菜单。
- `__init__.py`：公共入口。

网页启动：

```powershell
python -m src.middlewares.trajectory.web --directory workspace/my-run --port 62621
```

`--port 0` 自动选择端口，地址写入同目录 `monitor.json`。Agent 服务退出后网页仍可查看；再次启动网页指向同一目录即可回看。网页不会自己启动/恢复 Agent。

## 首错停止契约

严格模式识别：`ToolMessage.status=error`、Command 中的工具错误、顶层结构化失败信封、模型异常、无效工具 JSON，以及显式启用严格桥接的 ACP 原生 failed/非零退出码。工具节点在包装器外生成的参数错误，会在下一次模型调用之前被拦截。成功查询返回的历史Worker错误、失败制品正文与客户材料中的error字段属于数据，不递归升级成本次工具故障；ACP通知的协议嵌套单独深层检查。未上报结构化状态的任意外部程序错误无法保证自动识别。

首错保存 `first-error.json`，阻止后续模型/工具调用并抛出 `TrajectoryStopped`。已在执行的其他 Worker 要由宿主取消，不是同步强杀进程。网页停止按钮只写 `stop-request.json`，**宿主必须监听并通过官方接口取消父/子 Run**；未接入宿主时按钮不能保证取消。`tests/run_pm_project_hub.py` 已接入该契约，每0.5秒检查并并发取消官方 Run，不在错误后修参数或重跑。

正常取消不记作新故障。采集文件系统失败会作为异常暴露，不能悄悄继续并宣称轨迹完整。运行退出码或 official Run success 不代表业务验收通过。

正式网站观察入口按用户要求，对Claude原生开发工具只记录错误、允许其自行恢复；PM工具/模型和ACP传输/官方新Run失败仍立即停止。宿主从已结束运行续跑时，复制官方dev持久化目录到新的证据目录，保持原PM线程与项目，以`input=None`恢复待执行节点；恢复前已经终止的旧Worker错误记录为历史基线，不能冒充新故障，也不能改成成功。`PROJECT_HUB_RESUME_RUN_DIRECTORY`由宿主提供，不是模型工具参数。

## 数据边界

### 与DSH内容结构的对应

网页从同一份JSONL独立投影，不读取或改写Chat/业务会话快照，也不移植DSH的宿主插件注册表。`TrajectoryProjection.apply`按事件id去重、调用id合并开始/更新/结果，`snapshot`保留请求和Schema索引，`deriveLayout`生成轮次轨道、请求绑定、折叠与历史入口，`virtualWindow`只呈现可见行及缓冲区。布局由网页与Node回归共用，没有第二套闲置渲染算法。每页300个事件继续使用原有字节游标；账本初始显示最近200条，可在顶部加载更早记录，原始事件不会被丢弃。长历史的DOM有界，已加载数据仍保存在浏览器内存，不宣称无限内存使用。

实页对照的几何尺寸为280px侧栏、76px会话标题/标签、32px工具栏、50px输入/模型/工具时间线与30px记录行。详情默认隐藏，选中记录后打开，提供概述/预览/原始内容，可拖动分隔条或用方向键调整宽度，双击恢复38%。时间线默认等宽事件，时长按钮切换记录时长；拖选保留全部上下文并淡化选区外记录。无原生轮次/模型请求编号时明确显示原生会话/消息片段，不用记录序号冒充。侧栏只列当前绑定运行中的真实会话及接入记录，不扫描其他项目或复制DSH中的客户会话。只读对话视图也来自这些事件，没有伪造可发消息的聊天输入框。

记录包括System、User、Context、Compacted、Message、Tool、Subtool，以及Worker委派。模型请求展示输入、输出、调用时请求快照、工具Schema、原始事件与指标；ACP工具按原生toolCallId合并，公开parentToolCallId保留在详情。未提供父调用标识时不猜测嵌套关系。官方`lc_source=summarization`消息作为压缩摘要；宿主可用`emit("compaction", ...)`补充压缩生命周期事件，不从普通文本猜压缩是否发生。

外层观察器负责最终错误与总耗时；`request_capture()`放在业务中间件之后，捕获经过记忆/上下文注入的ModelRequest。两处使用ContextVar共享span，前端用实际请求快照更新初始快照。生产PM和正式观察入口均已装配；只使用外层时，快照明确标为`trajectory_wrapper_request`，不能声称包含后续注入。快照包含system、messages、工具Schema、模型名和model_settings，经原有脱敏/截断后保存，不包含整个认证runtime或HTTP请求头。

五维用量为输入、缓存读、缓存写、输出、推理，来自公开usage_metadata。未知项为`—`/null，提供方明确返回0才显示0；缓存可能包含在输入内，不重复相加。ACP的used/size是上下文占用，单独显示在指标详情，绝不当成计费Token。数值reasoning计数保留，隐藏思考正文仍不采集。当前钩子没有首Token时间，因此TTFT与解码速率标为未采集；不能从整次请求耗时倒推。

旧日志可直接回看，但缺失的请求正文、Schema、推理用量和轮次边界无法补采。实际图片仅显示日志已包含且完整的内嵌图片，不根据日志路径读取其他本地文件或加载远程图片。页面源码可由现有网页服务读取，刷新即可更新；新采集字段须在下次启动Agent后产生。

`trajectory.jsonl` 包含 schema_version、时间、分类、阶段、调用标识、Agent、线程/Run、耗时和数据。时间为 UTC，网页显示北京时间。事件文件和 HTTP 状态先脱敏；隐藏思考块不记录，长字符串/列表明确截断。该过滤覆盖常见凭据字段、已知环境密钥、Bearer 和数据库口令，不宣称能识别任意未知敏感文本。页面通过 textContent 呈现事件，不执行日志中的 HTML。

日志是运行投影，业务状态仍以官方 Store 为准，Run/线程/checkpoint 仍由 Agent Server 管理。本地网页不是公网服务，不替代部署认证。

官方依据：[自定义中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[运行身份](https://docs.langchain.com/oss/python/langchain/runtime#execution-info-and-server-info-inside-middleware)。
