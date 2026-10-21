"""当前项目状态摘要：控制长度，与 PM 历史记忆分开管理。"""


def render_project_context(snapshot) -> str:
    """根据当前项目事实生成摘要，完整数据通过项目读取工具获取。"""
    context = snapshot
    active_tasks = tuple(t for t in snapshot.tasks if t.status.value != "done")
    blockers = tuple(
        t
        for t in active_tasks
        if t.status.value in {"failed", "rework", "waiting_human"}
    )
    lines = [
        f"Project: {context.project_id}",
        f"Goal: {context.goal}",
    ]

    if context.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {constraint}" for constraint in context.constraints)

    if active_tasks:
        lines.append("Active tasks:")
        for task in sorted(active_tasks, key=lambda task: task.priority, reverse=True)[
            :30
        ]:
            owner = f" owner={task.owner_role}" if task.owner_role else ""
            dependencies = (
                f" deps={','.join(sorted(task.dependencies))}"
                if task.dependencies
                else ""
            )
            lines.append(
                f"- {task.task_id}: {task.title} [{task.status.value}]"
                f"{owner}{dependencies}"
            )
            lines.append(
                f"  Acceptance: {task.acceptance_criteria}; Artifacts: {task.artifact_refs}"
            )
    else:
        lines.append("Active tasks: none")

    if blockers:
        lines.append("Blockers:")
        lines.extend(
            f"- {task.task_id}: {task.title} [{task.status.value}]" for task in blockers
        )
    else:
        lines.append("Blockers: none")

    accepted = [
        task
        for task in snapshot.tasks
        if task.status.value in {"passed", "approved", "done"}
    ]
    if accepted:
        lines.append(
            "Current acceptance state (authoritative, not historical completion claims):"
        )
        lines.extend(
            f"- {task.task_id}: {task.title} [{task.status.value}] criteria={task.acceptance_criteria} artifacts={task.artifact_refs}"
            for task in accepted[-20:]
        )
    return "\n".join(lines)[:8_000]
