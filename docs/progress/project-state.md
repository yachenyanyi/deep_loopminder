# 项目状态中间件

目标：集中项目数据、确定性规则、官方 Store 与 Agent 工具/上下文注入。

方案比较（实施前）：
1. 只搬目录，继续由 PM 记忆读取状态：改动小，但工具与上下文仍分散。
2. 独立 ProjectStateMiddleware 拥有状态工具和实时上下文，PM 记忆只管理历史：采用，领域规则仍是纯函数。

执行边界：复用官方 AgentMiddleware、ToolRuntime、BaseStore。BaseStore 没有原子 CAS；默认只读，宿主声明 project_state_single_writer=True 后才允许写入，必须保证每个项目只有一个串行写入线程；同一模型回复含多个写调用时全部拒绝，要求逐步调用。暂不新增队列、锁或调度器。
可信验收/审批：通过构造器注入异步 facts_provider，按当前快照及目标转换查询；默认无事实，拒绝 passed/approved。模型工具参数不包含审批布尔值。

验收：迁移所有导入；PM 装配工具不重复；官方 create_agent 工具调用可保存状态并刷新上下文；依赖/可信门禁与只读模式通过。

状态：进行中。基线存在 ACP/PM 未提交修改，保留；不运行无关 ACP 实测。

已完成：领域文件迁移、全部源码/测试导入更新；PM 装配新增中间件；当前上下文从记忆模块移出，历史模块继续观察 Store 变化。工具实现已完成，待验证。

已验证：首次相关测试 55 通过、1 失败（PM profile 测试仍期待旧列表，已更新）；3 项新增官方 create_agent 调用链测试通过，覆盖全生命周期、状态刷新、范围隔离、只读/并行拒绝、依赖和可信门禁。使用确定性测试模型与真实 InMemoryStore，没有调用外部 LLM。文档已补充单写入者边界、异步调用及 Store 持久化条件。

已验证：失败的 profile 断言修正后，与已变更的记忆公共导入、routing 消费方共 15 项针对性测试通过；项目状态包及 PM 装配/profile 的 Ruff 检查通过；git diff --check 通过；源码、测试、README 无旧项目路径引用。没有重复执行未变更的状态规则测试。

Checkpoint：实现和本轮验证完成；现有 Store 数据格式兼容；PM 已装配，其他 Agent 可复用。默认写入关闭，可信验收/审批 provider 尚未绑定到实际业务验证服务；单写入者是宿主部署约束，未验证多线程/跨进程并发写入。Notion 候选经验已保存：https://app.notion.com/p/3f8ee7753a9081509123c11bc8424fb9 。无提交或推送。

## PM 记忆目录迁移（用户指定）

进行中：完整包迁到 middlewares/memory/pm_agent_memory；保持功能与官方图入口，更新消费者。目标原为空目录，已移除空目录后整体迁入。


已完成并验证：PM 记忆模块的 6 个 Python 文件和 README 全部迁到 `src/middlewares/memory/pm_agent_memory/`；消费者导入已更新，旧目录和缓存已删除；16 项相关测试通过，旧路径引用扫描和 diff 检查通过。整体目录移动受 Windows 占用阻止后，改为逐文件移动，最终旧目录已清理。

用户代码规范（后续持续遵守）：注释和说明尽量中文；风格统一；一个完整功能优先集中在一个文件，避免过度拆文件、拆函数；以人类阅读便利为准，只有明确复用或独立规则需要才提取函数。本次已统一新项目状态入口、摘要与工具内部说明，未改变业务逻辑。

Checkpoint：目录迁移及验证完成。本阶段无剩余迁移事项；后续修改按上述阅读性规范执行。


## 继续检查迁移归属

检查发现：后台记忆图 pm_memory_agent 仍留在 PM agent.py，完整功能归属记忆中间件。
方案比较：保留在 PM 图入口文件可减少一个文件，但记忆实现与注册路径仍分散；整体移入记忆包 graph.py 可集中完整后台执行功能，采用此方案。仅移动现有函数，不拆分内部步骤、不新增抽象。
runtime/routing 被 PM 与 ACP 共同使用，保留；reconciliation、restart、partial_start、warm_reuse、teardown 属于通用运行策略，保留。
验收：官方配置指向新图入口；编译与执行既有快照记忆任务；旧文件无残留函数。
状态：进行中。

已完成：后台记忆图整体移入 pm_agent_memory/graph.py，官方注册路径和模块文档同步更新，PM agent.py 只保留主图。已验证：3 项记忆集成测试通过，新增测试从 langgraph.json 解析并加载真实图工厂，编译执行固定快照任务（记忆服务模拟，不调用外部模型）。检查发现中文句号被 D415 按英文标点误报，配置忽略该标点规则以遵守中文注释规范。

Checkpoint：本轮归属检查及后台记忆图迁移完成；3 项集成测试通过，相关代码 Ruff 与 diff 检查通过。已明确共享路由/运行策略保留原位，不强行迁入 ACP 或项目状态中间件。无提交或推送。


## PM 单文件整理（用户指定）

目标：将配置、提示词、上下文和装配完整合并到 agent.py，按组装顺序组织并使用中文注释。
方案比较：保留多个职责文件便于单独导入，但阅读需频繁跳转；集中 agent.py 便于顺序理解完整 PM 装配，按用户要求采用。__init__.py 仅作为公共导出入口；中间件实现仍由各自模块拥有。
验收：公共接口与别名身份保持；配置、提示词、上下文、装配相关测试通过；所有旧模块导入更新，4 个旧实现文件清理。
状态：进行中。

已完成：四个实现文件合并并清理，源码与测试导入及 README 已更新。首次检查发现合并抽取遗漏 dataclass 装饰器，以及静态检查删除纯导出类型导入，已补回装饰器并使用显式同名导出导入，待重新验证。

已验证：19 项 PM 配置、提示词、上下文、装配、委派与项目命令测试通过，保留原 dataclass 约束和公共别名身份；Ruff 检查通过。仅说明/结构调整，不重新执行无关 ACP 或记忆实测。

Checkpoint：PM 单文件整理完成。agent.py 按五个中文说明分组组织，__init__.py 仅导出，四个旧实现文件已删除，文档/引用同步。无提交或推送。


## 记忆整理 Agent 真实命令测试

目标：使用 openrouter/free 实际模型执行官方整理图，验证记录、修订、重新读取和搜索；独立测试项目，不修改真实业务项目。
范围：复用当前图、PMMemory、StoreBackend 和存储提供方；命令直接调用图 ainvoke。当前 2024 端口未运行 Server，此次不把直接执行当作后台 enqueue 链路验证。
验收：文件非空且包含测试事实；来源引用和索引版本可查；第二次输入能够记录方案更正；明确 Store 持久化级别。状态：进行中。

真实运行已启动：独立项目 pm-memory-live-0d92db2bf2b6；请求模型 openrouter/free，响应标识 google/gemma-4-26b-a4b-it:free；生产存储提供方返回 InMemoryStore/ephemeral。运行直接调用整理图，不验证 Server 排队与跨进程持久化。证据将写入 docs/progress/evidence/pm-memory-live-20261013.json。

