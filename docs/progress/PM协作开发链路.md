# PM 协作开发链路：实施进度

对应需求：[需求文档](../../../re_build/docs/progress/需求文档.md) 第 0 条（商讨模式）、第 2 条（文件夹建项目）、第 3 条（切换模型）、第 4 条（从 API 获取模型）。

本文记录**当前代码分支 `pm-continue` 的实际状态**，不是设计意图。凡是未做、未验证、只在测试里成立的，都单列在"未完成与限制"一节。

实施过程中撞上的坑（现象 → 根因 → 解决 → 佐证）单列在[踩坑记录](踩坑记录.md)。

---

## 一、结论先行

| 项目 | 状态 | 证据等级 |
|---|---|---|
| 商讨模式可配置（按项目、按提供方） | 已实现 | 真实测试 + 端到端拼接测试；无真实适配器实跑 |
| 适配器回报的模式进入业务记录 | 已实现 | 图级测试 + 变异验证 |
| Worker 从 WSL 改为 Windows 原生 | 已实现 | 单元测试；未真实拉起适配器 |
| 商讨结论绑定项目内容指纹 | 已实现 | 真实测试（含失效场景） |
| ACP 破坏性操作默认拒绝审批 | 已实现 | 真实测试（含策略层） |
| 项目目录指纹支持普通仓库 | 已实现 | 真实测试（硬链接、符号链接、依赖目录） |
| 独立 Clef 开发门禁 | 保持独立，未改动 | 真实测试 |
| 真实端到端商讨→开发→审查 | **已跑通一轮**（2026-11-13，计算器任务） | 真实 Claude Code + Codex 适配器、真实模型 |
| 提交 | **未提交** | 工作树 30 项改动 |

本轮完整测试：**342 项通过**（`conda activate python312`，Python 3.12.0），另有 2 项
`test_pm_memory_integration.py` 失败属既有测试桩与实现签名不同步，与本次改动无关
（`HEAD` 中即为同一写法）。

---

## 二、角色与链路

需求定义的链路，当前代码的落地位置：

```
用户 → PM（判断与协调）
        ↓ begin_discussion（宿主建只读连接、设模式、派工）
     开发负责人 Claude Code（讨论方案，不实施）
        ↓ assess_discussion（PM 提交结论 + 记录结果）
     Clef（@cf/cloudflare/clef-flash）判断是否通过
        ↓ require_pass（开发前再校验指纹与授权）
     负责人开发（续接同一会话，切换为可写）
        ↓ begin_codex_review
     独立 Codex 会话（只读审查）
```

PM 是判断与协调者，不代替负责人写代码；负责人不接受命令式指令。这两点在系统提示词与 `begin_discussion` 的消息模板里都有约束。

---

## 三、商讨模式（需求第 0 条的核心）

### 3.1 三件不同的事，分三个地方记录

这是本轮返工最多的地方，因为最容易混淆：

| 概念 | 存放位置 | 谁写入 | 能否被后续修改覆盖 |
|---|---|---|---|
| **目标模式**（用户选的） | `("deep_loopminder", "project_worker_modes")`，键 `project_id/provider_id` | 工作台保存 | 改默认值不影响历史商讨 |
| **请求的模式 + 适配器回报** | 派工意图 → 执行关联 → Clef 决策记录，字段 `mode_record` | 宿主在派工时写入目标值，图执行后补写回报 | 目标值保持派工时的值 |
| **本次 Run 的回报快照** | `("deep_loopminder", "project_run_modes", project_id)`，键 `run_id` | 图在发送进程中写 | 只供 PM 读回，不是权威源 |

三者绝不合并成同一个字段。修改项目默认值只影响**下一次**商讨。

### 3.2 只认适配器快照，不认"请求成功"

`ACPWorker.apply_mode()` 的关键点：

```python
changed = await self._call(
    self.connection.set_config_option(session_id=ref.session_id, config_id="mode", value=mode_id)
)
confirmed = self._mode_option(changed)
reported = getattr(confirmed, "current_value", None) if confirmed is not None else None
return {"requested_mode": mode_id, "reported_mode": reported,
        "reported_mode_source": "config_option_snapshot" if reported else None,
        "confirmed_at": datetime.now(UTC).isoformat(),
        "matched": reported == mode_id}
```

- `set_session_mode` 返回空响应，**不构成确认**，不写本地状态。
- `current_mode_update` 事件单独记到 `_mode_events`，**不参与确认、不覆盖快照**。适配器（Claude 0.94.0）在配置切换、权限回调、进入 plan 三处都会发这个事件，事件本身不带请求标识，凭到达时间认定它确认了某次请求就是误判。
- 只有 `set_config("mode", …)` 响应里的 `config_options` 快照中 `currentValue` 才算数。
- 适配器没提供 mode 选项、或提供的列表里没有目标值，直接抛 `UnsupportedControl`，不猜测、不降级。

### 3.3 模式在发送商讨论内容之前设置

**这是用户明确指出的一条。** 模式切换与核对位于 Worker 图 `execute` 节点内、调用 `worker.control(..., "ask", …)` 之前：

