from langchain.agents.middleware import ToolRetryMiddleware


retry_middleware = ToolRetryMiddleware(
    max_retries=3,
    initial_delay=1.0,
    backoff_factor=2.0,
)
