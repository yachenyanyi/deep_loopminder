from src.runtime.project.commands import CreateTaskCommand, TransitionTaskCommand
from src.runtime.project.models import ProjectSnapshot, Task, TaskStatus
from src.runtime.project.transitions import (
    DependencyReadiness,
    TransitionDecision,
    dependency_readiness,
    transition_decision,
    validate_dependency_graph,
)


def make_task(
    task_id: str,
    status: TaskStatus,
    *,
    dependencies: frozenset[str] = frozenset(),
) -> Task:
    return Task(
        task_id=task_id,
        title=task_id,
        status=status,
        owner_role=None,
        required_capabilities=frozenset(),
        dependencies=dependencies,
        acceptance_criteria=("accepted",),
    )


def make_snapshot(*tasks: Task) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id="project-1",
        goal="Ship safely",
        constraints=(),
        tasks=tasks,
    )


def test_create_task_command_requires_task_brief_ref() -> None:
    task = make_task("task-1", TaskStatus.BACKLOG)

    try:
        CreateTaskCommand(project_id="project-1", task=task, task_brief_ref="")
    except ValueError as exc:
        assert str(exc) == "task_brief_ref must be non-empty"
    else:
        raise AssertionError("CREATE_TASK without TaskBrief ref must fail closed")


def test_dependency_is_ready_only_when_all_dependencies_are_done() -> None:
    dependency = make_task("dep", TaskStatus.DONE)
    task = make_task("task", TaskStatus.BACKLOG, dependencies=frozenset({"dep"}))
    snapshot = make_snapshot(dependency, task)

    assert dependency_readiness(snapshot, "task") is DependencyReadiness.READY


def test_incomplete_dependency_blocks_ready_transition() -> None:
    dependency = make_task("dep", TaskStatus.PASSED)
    task = make_task("task", TaskStatus.BACKLOG, dependencies=frozenset({"dep"}))
    snapshot = make_snapshot(dependency, task)
    command = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.READY,
    )

    assert dependency_readiness(snapshot, "task") is DependencyReadiness.BLOCKED
    assert transition_decision(snapshot, command) is TransitionDecision.REJECTED


def test_unknown_dependency_fails_closed() -> None:
    snapshot = make_snapshot(
        make_task("task", TaskStatus.BACKLOG, dependencies=frozenset({"missing"}))
    )

    try:
        validate_dependency_graph(snapshot)
    except ValueError as exc:
        assert "unknown dependencies" in str(exc)
    else:
        raise AssertionError("Unknown dependency must fail closed")


def test_dependency_cycle_fails_closed() -> None:
    snapshot = make_snapshot(
        make_task("a", TaskStatus.BACKLOG, dependencies=frozenset({"b"})),
        make_task("b", TaskStatus.BACKLOG, dependencies=frozenset({"a"})),
    )

    try:
        validate_dependency_graph(snapshot)
    except ValueError as exc:
        assert str(exc) == "project task dependencies must be acyclic"
    else:
        raise AssertionError("Dependency cycle must fail closed")


def test_worker_completion_cannot_jump_directly_to_done() -> None:
    task = make_task("task", TaskStatus.IN_PROGRESS)
    snapshot = make_snapshot(task)
    command = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.DONE,
    )

    assert transition_decision(snapshot, command) is TransitionDecision.REJECTED


def test_review_requires_trusted_validation_before_passed() -> None:
    task = make_task("task", TaskStatus.REVIEW)
    snapshot = make_snapshot(task)
    missing = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.PASSED,
        validation_accepted=None,
    )
    accepted = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.PASSED,
        validation_accepted=True,
    )

    assert transition_decision(snapshot, missing) is TransitionDecision.REJECTED
    assert transition_decision(snapshot, accepted) is TransitionDecision.ALLOWED


def test_human_approval_is_required_before_approved() -> None:
    task = make_task("task", TaskStatus.WAITING_HUMAN)
    snapshot = make_snapshot(task)
    missing = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.APPROVED,
    )
    approved = TransitionTaskCommand(
        project_id="project-1",
        task_id="task",
        target_status=TaskStatus.APPROVED,
        human_approved=True,
    )

    assert transition_decision(snapshot, missing) is TransitionDecision.REJECTED
    assert transition_decision(snapshot, approved) is TransitionDecision.ALLOWED