首次真实结果：记录步骤通过（可读取、测试标记、来源索引、搜索全部通过），outcome=candidate，version=1。修订步骤失败：结构化结果 None 导致 AttributeError；记录正文还出现字面量反斜杠 n，标题索引因此过长。保留证据，仅重试失败修订并补抓输出；内存 Store 丢失后使用已验证文档作为回放输入，不冒充跨进程持久化。


针对失败修订的一次诊断重试：仍使用 openrouter/free，响应 nvidia/nemotron-3-ultra-550b-a55b:free；finish_reason=stop，模型仅在文本里输出 read_file 名称/参数，tool_calls=[]，未实际调用工具。读回文件没有方案B，确认不是成功写入后仅返回失败。不是输出 token 耗尽（output_tokens=180）。

最终结果：首次真实记录通过；修订未通过，当前免费路由下的工具/结构化输出不稳定；记录正文包含字面量 \n 导致 Markdown 标题和摘要质量不佳。没有伪造工具执行，也未将文本工具提议转换执行。
证据：tests/run_pm_memory_live.py（复用命令）；docs/progress/evidence/pm-memory-live-20261013.json（保留首次记录和诊断重试）；同名 .md（实际首次记录正文）。
Checkpoint：本轮命令测试完成。仅诊断重试失败步骤，没有重复跑已通过的记录任务；内存数据在进程退出后丢失，首个成功记录导出为证据。未启动 Server，因此不覆盖后台 Run/enqueue；未验证 PostgreSQL 持久化；未修改生产 Agent 行为。剩余问题为模型协议稳定性与 Markdown 格式，需要独立后续修复/验证。无提交或推送。


## PostgreSQL 连接与持久化测试（2026-10-14）

目标：使用本机 PostgreSQL 验证 PM 记忆真实写入及独立进程读回，不将内存回退当作成功。
已验证：psql 位于 H:/PostgreSQL15/bin/psql.exe；127.0.0.1:5432 可连接；以 postgres 用户连接 postgres 数据库，无密码方式被拒绝，错误 fe_sendauth: no password supplied。
配置检查：项目 .env 与当前进程均未设置 LANGGRAPH_POSTGRES_URI 或 DATABASE_URL；默认密码文件不存在。不修改数据库认证规则、不猜测密码。
待用户信息：请在 .env 配置真实 LANGGRAPH_POSTGRES_URI，密码不必发送到聊天。已通过输入请求告知。
Checkpoint：连接检查完成，实际读写待数据库凭据配置；下一步认证查询确认数据库与用户，再执行独立测试项目记忆写入和跨进程读回。没有执行数据库写入、创建数据库、修改生产代码或启动后台服务。

2026-10-14：按用户授权重置本机 PostgreSQL 15 的 postgres 密码。核实实际进程数据目录与 hba_file 后，临时规则仅允许 127.0.0.1 的 postgres 连接 postgres 数据库；ALTER ROLE 成功，finally 恢复原认证文件（字节一致），新密码连接验证通过。密码不记录到文档；配置备份位于 H:/PostgreSQL15/data/pg_hba.conf.codex-backup-20261014-101923。尚未修改项目数据库配置。

继续原持久化测试：创建独立 deep_loopminder_test 数据库；只在测试进程注入连接地址，不改现有业务数据库和项目 .env。本轮测试覆盖官方 PostgreSQL Store 的跨进程记忆读写，不重复已知不稳定的免费模型生成测试。

首次 PostgreSQL Python 测试在 Windows 默认 Proactor 事件循环下被 psycopg 拒绝，尚无数据表写入；CLI 输出另有 GBK emoji 编码问题。测试入口显式使用 WindowsSelectorEventLoopPolicy 与 UTF-8，仅针对这些新环境问题重试，未修改生产运行配置。

已验证：官方 AsyncPostgresStore 初始化并写入项目快照、PM 来源快照、确定性记忆测试文档及索引；关闭写入进程连接后，独立 Python 进程读回项目/来源/文档并搜索，4 项检查全部通过，durability=durable。证据 docs/progress/evidence/pm-memory-postgres-20261014.json；测试命令入口 tests/run_pm_memory_postgres.py。文档为存储测试输入，未调用 LLM，也不代表此前修订协议问题已解决。

Checkpoint：用户指定的 postgres 密码重置与登录验证完成；原认证配置恢复。原 PostgreSQL 跨进程持久化测试完成，独立测试库 deep_loopminder_test 保留测试证据；业务数据库未修改，项目 .env 仍未设置连接地址。生产 Windows 启动若使用 psycopg 异步驱动，同样需使用兼容事件循环。无提交或推送。


## PostgreSQL + 真实记忆 Agent 测试（2026-10-14）

用户授权：使用已验证 PostgreSQL 后继续实际 Agent 测试。
范围：独立 deep_loopminder_test 数据库、真实 openrouter/free 模型、当前官方记忆整理图；记录方案A后修订方案B；关闭进程连接后独立进程读回。命令仍直接图调用，不覆盖后台 Server 排队。
测试入口增加 require-postgres 防止内存回退冒充成功，按日期分开证据，兼容 Windows psycopg 事件循环。无生产行为修改。
状态：进行中；不重复之前已通过的确定性 Store 测试。


实际结果：独立项目 pm-memory-live-1ce7a2175379，后端 AsyncPostgresStore/durable；请求 openrouter/free，响应 nvidia/nemotron-3-ultra-550b-a55b:free。首条记录失败，模型只输出 read_file 文本 JSON，实际 tool_calls=[]，finish_reason=stop，随后 structured_response=None 导致 AttributeError。
独立进程验证：source_persisted=true；document_persisted=false；search_persisted=false。来源快照已真实入库，但记忆正文未生成，也未索引。明确阻塞在模型工具协议，不是数据库连接或持久化。
证据：docs/progress/evidence/pm-memory-live-20261014.json；tests/run_pm_memory_live.py，启用 require-postgres；两进程均关闭数据库连接，未启动后台服务。
Checkpoint：用户要求的 PostgreSQL + 实际 Agent 测试已执行，结果失败，不冒充成功；首步失败后未继续修订或反复调用随机免费路由。尚需单独修复/验证工具协议与空结构化结果处理；此前 Markdown 格式问题也未解决。生产 Agent 行为与项目 .env 未修改。无提交或推送。


## 模型指导与真实成果验证

用户授权：尝试给模型明确指导，以实际测试成果为准。
调整：记忆提示词改为中文顺序说明，明确真实 tool_calls、目标写入/读回、MemoryOutcome 和真实换行；使用官方 model_settings 的 tool_choice=required、parallel_tool_calls=False 要求工具调用；缺失结构化输出时最多指导一次，并给出明确错误，禁止解析模型文本冒充调用。
验收：新增协议指导回归用例；重新执行已变化的真实模型 + PostgreSQL 链路，记录实际文档、版本、来源、搜索和独立进程读回。此前失败证据保留。
状态：进行中。

回归检查发现 ToolStrategy 已内部强制工具选择，在 model_settings 重复设置 tool_choice 会产生参数冲突，已移除重复设置，复用官方机制；仅保留 parallel_tool_calls=False。尚未执行修改后的真实模型任务。