```python
mode_record = await _apply_discussion_mode(worker, ref, request)
if mode_record is not None and request.get("pm_governed"):
    run_id = str(config.get("metadata", {}).get("run_id", ""))
    if run_id:
        await get_store().aput(("deep_loopminder", "project_run_modes", ref.project_id), run_id,
                               {**mode_record, ...}, index=False)
```

不匹配时抛 `PermissionError`，Run 不会进入发送阶段——Worker 不可能先按默认模式处理商讨内容。

测试 `test_discussion_mode_is_applied_before_the_prompt_is_sent` 直接断言调用顺序：

```python
assert kinds == ["apply", "ask"]
assert observed[0] == ("apply", "plan")
```

放在 `execute` 而不是 `prepare`，原因写在这里以免以后有人"优化"回去：`prepare` 有一个执行阶段分支会提前返回，且 `execute` 每个 Run 都会重新解析连接与 `ref`。放在发送前一个位置，新会话、续接会话、恢复的旧排队 Run 三种情况都覆盖。

### 3.4 模式选择不能替代开发门禁

- `assess_discussion` 在送 Clef 判断前**剔除** `mode_record`（`worker_discussion` 只保留商讨内容）。
- `require_pass` 不看模式，只看：decision 为 `pass`、输入版本一致、商讨轮次与水位一致、结果引用与登记一致、项目指纹未变。
- 因此"模式设置正确"不产生任何开发授权；反过来，模式切换失败也不会被门禁"兜住"。

### 3.5 建立连接时就带上模式

`begin_discussion` 预检建只读连接时传入项目保存的模式：

```python
saved_mode = await ProjectWorkerModes(runtime.store).get(project_id, decision.provider_id)
saved_mode_id = (saved_mode or {}).get("mode_id")
worker = await acp_server._project_workers.get_readonly(
    decision.provider_id, folder["directory"], mode_id=saved_mode_id
)
```

连接缓存键包含模式：`(provider_id, cwd, version, mode_id)`。否则切换模式后复用的仍是旧连接，边界事实会把可写商讨记成 `ENFORCEABLE mode=plan`——这是第二轮复审抓到的真实缺陷，有变异验证（去掉键里的模式后测试失败）。

---

## 四、只读边界：如实降级

### 4.1 边界是什么，不是什么

**当前边界 = 适配器自身的权限模式 + 宿主 grants + 审批回调。** 不是操作系统级只读挂载。

代码里写得很直白，记录一律按这个措辞：

```python
NATIVE_READONLY_MECHANISM = (
    "native Windows ACP adapter bounded by its own permission mode, "
    "host grants and approval callbacks"
)
READONLY_ISOLATION = "native-readonly"
```

`EnforcementFact.surface` 记为 `native_acp_permission_mode`；`WorkerRef.isolation` 从 `wsl-readonly` 改为 `native-readonly`。

### 4.2 用户可以选可写模式，但不会静默

按用户要求，`READONLY_MODES` 不再是准入闸门：

```python
def mode_can_write(provider_id, mode_id):
    """Unknown modes are treated as writable: an unrecognised value must not be
    presented as safe. This is a warning signal, not a permission gate."""
    return mode_id not in READONLY_MODES.get(provider_id, frozenset())
```

选了可写模式时：

- `_readonly_fact()` 能力记为 `PARTIAL`，`gap` 写明"该模式可以修改项目；商讨安全依赖确认与 Clef 门禁，而非此边界"。
- 记录同时保存 `readonly_capability` 与 `readonly_gap`，恢复路径按记录原样重建，不会把可写升级成只读。
- 工作台在**已保存值**和**下拉选择**两处都显示警示（`modeWriteWarning`）。

不静默拒绝，也不静默允许。

### 4.3 已知白名单

| 提供方 | 视为只读的模式 |
|---|---|
| claude | `plan` |
| codex | `read-only` |

Claude 的 `default` 模式仍会请求编辑与执行，所以**不在**只读集合里。未知模式一律按可写处理。

### 4.4 审批默认拒绝

`risk_analyzer.py` 按 ACP 工具类别判断：

```python
ACP_READ_ONLY_KINDS = frozenset({"read", "search", "think"})
...
if tool_name.startswith("acp."):
    kind = tool_name[len("acp."):]
    if kind in ACP_READ_ONLY_KINDS:
        return RiskLevel.LOW
    return RiskLevel.MEDIUM
```

`delete`、`move`、`fetch`、`switch_mode`、`other` 以及未来新增类别全部需要人工确认。`tool_config` 把某个 `acp.*` 设为 disabled 也不能让它在审批里降级为低危——那是配置绕过默认拒绝。

---

## 五、Windows 原生 Worker

### 5.1 变更内容

新增 `src/middlewares/acp/pool.py`（361 行），`ProjectWorkerPool` 用本机 Node 直接拉起 ACP 适配器：

```python
node = settings.get("node") or "node"
entry = Path(runtime) / "node_modules" / "@agentclientprotocol" / adapter / "dist" / "index.js"
return (node, str(entry))
```

删除 `src/middlewares/acp/wsl_readonly.py`（369 行）。`server.py` 去掉 `_WSL_BOUNDARY_AUDITED` 与 WSL 启动分支。设置字段从 distro 改为可选的 Node 可执行文件路径。

