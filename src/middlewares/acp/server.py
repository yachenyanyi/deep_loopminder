"""Configure ACP connections using the official Agent Server application lifespan."""

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette

from src.middlewares.agent.model_catalog import WindowsCredentials
from src.middlewares.approval.config import ApprovalProviderConfig, resolve_provider
from src.middlewares.execution.security_policy import SecurityGrant
from src.runtime.routing.models import WorkerCandidate

from .capabilities import worker_candidate
from .control import ACPWorker
from .graph import create_control_graph, create_worker_graph
from .pool import ProjectWorkerPool, observed_modes

_workers: dict[str, ACPWorker] = {}
_candidates = []
_worker_errors = {}
_active_managed_revision = {}
_MANAGED_SUBAGENTS = {
    "claude": {"name": "developer", "graph_id": "acp_worker",
               "description": "开发负责人：先商讨方案、范围和风险，通过门禁后实现并交付。"},
    "codex": {"name": "reviewer", "graph_id": "acp_worker",
              "description": "独立审查者：核查实际交付与验收要求，并报告发现。"},
}

def _worker_settings_path():
    base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "DeepLoopMinder" / "worker-settings.json"


def _validate_managed_worker(value, provider_id="claude", *, check_runtime=True):
    if provider_id not in {"claude", "codex"}:
        raise ValueError("未知的 Worker 提供方")
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("负责人配置无效")
    timeout = value.get("timeout")
    if timeout is not None and (type(timeout) is not int or not 30 <= timeout <= 7200):
        raise ValueError("单轮超时必须是 30 到 7200 之间的整数秒")
    if not value["enabled"]:
        return
    endpoint, runtime, model = (value.get(key) for key in ("endpoint", "runtime_dir", "model"))
    if not isinstance(endpoint, str):
        raise ValueError("负责人 API 地址无效")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("负责人 API 地址必须是无凭据和参数的 HTTPS 地址")
    if not isinstance(runtime, str) or not Path(runtime).is_absolute():
        raise ValueError("请选择已安装的 ACP 适配器目录")
    adapter = "claude-agent-acp" if provider_id == "claude" else "codex-acp"
    if check_runtime and not (
        Path(runtime) / "node_modules" / "@agentclientprotocol" / adapter / "dist" / "index.js"
    ).is_file():
        raise ValueError(f"适配器目录缺少 {adapter}")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("请输入负责人模型名称")
    if not isinstance(value.get("node"), str | None) or len(value.get("node") or "") > 300:
        raise ValueError("Node 可执行文件路径无效")


def _load_legacy_managed_workers():
    path = _worker_settings_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if isinstance(value, dict) and "enabled" in value:
        value = {"claude": value}
    if not isinstance(value, dict) or any(key not in {"claude", "codex"} for key in value):
        raise ValueError("Worker 配置无效")
    for provider, settings in value.items():
        _validate_managed_worker(settings, provider, check_runtime=False)
    return value


def _load_managed_workers():
    from src.middlewares.agent.pm_settings import current

    settings = current()
    if settings is None:
        return _load_legacy_managed_workers()
    value = settings.read().get("workers", {})
    if not isinstance(value, dict) or any(key not in {"claude", "codex"} for key in value):
        raise ValueError("Worker 配置无效")
    for provider, worker in value.items():
        _validate_managed_worker(worker, provider, check_runtime=False)
    return value


def load_managed_worker_settings(provider_id="claude"):
    """Load public local Worker settings; credentials are stored separately."""
    value = _load_managed_workers().get(provider_id)
    if value is None:
        return None
    result = {key: value.get(key) for key in ("enabled", "endpoint", "runtime_dir", "model", "node", "revision")}
    result["timeout"] = value.get("timeout", 300)
    if value.get("credential_env"):
        result["credential_env"] = value["credential_env"]
    return result


def resolve_managed_subagents(specs):
    """Apply the host's managed Worker roles before exposing them to PM."""
    managed_workers = _load_managed_workers()
    for provider, managed_spec in _MANAGED_SUBAGENTS.items():
        managed = managed_workers.get(provider)
        if managed is None:
            continue
        specs = [spec for spec in specs if spec.get("name") != managed_spec["name"]]
        if managed["enabled"]:
            specs.append(dict(managed_spec))
    return specs