已验证：6 项相关回归通过。首轮指导后真实模型执行 write_file，记录、索引、搜索及独立进程读回均成功，但正文没有实际换行，格式验收拒绝通过；因此写工具新增最小 Markdown 换行检查，格式不合格时不执行写入并返回修正指导。失败/中间结果保留在 guided 证据 previous_attempt。

新轮格式校验后真实文件已有实际换行，来源和内容正确且 PostgreSQL 可独立读回；模型最后仍把 MemoryOutcome 写成文本，导致未索引。方案比较：严格结果工具缺失就失败会留下已写入却不可索引的数据；采用“仅对当前依据未变化、正文真实新增、多行且引用本次书签的文档保守确认 candidate”，并显式记录 outcome_origin=verified_document。不解析或执行文本工具提议，不确认 promote/业务完成；无文档或来源不符仍失败。

真实新轮：记录和修订均实际写入，实际多行正文清楚注明“方案 A 已作废，现在改为方案 B，原因是方案 B 支持后台排队”，索引版本2且含来源引用。测试字符串原来只接受无空格的“方案B”，产生误判；已改为按去除空白后的等价文字检查，进一步新进程验证旧方案失效、修订原因、版本和两次来源索引，不重复调用模型。模型本轮正常报告（outcome_origin=model），报告的 ignore/promote 分类与独立测试内容仍需人工辨别，真实文档与索引作为成果依据。

已验证：8 项相关回归通过；指导后的真实 openrouter/free + AsyncPostgresStore 记录/修订链路成功；独立读回8项检查全部通过（来源、正文、检索、方案B、A作废、原因、版本2、两次来源索引）。证据最终 status=passed，保留前两轮真实运行的 previous_attempt，不伪造旧失败为成功。指导前旧证据仍保留。

Checkpoint：本轮用户要求的成果已验证。中文顺序指导、串行工具、格式检查、一次协议指导和显式来源的候选确认已实现；实际文档与来源索引/版本读回证明记录和修订。免费路由依然可能输出不准确的 outcome 分类，未验证后台 Server enqueue，不声称所有模型都稳定。无提交或推送。


## 记忆生命周期专项实测

用户要求：测试压缩、交接、自动整理、书签及记忆查找。
范围：openrouter/free、PostgreSQL 独立测试库、官方 Agent Server 真实 Run/enqueue；测试图降低摘要阈值，生产配置不变。使用既有实际记忆作为查找输入，不重复普通记录/修订测试。
原子验收：书签来源与分页/项目隔离；查找及文档读取；真实后台交接 ready；官方压缩状态与后续上下文；显式业务事件自动提交；无 source_id 整理入口；Cron API 支持与清理。定时自动运行只能有真实调度证据才报告成功。
状态：进行中；测试服务只绑定 localhost，结束停止；口令仅进程环境注入。


已验证：查询5项全通过（搜索、修订文档、原始书签、分页、项目隔离）。真实 Server 首个 PM Run 失败：Windows 后台循环是 Proactor，AsyncPostgresStore 回退内存；且 Server 覆盖 compile 时的 Store，导致记忆服务与 runtime Store 不同。
官方依据：https://docs.langchain.com/langsmith/custom-store ，Server 通过 asynccontextmanager 管理 store.path；工厂上下文可使用官方 get_store。
方案比较：绕开 Store 一致性检查会混用不同数据，拒绝；采用官方自定义 Store 为 Server 提供同一官方 PostgresStore，provider 优先官方 runtime Store，普通命令仍用已有数据库提供方。官方同步 PostgresStore 的异步接口负责线程执行，兼容 Windows Proactor，不自造连接/调度器。未配置 URI 则显式提供 InMemoryStore，配置 URI 却连接失败则 Server 启动失败，不冒充成功。
状态：修复接入后准备重试受阻阶段；原生产 LangGraph 配置补充 store.path，.env 未修改。

官方 Store 接入修复后，后台 enqueue 成功创建真实记忆 Run；首次交接模型提前报告结果却未生成目标文件，PM 正确拒绝压缩，旧对话保留。新增目标路径明确指导，且结果工具先检查应生成的真实文档，缺失时只追加一次指导；交接的 ignore 不得跳过写入，保留原安全门禁。

已验证：实际后台交接与压缩7项检查通过；交接文档已保存、marker/下一步保留、出现官方摘要、消息减少、后续上下文包含交接，模型答复保留方案B和重启验证。PM Run 01a139a0-65c2-7cd0-9158-4d8675f14d55。
已验证：事件经真实 after_agent 自动创建后台 Run 01a139a2-1cf5-73d1-93dc-65e151a6fcd7 并成功记录 AUTO 标记；不是测试脚本手动 submit。
已验证：无 source_id 综合整理入口执行成功，consolidation=true 且目标文档可读；Run 01a139a2-adef-79a1-ab2d-5e3058c452a8。Cron API 可创建，正在观察真实定时 Run，结束删除临时 cron。

已验证：Cron API 创建成功并实际触发 Run 01a139a3-db8c-7522-9613-54ad25c8f149，但模型未完成结果协议/新增来源校验，Run=error；调度触发正常，不报告定时整理执行成功。临时 cron 已通过 delete 清理，测试没有留下周期任务。依据保留在生命周期 JSON。


已验证：测试 Server 停止后，新进程从 AsyncPostgresStore 读回交接文档、事件自动整理正文和搜索结果，4项持久化检查全部通过。
失败保护补充：首次失败 Run 被 Server 回滚，线程没有已提交消息，不能把空线程状态解释为已保留对话；对应 PostgreSQL 固定书签完整保留五条原始输入，且未生成官方摘要。该事实单独记录，不混入成功压缩验收。
相关回归：Store 接入修改后16项通过；补充交接提前报告指导后8项集成测试通过；相关 Ruff 和 diff 检查通过。既有测试文件顺便统一格式，未改变其断言语义。
Checkpoint：本轮所有要求已实际测试。查找/书签5项、交接压缩7项、事件自动整理2项、综合整理入口2项、停服务后持久化4项通过；临时Cron真实触发，但模型执行失败，不能宣称定时整理全通过。临时cron已删除，测试Server已停止（2024端口关闭）；证据 pm-memory-lifecycle-20261014.json 及 handoff/consolidated Markdown。修复采用官方 store.path 和 get_store，未删除一致性/真实成果门禁；生产阈值和 .env 未改。无提交或推送。


## 定时整理失败修复（2026-10-15）

范围：仅修复无 source_id 整理入口与整理成果校验；不重复已通过的查找、压缩及交接实测。
方案比较：单纯重试模型会对相同资料重复生成来源且仍要求改写每日记录；采用 Store 保存成功整理后的输入指纹，相同资料直接返回明确的 unchanged_inputs，新增资料仍交给官方 Run 中的模型。主题更新也作为整理成果，不强迫改写每日记录。指纹不是任务状态表，调度/排队仍由官方 Server 管理。
验收：主题单独更新、无变化跳过、证据变化重新整理、失败不保存凭据、整理期间新证据不被吞掉；最后验证一次真实 Cron。状态：实现中。

