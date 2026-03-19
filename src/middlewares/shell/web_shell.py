from langchain.agents.middleware import (
    ShellToolMiddleware,
    DockerExecutionPolicy,
    RedactionRule,
)


web_shell_middleware = ShellToolMiddleware(
    workspace_root="/sandbox",
    execution_policy=DockerExecutionPolicy(
        image="python:3.11-slim",
    ),
    redaction_rules=[
        RedactionRule(pii_type="api_key", detector=r"sk-[a-zA-Z0-9]{20,}"),
        RedactionRule(pii_type="password", detector=r"password[=:]\s*\S+"),
        RedactionRule(pii_type="token", detector=r"token[=:]\s*\S+"),
        RedactionRule(pii_type="secret", detector=r"secret[=:]\s*\S+"),
    ],
    startup_commands=[
        "pip install --quiet requests httpx",
        "export PYTHONUNBUFFERED=1",
    ],
)
