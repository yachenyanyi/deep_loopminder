from langchain.agents.middleware import SummarizationMiddleware
from src.models.llm import default_model


role_playing_summary = SummarizationMiddleware(
    model=default_model,
    trigger=("tokens", 30000),
    keep=("messages", 30),
    summary_prompt=(
        """你是一位小说作者，你需要为这段故事写一段总结章节，保证读者可以清楚了解前文时间线，剧情发展，当前事件，出场人物"""
    ),
)
