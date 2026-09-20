from src.runtime.project.models import ProjectSnapshot, Task, TaskStatus


def test_task_requires_acceptance_criteria() -> None:
    try:
        Task(
            task_id="task-1",
            title="Implement project domain",
            status=TaskStatus.BACKLOG,
            owner_role="developer",
            required_capabilities=frozenset({"repo.read"}),
            dependencies=frozenset(),
            acceptance_criteria=(),
        )
    except ValueError as exc:
        assert str(exc) == "acceptance_criteria must be non-empty"
    else:
        raise AssertionError("Task without acceptance criteria must fail closed")


def test_task_keeps_execution_refs_as_refs_not_status_mirror() -> None:
    task = Task(
        task_id="task-1",
        title="Implement project domain",
        status=TaskStatus.IN_PROGRESS,
        owner_role="developer",
        required_capabilities=frozenset({"repo.write.source"}),
        dependencies=frozenset(),
        acceptance_criteria=("domain tests pass",),
        execution_refs=("langgraph:thread/t-1", "acp:session/s-1"),
    )

    assert task.status is TaskStatus.IN_PROGRESS
    assert task.execution_refs == ("langgraph:thread/t-1", "acp:session/s-1")
    assert not hasattr(task, "run_status")
    assert not hasattr(task, "checkpoint")


def test_project_snapshot_rejects_duplicate_task_identity() -> None:
    task = Task(
        task_id="task-1",
        title="One",
        status=TaskStatus.READY,
        owner_role=None,
        required_capabilities=frozenset(),
        dependencies=frozenset(),
        acceptance_criteria=("done",),
    )

    try:
        ProjectSnapshot(
            project_id="project-1",
            goal="Ship",
            constraints=(),
            tasks=(task, task),
        )
    except ValueError as exc:
        assert str(exc) == "task_id must be unique within a project snapshot"
    else:
        raise AssertionError("Duplicate task identities must fail closed")


def test_task_cannot_depend_on_itself() -> None:
    try:
        Task(
            task_id="task-1",
            title="One",
            status=TaskStatus.BACKLOG,
            owner_role=None,
            required_capabilities=frozenset(),
            dependencies=frozenset({"task-1"}),
            acceptance_criteria=("done",),
        )
    except ValueError as exc:
        assert str(exc) == "task cannot depend on itself"
    else:
        raise AssertionError("Self dependency must fail closed")