### 5.2 进程生命周期故障关闭

`release_execution` 在找不到连接时不当作"已停止"：

```python
worker = self._execution_connections.get(key)
if worker is None:
    raise ValueError("无法确认 Worker 进程已停止；项目保持隔离等待核对")
```

退出码未知同样报错。失败时**保留**连接条目，便于后续重试关闭，而不是先删后关。

隔离记录带 `ticket` 与 `intent_id`，工作台核对时可据此解除。释放失败分支采用**合并写入**而不是覆盖——覆盖会冲掉这两个字段，导致核对永远 409、项目永久阻断且人工也解不开（第五轮复审抓到的缺陷）。

### 5.3 切换开发时关闭全部只读连接

连接按模式缓存，同一会话可能有多个。`get_execution` 关闭**全部**该会话的只读连接；任一有在途请求就拒绝切换：

```python
if any(lock.locked() for worker in readonly for lock in worker._prompt_locks.values()):
    raise ValueError("负责人还有正在运行的只读请求，不能切换到开发")
```

### 5.4 环境变量继承

`_version()` 把会影响运行中 Worker 的设置与继承的环境变量一起做 sha256 指纹，作为 `config_version`。设置变化后旧会话不能被复用，报"请重启宿主后重新验证"。

---

## 六、项目内容指纹

### 6.1 用途边界

**是变更检测器，不是访问控制。** 代码 docstring 与文档都按这个措辞，不夸大。

### 6.2 绑定点

- `DiscussionGate.evaluate` 写入结论时计算 `project_content_digest`。
- Clef 返回后重新计算，不一致则结论降级为 `unknown`（`discussion_input_changed`）。
- `require_pass` 在开发前再算一次：

```python
if value.get("project_content_digest") != await ProjectFolders(self.store).content_digest(project_id):
    raise ValueError("项目内容在商讨后已变化，需重新商讨")
```

开发不会从与结论不同的项目内容开始。

### 6.3 目录扫描

重写 `_directory_digest` 以支持普通仓库：

- 任何深度跳过：`.git`、`.venv`、`node_modules`、`__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.tox`、`.nox`。
- 仅嵌套层跳过：`dist`、`build`、`.next`、`.nuxt`、`.gradle`、`.idea`、`.vs`（顶层同名目录是真实内容，必须计入）。
- 硬链接按内容哈希、标 `H`，不再直接报错。
- 符号链接只记录目标字符串、**不跟随**（Windows junction 同样读 `os.readlink`）。
- 设备、管道等特殊文件只记录类型、从不打开。

实测此前在 0.01 秒内就因 `.venv` 的 17955 个硬链接失败；现在重复计算结果稳定。

---

## 六之二、PM 与 Worker 的全链路可视化

用户要求：在网页面板与 PM 对话时，既要看到 PM 调用工具的细节，也要看到它调用的
Worker 的细节，一切可追踪。

### 缺口在哪

`TrajectoryRecorder.native()` 早就写好了——它把 ACP 原生通知规范化成轨迹事件，
仪表盘也早就实现了 `kind: "native"` 的完整渲染（工具调用、工具结果、计划、压缩、
用量）。但**生产路径一次都没调用它**：只有测试和一个演示脚本用过。worker 的
通知被推到它自己的 run 流里，然后就没了。

结果就是：轨迹文件里全是 `agent: "pm"`，仪表盘的"角色"筛选器里 `claude` /
`codex` 两个选项永远是空的。用户只能看 PM 那一半。

### 改了什么

`src/middlewares/acp/graph.py`：

- 新增 `project_recorder(project_id)`：按项目打开轨迹记录器。目录解析失败时返回
  `None` 而不是抛错——记录是观察通道，丢记录不能连累 worker 运行。
- `execute` 的 `notification` 回调里，每条 ACP 通知都调 `recorder.native()`，
  `agent` 用 `provider_id`（`claude` / `codex`），并带上 `run_id` 与 `thread_id`。
- 在 `worker.control(...)` 前后写 `turn start` / `turn end` 标记，失败时写
  `turn error`。这样仪表盘能把一轮里的几百条增量通知归成一组，而不是摊平。

`src/middlewares/trajectory/middleware.py`：

- `native()` 增加 `thread_id` 参数。它必须和 turn 标记用同一个值，否则同一个
  worker 的事件会被拆进两个作用域。原来默认取通知里的 `sessionId`，那是适配器
  的会话号，和 worker thread 不是一回事。

### 验证

真实跑了一次商讨（`trace_probe` 项目，Claude Code 作为开发负责人）：

```
agents: {'pm': 55, 'claude': 378}
claude kinds: {'native': 373, 'turn': 2}
turn markers: [('start','13:12:28'), ('end','13:12:54')]
分类: agent_message_chunk 362 / tool_call_update 4 / tool_call 1 / usage_update 3
```

工具调用带完整结构：

```json
{"name":"Find","agent":"claude","span_id":"call_ef35...",
 "data":{"toolCallId":"call_ef35...","title":"Find","kind":"search","status":"pending"}}
```

