# PM Agent

PM Agent workspace.

## Design

PM Agent 的长期角色与治理原则属于基础 Prompt；Skill 只用于特定项目状态下的按需判断增强。

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