已完成：整理入口集中到 jobs.py 的 consolidate；成功后保存业务输入凭据，相同输入返回 unchanged_inputs；主题文档纳入整理成果验证。已验证：12项集成测试通过，覆盖主题工具真实写入/索引、失败保护、无变化跳过、四类输入变化及执行期间新来源。下一步：一次真实免费模型整理和一次官方 Cron 无变化验证。

真实修复验证首轮：Run 01a13e86-457b-7fb3-aa43-449816f704ac 失败；独立 PostgreSQL 检查确认只有测试候选和来源书签，无模型新增正文，不保存整理凭据。尚未创建 Cron。补充综合整理指导允许明确报告 ignore，并为下一轮测试保留真实模型响应用于协议诊断，禁止将无成果转成成功。

已验证：补充整理无新增结论指导后，真实 openrouter/free Run 01a13e88-bd0c-73f2-9b21-56f7601fcfce 成功，模型通过真实 MemoryOutcome 工具返回 ignore，PostgreSQL 已保存凭据。实际响应路由为 nvidia/nemotron-3.5-lightning:free 与 liquid/lfm-2.5-2.6b:free。官方 Cron 01a13e88-f88e-7e50-9934-b738cbb565a4 已创建，等待首个触发；尚不宣称定时执行通过。首轮失败仍保留在证据 previous_attempt。

已验证：真实官方 Cron 触发 Run 01a13e89-3f4b-7c91-8b88-5acbc56de28f 成功；四项检查全部通过：官方定时Run成功、返回 ignore/unchanged_inputs、凭据未改变、未创建额外来源。Cron 首次触发即删除，避免重复排队。证据 pm-memory-cron-20261015.json 保留首轮失败和免费路由响应摘要。测试 Server 已发出停止指令，准备核实端口关闭。

已验证：追加的综合整理协议指导专项测试1项通过（其余12项未重跑）；相关 Ruff 和 diff 检查通过。2024端口已无监听，临时Cron已删除，未留下测试服务或周期任务。
Checkpoint：本轮修复完成。新增/修改集中在记忆中间件 jobs.py、graph.py 和 README，专项集成测试与 run_pm_memory_cron.py、精确忽略例外及证据已保存。主题单独更新、失败门禁、凭据无变化/变化/并发新证据已覆盖；真实免费模型明确 ignore 后的官方Cron无变化路径通过。新资料下免费路由仍可能工具协议失败，首轮失败如实保留，不承诺免费模型始终可靠；调度、排队和运行状态继续由官方管理。没有新增调度器、没有提交或推送，项目.env未改，保留此前无关修改。后续如需评估有新资料时的模型稳定性，应另设有限样本评估，不重复当前已通过测试。


## PM Agent 只读能力实测（2026-10-15）

用户授权：实际调用 PM，读取项目或回答问题，不修改东西。范围：生产 pm_agent 工厂与原有中间件，真实 openrouter/free；无 ACP 配置，PM 无直接仓库工具。使用隔离 InMemoryStore 的明确测试快照，项目真实代码和验证记录为只读资料副本，不伪装为模型直接访问仓库；不连接真实业务 Store，不启动后台执行。测试宿主仅写测试入口、证据和进度。
验收：读取项目及记忆追溯、历史与当前事实冲突判断、执行成功不等于验收/关闭、有限可执行下一步、明确能力缺口；工具层只允许读取，核对所有工具调用和项目/文档未变化。状态：进行中；三轮不同问题，不重复已通过的记忆生命周期测试。

已完成前三轮真实PM调用：实际执行 project_read、memory_search、pm_read_memory；识别PM边界与 dev_complete 非验收完成。发现问题：只读默认100行未追分页，第二轮将未知定时状态写成否定、未验收写成未满足；第三轮引入聚类纯度/召回率及2%阈值等无依据指标，并建议重跑回归。四项只读核对通过：项目未变化、资料未变化、无后台Run、仅只读工具执行。下一步只追加一次针对分页遗漏和错误结论的纠正追问，不重跑前三轮。未修改生产PM。

已完成纠正追问：PM真实补读offset=100/limit=200，承认定时无变化有成功证据、稳定性未证明且不可直接done；但仍把consolidation_run_id当定时ID，并混淆model ignore与unchanged_inputs路径，也继续把无验收证据写成条件未满足。不是完整纠正，不再反复追问。
最终评估：工具执行通过、职责理解基本通过；主动取证和UNKNOWN精确表达未通过；规划和指导后纠错部分通过。证据status=completed_with_findings，保存四轮原文、8个模型响应、4次工具执行和人工核对结论。四项只读检查全通过，未启用ACP、未调用业务数据库或后台Server。
Checkpoint：本轮用户要求的PM只读测试完成。新增tests/run_pm_agent_readonly.py、evidence/pm-agent-readonly-20261015.json及精确忽略例外；没有修改生产PM或其他业务代码。测试是真实pm_agent工厂与原装配，测试宿主注入免费模型、临时Store和只读工具过滤/拦截；项目状态为测试夹具、源码/运行记录为真实只读副本，不能声称自主直接读仓库或checkpoint持久化已验证。当前结果支持PM辅助阅读与建议，验收结论仍需复核。所有测试进程已结束，没有启动服务或周期任务。无提交或推送。


## 功能层稳定性收尾（2026-10-15）

用户调整验收重点：功能完整、运行稳定，暂不追求免费模型回答质量。复用此前已验证链路，不重跑模型或生命周期全套。发现确定性遗漏：只有来源快照、没有官方Run引用时status读取None.value会导致PM工具异常中断。
处理比较：返回自定义任务状态会混淆官方Run生命周期；选择显式拒绝无Run的状态查询/重试，并复用官方ToolException的可恢复错误输出。未知来源和不允许重试同样返回工具错误，保留原始来源，不自动重提或新建Run。验收仅覆盖新边界及现有排队重试回归。状态：修复中。

已完成：jobs.status 对缺失Run引用明确报错；pm_memory_status 复用官方 ToolException + handle_tool_error，将未知来源/未关联Run/无效重试作为可纠正工具错误返回，来源保持不变，不自动重提。已验证：3项真实官方Agent工具链边界测试和1项现有官方排队/状态/重试回归通过（4 passed，13项无关集成未重跑）；无模型服务调用。
Checkpoint：按功能层收尾已修复确定性的状态查询中断，相关中文说明同步。核心链路此前的持久化、排队、交接、压缩、事件和Cron证据继续有效；只读PM工具调用也已验证。当前没有ACP部署配置，不能把可选Worker接入说成当前PM已能读取仓库；未新增后台服务或调度器，没有全量重跑或继续评测免费模型文本质量。基础设施不可用时仍明确失败，稳定性不以吞异常或伪造成功实现。相关静态检查通过；保留无关修改，无提交或推送。


## PM 运行边界命令测试（2026-10-15）

范围：继续真实 PM + openrouter/free 调用，新增不存在项目、未关联Run来源、越权项目写入、路径越界、负分页、跨项目书签六种边界。每项独立调用，保存真实工具调用/错误/后续响应，失败不反复换免费路由；临时内存夹具，不连接业务数据库和ACP，不启动后台任务。项目写操作保持宿主single_writer=False，验证原生拒绝路径；其它写入工具不暴露。验收区分模型未调用、工具可恢复错误、Run中断和权限隔离。状态：准备执行。

