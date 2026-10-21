# Codex ACP 接入

2026-10-20 用户要求：增加 Codex ACP Worker，默认模型为 `gpt-6-luna`。

范围：复用现有 ACPWorker、角色路由和六工具，仅增加部署配置与说明；保留 Claude 接入及 PM 主模型，不修改全局 Codex 配置。

验收：适配器真实连接、新会话报告指定模型、一次简短 prompt 成功；区分直接 ACP 验证与 PM 自主派工验证。避免重复运行已有功能套件。

已完成：检查现有 server.py 可直接读取 Worker 的 command/env；本地安装 `@agentclientprotocol/codex-acp@2.2.2` 于忽略目录 `.local-acp/`。适配器文档支持 `CODEX_CONFIG` 合并会话配置，并捆绑兼容的 Codex。安装未修改全局 CLI 或用户配置。

进行中：以 Worker 专属 `CODEX_CONFIG={"model":"gpt-6-luna"}` 配置，先验证真实模型与连接，再保存部署配置。正式权限继续由原生模式、grants 和审批共同约束。

已验证：真实 ACPWorker 完成初始化与新会话创建，会话返回的当前模型为 `gpt-6-luna`；session `01a15835-6120-71c1-8846-21614744a14e`。正在等待一次禁止工具调用的简短回复；尚不把会话创建等同于模型推理成功。

已验证：一次真实 prompt 完成，`stop_reason=end_turn`，返回“Codex ACP 连接成功。”；角色路由也实际选中 codex。原始脱敏证据见 [codex-acp-20261020.json](evidence/codex-acp-20261020.json)。范围仅为直接 ACPWorker 连接、会话模型及回复，不是 PM 自主派工或写入/执行测试。此次测试使用 read-only，未请求工具权限。

已完成：本机 `.env` 合并 Codex Worker 与 developer/researcher/reviewer 角色，模型覆盖只存在该 Worker 的 CODEX_CONFIG；使用 workspace-write 模式及现有 YamlPolicyProvider 默认策略，未启用全自动审批。代理仅配置于 Worker 进程环境。原 `.env` 没有配置其他 ACP 提供方，因此当前启用列表仅有 codex；Claude 中间件支持与历史测试配置未删除。

注意：关闭测试连接后，上游适配器的异步命令列表发布报告 AppServerUnavailable（app-server 正常退出 code 0）；回复已在退出前完成。捆绑 Codex 另提示用户全局配置中的 subagents 字段不识别；本次未修改全局配置，也未据此宣称原生子代理可用。

Checkpoint：Codex 直接 ACP 链路已验证，本机部署配置与模板已保存。需重启 Agent Server 后载入；未重启已有服务，未验证 PM 自主派工、长期运行、写入执行或完整六工具回归。测试进程已退出。正在同步结构图中的 Codex 节点。

最终 checkpoint：结构图已增加 Codex 节点及详细说明，保留原 11 节点与记忆详情；图基础布局检查 9/9 通过，详情脚本语法通过。未重复浏览器验收（先前本地文件页面访问受工具策略限制）。脱敏证据和进度文档已加入 Git 忽略例外，本机 `.env` 与 `.local-acp/` 仍保持忽略。此次无需修改 ACP 生产 Python 代码；只复用现有提供方接入和配置。没有提交、推送或遗留测试进程。

## 用户明确分工（2026-10-20）

新要求：开发由 Claude Code 负责；验收、测试、研究、调查、信息搜集由 Codex 负责。采用互不重叠的业务角色：Claude → developer；Codex → reviewer、tester、researcher。更新 PM 可见的角色说明与部署配置，复用现有路由，不让 provider_id 字母排序决定开发提供方。

进行中：安装固定版 Claude ACP 适配器到项目忽略目录，补充 Claude Worker；将 Codex 的 developer 角色移除。验收只表示依据标准执行核验并提交证据，不绕过项目中间件的可信验收或人工批准。

已完成：安装 `@agentclientprotocol/claude-agent-acp@0.94.0`；本机 `.env` 启用 claude（developer）及 codex（reviewer/tester/researcher）。Claude Worker 沿用 MiniMax 免费模型与原生认证；Codex 保持 gpt-6-luna。PM 的 spawn_agent 工具描述由角色配置自动生成，四类角色说明已更新，不需改 PM 固定提示词。

已验证：实际调用现有 route_worker，四条配置路由分别唯一选中预期提供方；适配器入口文件存在，dotenv 读写回读一致。证据 [acp-role-routing-20261020.json](evidence/acp-role-routing-20261020.json)。这只是配置路由验证，runtime_available=True 是验证条件，不是服务存活探测；没有新增模型调用或重复跑已有套件。初次检查使用的默认 Python 缺少 langgraph，在写配置前结束；随后复用已有 uv 依赖环境完成。

Checkpoint：角色分工配置、模板和说明已更新。Agent Server 需重启后加载；现有已创建任务或会话不会自动换提供方。未声称 Claude 新安装版本已完成真实开发或 Codex 已完成真实测试，权限策略维持原样。正在同步图中两类 Worker 的分工说明。

最终 checkpoint：结构图总览与节点详情已同步固定分工，基础图布局检查 9/9、详情脚本语法通过。没有修改生产 Python、全局模型配置或权限策略，没有新增模型调用。当前完成配置与路由检查，剩余是重启实际部署后载入，不会在本轮中断用户的已有服务。
