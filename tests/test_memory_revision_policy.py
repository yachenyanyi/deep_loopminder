import pytest

from src.middlewares.memory.revision_policy import (
    MemoryInjectionEvidence,
    MemoryRevision,
    MemoryRevisionState,
    RetentionPolicy,
    can_hard_delete,
)


def test_active_revision_is_selectable_for_current_recall():
    revision = MemoryRevision("memory:user/name:v2", "v2", "hash-v2")
    assert revision.selectable_for_current_recall is True


def test_superseded_revision_is_excluded_from_current_recall():
    revision = MemoryRevision(
        "memory:user/name:v1",
        "v1",
        "hash-v1",
        state=MemoryRevisionState.SUPERSEDED,
        superseded_by="memory:user/name:v2",
    )
    assert revision.selectable_for_current_recall is False


def test_recall_deleted_revision_is_excluded_without_implying_hard_delete():
    revision = MemoryRevision(
        "memory:user/name:v1",
        "v1",
        "hash-v1",
        state=MemoryRevisionState.RECALL_DELETED,
    )
    assert revision.selectable_for_current_recall is False
    assert can_hard_delete(policy=RetentionPolicy()) is False


def test_historical_evidence_stays_bound_to_injected_revision_after_supersede():
    old = MemoryRevision(
        "memory:user/name:v1",
        "v1",
        "hash-v1",
        state=MemoryRevisionState.SUPERSEDED,
        superseded_by="memory:user/name:v2",
    )
    evidence = MemoryInjectionEvidence.from_revision(
        old, projection_ref="artifact://memory-projection/123"
    )

    assert evidence.memory_ref == "memory:user/name:v1"
    assert evidence.version == "v1"
    assert evidence.content_hash == "hash-v1"
    assert evidence.projection_ref == "artifact://memory-projection/123"


def test_hard_delete_requires_explicit_control_plane_policy():
    assert can_hard_delete(policy=RetentionPolicy()) is False
    assert can_hard_delete(policy=RetentionPolicy(allow_hard_delete=True)) is True


def test_superseded_revision_requires_successor():
    with pytest.raises(ValueError, match="superseded_by"):
        MemoryRevision(
            "memory:user/name:v1",
            "v1",
            "hash-v1",
            state=MemoryRevisionState.SUPERSEDED,
        )


def test_non_superseded_revision_cannot_claim_successor():
    with pytest.raises(ValueError, match="only valid"):
        MemoryRevision(
            "memory:user/name:v2",
            "v2",
            "hash-v2",
            superseded_by="memory:user/name:v3",
        )
