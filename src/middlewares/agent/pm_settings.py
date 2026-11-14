"""PM-only settings and .env credentials, independent of project state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from dotenv import dotenv_values, set_key

from src.middlewares.project_state.scope import directory_write_lock

_ROOT = Path(__file__).resolve().parents[3]
_ENV_NAME = re.compile(r"^PM_[A-Z0-9_]+$")
_SENSITIVE = re.compile(r"api[_-]?key|token|secret|password|authorization|credential|(?:^|[_-])auth(?:$|[_-])", re.IGNORECASE)


def enabled() -> bool:
    """Keep other Agent runtimes on their existing configuration paths."""
    return os.environ.get("LOOPMINDER_PM_SETTINGS") == "1"


def current() -> PMSettings | None:
    """Return the PM configuration only in the dedicated PM runtime."""
    return PMSettings() if enabled() else None


def resolve_compaction_settings(values: dict, *, use_environment: bool = True) -> tuple[int, int]:
    """Resolve workspace thresholds once for the workbench and PM assembly."""
    def pick(stored, env, default):
        for source in (stored, env):
            if source is None:
                continue
            try:
                return int(source)
            except (ValueError, TypeError):
                continue
        return default

    return (
        pick(values.get("compaction_token_threshold"),
             os.getenv("PM_COMPACTION_TOKEN_THRESHOLD") if use_environment else None, 300000),
        pick(values.get("context_token_limit"),
             os.getenv("PM_CONTEXT_TOKEN_LIMIT") if use_environment else None, 500000),
    )


def secret_name(category: str, identity: str) -> str:
    """Create stable, case-insensitive Windows environment variable names."""
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32].upper()
    return f"PM_{category.upper()}_{digest}"


def protect_runtime_config(entry: dict, category: str) -> tuple[dict, dict[str, str]]:
    """Move Worker environment and explicitly named credentials into .env."""
    if not isinstance(entry, dict):
        raise ValueError("PM 运行配置必须是对象")
    identity = entry.get("provider_id") or entry.get("name")
    if not isinstance(identity, str) or not identity:
        raise ValueError("PM Worker 或角色缺少标识")
    secrets = {}

    def reference(value, path):
        if isinstance(value, dict) and set(value) == {"secret_env"}:
            if not isinstance(value["secret_env"], str) or not _ENV_NAME.fullmatch(value["secret_env"]):
                raise ValueError("PM 凭据引用无效")
            return dict(value)
        if not isinstance(value, str):
            raise ValueError("PM 凭据必须是字符串")
        if not value:
            return ""
        # Preserve the original Worker environment identity used by migrations.
        env_path = len(path) == 2 and path[0] == "env"
        key = secret_name("worker_env" if env_path else category,
                          identity + "/" + (path[-1] if env_path else "/".join(path)))
        secrets[key] = value
        return {"secret_env": key}

    def convert(value, path=(), protected=False):
        if isinstance(value, dict):
            if set(value) == {"secret_env"}:
                return reference(value, path)
            result = {}
            for name, child in value.items():
                if not isinstance(name, str):
                    raise ValueError("PM 运行配置字段名无效")
                child_path = (*path, name)
                if name == "env":
                    if not isinstance(child, dict):
                        raise ValueError("PM Worker 环境配置无效")
                    if any(not isinstance(key, str) or not key for key in child):
                        raise ValueError("PM Worker 环境变量名称无效")
                    result[name] = {key: reference(item, (*child_path, key)) for key, item in child.items()}
                elif isinstance(child, str) and (protected or _SENSITIVE.search(name)):
                    result[name] = reference(child, child_path)
                else:
                    result[name] = convert(child, child_path, protected or bool(_SENSITIVE.search(name)))
            return result
        if isinstance(value, list):
            result, protect_next = [], False
            for index, child in enumerate(value):
                child_path = (*path, str(index))
                if path and path[-1] == "command" and isinstance(child, str):
                    flag = child.startswith("--") and _SENSITIVE.search(child.split("=", 1)[0])
                    if protect_next or (flag and "=" in child):
                        result.append(reference(child, child_path))
                    else:
                        result.append(convert(child, child_path, protected))
                    protect_next = bool(flag and "=" not in child)
                else:
                    result.append(convert(child, child_path, protected))
                    protect_next = False
            return result
        if isinstance(value, str):
            try:
                parsed = urlsplit(value)
                sensitive_url = bool(parsed.username or parsed.password or any(
                    _SENSITIVE.search(name) for name, _ in parse_qsl(parsed.query, keep_blank_values=True)))
            except ValueError:
                sensitive_url = False
            if protected or sensitive_url:
                return reference(value, path)
        return value

    return convert(entry), secrets


def resolve_runtime_config(value, settings: PMSettings, *, path=()):
    """Resolve explicit PM .env references for a Worker or subagent."""
    if isinstance(value, dict):
        if set(value) == {"secret_env"}:
            sensitive = any(_SENSITIVE.search(str(part)) for part in path) or "command" in path
            secret = settings.secret(value["secret_env"], redact=False)
            if secret is None:
                raise ValueError("PM 运行配置的 .env 凭据未配置")
            if sensitive or (len(path) >= 2 and path[-2] == "env"):
                # Environment keys are arbitrary: even an innocuous-looking
                # name can contain a credential. Short values are exact-match
                # only so common flags do not corrupt unrelated identifiers.
                from src.middlewares.trajectory.middleware import register_secret

                register_secret(secret, exact_only=len(secret) < 8)
            if len(path) >= 2 and path[-2] == "env":
                try:
                    configuration = json.loads(secret)
                except (TypeError, json.JSONDecodeError):
                    configuration = None
                if configuration is not None:
                    from src.middlewares.trajectory.middleware import register_secret

                    def register_nested(item):
                        if isinstance(item, dict):
                            for nested in item.values():
                                register_nested(nested)
                        elif isinstance(item, list):
                            for nested in item:
                                register_nested(nested)
                        elif isinstance(item, str):
                            register_secret(item, exact_only=len(item) < 8)

                    register_nested(configuration)
            return secret
        return {key: resolve_runtime_config(item, settings, path=(*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_runtime_config(item, settings, path=(*path, str(index))) for index, item in enumerate(value)]
    return value


class PMSettings:
    """A single JSON configuration and a separate .env file for PM secrets."""

    def __init__(self, root: Path | None = None):
        """Allow isolated directories in tests without changing the real files."""
        self.root = (Path(root) if root is not None else
                     Path(os.environ.get("LOOPMINDER_PM_CONFIG_ROOT") or _ROOT))
        self.path = self.root / "settings.json"
        self.env_path = self.root / ".env"

    def read(self) -> dict:
        """Read a complete snapshot; reject malformed nonempty configuration."""
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except UnicodeError as exc:
            raise ValueError("PM settings.json 编码无效；原文件未修改") from exc
        if not text.strip():
            return {}
        try:
            result = json.loads(text)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("PM settings.json 格式无效；原文件未修改") from exc
        if not isinstance(result, dict) or result.get("version", 1) != 1:
            raise ValueError("PM settings.json 格式或版本不受支持；原文件未修改")
        return result

    def secret(self, name: str, *, redact: bool = True) -> str | None:
        """Read a PM secret without expanding unrelated environment variables."""
        if not _ENV_NAME.fullmatch(name):
            raise ValueError("PM 凭据变量名无效")
        values = dotenv_values(self.env_path, interpolate=False) if self.env_path.exists() else {}
        value = values.get(name)
        if value and redact:
            from src.middlewares.trajectory.middleware import register_secret

            register_secret(value)
            if name == "PM_POSTGRES_URI":
                try:
                    parsed = urlsplit(value)
                    if parsed.password:
                        register_secret(unquote(parsed.password))
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
                        if _SENSITIVE.search(key):
                            register_secret(item)
                except ValueError:
                    pass
        return value or None

    def _stage(self, data: bytes) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            return Path(file.name)

    def _write(self, values: dict, secrets: dict[str, str]) -> None:
        """Stage both files before replacement and roll back a failed update."""
        config_tmp = env_tmp = None
        old_env = self.env_path.read_bytes() if self.env_path.exists() else None
        env_replaced = False
        try:
            config_tmp = self._stage((json.dumps(values, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            if secrets:
                env_tmp = self._stage(old_env or b"")
                for name, value in secrets.items():
                    if not _ENV_NAME.fullmatch(name) or not isinstance(value, str) or not value.strip():
                        raise ValueError("PM 凭据无效")
                    set_key(str(env_tmp), name, value, quote_mode="always")
                os.replace(env_tmp, self.env_path)
                env_replaced = True
            os.replace(config_tmp, self.path)
        except BaseException:
            if env_replaced:
                if old_env is None:
                    self.env_path.unlink(missing_ok=True)
                else:
                    rollback = self._stage(old_env)
                    os.replace(rollback, self.env_path)
            raise
        finally:
            for temporary in (config_tmp, env_tmp):
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    async def update(self, mutate, *, secrets: dict[str, str] | None = None):
        """Serialize all PM settings writes, including .env changes."""
        async with directory_write_lock("pm-settings:" + str(self.path.resolve()).casefold(),
                                        lock_directory=self.root / ".pm-settings-lock"):
            values = await asyncio.to_thread(self.read)
            values.setdefault("version", 1)
            result = mutate(values)
            await asyncio.to_thread(self._write, values, secrets or {})
            return result

    async def put(self, section: str, key: str, value):
        """Update one section entry without discarding unrelated settings."""
        def mutate(values):
            values.setdefault(section, {})[key] = value
            return value

        return await self.update(mutate)


class PMCredentials:
    """Use distinct PM .env variables while retaining endpoint-bound identity."""

    def __init__(self, category: str, settings: PMSettings | None = None):
        """Bind credential names to a PM use category."""
        self.category = category
        self.settings = settings or PMSettings()

    async def get(self, identity: str) -> str | None:
        """Read a credential from .env."""
        return await asyncio.to_thread(self.settings.secret, secret_name(self.category, identity))

    async def put(self, identity: str, value: str):
        """Store a credential without exposing it in settings.json."""
        await self.settings.update(lambda _: None, secrets={secret_name(self.category, identity): value})


async def migrate_legacy(store, settings: PMSettings | None = None, *, workers=None) -> list[str]:
    """Import PM preferences once, keeping existing JSON and .env values first.

    The legacy Store and Windows credentials are read but never deleted. A
    credential that cannot be read is listed without exposing its value.
    """
    settings = settings or PMSettings()
    if settings.read().get("migration", {}).get("legacy_imported"):
        return []
    if store is None:
        raise RuntimeError("无法读取原项目存储，PM 设置迁移尚未完成")
    from src.middlewares.agent.model_catalog import (
        ModelCatalog,
        WindowsCredentials,
        validate_model_base_url,
    )

    async def items(namespace):
        result, offset = [], 0
        while True:
            page = await store.asearch(namespace, limit=100, offset=offset)
            result.extend(page)
            if len(page) < 100:
                return result
            offset += len(page)

    legacy_models = await items(("deep_loopminder", "model_endpoints"))
    legacy_modes = await items(("deep_loopminder", "project_worker_modes"))
    default = await store.aget(("deep_loopminder", "model_settings"), "default")
    workspace = await store.aget(("deep_loopminder", "pm_settings"), "workspace")
    clef = await store.aget(("deep_loopminder", "pm_settings"), "clef")
    if workers is None:
        from src.middlewares.acp.server import _load_legacy_managed_workers

        workers = await asyncio.to_thread(_load_legacy_managed_workers)
    old_env = dotenv_values(settings.env_path, interpolate=False) if settings.env_path.exists() else {}

    def old_value(*names):
        return next((old_env.get(name) or os.environ.get(name) for name in names
                     if old_env.get(name) or os.environ.get(name)), None)

    secrets, warnings, models = {}, [], {}
    old_catalog = ModelCatalog(store)
    for item in legacy_models:
        original = dict(item.value)
        provider = {key: original[key] for key in ("provider_id", "name", "base_url", "models", "credential_scope")
                    if key in original}
        provider_id = provider.get("provider_id") or item.key
        try:
            provider["base_url"] = validate_model_base_url(provider.get("base_url"))
            models_list = provider.get("models", [])
            if not isinstance(models_list, list) or any(not isinstance(model, dict) for model in models_list):
                raise ValueError("invalid model list")
            provider["models"] = [{key: model[key] for key in ("id", "enabled", "reasoning_levels") if key in model}
                                  for model in models_list]
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"模型服务 {provider_id} 的原设置无效，PM 设置尚未迁移") from exc
        try:
            identity = (old_catalog.credential_identity(provider_id, provider["base_url"])
                        if provider.get("credential_scope") == "endpoint" else provider_id)
            key = secret_name("model", identity)
            token = await old_catalog.get_credential(provider_id, provider)
            if token:
                secrets[key] = token
                provider["credential_env"] = key
        except Exception:
            warnings.append(f"模型服务 {provider_id} 的凭据需要重新填写")
        models[provider_id] = provider

    old_workers = {}
    for provider, value in workers.items():
        record = {key: value.get(key) for key in ("enabled", "endpoint", "runtime_dir", "model", "node", "revision")}
        endpoint = record.get("endpoint") or ""
        identity = provider + "/" + hashlib.sha256(endpoint.encode()).hexdigest()
        key = secret_name("worker", identity)
        try:
            token = await WindowsCredentials("DeepLoopMinder/worker/").get(identity)
        except Exception:
            token = None
            warnings.append(f"Worker {provider} 的凭据需要重新填写")
        if not token:
            token = (old_value("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY") if provider == "claude"
                     else old_value("CODEX_API_KEY", "OPENAI_API_KEY"))
        if token:
            secrets[key] = token
            record["credential_env"] = key
        old_workers[provider] = record

    account = (clef.value.get("account_id") if clef else None) or old_value("CLOUDFLARE_ACCOUNT_ID")
    try:
        clef_token = await WindowsCredentials("DeepLoopMinder/clef/").get("account") if account else None
    except Exception:
        clef_token = None
        warnings.append("Clef 凭据需要重新填写")
    clef_token = clef_token or old_value("CLOUDFLARE_AUTH_TOKEN", "CLOUDFLARE_AI_API_KEY")
    for key, value in (("PM_CLEF_ACCOUNT_ID", account), ("PM_CLEF_API_KEY", clef_token),
                       ("PM_POSTGRES_URI", old_value("LANGGRAPH_POSTGRES_URI"))):
        if value:
            secrets[key] = value

    def legacy_list(name):
        raw = old_value(name)
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"原 {name} 配置无效，PM 设置尚未迁移") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError(f"原 {name} 配置无效，PM 设置尚未迁移")
        return value

    custom_workers, subagents = [], []
    for name, destination, category in (("ACP_WORKERS_JSON", custom_workers, "worker_field"),
                                         ("ACP_SUBAGENTS_JSON", subagents, "subagent_field")):
        for item in legacy_list(name):
            protected, values = protect_runtime_config(item, category)
            destination.append(protected)
            secrets.update(values)

    async with directory_write_lock("pm-settings:" + str(settings.path.resolve()).casefold(),
                                    lock_directory=settings.root / ".pm-settings-lock"):
        values = settings.read()
        if values.get("migration", {}).get("legacy_imported"):
            return []
        values.setdefault("version", 1)
        model_values = values.setdefault("models", {})
        providers = model_values.setdefault("providers", {})
        for provider, value in models.items():
            providers.setdefault(provider, value)
        if default:
            model_values.setdefault("default", dict(default.value))
        worker_values = values.setdefault("workers", {})
        for provider, value in old_workers.items():
            worker_values.setdefault(provider, value)
        if workspace:
            target = values.setdefault("workspace", {})
            for key, value in workspace.value.items():
                target.setdefault(key, value)
        for name, env_name in (("compaction_token_threshold", "PM_COMPACTION_TOKEN_THRESHOLD"),
                               ("context_token_limit", "PM_CONTEXT_TOKEN_LIMIT")):
            value = old_value(env_name)
            if value:
                try:
                    values.setdefault("workspace", {}).setdefault(name, int(value))
                except ValueError:
                    warnings.append(f"原 {env_name} 配置无效")
        for item in legacy_modes:
            value = dict(item.value)
            project, provider = value.get("project_id"), value.get("provider_id")
            if project and provider:
                values.setdefault("projects", {}).setdefault(project, {}).setdefault("worker_modes", {}).setdefault(provider, value)
        values.setdefault("clef", {}).setdefault("account_env", "PM_CLEF_ACCOUNT_ID")
        values["clef"].setdefault("token_env", "PM_CLEF_API_KEY")
        values.setdefault("runtime", {}).setdefault("postgres_env", "PM_POSTGRES_URI")
        runtime = values["runtime"]
        if custom_workers:
            runtime.setdefault("acp_workers", custom_workers)
        if subagents:
            runtime.setdefault("acp_subagents", subagents)
        for key, env_name in (("data_dir", "LOOPMINDER_DATA_DIR"),
                              ("trajectory_directory", "PM_TRAJECTORY_DIRECTORY")):
            value = old_value(env_name)
            if value:
                runtime.setdefault(key, value)
        poll = old_value("PM_CLAIM_POLL_SECONDS")
        if poll:
            try:
                runtime.setdefault("claim_poll_seconds", float(poll))
            except ValueError:
                warnings.append("原 PM_CLAIM_POLL_SECONDS 配置无效")
        stop_on_error = old_value("PM_TRAJECTORY_STOP_ON_ERROR")
        if stop_on_error:
            runtime.setdefault("trajectory_stop_on_error", stop_on_error.lower() == "true")
        values["migration"] = {"legacy_imported": True, "warnings": warnings}
        missing = {key: value for key, value in secrets.items() if settings.secret(key, redact=False) is None}
        await asyncio.to_thread(settings._write, values, missing)
    return warnings