async def save_managed_worker_settings(value, token=None, provider_id="claude"):
    """Persist public settings atomically and keep provider credentials in Windows."""
    _validate_managed_worker(value, provider_id)
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise ValueError("负责人凭据无效")
    if token is not None and not value["enabled"]:
        raise ValueError("启用负责人后才能保存凭据")
    previous = await asyncio.to_thread(load_managed_worker_settings, provider_id)
    if value["enabled"]:
        public = {key: value.get(key) for key in ("enabled", "endpoint", "runtime_dir", "model", "node")}
        public["timeout"] = value.get("timeout") if value.get("timeout") is not None else (previous or {}).get("timeout", 300)
    else:
        public = {key: (previous or {}).get(key) for key in ("endpoint", "runtime_dir", "model", "node")}
        public["timeout"] = value.get("timeout") if value.get("timeout") is not None else (previous or {}).get("timeout", 300)
        public["enabled"] = False
    endpoint = public.get("endpoint") or ""
    from src.middlewares.agent.pm_settings import PMCredentials, current, secret_name

    pm_settings = current()
    credentials = PMCredentials("worker", pm_settings) if pm_settings else WindowsCredentials("DeepLoopMinder/worker/")
    identity = provider_id + "/" + hashlib.sha256(endpoint.encode()).hexdigest()
    inherited_token = None if pm_settings else (os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
                       if provider_id == "claude" else os.getenv("CODEX_API_KEY") or os.getenv("OPENAI_API_KEY"))
    if (public["enabled"] and previous and previous.get("endpoint") != endpoint and token is None
            and previous.get("endpoint") and (inherited_token
                or await credentials.get(provider_id + "/" + hashlib.sha256(previous["endpoint"].encode()).hexdigest()))):
        raise ValueError("更换负责人 API 地址时请重新提供凭据")
    if token is not None and pm_settings is None:
        await credentials.put(identity, token)
    changed = not previous or any(previous.get(key) != public.get(key) for key in public) or token is not None
    public["revision"] = uuid.uuid4().hex if changed else previous.get("revision")

    if pm_settings is not None:
        key = secret_name("worker", identity)
        if token is not None:
            public["credential_env"] = key
        elif previous and previous.get("credential_env") and previous.get("endpoint") == endpoint:
            public["credential_env"] = previous["credential_env"]
        if token is None:
            await pm_settings.put("workers", provider_id, public)
        else:
            await pm_settings.update(
                lambda values: values.setdefault("workers", {}).__setitem__(provider_id, public), secrets={key: token}
            )
        return public

    def write():
        path = _worker_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        all_settings = _load_managed_workers()
        all_settings[provider_id] = public
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(all_settings, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    await asyncio.to_thread(write)
    return public


_ProjectWorkerPool = ProjectWorkerPool
_project_workers = None


@asynccontextmanager
async def lifespan(app):
    """Let the ACP SDK own process setup/teardown inside Starlette's lifespan."""
    global _project_workers, _active_managed_revision
    async with AsyncExitStack() as stack:
        # Register cleanup before initialization so startup failures also clear
        # the live graph references, after the SDK resources have been closed.
        stack.callback(_workers.clear)
        stack.callback(_candidates.clear)
        stack.callback(_worker_errors.clear)
        from src.middlewares.agent.pm_settings import (
            PMCredentials,
            current,
            resolve_runtime_config,
        )

        pm_settings = current()
        runtime = pm_settings.read().get("runtime", {}) if pm_settings else {}
        acp_enabled = runtime.get("middleware", {}).get("acp", True)
        app.state.acp_enabled = acp_enabled
        configured_workers = (runtime.get("acp_workers", []) if pm_settings and acp_enabled
                              else [] if pm_settings
                              else json.loads(os.getenv("ACP_WORKERS_JSON") or "[]"))
        if pm_settings:
            configured_workers = [resolve_runtime_config(entry, pm_settings) for entry in configured_workers]
        managed_workers = await asyncio.to_thread(_load_managed_workers) if acp_enabled else {}
        _active_managed_revision = {name: value.get("revision") for name, value in managed_workers.items()}
        if managed_workers:
            configured_workers = [entry for entry in configured_workers if entry.get("provider_id") not in managed_workers]
            for provider, managed in managed_workers.items():
                if not managed["enabled"]:
                    continue
                identity = provider + "/" + hashlib.sha256(managed["endpoint"].encode()).hexdigest()
                try:
                    credentials = PMCredentials("worker", pm_settings) if pm_settings else WindowsCredentials("DeepLoopMinder/worker/")
                    token = await credentials.get(identity)
                except Exception:
                    token = None
                    _worker_errors[provider] = "无法读取本机 Worker 凭据"
                if pm_settings is None:
                    token = token or (os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
                                      if provider == "claude" else os.getenv("CODEX_API_KEY") or os.getenv("OPENAI_API_KEY"))
                if provider == "claude":
                    worker_env = {"ANTHROPIC_BASE_URL": managed["endpoint"], "ANTHROPIC_MODEL": managed["model"]}
                else:
                    worker_env = {"CODEX_CONFIG": json.dumps({
                        "model": managed["model"], "model_provider": "loopminder",
                        "model_providers": {"loopminder": {"name": "Loopminder", "base_url": managed["endpoint"],
                                                          "env_key": "OPENAI_API_KEY", "wire_api": "responses"}},
                    })}
                if token:
                    worker_env["ANTHROPIC_AUTH_TOKEN" if provider == "claude" else "OPENAI_API_KEY"] = token
                else:
                    _worker_errors[provider] = "Worker 凭据未配置"
                configured_workers.append({
                    "provider_id": provider, "roles": [_MANAGED_SUBAGENTS[provider]["name"]],
                    "capabilities": (["repo.read", "repo.write", "files.read", "files.write", "shell.execute", "test.execute"]
                                     if provider == "claude" else ["repo.read", "files.read", "shell.execute", "test.execute"]),
                    "grants": ["worker.connect", "worker.new", "worker.ask", "worker.cancel", "worker.resume",
                               "worker.observe", "worker.steer", "worker.set_mode", "worker.set_config",
                               "external_tool.read", "external_tool.search",
                               "external_tool.edit", "external_tool.execute", "external_tool.fetch", "external_tool.other"],
                    "env": worker_env, "revision": managed.get("revision"), "timeout": managed.get("timeout", 300),
                    "approval": {"use": "src.middlewares.approval.builtin:YamlPolicyProvider", "config": {}},
                    "runtime_dir": managed["runtime_dir"], "node": managed.get("node"),
                    "managed": True,
                })
        review = getattr(app.state, "pm_execution_review", None)
        _project_workers = _ProjectWorkerPool(stack, configured_workers, execution_review=review)
        bootstrap = None
        for settings in configured_workers:
            provider = settings["provider_id"]
            if provider in _workers:
                raise ValueError(f"duplicate ACP provider: {provider}")
            if settings.get("managed"):
                try:
                    if provider in _worker_errors:
                        raise ValueError(_worker_errors[provider])
                    if bootstrap is None:
                        bootstrap = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="loopminder-acp-bootstrap-")))
                    bootstrap_project = bootstrap / (provider + "-project")
                    bootstrap_home = bootstrap / (provider + "-home")
                    await asyncio.to_thread(bootstrap_project.mkdir, parents=True, exist_ok=True)
                    worker = await _project_workers.get_readonly(provider, str(bootstrap_project), ephemeral_home=bootstrap_home)
                except Exception:
                    _worker_errors.setdefault(provider, "负责人只读环境不可用或安全检查未通过")
                    _candidates.append(WorkerCandidate(provider, "external_acp", frozenset(settings["roles"]),
                                                       frozenset(settings.get("capabilities", ())), False))
                    continue
                _workers[provider] = worker
                _candidates.append(worker_candidate(provider, frozenset(settings["roles"]), worker.initialized,
                                                    business_capabilities=frozenset(settings.get("capabilities", ()))))
                continue
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


def project_worker_pool():
    """Return the active project pool, if the server started one."""
    return _project_workers


async def inspect_worker_workspace(provider_id, directory, *, mode_id=None):
    """Inspect a configured project worker without exposing its connection."""
    if _project_workers is None:
        raise ValueError("Worker 连接管理器不可用")
    return await _project_workers.inspect_workspace(provider_id, directory, mode_id=mode_id)


def managed_worker_status(provider_id):
    """Expose public connection status without exposing the runtime registries."""
    return {"revision": _active_managed_revision.get(provider_id), "error": _worker_errors.get(provider_id)}


def discover_live_modes(provider_id, directory=None):
    """Find only modes reported by this provider and the requested project."""
    bootstrap = _workers.get(provider_id)
    if _project_workers is None:
        return observed_modes((bootstrap,), provider_id, directory)
    return _project_workers.discover_live_modes(provider_id, directory, bootstrap=bootstrap)


def acp_worker(governance=None):
    """Return the role-routed ACP worker graph with optional host governance."""
    return create_worker_graph(_workers, _candidates, project_workers=_project_workers,
                               governance=governance)


def acp_control():
    """Return the one-shot control graph registered in langgraph.json."""
    return create_control_graph(_workers, project_workers=_project_workers)