`/workbench/api/trace/events` 返回 `pm: 37 / claude: 263`，前端按
`[agent, thread_id, run_id]` 分组，三者齐备。

### 还没做的

- ~~Codex 审查侧没有实跑验证~~ **已补验（同日）**：`trace_probe` 项目完整重跑
  商讨 → Clef pass → 开发 → Codex 审查，轨迹里出现 `codex: 154` 条事件
  （turn start/end 成对），审查回执 `pass` 正确登记并可在
  `independent_codex_review` 里读出完整字段。PM/开发/审查三方在同一份轨迹里
  按 `agent` 列分开，仪表盘的角色筛选器终于有数据了。
- 工作台"协作与轨迹"页签是 iframe 嵌入的仪表盘；筛选器、按角色分组、工具入参
  和输出的展开都是既有功能，本次只补数据源，没有改渲染逻辑。

### 补验时新修的两个缺陷

**缺陷 4：`begin_codex_review` 的拒绝原因误导。** reviewer 只能在任务处于
review/testing 状态时派出，但 PM 忘了先转换状态时，错误只说"角色与
owner_role 不一致"，不提缺失的步骤是状态转换——PM 无从修正，审查就静默失败。
现在这种情况会明确报"reviewer/tester 只能在 review 或 testing 状态派出，
当前 X；请先用 transition_task 推进状态"。

**缺陷 5（缺陷 3 的第三处实例）：`codex_review_status` 里还有一处裸
`json.loads(report_result["result"])`**，审查者输出带前言时同样解析失败，
导致已登记的审查回执被降级为 `review_subject_unavailable`。实测修复后
`independent_codex_review` 能完整读出 pass/summary/findings/review_run_id。
审查者输出的解析现已全部收敛到 `parse_review_report`。

官方文档佐证（docs-langchain MCP）：deepagents async-subagents 明确要求本地
`langgraph dev` 加大 worker 池（Python CLI 默认 1，JS 默认 10），
`langgraph dev --n-jobs-per-worker 10` 正是文档给出的命令；worker 通知走
`get_stream_writer` + custom stream mode 也是官方 streaming 文档的标准模式。
与本项目实现一致，未造轮子。

---

## 七、测试

### 7.0 真实端到端运行记录（2026-11-13）

题目：在 `C:\Users\Administrator\Documents\ChatGPT\deep_loopminder\test_project`
建立一个支持加减乘除、处理除零的计算器；先与开发负责人商讨，经 Clef 判断后开发，
完成后由 Codex 独立审查。

参与者全部是真实进程：PM 主模型 `gpt-6-luna`（本地代理），开发负责人为
Claude Code（`claude-agent-acp`），审查者为 Codex（`codex-acp`），两者默认模型
同样是 `gpt-6-luna`。

实际走完的顺序，每一步都有宿主记录可查：

| 阶段 | 证据 | 结果 |
|---|---|---|
| 建任务 | `project_create_artifact` → `project_command create_task` | 任务 `Calculator` |
| 商讨 | `begin_discussion` 返回 `ok:true`，`worker_ref.isolation = native-readonly` | Claude Code 回复方案、范围、4 项风险 |
| 只读边界 | 负责人自述"本轮仅讨论方案，未读取或更改项目内容"；商讨期间目录为空 | 约束生效 |
| Clef | `assess_discussion` → `pass`，`probabilities.pass = 0.6711` | 门禁通过并记录 `decision_id` |
| 开发 | `followup_task` → dev run `success` | 写出 `calculator.py` 与 `test_calculator.py` |
| 开发自测 | 目录中出现 `.pytest_cache`；独立复跑 `pytest` | **4 passed, 4 subtests passed** |
| 审查 | `begin_codex_review` → reviewer run `success` | 判定 **fail** |
| 收尾 | PM 将任务置为 `failed` | 未当作通过，无验收记录 |

两处值得记录的细节：

- **商讨阶段的只读约束是真实生效的。** `begin_discussion` 用 `native-readonly`
  打开连接，负责人无法写目录；商讨完成后目录仍为空，开发阶段才落盘。
- **PM 没有把任何不确定的东西说成通过。** 最终汇报里明确区分"负责人报告的 4 项
  测试通过"是开发方自述、"不能替代独立审查"，并主动指出结构化审查结论仍是
  `unknown`。

### 7.0.1 Codex 判 fail 的原因经核实不成立

审查报告称两个文件的中文提示"显示乱码"。逐项核实：

```
calculator.py: Python script, Unicode text, UTF-8 text executable
bytes: 1052, BOM: False, utf-8 decode OK
ast.parse(...) -> OK
python -m pytest test_calculator.py  ->  4 passed
```

文件本身是合法的 UTF-8、无 BOM、可解析、测试全绿，中文提示
`raise ZeroDivisionError("除数不能为零")` 完好。**乱码出现在审查者自己的读取环节**，
不是交付缺陷。这是审查方的假阳性，PM 如实转述了它的结论而没有推翻它——在缺少
可信反证机制的前提下，这是正确行为。

