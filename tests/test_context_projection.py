from src.middlewares.context_projection import (
    ContextBlock,
    ContextProjection,
    render_context_blocks,
    select_context_blocks,
)


def _block(
    block_id: str,
    *,
    scope: str = "project:a",
    priority: int = 0,
    accepted: bool = True,
    content: str = "fact",
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        kind="project_fact",
        scope=scope,
        content=content,
        source="langgraph-store",
        priority=priority,
        accepted=accepted,
        version="v1",
        ref=f"fact:{block_id}",
    )


def test_selection_only_projects_accepted_in_scope_facts() -> None:
    projection = ContextProjection(
        scope="project:a",
        blocks=(
            _block("accepted", priority=1),
            _block("candidate", accepted=False, priority=100),
            _block("other-project", scope="project:b", priority=100),
        ),
    )

    assert [block.block_id for block in select_context_blocks(projection)] == [
        "accepted"
    ]


def test_selection_is_priority_ordered_and_bounded() -> None:
    projection = ContextProjection(
        scope="project:a",
        blocks=(
            _block("low", priority=1, content="1234"),
            _block("high", priority=10, content="1234"),
            _block("medium", priority=5, content="1234"),
        ),
        max_blocks=2,
        max_chars=8,
    )

    assert [block.block_id for block in select_context_blocks(projection)] == [
        "high",
        "medium",
    ]


def test_render_preserves_provenance_ref_and_version() -> None:
    rendered = render_context_blocks((_block("accepted"),))

    assert "source=langgraph-store" in rendered
    assert "version=v1" in rendered
    assert "ref=fact:accepted" in rendered


def test_negative_projection_budget_is_rejected() -> None:
    projection = ContextProjection(
        scope="project:a",
        blocks=(),
        max_chars=-1,
    )

    try:
        select_context_blocks(projection)
    except ValueError as exc:
        assert "budgets must be non-negative" in str(exc)
    else:
        raise AssertionError("negative budget must fail closed")
