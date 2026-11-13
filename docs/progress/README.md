# 项目进度索引

- [PM 协作开发链路](../../../re_build-pm-continue/docs/progress/PM协作开发链路.md)：商讨模式、Windows 原生 Worker、项目指纹与 Clef 门禁的实施状态、验证证据与已知限制。

- [踩坑记录](踩坑记录.md)：2026-11-13，PM 端到端实测撞上的全部坑（代理轮询、worker 池死锁、dev 重启失配、路由键校验、审查报告解析等）及排查方法论。

- [人工决策恢复闭环](PM协作开发链路.md)：2026-11-14，官方 interrupt/resume 接通工作台（pending-decision/submit-decision），真实端到端验证通过；dev 重启后检查点丢失有明确 409 反馈，绝不伪装恢复。

- [官方文档对照审计](官方文档对照审计-20261113.md)：2026-11-13，以 docs.langchain.com 为准逐项对照 PM 实现；10 项做对、3 项官方更优（HITL resume 链路、子代理通知/webhook、join_stream）、1 项 checkpointer 字段疑似无效待实测。

- [代码审查](代码审查-20261114.md)：2026-11-14，只读审查：execution-binding 扫描重复 8 处、middleware→UI 反向依赖、死代码清单、复杂度热点与 Top 5 修复排序。

- [ACP 子代理工具优化](acp-tools.md)：2026-10-13，工具优化完成；18 项测试与真实排队对话专项通过。
- [Claude ACP 审查实战](claude-review-20261013.md)：2026-10-13，两个真实会话、多种协作操作通过；审查误报已核对，留下一条可复现的状态查询边界。

- [项目状态中间件](project-state.md)：集中领域状态、Store 与工具注入。

- [SkillsBench 单题测试](skillsbench.md)：安装公开任务集并验证PM真实交付。
- [Codex ACP 接入](codex-acp.md)：增加 Codex Worker，默认模型为 `gpt-6-luna`。
- [KNX 客户聊天只读试跑](pm-knx-readonly.md)：关闭 Worker 和写入，观察真实 PM 对客户需求的回复。
- [项目管理网站真实开发](project-hub-development.md)：空目录起步，真实 PM 自主派工，完整记录但不干预产物。

- [运行轨迹中间件](../../src/middlewares/trajectory/README.md)：2026-10-21，实时网页、首错停止和原题正式重跑，记录见项目管理网站进度与trajectory-rerun-20261021.json。

- [DSH轨迹内容对齐](evidence/trajectory-dsh-20261021.json)：现有trajectory中间件升级；轮次/请求分组、详情快照/Schema、五维Token、虚拟行与时间选区已验证，旧数据缺项不伪造。

- [DSH实页界面复刻](evidence/trajectory-dsh-ui-20261021.json)：已对照本机DSH轨迹及详情，30px账本/三泳道/按需检查器/折叠与聚焦已验证，截图已保存。