由此暴露一个真实缺口：**当前没有"审查结论复核"路径**。PM 只能转述审查者判断，
无法把一个可验证的假阳性升级给人工复核，任务就停在 `failed`。需要人工决定是否
补一条"审查异议"通道，还是接受"审查有假阳性时由人重新派工"。

### 7.0.2 本轮修复的缺陷（真实运行才暴露）

**缺陷 1：`task.owner_role` 可写任意文本，写成显示名后任务永久卡死。**
PM 按中文提示选角色，把 `owner_role` 填成 `"开发负责人"`。状态机接受，
但 `begin_discussion` 只认 `"developer"`，于是永远返回 `developer_unavailable`，
且 PM 没有任何更正手段（`update_task_brief` 改不了角色），只能升级人工。
修复：`TaskDraft.owner_role` 与新增的 `update_task_owner_role` 都限定为
`developer / reviewer / tester / researcher`；系统提示词同步说明。

**缺陷 2：`task.required_capabilities` 可写任意文本，业务描述让任务无法路由。**
同一个 PM 把能力填成 `"calculator implementation"`。路由按精确集合匹配
worker 声明的能力，这条要求匹配不到任何负责人，同一个 `developer_unavailable`。
修复：能力限定为 `repo.read / repo.write / files.read / files.write /
shell.execute / test.execute / web.search`，并新增 `update_task_capabilities`。

两个缺陷源头相同：**工具把路由键当成了自由文本展示字段**，模型按提示词里的中文
自然填写，而校验只在路由那一刻才发生，错误已经写进 Store。现在两处都在写入时
校验，PM 收到的是可修正的明确错误，而不是一个无法恢复的死任务。

**缺陷 3：审查结论被前言吞掉，完成的 fail 降级成 unknown。**
审查者输出"我会只检查…"+JSON 对象，`json.loads` 在首字符就失败，
`record_codex_review` 抛错，中间件把结果记成 `{"decision": "unknown"}`。
于是一次**真实的 fail** 与"根本没有审查"在记录里无法区分，`independent_codex_review`
永远是 `independent_review_missing`。修复：新增 `parse_review_report`，
先按原文解析，失败时提取最外层 `{…}` 再解析；字段校验与外层逻辑完全不变，
仍然只读结构化结论、不解析文字中的通过声明。

### 7.0.3 人工决策恢复闭环（HITL resume，2026-11-14）

对照官方文档（`oss/python/langchain/human-in-the-loop`、Agent Server API）补上了
`request_human_decision` 中断后的恢复链路，全部用官方机制，未自造协议：

- **读取**：`GET /workbench/api/pending-decision` 从官方线程状态读
  `tasks[].interrupts[]`，解析 batched 中断的 `action_requests`/`review_configs`；
  无法识别的中断形态只报 pending、不给答复入口。
- **恢复**：`POST /workbench/api/submit-decision` 校验答复数量与 `respond`
  允许性后，在同一 thread 以官方 `command={"resume": {"decisions":
  [{"type": "respond", "message": …}]}}` 起 Run（SDK `runs.create`），配置与
  chat 相同的项目绑定。普通聊天消息（`input`）与 resume 严格分离——message
  形态直接 400，无待处理决策时 409。
- **UI**：对话区决策卡片（问题、背景、逐项答复框、提交按钮），并注明普通
  消息不能恢复决策。
- **真实闭环验证**（隔离项目、真实模型与真实服务）：chat 触发
  `request_human_decision` → SSE 出现 `__interrupt__` → pending 接口读出问题
  与 `allowed_decisions:["respond"]` → 提交答复 → 官方 run 以
  `kwargs.command.resume={"decisions":[…]}` 恢复至 `success` → pending 清除 →
  history 出现 PM 后续回复（"已记录人工决策…"）。PM 选择了"直接答复确认"
  而非派工执行——闭环成立，但行动力弱于答复内容，如实记录。
- **检查点丢失的明确反馈**：dev 重启后 `get_state` 返回 **200 + 空快照**
  （`checkpoint: null`）而非 404，按 `CheckpointLostError` 返回 409 明确文案；
  已在真实重启的服务上复验。**绝不返回 `resumed:true` 伪装恢复。**
- **提交绑定页面展示的中断**：pending 返回内容指纹与官方 `interrupt_id`、
  `checkpoint_id`（`binding_available` 标明可核对性）；submit 必须回传三者，
  内容不一致或标识不一致一律 409 并提示"已变化，请重新打开项目"。官方
  interrupt id 是任务命名空间派生的槽位标识（`types.py: _interrupt_id`，同一
  位置的两次中断同 id），出现级区分依赖 `checkpoint_id`；官方两者皆未提供时
  拒绝并明确提示，不猜测。真机验证：错 checkpoint 409、正确绑定恢复至
  success。
- 测试：`tests/test_workbench_hitl.py` 11 项，已入 .gitignore 白名单并
  `git add`（未提交）。
- 留待后续：wait_agent 改通知式领取、custom 流消费（审计报告 2026-11-13）。

阶段结论只保留两点：

1. 全量回归 **345 passed 不包含 `test_pm_memory_integration.py` 的 2 项既有
   失败**（测试替身签名过时，与本阶段改动无关，单独跑该文件仍会红）。
