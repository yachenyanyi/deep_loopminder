"""PM workbench settings routes backed by the shared settings service."""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from src.middlewares.agent.model_catalog import WindowsCredentials
from src.middlewares.agent.user_configurable import pm_model_catalog
from src.middlewares.project_state.persistence import ProjectStore
from src.middlewares.project_state.scope import directory_write_lock

from .context import forbidden as _forbidden
from .context import get_store
from .context import local as _local


async def providers(request):
    """List or update model endpoints; credentials never appear in responses."""
    if not _local(request):
        return _forbidden()
    catalog = pm_model_catalog(await get_store())
    if request.method == "GET":
        return JSONResponse(await catalog.list())
    try:
        data = await request.json()
        async with directory_write_lock("model-catalog-settings"):
            result = await catalog.save(data.get("provider_id"), data.get("base_url"),
                                        name=data.get("name"), token=data.get("token") or None,
                                        models=data.get("models"))
        return JSONResponse(result)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def discover(request):
    """Fetch one configured OpenAI-compatible model list."""
    if not _local(request):
        return _forbidden()
    try:
        data = await request.json()
        return JSONResponse({"models": await pm_model_catalog(await get_store()).discover(data.get("provider_id"))})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def default_model(request):
    """Read or update the configured default model."""
    if not _local(request):
        return _forbidden()
    catalog = pm_model_catalog(await get_store())
    if request.method == "GET":
        return JSONResponse(await catalog.get_default())
    try:
        data = await request.json()
        return JSONResponse(await catalog.set_default(data.get("provider_id"), data.get("model_id"), data.get("reasoning_effort")))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def clef_settings(request):
    """Configure the Clef account without returning the stored token."""
    if not _local(request):
        return _forbidden()
    from src.middlewares.agent.pm_settings import current

    pm_settings = current()
    store = await get_store()
    if pm_settings is not None:
        if request.method == "GET":
            return JSONResponse({"account_id": pm_settings.secret("PM_CLEF_ACCOUNT_ID") or "",
                                 "account_configured": bool(pm_settings.secret("PM_CLEF_ACCOUNT_ID")),
                                 "token_configured": bool(pm_settings.secret("PM_CLEF_API_KEY"))})
        try:
            data = await request.json()
            account, token = data.get("account_id"), data.get("token")
            if account and (not isinstance(account, str) or not account.isascii() or not account.isalnum()):
                raise ValueError("Cloudflare Account ID 无效")
            if not account and not pm_settings.secret("PM_CLEF_ACCOUNT_ID"):
                raise ValueError("请提供 Cloudflare Account ID")
            if token is not None and (not isinstance(token, str) or not token.strip()):
                raise ValueError("Cloudflare 令牌无效")
            secrets = {name: value for name, value in (("PM_CLEF_ACCOUNT_ID", account), ("PM_CLEF_API_KEY", token)) if value}
            await pm_settings.update(lambda values: values.setdefault("clef", {}).update(
                {"account_env": "PM_CLEF_ACCOUNT_ID", "token_env": "PM_CLEF_API_KEY"}), secrets=secrets)
            return JSONResponse({"account_id": "", "account_configured": True,
                                 "token_configured": bool(pm_settings.secret("PM_CLEF_API_KEY"))})
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    if request.method == "GET":
        item = await store.aget(("deep_loopminder", "pm_settings"), "clef")
        return JSONResponse({"account_id": (item.value.get("account_id") if item else None)
                            or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")})
    try:
        data = await request.json()
        account = data.get("account_id")
        token = data.get("token")
        if not isinstance(account, str) or not account.isascii() or not account.isalnum():
            raise ValueError("Cloudflare Account ID 无效")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ValueError("Cloudflare 令牌无效")
        if token:
            await WindowsCredentials("DeepLoopMinder/clef/").put("account", token)
        await store.aput(("deep_loopminder", "pm_settings"), "clef", {"account_id": account}, index=False)
        return JSONResponse({"account_id": account})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def worker_settings(request):
    """Configure a local Worker without returning its token."""
    if not _local(request):
        return _forbidden()
    from src.middlewares.acp import server as acp_server

    if request.method == "GET":
        provider = request.query_params.get("provider_id", "claude")
        if provider not in {"claude", "codex"}:
            return JSONResponse({"error": "未知的 Worker 提供方"}, status_code=400)
        try:
            settings = await asyncio.to_thread(acp_server.load_managed_worker_settings, provider)
        except (ValueError, OSError):
            return JSONResponse({"error": "负责人设置无法读取，请检查本机配置。"}, status_code=503)
        from src.middlewares.agent.pm_settings import current

        pm_settings = current()
        custom = bool(pm_settings and any(
            item.get("provider_id") == provider for item in pm_settings.read().get("runtime", {}).get("acp_workers", [])
            if isinstance(item, dict)
        ))
        source = "managed" if settings is not None else "custom" if custom else "none"
        if settings is None:
            runtime = Path(__file__).resolve().parents[2] / "workspace" / "tmp" / "pm-readonly-runtime"
            settings = {"enabled": False, "node": "",
                        "endpoint": "https://api.openai.com/v1" if provider == "codex" else "",
                        "model": "gpt-6-luna" if provider == "codex" else "",
                        "runtime_dir": str(runtime) if runtime.is_dir() else "", "revision": None}
        status = acp_server.managed_worker_status(provider)
        return JSONResponse({"settings": settings, "source": source,
                             "restart_required": settings.get("revision") != status["revision"],
                             "available": any(candidate.provider_id == provider and candidate.runtime_available
                                              for candidate in acp_server.worker_catalog()),
                             "error": status["error"]})
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("负责人配置无效")
        token = data.pop("token", None) or None
        provider = data.pop("provider_id", "claude")
        async with directory_write_lock("managed-worker-settings"):
            value = await acp_server.save_managed_worker_settings(data, token=token, provider_id=provider)
        return JSONResponse({"settings": value, "restart_required": True})
    except (ValueError, TypeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def worker_modes(request):
    """Read or save the project default mode and the adapter-reported modes."""
    if not _local(request):
        return _forbidden()
    from src.middlewares.acp import server as acp_server
    from src.middlewares.project_state.worker_modes import pm_worker_modes

    store = await get_store()
    provider = request.query_params.get("provider_id") or "claude"
    if provider not in {"claude", "codex"}:
        return JSONResponse({"error": "未知的 Worker 提供方"}, status_code=400)
    project_id = request.query_params.get("project_id", "")
    modes = pm_worker_modes(store)

    if request.method == "GET":
        saved = await modes.get(project_id, provider) if project_id else None
        directory = None
        if project_id:
            from src.middlewares.project_state.folders import ProjectFolders

            binding = await ProjectFolders(store).get(project_id)
            directory = (binding or {}).get("directory")
        live = acp_server.discover_live_modes(provider, directory)
        from src.middlewares.acp.pool import mode_can_write

        saved_mode = (saved or {}).get("mode_id")
        # Tell the UI which advertised modes can modify the project so it can
        # warn on selection. Unknown modes count as writable.
        live = {**live,
                "writable": [item["id"] for item in live.get("modes", [])
                             if mode_can_write(provider, item["id"])]}
        return JSONResponse({"provider_id": provider, "saved_mode": saved_mode,
                             "saved_mode_can_write": bool(saved_mode) and mode_can_write(provider, saved_mode),
                             "discovery": live})
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("模式设置无效")
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("项目编号无效")
        if await ProjectStore(store).load(project_id) is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        # 有本项目的活动会话时，模式必须来自适配器实际提供的列表；无会话时
        # 仍可保存默认值，派工时适配器会如实拒绝未提供的模式。
        from src.middlewares.project_state.folders import ProjectFolders

        binding = await ProjectFolders(store).get(project_id)
        live = acp_server.discover_live_modes(provider, (binding or {}).get("directory"))
        advertised = {item.get("id") for item in live.get("modes", []) if isinstance(item, dict)}
        mode_id = data.get("mode_id")
        if live.get("available") and mode_id not in advertised:
            offered = "、".join(sorted(item for item in advertised if item))
            raise ValueError(
                f"模式 {mode_id} 不被适配器提供；可用模式：{offered or '（无）'}"
            )
        value = await modes.save(project_id, provider, mode_id)
        return JSONResponse({"saved_mode": value["mode_id"], "provider_id": provider})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def workspace_settings(request):
    """Configure trace capture and PM compaction thresholds; approval stays mandatory."""
    if not _local(request):
        return _forbidden()
    store = await get_store()
    from langgraph.store.memory import InMemoryStore

    durability = "ephemeral" if isinstance(store, InMemoryStore) else "configured_store"
    checkpoint_runtime = "local_dev" if os.getenv("LANGSMITH_LANGGRAPH_API_VARIANT") == "local_dev" else "unknown"
    from src.middlewares.agent.pm_settings import current, resolve_compaction_settings

    namespace = ("deep_loopminder", "pm_settings")
    pm_settings = current()
    saved = await store.aget(namespace, "workspace") if pm_settings is None else None
    saved_values = pm_settings.read().get("workspace", {}) if pm_settings else (saved.value if saved else {})

    compaction_threshold, context_limit = resolve_compaction_settings(
        saved_values, use_environment=pm_settings is None
    )
    current = {
        "trace_enabled": saved_values.get("trace_enabled", True),
        "compaction_token_threshold": compaction_threshold,
        "context_token_limit": context_limit,
    }
    if request.method == "GET":
        return JSONResponse({**current, "approval": "local_human_required",
                             "store_durability": durability,
                             "checkpoint_runtime": checkpoint_runtime})
    try:
        data = await request.json()
        if not isinstance(data, dict) or type(data.get("trace_enabled")) is not bool:
            raise ValueError("轨迹设置无效")
        value = {"trace_enabled": data["trace_enabled"]}
        for name, floor in (("compaction_token_threshold", 1000), ("context_token_limit", 1000)):
            if name in data and data[name] is not None:
                parsed = int(data[name])
                if parsed < floor:
                    raise ValueError(f"{name} 不能小于 {floor}")
                value[name] = parsed
            elif name in saved_values:
                # 只保存轨迹开关时不得清掉已存的阈值。
                value[name] = saved_values[name]
        if pm_settings is not None:
            await pm_settings.update(lambda values: values.setdefault("workspace", {}).update(value))
        else:
            await store.aput(namespace, "workspace", value, index=False)
        return JSONResponse({**value, "approval": "local_human_required", "store_durability": durability,
                             "checkpoint_runtime": checkpoint_runtime})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def runtime_settings(request):
    """Configure PM-only startup options; never return the database URI."""
    if not _local(request):
        return _forbidden()
    from src.middlewares.agent.pm_settings import current, protect_runtime_config
    from src.middlewares.persistence.pm_runtime import status

    settings = current()
    if settings is None:
        return JSONResponse({"error": "此设置仅适用于 PM 专属服务。"}, status_code=404)
    try:
        snapshot = settings.read()
        runtime = snapshot.get("runtime", {})
        if request.method == "GET":
            for key, category in (("acp_workers", "worker_field"), ("acp_subagents", "subagent_field")):
                for entry in runtime.get(key, []):
                    _, exposed = protect_runtime_config(entry, category)
                    if exposed:
                        raise ValueError("PM 运行配置含未迁移的敏感值，请检查本机设置文件。")
            return JSONResponse({
                "database": status(),
                "middleware": {name: runtime.get("middleware", {}).get(name, True)
                               for name in ("memory", "trajectory", "acp")},
                "port": runtime.get("port", 2031),
                "n_jobs_per_worker": runtime.get("n_jobs_per_worker", 10),
                "claim_poll_seconds": runtime.get("claim_poll_seconds", 5),
                "data_dir": runtime.get("data_dir", ""),
                "trajectory_directory": runtime.get("trajectory_directory", ""),
                "trajectory_stop_on_error": runtime.get("trajectory_stop_on_error", False),
                "acp_workers": runtime.get("acp_workers", []),
                "acp_subagents": runtime.get("acp_subagents", []),
                "restart_required": True,
            })
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("PM 运行设置无效")
        changes, secrets = {}, {}
        if "middleware" in data:
            switches = data["middleware"]
            if (not isinstance(switches, dict) or set(switches) - {"memory", "trajectory", "acp"}
                    or any(type(value) is not bool for value in switches.values())):
                raise ValueError("PM 中间件开关无效")
            changes["middleware"] = {**runtime.get("middleware", {}), **switches}
        for key, lower, upper in (("port", 1024, 65535), ("n_jobs_per_worker", 2, 128)):
            if key in data:
                value = data[key]
                if type(value) is not int or not lower <= value <= upper:
                    raise ValueError(f"{key} 超出有效范围")
                changes[key] = value
        if "claim_poll_seconds" in data:
            value = data["claim_poll_seconds"]
            if type(value) not in (int, float) or not 0.5 <= value <= 300:
                raise ValueError("领取轮询间隔须在 0.5 到 300 秒之间")
            changes["claim_poll_seconds"] = value
        for key in ("data_dir", "trajectory_directory"):
            if key in data:
                value = data[key]
                if not isinstance(value, str) or (value and not Path(value).is_absolute()):
                    raise ValueError(f"{key} 必须为空或绝对路径")
                changes[key] = value
        if "trajectory_stop_on_error" in data:
            if type(data["trajectory_stop_on_error"]) is not bool:
                raise ValueError("轨迹错误处理设置无效")
            changes["trajectory_stop_on_error"] = data["trajectory_stop_on_error"]
        for key, category in (("acp_workers", "worker_field"), ("acp_subagents", "subagent_field")):
            if key not in data:
                continue
            value = data[key]
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError("PM Worker 和子代理配置必须是对象列表")
            protected_entries = []
            for entry in value:
                protected, extracted = protect_runtime_config(entry, category)
                protected_entries.append(protected)
                secrets.update(extracted)
            changes[key] = protected_entries
        uri = data.get("database_uri")
        if uri is not None:
            if not isinstance(uri, str) or not uri.strip():
                raise ValueError("数据库连接不能为空")
            parsed = urlsplit(uri)
            if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("请输入有效的 PostgreSQL 连接地址")
            changes["postgres_env"] = "PM_POSTGRES_URI"
            secrets["PM_POSTGRES_URI"] = uri
        await settings.update(lambda values: values.setdefault("runtime", {}).update(changes),
                              secrets=secrets)
        return JSONResponse({"saved": True, "restart_required": True})
    except (ValueError, TypeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


