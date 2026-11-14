"""OpenAI-compatible model endpoints, with secrets kept in Windows credentials."""

import asyncio
import hashlib
import re
from urllib.parse import urlsplit

import httpx
from langgraph.store.base import BaseStore

_NAMESPACE = ("deep_loopminder", "model_endpoints")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def validate_model_base_url(base_url: str) -> str:
    """Validate endpoints before loading or transmitting credentials."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("模型服务地址无效")
    value = base_url.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("模型服务地址无效")
    try:
        url = urlsplit(value)
        _ = url.port
    except ValueError as exc:
        raise ValueError("模型服务地址无效") from exc
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("请输入不含凭据、参数或片段的 HTTP(S) API 地址")
    if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本机模型服务必须使用 HTTPS")
    return value.rstrip("/")


class WindowsCredentials:
    """Store secrets in the current Windows user's generic credentials."""

    def __init__(self, prefix="DeepLoopMinder/model/"):
        """Use a separate target namespace for each credential type."""
        self.prefix = prefix

    def _target(self, identity):
        return self.prefix + identity

    def _put(self, identity, secret):
        import win32cred

        # Some pywin32 builds reject a bytes CredentialBlob outright; passing the
        # text lets win32cred encode it. Reads may return either form.
        win32cred.CredWrite(
            {"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": self._target(identity),
             "CredentialBlob": secret, "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
             "UserName": "DeepLoopMinder"}, 0
        )

    def _get(self, identity):
        import pywintypes
        import win32cred

        try:
            credential = win32cred.CredRead(self._target(identity), win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as exc:
            if exc.winerror == 1168:
                return None
            raise
        blob = credential["CredentialBlob"]
        return blob.decode("utf-16-le") if isinstance(blob, bytes) else blob

    async def put(self, identity, secret):
        """Write one credential without placing it in the project Store.

        No minimum length: local gateways commonly use short keys, and the
        host must not reject a credential the user's own endpoint accepts.
        """
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("凭据不能为空")
        await asyncio.to_thread(self._put, identity, secret)
        from src.middlewares.trajectory.middleware import register_secret

        register_secret(secret)

    async def get(self, identity):
        """Read one credential when a configured endpoint is used."""
        secret = await asyncio.to_thread(self._get, identity)
        from src.middlewares.trajectory.middleware import register_secret

        register_secret(secret)
        return secret


class ModelCatalog:
    """Manage provider metadata and enabled models in the official Store."""

    def __init__(self, store: BaseStore, credentials=None, *, settings=None):
        """Bind to the host Store and credential service."""
        self.store = store
        self.settings = settings
        if settings is not None and credentials is None:
            from .pm_settings import PMCredentials

            credentials = PMCredentials("model", settings)
        self.credentials = credentials or WindowsCredentials()

    async def get(self, provider_id):
        """Return public endpoint metadata, without the credential."""
        if self.settings is not None:
            value = self.settings.read().get("models", {}).get("providers", {}).get(provider_id)
            return dict(value) if value else None
        item = await self.store.aget(_NAMESPACE, provider_id)
        return dict(item.value) if item else None

    async def get_default(self):
        """Return the saved default selection, if any."""
        if self.settings is not None:
            value = self.settings.read().get("models", {}).get("default")
            return dict(value) if value else None
        item = await self.store.aget(("deep_loopminder", "model_settings"), "default")
        return dict(item.value) if item else None

    @staticmethod
    def credential_identity(provider_id: str, base_url: str) -> str:
        """Bind new credentials to both provider and normalized endpoint."""
        digest = hashlib.sha256(validate_model_base_url(base_url).encode("utf-8")).hexdigest()
        return provider_id + "/" + digest

    async def get_credential(self, provider_id: str, provider: dict):
        """Read endpoint-bound credentials, with legacy fallback only for legacy records."""
        base_url = validate_model_base_url(provider.get("base_url"))
        if self.settings is not None and provider.get("credential_env"):
            return self.settings.secret(provider["credential_env"])
        if provider.get("credential_scope") == "endpoint":
            return await self.credentials.get(self.credential_identity(provider_id, base_url))
        if provider.get("credential_scope") is not None:
            raise ValueError("模型凭据配置无效")
        return await self.credentials.get(provider_id)

    async def set_default(self, provider_id, model_id, reasoning_effort=None):
        """Set a default only to a currently enabled configured model."""
        if (not isinstance(provider_id, str) or not _NAME.fullmatch(provider_id)
                or not isinstance(model_id, str) or not model_id.strip()
                or (reasoning_effort is not None and not isinstance(reasoning_effort, str))):
            raise ValueError("默认模型配置无效")
        provider = await self.get(provider_id)
        if provider is not None and (not isinstance(provider.get("models"), list)
                                     or any(not isinstance(m, dict) for m in provider["models"])):
            raise ValueError("模型配置记录无效")
        model = next((m for m in provider["models"] if m.get("id") == model_id), None) if provider else None
        if not model or model.get("enabled") is not True:
            raise ValueError("默认模型不存在或已禁用")
        levels = model.get("reasoning_levels")
        if levels is not None and (not isinstance(levels, list) or any(not isinstance(level, str) for level in levels)):
            raise ValueError("模型配置记录无效")
        if reasoning_effort and (levels is None or reasoning_effort not in levels):
            raise ValueError("模型未配置所选思考等级")
        value = {"provider_id": provider_id, "model_id": model_id, "reasoning_effort": reasoning_effort}
        if self.settings is not None:
            await self.settings.update(lambda values: values.setdefault("models", {}).__setitem__("default", value))
            return value
        await self.store.aput(("deep_loopminder", "model_settings"), "default", value, index=False)
        return value

    async def list(self):
        """List configured endpoints."""
        if self.settings is not None:
            return [dict(value) for value in self.settings.read().get("models", {}).get("providers", {}).values()]
        records, offset = [], 0
        while True:
            page = await self.store.asearch(_NAMESPACE, limit=100, offset=offset)
            records.extend(dict(item.value) for item in page)
            if len(page) < 100:
                return records
            offset += len(page)

    async def save(self, provider_id, base_url, *, name=None, token=None, models=None):
        """Save public settings and optionally replace the separate credential."""
        if not isinstance(provider_id, str) or not _NAME.fullmatch(provider_id):
            raise ValueError("模型服务编号格式无效")
        normalized_url = validate_model_base_url(base_url)
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("凭据不能为空")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("模型服务名称无效")
        previous = await self.get(provider_id)
        if (previous and previous.get("base_url") != normalized_url and token is None
                and await self.get_credential(provider_id, previous)):
            raise ValueError("修改服务地址时请重新提供凭据，避免将原凭据发送到新地址")
        value = {"provider_id": provider_id, "name": name or provider_id,
                 "base_url": normalized_url,
                 "models": models if models is not None else (previous or {}).get("models", [])}
        if token is not None or not previous or previous.get("base_url") != normalized_url:
            value["credential_scope"] = "endpoint"
        elif previous.get("credential_scope") is not None:
            value["credential_scope"] = previous["credential_scope"]
        if not isinstance(value["models"], list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"].strip() or not isinstance(m.get("enabled"), bool) for m in value["models"]):
            raise ValueError("模型列表无效")
        if any(m.get("reasoning_levels") is not None and (not isinstance(m["reasoning_levels"], list) or any(not isinstance(level, str) or not level.strip() for level in m["reasoning_levels"])) for m in value["models"]):
            raise ValueError("思考等级列表无效")
        # Secrets supplied in arbitrary model metadata must never enter Store.
        value["models"] = [{"id": m["id"], "enabled": m["enabled"],
                            "reasoning_levels": m.get("reasoning_levels")} for m in value["models"]]
        if self.settings is not None:
            from .pm_settings import secret_name

            identity = self.credential_identity(provider_id, normalized_url)
            key = secret_name("model", identity)
            if token is not None:
                value["credential_env"] = key
            elif previous and previous.get("credential_env") and previous.get("base_url") == normalized_url:
                value["credential_env"] = previous["credential_env"]
            def save_setting(values):
                values.setdefault("models", {}).setdefault("providers", {})[provider_id] = value
            await self.settings.update(save_setting, secrets={key: token} if token is not None else None)
            return value
        if token is not None:
            await self.credentials.put(self.credential_identity(provider_id, normalized_url), token)
        await self.store.aput(_NAMESPACE, provider_id, value, index=False)
        return value

    async def discover(self, provider_id, *, client=None):
        """Fetch /models without changing which existing models are enabled."""
        provider = await self.get(provider_id)
        if provider is None:
            raise ValueError("模型服务不存在")
        base_url = validate_model_base_url(provider.get("base_url"))
        token = await self.get_credential(provider_id, provider)
        own = client is None
        client = client or httpx.AsyncClient(timeout=15)
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await client.get(base_url + "/models", headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("模型服务返回格式无效")
            return list(dict.fromkeys(item["id"] for item in data["data"] if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]))
        except httpx.HTTPError as exc:
            raise ValueError("模型列表获取失败，请检查服务地址与凭据") from exc
        finally:
            if own:
                await client.aclose()