已验证前两项真实调用：不存在项目返回found=false，记忆无匹配正常返回；仅有来源无Run时retry返回status=error工具结果，随后PM真实project_read成功，未重提后台任务。边界证据写入pm-agent-boundaries-20261015.json，其余命令继续执行。

真实六项命令首轮完成：前三项工具正常返回或拒绝，项目写入被宿主原生single_writer门禁拒绝；后续路径越界、负分页和跨项目书签均未泄漏/写入数据，但ValueError/KeyError使当前PM调用中断。四项安全核对通过。后三项属于运行层缺陷，将仅修复已触发的读取异常恢复，再复测受影响项；原始失败保留。

已修复：pm_read_memory 将非法路径、缺失文件和分页范围错误转换为官方ToolException；pm_read_bookmark 对未知/外项目来源和分页错误同样返回可恢复工具错误。仍由官方handle_tool_error产生status=error。已验证7项确定性官方Agent链路用例：六种输入错误后能正确读取合法文件，外项目标记未泄漏；数据库连接类故障仍终止Run，不吞基础设施异常。16项无关测试未重跑。准备只复测真实首轮中断的三项。

已验证真实修复后复测：path_traversal、negative_page、foreign_bookmark三项均完成，指定工具真实返回status=error，不再中断；负分页错误后project_read成功，跨项目错误后project_context成功。原始六项失败/成功保留previous_attempt，不重跑前三项已通过命令。按实际ToolMessage逐项核验六类边界全部符合预期，模型文字不计作通过证据。22个免费模型响应，未调用其它付费模型。
Checkpoint：本轮PM边界命令测试和已触发的读取恢复修复完成。生产修改仅在记忆中间件middleware.py，补充7项定向回归；复用tests/run_pm_agent_readonly.py扩展边界模式，证据pm-agent-boundaries-20261015.json记录首轮6项与修复后3项，最终status=passed。项目/资料未变、无后台Run、工具白名单和外项目标记不泄漏核对通过；没有启动服务、数据库或ACP，不宣称全生产部署或所有边界都覆盖。相关文档已同步，无提交或推送。

证据核对补充：项目状态中间件直接返回的ToolMessage.name为空，核对器原先只按name匹配导致write_denied误报。已依据官方tool_call_id关联原始请求/结果，确认project_command真实status=error且内容明确写入禁用；未重新调用模型。最终六项核对通过，证据status=passed；原始消息未改写。


## 搜索工具相邻边界修复（2026-10-15）

用户要求检查是否还有错误并修复。前轮读取错误已修复，不重跑六类真实模型命令。相邻检查发现memory_search空白query或limit越界仍抛ValueError，不能复用工具错误恢复。范围仅补齐搜索输入错误，沿用官方ToolException/handle_tool_error；缺少宿主scope、数据库等运行故障仍明确失败。验收：空白query、limit下界/上界错误后PM可继续有效搜索，搜索连接故障不被吞掉。状态：复现中。

已复现：新增空白关键词官方Agent链路用例修复前失败，ValueError(query must be non-empty)中断tools节点。生产修改仅将两个输入检查转换为ToolException，并启用memory_search.handle_tool_error；不改变合法查询、检索范围或基础设施错误。准备定向验证4项新用例。

已验证：4项新增官方Agent链路测试通过，分别覆盖空白query、limit=0、limit=21后的正确搜索恢复，以及检索后端ConnectionError继续传播；23项不相关用例未重跑。相关Ruff与diff检查通过，没有调用真实模型或业务数据库。
Checkpoint：本轮发现并修复memory_search输入错误中断问题，沿用既有官方错误机制，中文说明同步。前轮六类真实PM命令的原始证据未改写，没有重复评测免费模型或重复全量测试。修改集中在memory中间件middleware.py、既有集成测试、README和本进度记录，保留此前无关修改；未启动服务或周期任务，无提交或推送。仍不宣称不存在其它未覆盖错误。


## PM 接管整个项目的就绪差距评估（2026-10-15）

范围：用户假设更换强模型，询问当前还欠缺什么。本轮只读代码、已有验证与Notion《PM Agent v2》设计，不运行新测试、不修改业务实现。
已确认配置：ACP_SUBAGENTS_JSON与ACP_WORKERS_JSON均为0项，当前本地配置/环境无持久Store URI（未输出敏感值）。Worker桥接、状态门禁、人工请求、记忆组件已存在，不应重新造同类底座。
已确认接入缺口：默认pm_agent无facts_provider，无法形成可信passed/approved；spawn关联采用宿主config/task_name，业务任务执行/新制品引用没有完整写回命令；TaskBrief目前只要求引用，未提供统一制品创建读取闭环；项目工具仅创建/更新项目、创建任务、转换状态，没有任务元数据调整和完整replan命令；PM场景Skills存在但默认工厂没有加载中间件。
已确认运行/验证缺口：需要配置Worker与最小权限执行环境、官方持久Server与项目单写入入口、Worker完成/失败及人工回复触发PM后续Run；当前官方Store持久化实测不能代表PM checkpoint与ACP会话在强制终止后可靠恢复。预算/验证策略原语已存在，默认PM/ACP未完整接入项目级限额与可信验证闭环；ACP权限回调不等同OS沙箱。
Checkpoint：评估完成，建议优先贯通一个真实任务的派工→制品→独立验证→可信状态→人工收口，再补重规划、事件驱动恢复、预算隔离和技能加载。完整闭环、故障恢复与长期持续运行仍需真实部署验证；不能用强模型或当前零散成功代替接线和证据。无代码变更、无重复测试或服务启动。


## 真实PM单任务闭环测试（2026-10-15）

用户授权开始上一轮建议的完整链路测试。本阶段使用真实免费PM、真实Claude ACP、官方Agent Server和独立PostgreSQL测试库；Worker仅只读当前源码副本。测试宿主提供真实TaskBrief文件及单任务task_id绑定，明确不冒充PM自动制品创建/多任务映射已完成。
阶段：PM创建项目和任务、推进状态并spawn；Worker实际读取与返回；官方Server正常停止再重启；PM领取旧Run结果；尝试进入可信验收并确认默认facts_provider门禁。没有模拟验收或人工批准。预计可验证派工/结果/恢复和门禁，完整DONE可能被当前接入缺口阻塞。只生成临时配置，不修改.env和生产图。状态：准备执行。

启动器首轮在创建子进程前失败：WindowsSelectorEventLoop不支持create_subprocess_exec，尚无PM/Worker运行。测试启动器改回默认Proactor，结果读取复用官方同步PostgresStore的异步接口，与已验证Server一致；原失败保存previous_attempt，不修改产品实现。

测试Server启动配置修正：当前版本store.path的模块形式使用module.attribute，不能使用图入口的module:attribute形式。按实际加载器错误改正测试配置；仍未产生PM或Worker Run，原启动失败保留，不影响已验证的生产文件路径配置。

已验证第一阶段：真实PM Run 01a13eb1-f024-70a0-ba8a-2f2f052eb42b 成功，模型实际创建项目/任务并调用spawn_agent；Worker线程01a13eb2-c221-7d11-8aa9-6137aa809622，Run 01a13eb2-c222-7b60-a36a-8024ad859d63后台执行。官方父checkpoint完整保存到测试临时目录parent-after-delegate.json，仍待核对Worker实际读取、重启和验收阶段。

