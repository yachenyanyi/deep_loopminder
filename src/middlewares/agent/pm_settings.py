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