2. **当前开发服务重启后 checkpoint 仍会丢失**（内存检查点，Postgres 中
   checkpoint 表 0 行）；HITL 恢复对这种情形给出明确 409 反馈，不伪装恢复
   原 Run。

> 2026-11-14 追记：第 2 点已解决——PM 会话检查点经官方 custom checkpointer
> 接入 PostgreSQL（`src/middlewares/persistence/checkpointer.py`），重启后
> 原中断可读、可提交、可恢复，实测通过；详见"检查点持久化"一节。

### 7.0.4 检查点持久化：官方 custom checkpointer 接入 PostgreSQL（2026-11-14）

- **官方写法**：`langgraph.json` 的 `checkpointer.path` 指向 async 上下文管理
  器工厂（langsmith/custom-checkpointer）；旧的 `{"type":"postgres",
  "connection_string":…}` 不在官方 schema 中、会被 dev 静默忽略。现配置为
  `./src/middlewares/persistence/checkpointer.py:generate_pm_checkpointer`，
  连接串复用项目 Store 的 `LANGGRAPH_POSTGRES_URI`；Store 配置原样未动。
- **Windows 事件循环冲突（实测定位）**：官方 `AsyncPostgresSaver`（psycopg
  async）在 Windows 强制要求 SelectorEventLoop，而本服务 ACP 适配器依赖
  ProactorEventLoop 创建子进程，两者不可兼得。解决：yield 官方**同步**
  `PostgresSaver`，阻塞调用经 `asyncio.to_thread` 卸载、实例级 `asyncio.Lock`
  串行（与项目 Store 的既有做法同构）——存储逻辑仍是官方实现，只是循环桥接。
- **启动入口修正（官方 CLI 缺口）**：本机 `langgraph dev`（langgraph_cli 的
  dev 命令）构造 `run_server(...)` 时**不转发** langgraph.json 的
  `checkpointer` 键（只转发 store/http/auth/ui/webhooks），配置被静默丢弃；
  新增 `scripts/run_dev_server.py` 直接调官方 `run_server(checkpointer=…)`，
  其余行为与 `langgraph dev` 一致。启动日志确认
  "Using custom checkpointer: ThreadOffloadedPostgresSaver"。
- **独立会话验证（真实模型、真实服务）**：触发 `request_human_decision`
  中断 → PG 确认 pending checkpoint 已落库（`checkpoints` 表出现对应行）→
  **重启服务** → `pending-decision` 读回同一问题、同一 interrupt_id、同一
  checkpoint_id → 提交答复恢复原 Run 至 `success` → PM 回复正常，pending
  清除；resume 后 checkpoint 继续落库（resume 线程 21 个 checkpoint，
  checkpoint_writes 60 行）。
- 测试：`tests/test_pm_checkpointer.py` 4 项（官方 schema 符合性、无 DB 回退
  内存+警告日志、包装官方同步 saver、真库 put→get_tuple→delete_thread 往返），
  全量 349 passed（仍不含 2 项既有失败）。
- **启动说明已更正**：README 启动一节现在明确要求用
  `python scripts/run_dev_server.py --port 2030 --n-jobs-per-worker 10`，
  并说明为什么不能直接 `langgraph dev`（CLI 丢弃 checkpointer 配置）；脚本
  固定 `reload=False`，Windows 无需再加 `--no-reload`。
- **数据库缺失回退有提示**：`LANGGRAPH_POSTGRES_URI` 未配置时，检查点工厂
  回退内存前打印 `[warning] ... (ephemeral)`；实测验证：真实无 URI 环境
  警告出现且 saver 为 InMemorySaver，本机已配置环境启动无该警告、项目数据
  完好。
- 已验证范围声明：**仅本机 dev 服务的单次重启恢复经过实测**（中断→重启→
  读回→提交→恢复）；`langgraph up`/生产部署未验证，README 已注明生产环境
  需另行验证后再依赖。
- 已知边界：dev 的 ops 元数据（threads/runs 记录）仍在本机 `.langgraph_ops`
  落盘，检查点在 PG——两者生命周期不同但重启后可对上（官方按 checkpoint
  重建状态）；`aprune/acopy_thread` 未实现时运行时有降级警告，功能不受损。

### 7.1 结果

```
conda activate python312        # Python 3.12.0
python -m pytest tests/ -q --ignore=tests/test_pm_memory_integration.py
# 331 passed in 19.08s
```

`test_pm_memory_integration.py` 另有 2 项失败，是**测试替身签名过时**（`write()` 不接受新增的 `verify_consolidation_profile` 参数、`lambda` 缺 `model` 参数）。该文件与本分支改动无关（`git status` 显示未修改），但它**不是全绿**，不应在报告里省略。

> 用 `conda run -n python312` 会崩溃并卡住无输出，直接用 `C:\miniconda\envs\python312\python.exe` 才稳定。

### 7.2 新增与变更的测试

`tests/test_worker_modes.py`（新增，18 项）：