已验证：真实Claude Worker Run 01a13eb2-c222-7b60-a36a-8024ad859d63 成功，原生读取工具completed事件存在。官方Server正常停止/重启后，父线程async_tasks与消息数量一致，均restored=true；不是强制终止恢复。PM开始Run 01a13eb3-7a76-7321-89c3-3f4607a09a59，通过官方wait_agent领取旧Worker结果。待核对实际返回内容及验收门禁。

已验证领取与门禁：重启后collect Run 01a13eb3-7a76-7321-89c3-3f4607a09a59成功领取旧Worker结果，含原生相同Run引用与FLOW标记；acceptance Run 01a13eb4-258b-7651-88ff-3e561a3ff319正常结束，真实passed请求被项目策略拒绝，独立PostgresStore读回任务状态review，未伪造通过或人工批准。测试服务已正常停止且端口关闭。
新缺口经实际数据确认：Task.execution_refs=[]，Worker Run尚未写回业务任务；模型创建的TaskBrief引用不指向实际文件，而Worker靠提示词中的正确相对路径完成读取。说明TaskBrief仅非空验证，交付引用尚未闭环。Worker/PM声称无facts_provider可短路通过的说法被实际门禁反驳，保留原文并标注误判。
测试预期修正：当前策略中dev_complete可分别进入review或testing，两者不是串行；本次测试指令误要求review→testing，拒绝为正确行为。已修正后续测试脚本路径为POSIX、指令走review→passed，未重跑，也未修改生产状态策略。原始结果保留review状态，不改写成testing。

最终人工核对：11项链路/保护检查全通过，状态记为partial_closed_loop_blocked，不能将“门禁拒绝符合预期”冒充项目完成。真实路径为PM创建/推进/派工→Claude读取3个文件→正常停止重启→wait领取同一Run→passed被拒→PG状态review。任务execution_refs未写回、TaskBrief引用无效和可信facts_provider未接入均记为缺口。官方ToolMessage按tool_call_id关联，原文与原生事件摘要保存在单独证据JSON。
Checkpoint：真实PM单任务链路测试已完成，证据docs/progress/evidence/pm-project-flow-20261015.json，测试入口tests/run_pm_project_flow.py。仅增加测试/进度/忽略例外，生产代码和.env未修改，源码与只读副本hash均未变。测试启动器的两次基础设施错误如实保留previous_attempt，真实PM链路没有反复换路由重跑。测试Server正常停止，端口已关闭，所有本轮执行进程结束。下一步应先补任务→Run/交付物关联及TaskBrief有效性，再接可信验收回调，之后才测试完整PASSED/APPROVED/DONE；不重跑已通过的单独记忆与ACP套件。


## 修复任务、交付物与可信验收闭环（2026-10-15）

用户授权修复实测三缺口。方案比较：仅校验本机文件路径并让模型填写Run/通过标志，会绑定宿主文件系统且无法证明引用/验收真实性；采用官方Store中的不可变项目制品、官方工具包装钩子关联真实ACP Command返回、宿主专用的版本绑定验收/审批记录。复用现有状态转换与validation_policy，不新增队列/执行状态注册表。
归属：制品与可信证据属于project_state；PM默认装配启用，通用ACP六工具协议保持不变。项目中间件根据task_name绑定业务任务，在运行时复制ToolRuntime.config传入角色/能力/任务说明；只记录官方Run引用及结果来源，不复制Run生命周期。新增制品创建/读取工具与修复任务说明引用命令，模型无验收/审批写入口。
验收：坏/外项目引用拒绝、真实派工关联和幂等结果写回、版本变化使旧验收无效、无依据仍拒绝、宿主独立核验后可passed、明确测试审批后可done。先定向确定性链路，最后只复测受影响真实PM/ACP单任务。状态：实现中。

已验证：首批闭环/原状态/PM装配15项通过；补充模式边界后当前11项闭环专项通过（包含原7项及新边界，不累计成重复覆盖）。覆盖坏引用、跨项目/任务、内容篡改、真实官方ACP工具的SDK替身派工/领取、Run与结果幂等写回、默认角色、宿主配置覆盖防错、可信验收与单独批准、旧版本失效、无效模型参数恢复、坏引用修复、目标变化后DONE拒绝以及可信负面验收撤销旧通过。
代码实现集中project_state/artifacts.py与workflow.py，官方工具包装集成在原middleware.py，PM默认工厂显式装配；通用ACP模块未修改。下一步针对改动复测真实单任务，审批恢复输入明确采用测试夹具，不冒充真实用户业务批准。

真实修复后首轮停在delegate：PM已创建有效Store任务说明并真实spawn，但在此之前多次不合法推进与纠错消耗45个图步骤，随后额外wait触发GraphRecursionError；测试清理关闭仍运行的Worker，Connection closed由停服引起，不计为Worker自身运行缺陷。原始失败证据保留。测试阶段步数调整为120（模型调用总上限仍40），明确列出合法推进步骤，并移除宿主task_id/required_capabilities，复测实际业务绑定；没有放宽生产状态门禁。

已验证追加3项定向边界：续问真实官方工具循环收到更新后的TaskBrief并关联第二个Run；新增能力要求时拒绝续用旧Worker且不发SDK请求；明确撤销批准后验收仍有效、批准事实为false。11项已通过专项未重复执行。项目状态、PM与ACP说明同步当前归属及宿主集成限制。

相邻检查补齐：默认验收回调对不存在task_id原先会StopIteration中断；改为明确ValueError，由已有工具包装返回可纠正错误。新增1项官方Agent循环验证：未知任务passed拒绝后同Run仍能project_read成功（1 passed，其余14项未重跑）。

真实复测第二轮已验证：PM自主创建有效Store制品并真实派工，Worker Run 01a13ed6-dac3-7482-97da-428429abe704成功；默认中间件在PM主动wait时已返回project-artifact交付引用。宿主本轮未提供task_id/required_capabilities。官方Server正常重启后父async_tasks与消息数量一致。继续领取/验收阶段；原始失败保留previous_attempt。

真实报告人工核对未通过：Worker将agent.py:37/236的导入/别名误当默认工厂，遗漏实际311行ProjectStateMiddleware(workflow=ProjectWorkflow(store))，还误称无facts_provider即可passed/approved。PM继续复述此误判。测试宿主原先只查文件名附近数字，不能据此签发可信验收；已主动interrupt当前验收Run，在写任何validation/approval之前停止测试，Server已正常停止。最终仅dev_complete，不冒充完整DONE。测试核验器已改为要求实际接线表达式与真实源码行号；这是修复宿主证据检查，不修改生产门禁、不反复评测免费模型。

最终真实证据复核：从独立PG读回有效TaskBrief、唯一worker_result和实际langgraph-run引用；两份源码原生读取completed、父状态/消息恢复、源码/副本未变、无验收/批准记录、服务停止/端口关闭共11项核对通过。证据status=integration_verified_report_rejected，业务任务dev_complete，未宣称真实验收或DONE。

