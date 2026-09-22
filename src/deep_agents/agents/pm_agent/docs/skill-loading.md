# PM Agent Skill 加载原则

## 核心理念

PM Skill 是按项目事件、状态和需要检索的治理能力，不是默认全部注入上下文的完整知识库。

默认预加载所有 Skill 容易造成：

- 上下文膨胀。
- 注意力分散。
- 机械执行 Skill。
- 限制模型自主判断。

## Agent 与 Skill 的边界

PM Agent 的基础 Prompt 负责长期稳定的角色认知，例如：

- PM 是项目治理者，不是普通 Worker。
- 持续关注目标、状态、风险、证据和决策。
- 不替代 Worker 执行具体工作。

Skill 只负责特定场景下的判断增强。

## 加载策略

优先按当前事件检索最相关的 Skill，而不是一次加载全部 Skill。

常见映射：

- 新项目、目标模糊、目标冲突 → `goal-understanding`
- 目标明确、需要规划 → `planning`
- 收到任务反馈或项目状态变化 → `progress-review`
- 失败、阻塞、重复重试无效 → `failure-diagnosis`
- 原计划失效或关键假设变化 → `replanning`
- 任务、里程碑或项目准备结束 → `acceptance-review`
- 阶段或项目结束后提取经验 → `retrospective`
- 需要项目级通用治理判断 → `project-governance`

如果多个 Skill 同时相关，只加载对当前决策真正有帮助的最小集合。

## 设计目标

让 PM Agent：

- 平时保持上下文简洁。
- 遇到具体问题时调用相关经验。
- 根据现实变化调整判断。
- 不被固定流程束缚。
