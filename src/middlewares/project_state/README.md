# 项目状态中间件

入口：`from src.middlewares.project_state import ProjectStateMiddleware`。
按照官方 `AgentMiddleware.tools` 注册工具，`awrap_model_call` 注入最新项目快照；`awrap_tool_call` 将规则拒绝返回为可修正的工具错误。
任何使用官方 `create_agent` 的 Agent 都可装配；其他图可以复用纯领域命令和 ProjectStore。

## 文件职责

- `middleware.py`：官方生命周期入口，实时上下文、串行写约束与 ACP 工具的业务关联。
- `artifacts.py`：官方 Store 中的不可变制品，内容散列引用、项目/任务隔离与完整性校验。
- `workflow.py`：任务与真实 Run/交付物关联、当前版本的可信验收及人工批准记录。
- `tools.py`：隐藏 ToolRuntime 绑定项目范围；读取、命令工具及可信验收/审批回调。
- `context.py`：最多 8000 字符的当前项目视图；完整任务使用 project_read。
- `models.py`：不可变 ProjectSnapshot、Task 与 TaskStatus。
- `commands.py`、`service.py`：类型化创建/转换命令及纯函数 reducer。
- `transitions.py`：依赖存在性、环检查、状态转换和验收/审批门禁。
- `persistence.py`：官方 BaseStore 保存、加载、列表；原命名空间和 schema_version=1 保持兼容。
- `runtime.py`：通过官方 get_store 获取图运行时的 Store。
- `__init__.py`：公共领域接口，按需加载 Agent 中间件。

## 注入能力

- `project_context()`：当前项目的目标、约束、活动任务、阻塞、验收状态摘要。
- `project_read()`：完整项目快照；传入 `task_id` 只读取该任务和状态诊断。
- `project_command(action, ...)`：create_project、update_project（目标/约束）、create_task、update_task_brief、update_task_dependencies、transition_task。
  新任务固定从 backlog 开始，必须带 TaskBrief 引用与验收标准；禁止跳过验收/人工审批直接标完成。

project_id 和 Store 从官方 runtime/config 注入，不是模型参数。
实时项目上下文由本中间件唯一注入，PM 记忆只负责历史召回、整理和压缩交接；记忆调查流程可以调用这里的 project_context。

## 装配

```python
from langchain.agents import create_agent
from src.middlewares.project_state import ProjectStateMiddleware

graph = create_agent(model, middleware=[ProjectStateMiddleware()], store=store)
result = await graph.ainvoke(
    {"messages": [("user", "读取当前项目")]},
    {"configurable": {"project_id": "my-project"}},
)
```

PM 默认入口使用 `ProjectStateMiddleware(workflow=ProjectWorkflow(store), pm_discussion_gate=True)`，与图共享同一 Store。
独立装配 `ProjectStateMiddleware()` 仍兼容原有用法；此时不提供制品与 ACP 业务关联工具，passed/approved 需要宿主显式的可信 facts_provider。

## PM 交付与验收闭环

PM 启用商讨门禁时，按以下顺序操作：

1. 创建项目；调用 `project_create_artifact(task_id, content, kind="task_brief")` 保存完整任务说明。
2. 使用返回的 `project-artifact://...` 作为 create_task 的 task_brief_ref；路径、自造引用、外项目/外任务制品均拒绝。
3. 依赖满足后通过 `begin_discussion` 与负责人商讨；领取实际讨论结果，PM 确认后调用 `assess_discussion`。只有当前输入版本的 Clef 通过结果才允许开发。
4. 使用 `followup_task` 在已商讨的负责人会话开发；宿主注入已确认的任务材料，确认官方 Run 后关联任务。结果不明时先核对，不能盲目重派。
5. `wait_agent` 领取开发结果；进入审查后通过 `begin_codex_review` 发起独立 Codex 会话，再领取其审查结果。原范围返工可使用当前不通过审查的发现；实质变化须重新商讨和判断。
6. 独立审查通过后，可信宿主逐条记录验收；人工批准单独记录。只有对应记录有效时才能推进 passed、approved 或 done。

