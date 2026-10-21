# PM Agent

PM Agent 的实现、配置、上下文与场景技能集中放在本目录。

## 文件与阅读顺序

- `agent.py`：完整 PM 实现，依次是配置、固定提示词、上下文整理、中间件装配、主图创建。各部分有中文注释，按照组装顺序阅读即可。
- `__init__.py`：统一导出公共接口，不放业务实现。
- `skills/`：按需加载的 PM 场景技能。

历史记忆和后台记忆图集中在 `src/middlewares/memory/pm_agent_memory/`；项目状态与 ACP 使用各自的中间件模块，PM 只负责装配。

项目状态统一由 `src/middlewares/project_state/` 管理；Worker 路由继续使用 `src/runtime/routing/`。通用审批和上下文投影复用 `src/middlewares/`；ACP 接入集中在 `src/middlewares/acp/`。

配置 `ACP_SUBAGENTS_JSON` 后，PM 装配追加 ACPMiddleware 的六个子代理工具。角色端点与 Worker 连接配置见 `src/middlewares/acp/README.md`；未配置时保留原有装配。

## Design

PM Agent 的长期角色与治理原则属于基础 Prompt；Skill 只用于特定项目状态下的按需判断增强。

PM 负责管理协调：理解业务需求、必要的功能拆解、明确范围和验收要求、安排负责人、跟进阻塞、汇总结果。PM 不浏览项目目录、源码、代码差异或原始技术日志，不通过记忆和制品绕道审查技术材料，不制定数据库、接口或代码实现方案。技术调查、开发、测试与核验由各 Worker 承担。

任务状态、需求、决定、历史管理记忆和 Worker 管理摘要仍由 PM 读取，用于推进业务。禁止委派时，PM 仅依据客户材料提出管理安排与待确认事项，不自行代替 Worker 调查。已有工具没有本地项目文件系统入口；上述语义边界同时写入固定提示词，不代表工具层能自动识别并拦截任意文档中的源码内容。

当前场景 Skill：

- `goal-understanding`
- `planning`
- `progress-review`
- `failure-diagnosis`
- `replanning`
- `acceptance-review`
- `retrospective`

设计与加载原则见：

- `docs/skill-design-guidelines.md`

不要新增“几乎所有项目都适用”的常驻 Skill。通用原则应进入基础 Prompt，而不是通过 Skill 重复注入。

项目状态工具和实时上下文由 ProjectStateMiddleware 注入；`src/middlewares/memory/pm_agent_memory/` 保留历史记忆职责。写入配置与可信验收回调见 `src/middlewares/project_state/README.md`。

默认工厂已装配共享 Store 的 ProjectWorkflow：验证 TaskBrief 制品、将实际 ACP Run 与完成结果关联业务任务，并读取版本绑定的可信验收/人工批准。验收与批准写入接口仅供可信宿主使用，模型不能自行签发。未接入核验服务和人工入口时，任务仍会被相应门禁阻止收口。
