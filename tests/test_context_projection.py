import pytest

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
    version: str = "v1",
    mutable: bool = False,
) -> ContextBlock:
    return ContextBlock(
        block_id=block_id,
        kind="project_fact",
        scope=scope,
        content=content,
        source="langgraph-store",
        priority=priority,
        accepted=accepted,
        version=version,
        ref=f"fact:{block_id}",
        mutable=mutable,
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


def test_mutable_blocks_require_matching_current_version() -> None:
    projection = ContextProjection(
        scope="project:a",
        blocks=(
            _block("fresh", mutable=True, version="v2", priority=10),
            _block("stale", mutable=True, version="v1", priority=20),
            _block("unknown", mutable=True, version="v1", priority=30),
            _block("immutable", priority=1),
        ),
        current_versions=(
            ("fact:fresh", "v2"),
            ("fact:stale", "v2"),
        ),
    )

    assert [block.block_id for block in select_context_blocks(projection)] == [
        "fresh",
        "immutable",
    ]


def test_mutable_block_requires_ref_and_version() -> None:
    with pytest.raises(ValueError, match="require ref and version"):
        ContextBlock(
            block_id="mutable",
            kind="project_fact",
            scope="project:a",
            content="fact",
            source="langgraph-store",
            accepted=True,
            mutable=True,
        )


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

    with pytest.raises(ValueError, match="budgets must be non-negative"):
        select_context_blocks(projection)


def test_conflicting_current_versions_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique refs"):
        ContextProjection(
            scope="project:a",
            blocks=(),
            current_versions=(("fact:a", "v1"), ("fact:a", "v2")),
        )
