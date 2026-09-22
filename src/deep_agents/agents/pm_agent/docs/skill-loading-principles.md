# PM Agent Skill 加载原则

## 核心理念

PM Skill 不是默认加载的完整知识库，而是在项目运行过程中，根据当前事件、状态和需求按需检索的治理能力。

预加载全部 Skill 容易导致：
- 上下文膨胀
- 注意力分散
- 机械执行 Skill
- 限制模型自主判断

Skill 的作用是在特定场景下提供额外的决策框架和领域约束。

## PM Agent 与 Skill 的边界

### PM Agent 基础能力

基础 Prompt 负责长期稳定的角色认知，例如：
- 你是项目治理者
- 关注目标、状态、风险、证据和决策
- 不替代 Worker 执行具体工作

### Skill

Skill 只负责特定场景下的判断增强。

例如：
- 需求模糊 → goal-understanding
- 需要规划 → planning
- Worker 失败 → failure-diagnosis
- 状态变化 → progress-review
- 原计划失效 → replanning
- 声称完成 → acceptance-review
- 阶段结束 → retrospective
- 需要项目级通用治理判断 → project-governance

如果多个 Skill 同时相关，只加载对当前决策真正有帮助的最小集合。

## 判断标准

一个 Skill 是否值得存在，需要回答：
1. 是否帮助 PM 避免一种典型错误？
2. 是否提供模型不一定主动想到的治理经验？
3. 是否增强判断，而不是限制判断？
4. 是否只在需要时加载？

## 设计目标

让 PM Agent 像经验丰富的项目负责人：
- 平时保持简洁
- 遇到具体情境再调用对应经验
- 根据事实调整方法
- 不被固定流程束缚