| 测试 | 覆盖 |
|---|---|
| `test_apply_mode_uses_config_snapshot_as_confirmation` | 只认快照 |
| `test_apply_mode_reports_adapter_fallback_without_claiming_success` | 不匹配如实回报 |
| `test_apply_mode_rejects_modes_the_adapter_never_offered` | 不猜测 |
| `test_mode_events_never_overwrite_a_confirmed_snapshot` | 事件不覆盖 |
| `test_mode_state_reports_unavailable_when_adapter_offers_no_option` | 不可用如实说 |
| `test_saved_default_is_per_project_and_provider` | 按项目/提供方分别存 |
| `test_saved_default_rejects_invalid_provider_and_mode` | 非法输入 |
| `test_writable_modes_are_flagged_not_refused` | 可写被标记不被拒 |
| `test_writable_mode_is_reported_as_partial_boundary` | 可写记为 PARTIAL |
| `test_graph_persists_reported_mode_for_the_run` | 图级持久化（变异验证） |
| `test_readonly_fact_does_not_claim_readonly_capability_when_unconfirmed` | 未确认不冒充只读 |
| `test_acp_mutating_kinds_require_approval` | 破坏性操作需审批 |
| `test_acp_mutation_is_not_auto_allowed_by_the_policy` | 策略层实际结果 |
| `test_tool_config_cannot_downgrade_acp_mutations` | 配置不能降级 |
| `test_readonly_connection_is_not_reused_across_modes` | 缓存键含模式（变异验证） |
| `test_execution_is_registered_and_released_after_readonly_connections` | 注册/释放（变异验证） |
| `test_live_mode_state_filters_by_provider_and_directory` | 工作台按提供方+目录过滤 |
| `test_readonly_fact_names_the_saved_mode_not_unconfirmed` | 事实命名已保存模式 |

其余新增：`test_project_folders.py` 3 项（模式先于发送、依赖目录容忍硬链接、符号链接不跟随）、`test_pm_discussion.py` 1 项（指纹变化使通过失效）、`test_project_workflow.py` 1 项（恢复接受已有模式记录的绑定）。

### 7.3 测试契约修正

三项既有失败确认是断言落后于实际装配，已更新：

- `test_pm_profile` / `test_pm_middleware_assembly`：反映 `PMModelSelectionMiddleware`、`PMDiscussionMiddleware` 已在列表里（装配后共 7 项）。
- `test_pm_prompt`：保留"不得出现具体提供方名称"的原意，删掉提示词里两处 `Codex` 字样，`begin_codex_review` 作为工具契约名豁免。

### 7.4 本轮写文档时的额外发现

静态检查（`ruff --select F821`）在 `discussion.py` 发现 `ProjectWorkerModes` **被使用但未导入**——`begin_discussion` 预检会在首次真实商讨时抛 `NameError`。已补导入。这个缺陷测试覆盖不到，因为测试走的是图路径，不经过 `begin_discussion` 预检本身；这也说明"331 项通过"不等于"链路可用"。

顺带清理：`server.py` 未使用的 `READONLY_ISOLATION` 导入、几处导入排序。剩余 I001/D107 均为 HEAD 已存在的既有问题，未在本轮扩大范围处理。

---

## 八、审查过程与已修缺陷

审查 6 轮，每轮只审上一轮未过关项。确认修好的阻断项：

| 轮次 | 缺陷 | 后果 |
|---|---|---|
| 1 | 适配器回报没进业务记录 | `reported_mode` 永远是 None |
| 1 | 恢复路径硬编码 ENFORCEABLE | 把可写商讨升级成只读保证 |
| 2 | 连接缓存不含模式 | 切换模式后复用的仍是旧边界事实 |
| 2 | ACP 破坏性操作自动放行 | 删除/移动可直接改项目 |
| 3 | `for key in readonly_keys` 覆盖执行键 | 执行连接登记错位，项目永久隔离 |
| 3 | 切换开发只关第一个只读连接 | 其余适配器进程与会话残留 |
| 5 | 预检没传 `mode_id` | 边界永远写 `mode=unconfirmed` |
| 5 | 隔离条目覆盖丢失 `ticket`/`intent_id` | 核对永远 409，人工也解不开 |
| 5 | `_live_mode_state` 未按提供方过滤 | 查 claude 显示 codex 的模式列表 |
| 文档 | `ProjectWorkerModes` 未导入 | 首次真实商讨 `NameError` |

其中第 5 轮"预检没传 `mode_id`"值得单独说明：上一轮我以为修过，实际那段修改因字符串断言失败，整段没有写盘，代码从未变过。本轮用 AST 检查确认调用参数，并补了导入。

---

## 九、未完成与限制（需要用户决定）

以下都是**已知且未修**，不是遗漏：

1. **真实端到端已跑通一次（2026-11-13），但依赖启动参数。** 计算器任务完整走完
   商讨 → Clef 通过 → 开发 → 落盘 → Codex 审查。是否可复现取决于下面第 9 条
   的并发设置；旧文档中"真实端到端未运行"的结论已不再成立。