收尾追加修复：新的不同验收结论先撤销旧批准，避免通过→失败→再次通过复活历史批准；同一验收重复投递保持幂等。新增反复验收专项与受影响负面验收回归2项通过，其余14项未重跑。闭环专项共16项按新增/受影响范围分批验证，另有早先原状态/PM装配回归通过。

Checkpoint：本轮三项产品接入缺口修复完成，代码集中project_state及PM装配；README同步，中文注释，通用ACP六工具保持不变。TaskBrief有效性、业务Run/交付物关联、当前版本可信验收和单独人工批准、撤销/失效、续问能力与未知任务恢复已确定性验证；真实免费PM/ClaudeACP/官方Server/隔离PG验证了制品、派工、交付和正常重启恢复。真实Worker报告误判，宿主主动中止验收，没有签发可信验收或人工批准；完整真实PASSED/APPROVED/DONE仍未验证，认证验收服务和人工界面仍需部署接入。原失败保留，没有为了免费模型质量反复测试；无付费路由、无业务库修改、无提交推送、.env未改，保留无关工作树修改。下一步按部署需求接入可信核验/人工入口，不重跑本轮已通过链路。

测试配置调整（用户要求）：真实PM/记忆测试的四个模型入口输出上限统一为32768（32k）；PM项目闭环测试的Claude ACP子进程也显式设置CLAUDE_CODE_MAX_OUTPUT_TOKENS=32768。免费路由保持openrouter/free。仅做四文件语法检查，不重新执行已通过测试或真实模型调用；供应商实际可输出长度仍受其模型限制。Checkpoint：配置修改完成。

配置统一（用户明确选择真实运行和测试均openrouter/free）：生产get_default_model固定调用共享OpenRouter工厂，32768输出token、120秒超时、SDK重试1次，缺少Key明确失败、不回退其它付费服务。移除四个命令测试的ChatOpenAI独立实例与sys.modules模型替换；记忆图取消额外Command Code分支。测试仅追加追踪回调；ClaudeACP去除测试专用模型别名和输出预算覆盖，沿用本机Claude配置，项目.env统一CLAUDE_CODE_MAX_OUTPUT_TOKENS=32768。数据库和只读副本隔离仍明确保留。状态：静态/无网络配置核对中，不重新启动真实模型测试。

已验证配置统一：无网络实例化生产真实ChatOpenAI，确认get_default_model与get_open_router_model为同一缓存对象、openrouter/free、32768输出、重试1次、ChatCompletions接口；四个测试入口无独立ChatOpenAI或模型模块替换；清空Key后明确ValueError且无其他供应商实例。默认模型的其他供应商SDK改为在显式工厂调用时加载，不要求免费测试安装无关SDK。受影响官方记忆图入口定向回归1项通过（26项未重跑），相关静态与diff检查通过。

Checkpoint：真实默认模型与四个命令测试的模型配置已统一并文档同步；后台记忆复用默认配置，ClaudeACP移除测试专用别名覆盖、继承原生配置，项目输出上限32k。用户选择免费路由已落实，不调用其他付费模型；保留其他提供商显式工厂。只做无网络配置核对和1项图入口回归，没有重新执行真实闭环，未提交或推送；此前真实结果仍代表当时参数，不能冒充32k新配置实测。

模型切换（2026-10-20，用户明确指定）：共享默认模型由openrouter/free改为minimax/minimax-m3.1-flash:free；生产PM、后台记忆及命令测试继续复用同一工厂，测试新证据的模型标记同步更新，历史证据不改写。输出上限32768、超时120秒、重试1次保留。同步当前使用说明；没有运行τ-bench或真实模型调用，进行无网络配置检查。

任务规模核对：τ²原论文Table1的retail/airline/telecom分别115/50/114，合计279；telecom的114来自2285个组合任务，子类型29/36/49。此为原论文口径，不冒充最新修订任务数。Checkpoint：模型切换和当前说明已同步，历史证据保留；本轮未启动τ-bench评测或Claude会话。

## Clef Flash 实际接口检查（2026-11-12）

用户授权使用本机 `.env` 中的 Cloudflare AI 凭据。通过已连接的 Cloudflare 账户确认账号后，调用 `@cf/cloudflare/clef-flash` 官方 Workers AI 结构化接口，使用虚构讨论材料覆盖三类结果：明确一致返回 `pass`，实质分歧返回 `reject`，信息不足返回 `unknown`；接口返回选项概率。测试没有读取或发送真实项目内容。密钥未写入代码和日志；本机 `.env` 补充了账号 ID，现有 `CLOUDFLARE_AI_API_KEY` 可作为兼容配置读取。

