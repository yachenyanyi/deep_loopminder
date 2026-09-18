"""Token budget policy primitives for bounded agent execution.

This module deliberately does not implement model/tool call counting, retries,
timeouts, routing, or delegation lifecycle. Those remain owned by the official
LangChain/LangGraph/Deep Agents facilities described in issue #16.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable


class UsageConfidence(str, Enum):
    """How trustworthy the recorded token usage is."""

    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Usage attributable to one workflow leg."""

    tokens: int | None
    confidence: UsageConfidence = UsageConfidence.VERIFIED

    def __post_init__(self) -> None:
        if self.tokens is not None and self.tokens < 0:
            raise ValueError("tokens must be non-negative")
        if self.tokens is None and self.confidence is UsageConfidence.VERIFIED:
            raise ValueError("verified usage requires a token count")


@dataclass(frozen=True, slots=True)
class BudgetContext:
    """Immutable token-budget envelope.

    ``max_tokens`` is the trusted ceiling for this scope. ``consumed_tokens``
    only contains machine-readable usage that could actually be measured.
    Unknown or partial usage is represented by ``usage_unverifiable`` and
    conservatively closes remaining capacity rather than treating uncertainty
    as verified zero cost.
    """

    scope_id: str
    max_tokens: int
    consumed_tokens: int = 0
    usage_unverifiable: bool = False

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("scope_id must not be empty")
        if self.max_tokens < 0:
            raise ValueError("max_tokens must be non-negative")
        if self.consumed_tokens < 0:
            raise ValueError("consumed_tokens must be non-negative")

    @property
    def remaining_tokens(self) -> int:
        """Return trusted remaining capacity, failing closed on uncertain usage."""

        if self.usage_unverifiable:
            return 0
        return max(0, self.max_tokens - self.consumed_tokens)

    @property
    def exhausted(self) -> bool:
        """Return whether no trusted capacity remains for another expensive leg."""

        return self.remaining_tokens == 0

    def narrow(self, *, scope_id: str, requested_max_tokens: int | None = None) -> "BudgetContext":
        """Create a child envelope that can only narrow the parent's remainder.

        A child request is never authority to enlarge the parent budget. If the
        parent's usage is uncertain, the child receives no trusted capacity.
        """

        if not scope_id:
            raise ValueError("scope_id must not be empty")
        if requested_max_tokens is not None and requested_max_tokens < 0:
            raise ValueError("requested_max_tokens must be non-negative")

        ceiling = self.remaining_tokens
        if requested_max_tokens is not None:
            ceiling = min(ceiling, requested_max_tokens)
        return BudgetContext(
            scope_id=scope_id,
            max_tokens=ceiling,
            usage_unverifiable=self.usage_unverifiable,
        )

    def charge(self, usage: UsageRecord) -> "BudgetContext":
        """Charge one workflow leg while preserving uncertainty explicitly."""

        measured = usage.tokens or 0
        unverifiable = self.usage_unverifiable or (
            usage.tokens is None or usage.confidence is not UsageConfidence.VERIFIED
        )
        return replace(
            self,
            consumed_tokens=self.consumed_tokens + measured,
            usage_unverifiable=unverifiable,
        )

    def charge_many(self, usages: Iterable[UsageRecord]) -> "BudgetContext":
        """Charge all measurable legs of a compound workflow to this scope."""

        result = self
        for usage in usages:
            result = result.charge(usage)
        return result