非 PM 场景若未启用商讨门禁，仍可使用通用 ACP `spawn_agent`；这不适用于上述 PM 流程。PM Worker 在本机以原生 ACP 进程运行，商讨阶段的边界是适配器自身的权限模式加宿主 grants 与审批回调；用户也可以选择可写模式，此时记录会标明边界不保证只读，安全依赖双方确认与 Clef 门禁。

`project_read_artifact(reference, offset, limit)` 按字符分页读取制品。任务说明有误时，先创建新说明，再调用 `project_command(action="update_task_brief", task_id=..., task_brief_ref=...)`；历史交付保留，approved/done 任务不能修改说明。

验收接口参数为 `project_id, task_id, validator_id, criteria, evidence_refs`，后面三项为关键字参数；criteria 必须与任务验收标准逐条一致，evidence_refs 必须包含每个子代理在当前输入下最新成功 Run 的交付制品。校验复用既有 validation_policy；验收身份不能等于执行者。
人工接口关键字参数为 `actor_id, decision_ref, approved=True`，允许显式撤销批准。两接口仅供可信宿主调用，没有注册为模型工具；认证、核验逻辑与人工界面需部署方接入，不从模型文字推断批准。

验收记录绑定目标、约束、任务说明、任务要求、运行与交付物版本。新输入/新 Run/新交付物使旧验收失效；返工或失败撤销当前验收/批准指针；新的负面验收覆盖旧通过；验收结论改变时先撤销旧人工批准，再次通过也必须重新批准。DONE 同样复查当前验收与批准。续问注入最新说明，角色改变或能力要求增加时须重新派工。

此层保存业务证据，Run 生命周期、排队、checkpoint 仍由官方 Server 管理。官方 Run 创建与 Store 关联不是跨系统原子事务；基础设施故障应检查官方 Run 后恢复关联，不能声称任意故障下恰好执行一次。旧的未关联会话不会自动归入业务任务。

## 写入与并发边界

默认只读。宿主必须设置 `config.configurable.project_state_single_writer=True` 并保证项目写入遵循该约定。项目中间件对支持的本机写入口使用跨进程文件锁；共享 Store 的本机服务还必须使用相同的 `LOOPMINDER_DATA_DIR` 或应用数据目录。锁不是分布式事务，也不能约束绕过这些入口的其他写入者。
同一模型回复中的多个独立 `project_create_artifact` 可以并行：它们按内容哈希写入独立制品键，不读改写项目快照，相同正文/归属返回相同引用。此入口不会自动将引用附加到任务，任务绑定仍由后续命令完成。

`project_command` 和启用 workflow 后的 spawn/followup/wait 会修改项目快照。不同业务任务、不同别名的纯 `spawn_agent` 批次由中间件串行处理；该批次不承诺全有或全无，单项失败时保留已确认的官方 Run，不可整批盲目重试。其他多快照写、同业务任务重复派工或与制品创建混批仍整批拒绝，返回 `parallel_project_writes`。先创建说明、取得真实引用，再创建任务；不能同批创建项目和该项目的制品。

启用 PM 商讨门禁时，先通过 `begin_discussion` 和 Clef 建立当前版本的授权；开发续问使用固定指令，宿主交接当前授权材料并核验完整消息摘要。审查返工使用已记录的独立审查发现。在途自由消息被阻断。Worker 不继承 PM 的项目、记忆或制品工具，通过会话返回成果；`wait_agent` 领取时保存并关联。商讨阶段的边界是适配器的权限模式与宿主审批，不是操作系统级只读；可写模式会在记录中标明缺口。

宿主同时展开简报直接引用的同项目制品正文（最多12份、正文合计40000字符，不递归）；无法解析或被截断的材料明确要求向PM索取，不猜路径或查宿主接口。旧会话来源记录没有 `worker_context_version` 时，在下一次续接补齐一次新交接格式，之后同输入不重复全文。原不可变简报中的错引用不自动纠正，交由PM处理；宿主内部记录不因简报提及就成为Worker授权范围。

续问message新增的制品引用同样由宿主展开。当前工具边界放在派工/续问消息末尾，明确旧简报中的PM工具、宿主仓库访问要求不生效，负责人自主安排技术细节。PM每轮获得Store计算的验收记录存在性、当前版本验收和批准事实，不能将“尚未记录”解释为“宿主拒绝”。混批拒绝也带宿主可恢复标记，整批不执行，PM可随后逐次重发；执行异常不因该规则被豁免。