9. **`langgraph dev` 的单 job 并发会让整条链路必然死锁（已在文档层规避）。**
   `langgraph_api/cli.py:255` 把 `N_JOBS_PER_WORKER` 设为 `1`（与 `--help`
   文案"Default: 10"不符）。父 PM run 阻塞在 `wait_agent` 时占着唯一执行槽，
   子代理 `acp_worker` run 永远停在 `pending`，直到父 run 等待超时才被调度；
   PM 会一直报"负责人会话仍在排队"。这不是 PM 逻辑缺陷，是宿主并发配置。
   规避方式：`langgraph dev --n-jobs-per-worker 10`（Windows 另加 `--no-reload`
   保证 ProactorEventLoop）。已写入 README。**代码层未加保护**——若部署方
   忘了这个参数，表现就是"商讨卡住"，而不是启动时报错。

10. **`langgraph dev` 重启会清空检查点，但 Store 里的项目数据仍在，两者会失配。**
   父会话的 `async_tasks` 随检查点丢失，`wait_agent` 报 `unknown_target`；
   `recover_dispatched_session` 因"官方 checkpoint 与派工意图不一致"而拒绝
   ——这是正确的失败关闭（没有造假），但意味着**开发模式下重启服务等于放弃
   当前会话的派工关联**，只能重新发起商讨。生产环境用持久 checkpointer 后
   不成立，本轮未验证。

2. **商讨期间的写入会成为指纹基线。** `require_pass` 只能发现商讨**之后**的改动。若 Worker 在商讨期间改了项目（选可写模式时可能），这次改动会被当作基线接受。方向是记录如实、不假装能防，但确实存在盲区。

3. **宿主重启后的孤儿子进程无法证明已结束。** 关闭路径是 fail-closed（报"无法确认"并保持隔离），但重启前拉起的适配器子进程是否仍存在，宿主证明不了，孤儿化风险未消除。

4. **Codex 项目默认模式不作用于 `begin_codex_review`。** 同类问题——那个入口也没传 `mode_id`。超出本轮范围。

5. **只读阶段无人审回调时，中危请求直接拒绝而非等待确认。** 方向保守，但记录里不应写成"已要求人工确认"。

6. **`ephemeral_home` 参数在原生方案下已无作用。** 保留未删。

7. **`write_file`/`shell` 的通用低危判定属既有策略**，本轮未评估。

8. **未提交。** 工作树改动尚未提交（22 个已修改文件、4 个新增、1 个删除；代码部分约 919 行新增 / 895 行删除）。

---

## 十、改动清单

### 新增

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/middlewares/acp/pool.py` | 361 | 原生 Windows Worker 连接池、模式缓存、进程生命周期 |
| `src/middlewares/project_state/worker_modes.py` | 45 | 项目级默认商讨模式（按项目/提供方） |
| `tests/test_worker_modes.py` | 517 | 模式、边界、审批、生命周期测试 |
| `docs/progress/PM协作开发链路.md` | — | 本文 |

### 删除

| 文件 | 行数 |
|---|---|
| `src/middlewares/acp/wsl_readonly.py` | 369 |

### 主要修改

| 文件 | 变更 |
|---|---|
| `src/middlewares/acp/graph.py` | 模式应用时机、回报持久化、隔离合并写入 |
| `src/middlewares/acp/control.py` | 快照确认、事件隔离、`apply_mode` |
| `src/middlewares/acp/server.py` | 移除 WSL 池与审计开关 |
| `src/deep_agents/agents/pm_agent/discussion.py` | 指纹绑定、预检传模式、读回回报 |
| `src/middlewares/project_state/workflow.py` | 逐字段比较 `mode_record`、接受 PARTIAL |
| `src/middlewares/project_state/folders.py` | 目录指纹重写 |
| `src/middlewares/approval/risk_analyzer.py` | ACP 默认拒绝 |
| `src/workbench/app.py` / `index.html` | 模式设置、可写警示、按提供方过滤 |
| `tests/*`（10 个） | 契约更新与新增覆盖 |

---

## 十一、复现命令

```bash
conda activate python312
cd re_build-pm-continue
python -m pytest tests/ -q --ignore=tests/test_pm_memory_integration.py
```

```bash
python -m pytest tests/test_worker_modes.py -q
```

```bash
python -m ruff check src/middlewares/acp src/middlewares/project_state src/deep_agents/agents/pm_agent src/workbench tests/test_worker_modes.py
```

---

## 十二、与需求其余条目的对照

| 需求 | 状态 |
|---|---|
| 0 商讨模式（中间件提供、PM 专用、Clef 判断、Codex 审查） | 本条主体已实现，见第三节；真实端到端已跑通一轮（7.0），但审查结论登记依赖新增的容错解析，且需人工复核审查假阳性 |
| 1 独立运行网页 + 轨迹监测 | 工作台已有 `/workbench` 与 `/workbench/trajectory`，本轮未改动 |
| 2 按文件夹创建项目 | 已实现（`ProjectFolders`、`/workbench/api/select-folder`），本轮补充目录指纹 |
| 3 对话中切换模型 + 思考等级 + 切换提示词 | 已实现（`PMModelSelectionMiddleware`），本轮未改动 |
| 4 从 API 获取模型、启用/禁用 | 已实现（`ModelCatalog.discover`、`/workbench/api/providers/discover`），本轮未改动 |
| 5 所有设置可在网页配置 | 部分；本轮新增商讨模式设置，其余设置项未逐一核对 |
