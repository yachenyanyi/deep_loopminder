"""Configure ACP connections using the official Agent Server application lifespan."""

import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace

from starlette.applications import Starlette

from src.middlewares.approval.config import ApprovalProviderConfig, resolve_provider
from src.middlewares.execution.security_policy import SecurityGrant

from .capabilities import worker_candidate
from .control import ACPWorker
from .graph import create_control_graph, create_worker_graph

_workers: dict[str, ACPWorker] = {}
_candidates = []


@asynccontextmanager
async def lifespan(app):
    """Let the ACP SDK own process setup/teardown inside Starlette's lifespan."""
    async with AsyncExitStack() as stack:
        # Register cleanup before initialization so startup failures also clear
        # the live graph references, after the SDK resources have been closed.
        stack.callback(_workers.clear)
        stack.callback(_candidates.clear)
        for settings in json.loads(os.getenv("ACP_WORKERS_JSON") or "[]"):
            provider = settings["provider_id"]
            if provider in _workers:
                raise ValueError(f"duplicate ACP provider: {provider}")
            grant = SecurityGrant(frozenset(settings["grants"]))
            worker = await stack.enter_async_context(
                ACPWorker(
                    provider,
                    settings["command"],
                    settings["cwd"],
                    grants=lambda _ref, grant=grant: grant,
                    approval=(
                        resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval")
                        else None
                    ),
                    env=settings.get("env"),
                    timeout=settings.get("timeout", 180),
                )
            )
            _workers[provider] = worker
            _candidates.append(
                worker_candidate(
                    provider,
                    frozenset(settings["roles"]),
                    worker.initialized,
                    business_capabilities=frozenset(settings.get("capabilities", ())),
                )
            )
        yield


app = Starlette(lifespan=lifespan)


def worker_catalog():
    """返回当前连接的角色/能力事实，不读取或暴露认证配置。"""
    return tuple(
        replace(candidate, runtime_available=bool(
            (worker := _workers.get(candidate.provider_id)) is not None
            and worker.process.returncode is None
        ))
        for candidate in _candidates
    )


def acp_worker():
    """Return the role-routed ACP worker graph registered in langgraph.json."""
    return create_worker_graph(_workers, _candidates)


def acp_control():
    """Return the one-shot control graph registered in langgraph.json."""
    return create_control_graph(_workers)
