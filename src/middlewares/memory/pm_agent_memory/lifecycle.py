"""Connect PM continuity to official LangChain compaction and graph lifecycle."""

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_config

from .jobs import PMMemory


class PMMemorySummarizationMiddleware(SummarizationMiddleware):
    """Generate a readable handoff before official message compaction.

    Uses the official public before-model hook and token trigger. Summary
    selection, message pairing and replacement remain owned by LangChain.
    """

    def __init__(
        self,
        jobs: PMMemory,
        model,
        *,
        token_threshold: int = 300_000,
        keep_messages: int = 20,
    ):
        """Bind the continuity writer and the official compaction configuration."""
        self.jobs = jobs
        self.token_threshold = token_threshold
        self.keep_messages = keep_messages
        super().__init__(
            model,
            trigger=("tokens", token_threshold),
            keep=("messages", keep_messages),
            token_counter=count_tokens_approximately,
        )

    async def abefore_model(self, state, runtime):
        """Wait for durable handoff before allowing the official summary update."""
        messages = state["messages"]
        if (
            len(messages) > self.keep_messages
            and self.token_counter(messages) >= self.token_threshold
        ):
            scope = get_config()["configurable"]
            if runtime.store is not self.jobs.store:
                raise ValueError(
                    "PM compaction and memory jobs must share the same Store"
                )
            await self.jobs.prepare_handoff(
                str(scope["project_id"]), str(scope["thread_id"]), messages
            )
        return await super().abefore_model(state, runtime)