接口说明见 [Cloudflare Clef Flash 文档](https://developers.cloudflare.com/workers-ai/models/clef-flash/)。

该结果只验证这三次接口交互，不证明门禁、只读隔离、项目流程或最终验收闭环已经完成。Clef 的选择也不等于技术方案正确或交付已经验收。

## 只读协作环境现状

按照 [Claude 官方沙箱文档](https://code.claude.com/docs/en/sandboxing)，原生 Windows 不支持 Claude 的命令沙箱，且该沙箱不覆盖内置文件工具、MCP 和 hooks。不能仅凭 ACP 权限回调或 `plan` 模式认定进程只读。

本机 Ubuntu WSL 的 bubblewrap 在临时目录探针中阻止了对 Windows 项目目录挂载的直接写入和重新挂载；隔离目录已准备 Linux Node 与 Claude ACP。这只验证了局部文件系统行为，尚未验证网络出口、所有进程与工具、跨进程会话恢复或只读到开发的安全切换。商讨入口继续关闭，不将探针结果标记为完整的只读能力。后续完整验证按用户要求交给子代理统一执行。

独立 Codex 审查方面，OpenAI 官方文档说明 [Windows 沙箱](https://learn.chatgpt.com/docs/windows/windows-sandbox)有不同实现及网络限制；其命令沙箱能力不能自动证明 ACP 适配器、本机 MCP、会话配置和全部子进程都处于预期限制。审查入口在验证有效隔离前保持关闭。

## 当前开发检查

执行续问现在由宿主构造，并将完整消息摘要保存在派工意图中，实际发送边界会重新核对；自由文本不能附加到已通过的执行授权。原范围返工使用当前有效的不通过审查记录，目标或方案变化仍需重新商讨。Clef 输入包含任务简报，超出接口大小限制时仍返回未知。新项目绑定拒绝与已有项目形成父子目录重叠，目录注册操作使用跨进程共享锁。Worker 运行环境与项目目录不能重叠。

开发进程启动或停止结果不明时保留隔离记录；商讨和审查只允许将已知会话中核对到的同阶段 Run 恢复为对应阶段，无法确定会话或来源时保留记录供人工核对。在途自由消息仍被阻止，尚未实现版本化的在途消息。

工作台新增首次会话创建中断时的候选会话编号输入；仅当意图已绑定的官方 Run 与 checkpoint 中项目、阶段、父会话及消息摘要一致时允许关联。无法核对时继续保持未知，不自动重派。该恢复改动尚待最终统一验证。

关联已核验 Run 后，PM 可调用 `recover_dispatched_session` 将官方会话引用恢复到当前线程，再通过 `wait_agent` 领取结果；恢复不创建 Run，来源、角色、目录和消息摘要不匹配时拒绝。此路径尚未经过统一测试和复审。

工作台从任务关联的官方 Run 来源显示商讨轮次，而不再要求先存在 Clef 判断；未领取的结果与未纳入当前判断的轮次单独标记。该展示调整尚待最终统一验证。

工作台的项目切换使用页面代次隔离历史、流式回复、任务/商讨/用量、人工验收和故障核对的异步结果；旧请求返回时不再写入新项目页面。选择待绑定目录时也固定发起时的项目 ID，切换项目后不对新项目执行旧绑定。上述界面竞态修复尚待子代理统一验证。

切换项目时工作台从官方线程 checkpoint 恢复该会话的模型和思考等级选择；历史读取失败或选择记录损坏时阻止发送，避免沿用其他项目的界面选择。不可用的已有模型或等级会保留并标记，不自动切换。旧 EasyCLI 配置可通过显式选项使用。该界面与模型兼容改动尚待统一验证。

独立审查在临时 WSL 目录中发现原先的网络命名空间预检无法启动；调整初始化和系统调用过滤后，预检以及部分 socket、clone、unshare 对抗探针通过。这些只是局部验证，项目路径替换和硬链接扫描存在竞态，真实 ACP 会话切换、进程终止和完整安全边界尚未验证；WSL 协作开关继续保持关闭。

独立复审使用临时项目、临时应用数据、模拟 ACP 与 Store；首批相关测试经修复后为 47 项通过，旧版 WorkerRef 兼容及只读意图定向测试另行通过。浏览器用模拟服务验证两个项目的对话隔离，独立进程验证项目锁；这些不能替代真实 Agent Server 和 Worker 的完整闭环。监督器副本迁出宿主项目，工作台拒绝与宿主、应用数据和其他项目重叠的目录；项目目录扫描对链接、硬链接和大型目录有限制。真实 Worker/独立 Codex 的身份配置、实际只读到开发切换、崩溃恢复与对抗隔离仍需验证。

后续静态检查发现 `wait_agent` 原本在等待远端 Run 时持有项目写锁，而远端 Run 的启动和退出也需要该锁；现已将等待移出锁，仅在关联结果时持锁。工作台对已知模型配置错误提供明确提示；上述后续改动尚未由子代理验证。目录竞态的最终解决方式仍在等待用户对直接使用项目目录与隔离副本的选择。

工作台增加 InMemoryStore 状态提示：当官方 Store 是临时内存时，明确告知项目状态和模型设置会在服务重启后丢失；其他 Store 仅报告已配置，不据此断言持久性。该变更尚待最终统一验证。

PM 专用 ACP 提示与固定提示中删除了“满足依赖即可直接派工”等旧指令，改为先商讨、确认并判断后再续接开发；普通 ACP 使用原有通用提示。该提示词调整尚待最终统一验证。

后台记忆图在每次处理时读取已配置的默认模型；配置存在但无效时不回退。PM 对话压缩在未显式指定独立压缩模型时使用当前会话选择或配置的默认模型；未配置时保留现有兼容默认值。这些模型选择改动尚待子代理统一验证。

复审发现会话压缩前的记忆交接原先仍读取全局默认模型。现将会话发起的记忆来源绑定到模型选择与服务地址摘要，后台处理时复核；服务地址变化则拒绝，不把会话资料静默发送到新地址。定期整理没有会话来源时继续使用已配置默认模型。该修正尚待静态复审和统一测试。

工作台模型选择增加显式“现有 EasyCLI 配置”选项，以保留原有部署配置；它与工作台默认模型和自选模型互斥，不作为模型加载失败时的静默回退。对应上下文压缩也沿用明确选择的配置。尚待最终统一测试和复审。

模型目录对已保存的地址在发现模型及实际创建模型前重新校验，拒绝无效 URL、内嵌凭据以及非本机 HTTP，避免只依赖初次保存时的检查；损坏的模型记录明确报错。此调整尚待统一验证。

新保存的模型凭据按服务编号和规范化地址绑定；若地址变更后配置写入失败，旧地址不会读取新地址的凭据。旧格式记录仍按原编号兼容读取；工作台模型配置写入使用本机跨进程锁。该兼容与故障路径尚待统一测试和复审。

空或损坏的模型选择记录不再被当成“未配置”并静默回退，PM 调用、对话压缩和后台记忆均会明确报错；没有记录时仍保留显式的兼容路径。尚待统一验证。

对照 [LangGraph CLI 文档](https://langchain-ai.github.io/langgraph/concepts/langgraph_cli/)和本机 Agent Server 代码：现有 `langgraph.json` 的 `checkpointer` 使用 `type`/`connection_string`，不等同于官方自定义 checkpointer 的 `path` 配置。本机解析缺少 `backend`/`path` 时选择默认运行时后端，不能由该旧配置声称 checkpoint 使用 PostgreSQL。保留用户原配置；工作台检测到 `local_dev` 时提示异常退出后的会话恢复不受保证。Store 的 PostgreSQL 配置是独立机制，不能据此推断 Run/checkpoint 的持久性。该提示尚待最终统一验证。

工作台新建 PM 线程使用官方 SDK 支持的指定 UUID 和 `if_exists=do_nothing`，并核验项目归属，减少创建响应丢失时产生不可追踪线程的风险；已关联线程缺失或不可读取时报告未知，不自动替换。旧线程绑定仍兼容。该更改尚待独立测试，不能代替官方服务的持久化或故障恢复验证。

新建线程的归属标记现在会在后续读取和发送前复核；历史无标记的绑定继续兼容，但如果官方线程声称属于其他项目则拒绝。读取历史时不会因并发丢失绑定而创建新线程。该调整尚待统一验证。

后台记忆来源进一步绑定会话模型和服务地址；来源缺失、混合或内容哈希不一致时阻止自动发送。普通记忆、压缩交接及定期整理使用经过来源检查的内存快照，提交时检查项目状态及目标文件，并在项目写锁内更新索引；无法恢复模型时仍记录不可用的最新观测，避免后台任务忽略新输入。写回与索引不是跨 Store 的原子事务，中途失败可能需要人工检查；这些改动正在静态复审，尚待统一测试。

记忆提交新增 pending 记录，工作台可核对并仅在全部目标文件及索引可证明保持原样时恢复；已写入、部分结果和旧格式不自动解除阻断。PM 的主要记忆读取和写入使用同一本机项目锁，调查 Agent 使用当前会话模型及相同读取保护。底层 `project_memory_filesystem_middleware` 是独立的低级装配接口，不应与 PM 记忆写入共用 Store；上述恢复及锁行为尚待独立统一测试，不代表跨系统原子事务。

独立静态复审确认，项目内硬链接还可能让只读商讨读取项目外的同卷文件；执行前扫描不能消除扫描后目录或链接变化。等待 bwrap 主进程退出也不能直接证明全部 WSL 后代进程和异步 I/O 已结束。当前保持真实 Worker 开关关闭，真实目录模式与隔离副本的选择仍待用户决定。此前的 Clef 真实接口检查使用虚构材料，并不替代端到端测试。
