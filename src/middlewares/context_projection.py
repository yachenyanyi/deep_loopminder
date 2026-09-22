"""Thin model-call context projection using LangChain's official dynamic_prompt hook.

This module owns only bounded selection/rendering. It does not persist context,
validate candidate facts, summarize conversations, or mirror LangGraph state.
"""

from dataclasses import dataclass
from typing import Iterable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, dynamic_prompt


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """Read-only descriptor for already-owned context."""

    block_id: str
    kind: str
    scope: str
    content: str
    source: str
    priority: int = 0
    accepted: bool = False
    version: str | None = None
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """Per-invocation context supplied through official runtime.context."""

    scope: str
    blocks: tuple[ContextBlock, ...]
    max_blocks: int = 8
    max_chars: int = 8_000


def select_context_blocks(projection: ContextProjection) -> tuple[ContextBlock, ...]:
    """Select accepted, in-scope blocks under deterministic bounded budgets."""
    if projection.max_blocks < 0 or projection.max_chars < 0:
        raise ValueError("context projection budgets must be non-negative")

    eligible = (
        block
        for block in projection.blocks
        if block.accepted and block.scope == projection.scope
    )
    ordered = sorted(eligible, key=lambda block: (-block.priority, block.block_id))

    selected: list[ContextBlock] = []
    used_chars = 0
    for block in ordered:
        if len(selected) >= projection.max_blocks:
            break
        block_chars = len(block.content)
        if used_chars + block_chars > projection.max_chars:
            continue
        selected.append(block)
        used_chars += block_chars
    return tuple(selected)


def render_context_blocks(blocks: Iterable[ContextBlock]) -> str:
    """Render selected descriptors without creating another prompt/runtime protocol."""
    rendered: list[str] = []
    for block in blocks:
        provenance = f"source={block.source}"
        if block.version is not None:
            provenance += f" version={block.version}"
        if block.ref is not None:
            provenance += f" ref={block.ref}"
        rendered.append(
            f"[{block.kind}:{block.block_id} {provenance}]\n{block.content}"
        )
    return "\n\n".join(rendered)


def make_context_projection_prompt(base_prompt: str) -> AgentMiddleware:
    """Build official dynamic_prompt middleware for a bounded projection."""

    @dynamic_prompt
    def context_projection_prompt(request: ModelRequest) -> str:
        projection = request.runtime.context
        if not isinstance(projection, ContextProjection):
            return base_prompt
        selected = select_context_blocks(projection)
        rendered = render_context_blocks(selected)
        if not rendered:
            return base_prompt
        return f"{base_prompt}\n\n## Current validated context\n{rendered}"

    return context_projection_prompt
