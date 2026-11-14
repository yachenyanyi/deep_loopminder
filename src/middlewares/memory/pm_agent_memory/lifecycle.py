"""Connect PM continuity to official LangChain compaction and graph lifecycle."""

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_config

from src.middlewares.project_state.scope import resolve_project_id

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
        dynamic_model: bool = False,
        model_profile_enabled: bool = False,
    ):
        """Bind the continuity writer and the official compaction configuration."""
        self.jobs = jobs
        self.token_threshold = token_threshold
        self.keep_messages = keep_messages
        self.dynamic_model = dynamic_model
        self.model_profile_enabled = model_profile_enabled
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
            config = get_config()
            scope = config["configurable"]
            if runtime.store is not self.jobs.store:
                raise ValueError(
                    "PM compaction and memory jobs must share the same Store"
                )
            selected_model = None
            model_profile = None
            if self.model_profile_enabled:
                from src.middlewares.agent.user_configurable import memory_model_profile

                model_profile, session_model = await memory_model_profile(state, runtime.store)
                if self.dynamic_model:
                    selected_model = session_model
            if model_profile is None:
                await self.jobs.prepare_handoff(resolve_project_id(config), str(scope["thread_id"]), messages)
            else:
                await self.jobs.prepare_handoff(resolve_project_id(config), str(scope["thread_id"]), messages,
                                                model_profile=model_profile)
            if selected_model is not None:
                summarizer = SummarizationMiddleware(
                    selected_model,
                    trigger=("tokens", self.token_threshold),
                    keep=("messages", self.keep_messages),
                    token_counter=count_tokens_approximately,
                )
                return await summarizer.abefore_model(state, runtime)
        return await super().abefore_model(state, runtime)