删除依赖还须检查前置任务：有执行来源且尚未done的边属于 `protected_dependencies`，PM不能以“只差验收账本”为由删除；新派工及同会话续问均检查依赖是否完成。宿主纠正旧漏洞误删时，可使用不暴露给模型的 `RestoreTaskDependenciesCommand`，仅增加依赖、附加历史证据，保留状态、运行和制品历史，并继续做DAG校验。该命令不授予验收或批准，也不清理已经产生的产品文件。

并行相同制品的正文/引用保持一致；BaseStore不提供通用put-if-absent，创建时间等非哈希元数据不承诺跨进程精确一次写入，不把本地并行验证当作分布式事务保证。

BaseStore 不提供通用原子 CAS，单纯 aget/aput 不具备跨线程并发更新保护。本包没有自造进程锁、队列或执行注册表；不能在多个线程都启用写入。
工具更新目标/约束时保留任务；禁止创建覆盖现有项目。仅支持明确的说明和未执行依赖修正，不提供删除任务或任意改写验收标准的工具。
官方 Store 的数据库实现决定持久化能力；InMemoryStore 只用于本进程，测试不代表跨进程持久化。
当前中间件工具/钩子采用异步接口，调用使用 ainvoke/astream。

参考：[官方中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[官方工具与 ToolRuntime](https://docs.langchain.com/oss/python/langchain/tools)。

## 状态诊断与错误恢复

新任务编号与 ACP 别名使用相同字符约束：字母开头，字母/数字/下划线/连字符，最长64字符，区分大小写；不修改历史存储编号。task_name 是 Worker 别名，task_id 是业务归属；例如 `spawn_agent(task_name="T0_review", task_id="T0", agent_type="reviewer", message="独立核验交付")`。同任务可关联不同角色的多个独立会话。

任务读取和修改返回 controls，包括 current_status、allowed_transitions、gated_transitions、blocking_dependencies、allowed_worker_roles 和 dependency_editable。写操作仅返回修改后的任务或项目概要，不再每一步重复全部任务；需要完整快照时使用 project_read。通过官方后台通知看到成功后，仍须 wait_agent 领取交付制品；通知不会自动写入项目验收。

状态拒绝返回明确 code/reason/next_action：dependencies_blocked 要核对前置依赖；validation_required 需要宿主可信验收；approval_required 需要认证人工批准；invalid_task_transition 要依照 controls 修正。错误依赖仅在尚未执行的 backlog/ready 任务上用 update_task_dependencies 修正，修改后重回 backlog 并重新检查未知依赖和环；真正的前置依赖仍必须等到 done，不得为了推进而删除。review 与 testing 可以互转，均不能绕过可信验收。

只有 `ProjectStateError` 类型的业务拒绝由中间件在 ToolMessage.additional_kwargs 标记为可恢复。严格轨迹保留红色事件并把响应交回PM，不生成首错文件；普通ValueError、来源记录失败、未知工具错误和基础设施异常不受豁免。缺少验收/批准/完成证据时，next_action 指向现有 `request_human_decision` 官方HITL入口，携带任务、逐条条件和已领取证据，请求可信宿主处理。宿主独立核验后调用 `record_validation`，认证人工批准另走 `record_approval`；人工文字回复不自动生成这两种记录。测试观察入口将待处理官方中断保存为 `waiting_for_host`，不把Run成功当成验收或项目交付。

相同普通状态重复提交是幂等的；重复 passed/approved/done 仍检查可信依据。外部调用已返回后发生业务来源关联错误，会抛出明确异常并要求停止核对官方 Run，不能作为普通参数错误盲目重试。官方 Run 和业务 Store 不具备跨系统事务，人工核对与恢复仍可能需要。

恢复时的失败证据：同一官方Run取消收尾后，API先观测interrupted、官方dev持久化恢复后呈现底层error时，只允许无交付正文、无checkpoint/stop_reason且错误码分别为run_interrupted/run_error的失败观察切换。两份不可变制品都保留，旧引用记录在previous_result_refs及任务证据中；不会把失败提升为成功，也不接受成功交付正文变更或普通同状态结果漂移。
