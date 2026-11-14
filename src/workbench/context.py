"""Shared local request and Store access for the PM workbench."""

import asyncio
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

ROOT = Path(__file__).parent
THREADS = ("deep_loopminder", "workbench_threads")
WRITE_LOCK = asyncio.Lock()
_injected_store = None


def run_config(project_id, data):
    """Build the same project and model configuration for chat and resume."""
    configurable = {
        "project_id": project_id,
        "project_state_single_writer": True,
        "workbench_trace": True,
    }
    for key in ("model_use_default",):
        if data.get(key) is True:
            configurable[key] = True
    for key in ("model_provider", "model_name", "reasoning_effort"):
        if data.get(key):
            configurable[key] = data[key]
    return {"configurable": configurable}


async def get_store():
    """Return the same Store used by PM graphs, or an injected test Store."""
    if _injected_store is not None:
        return _injected_store
    try:
        from langgraph_api.store import get_store as runtime_store

        return await runtime_store()
    except Exception:
        from langgraph.config import get_store as context_store

        return context_store()


def local(request) -> bool:
    """Accept only a loopback peer with a local host and matching origin."""
    try:
        peer = ipaddress.ip_address(request.client.host) if request.client else None
    except ValueError:
        return False
    if peer is None or not (peer.is_loopback or getattr(peer, "ipv4_mapped", None) and peer.ipv4_mapped.is_loopback):
        return False
    host = request.headers.get("host", "")
    try:
        parts = urlsplit("http://" + host)
        hostname = parts.hostname
    except ValueError:
        return False
    if hostname not in {"127.0.0.1", "localhost", "::1"} or parts.username or parts.password:
        return False
    origin = request.headers.get("origin")
    return not origin or origin == "http://" + host


def forbidden():
    """Report a rejected nonlocal request."""
    return JSONResponse({"error": "只允许本机同源访问"}, status_code=403)
