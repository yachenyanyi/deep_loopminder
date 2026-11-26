"""UserConfigurableMiddleware - 用户可选模型/提示词中间件

每次 LLM 调用前从 RunnableConfig.configurable 中读取 model 和 system_prompt，
动态替换本次调用的模型和系统提示词。

使用方式：
    from src.middlewares.agent.user_configurable import UserConfigurableMiddleware

    agent = create_deep_agent(
        model="deepseek:deepseek-chat",
        middleware=[UserConfigurableMiddleware()],
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]},
        config={
            "configurable": {
                "model": "anthropic:claude-sonnet-4-6",
                "system_prompt": "用温柔的语气回复。",
            }
        },
    )

参数说明（均放在 config.configurable 中）：
    - model: str | None  →  传入则切换模型（provider:model 格式），不传则用 Agent 默认模型
    - system_prompt: str | None  →  传入则替换系统提示词，不传则用 Agent 默认提示词
    - 两个参数都是可选的，互不影响
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, NotRequired
from urllib.parse import urlsplit

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model
from langgraph.config import get_config

from .model_catalog import ModelCatalog, validate_model_base_url


def pm_model_catalog(store):
    """Use the PM settings file for every PM model selection."""
    from .pm_settings import PMSettings

    return ModelCatalog(store, settings=PMSettings())


class UserConfigurableMiddleware(AgentMiddleware):
    """允许用户在每次调用时动态指定模型和系统提示词。

    从 config.configurable 中读取以下可选参数：
    - model: str | None  →  override 模型
    - system_prompt: str | None  →  override 系统提示词
    """

    @staticmethod
    def _get_configurable(request: ModelRequest) -> dict[str, Any] | None:
        """从 ModelRequest 中提取 configurable 字典。"""
        try:
            from langgraph.config import get_config
            config = get_config()
            return config.get("configurable", {}) if isinstance(config, dict) else None
        except RuntimeError:
            return None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Apply configured overrides to one model request."""
        configurable = self._get_configurable(request)
        if not configurable:
            return await handler(request)

        overrides: dict[str, Any] = {}

        # 用户指定了模型 → 切换
        if configurable.get("model"):
            try:
                overrides["model"] = init_chat_model(configurable["model"])
            except Exception:
                logging.warning(
                    "UserConfigurableMiddleware: 无法加载模型 '%s'，使用默认模型",
                    configurable["model"],
                )

        # 用户指定了提示词 → 替换
        if configurable.get("system_prompt"):
            overrides["system_message"] = configurable["system_prompt"]

        if overrides:
            request = request.override(**overrides)

        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Apply configured overrides for synchronous callers."""
        import asyncio
        return asyncio.run(self.awrap_model_call(request, handler))


class PMModelSelectionState(AgentState):
    """Remember the active selection in the official thread checkpoint."""

    pm_model_selection: NotRequired[dict | None]
    pm_model_changed: NotRequired[bool]
    pm_model_selection_run_id: NotRequired[str | None]


async def configured_pm_model(catalog: ModelCatalog, selected: dict, *, expected_endpoint_hash: str | None = None):
    """Resolve an enabled catalog selection for PM and background memory."""
    if (not isinstance(selected, dict) or not isinstance(selected.get("provider_id"), str)
            or not selected["provider_id"] or not isinstance(selected.get("model_id"), str)
            or not selected["model_id"] or (selected.get("reasoning_effort") is not None
                                           and not isinstance(selected.get("reasoning_effort"), str))):
        raise ValueError("模型配置记录无效")
    provider = await catalog.get(selected["provider_id"])
    if not provider:
        raise ValueError("所选模型服务不存在")
    if (not isinstance(provider.get("models"), list) or not isinstance(provider.get("base_url"), str)
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not isinstance(item.get("enabled"), bool) for item in provider["models"])):
        raise ValueError("模型配置记录无效")
    base_url = validate_model_base_url(provider["base_url"])
    if expected_endpoint_hash is not None and sha256(base_url.encode("utf-8")).hexdigest() != expected_endpoint_hash:
        raise ValueError("记忆来源的模型服务地址已变化")
    model = next((item for item in provider["models"] if item["id"] == selected["model_id"]), None)
    if not model or not model["enabled"]:
        raise ValueError("所选模型不存在或已禁用")
    levels = model.get("reasoning_levels")
    if levels is not None and (not isinstance(levels, list) or any(not isinstance(level, str) for level in levels)):
        raise ValueError("模型配置记录无效")
    effort = selected.get("reasoning_effort")
    if effort and (levels is None or effort not in levels):
        raise ValueError("模型未配置所选思考等级")
    token = await catalog.get_credential(selected["provider_id"], provider)
    if not token and urlsplit(base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("所选模型服务未配置凭据")
    from langchain_openai import ChatOpenAI

    kwargs = {"model": selected["model_id"], "base_url": base_url, "api_key": token or "not-set"}
    if effort:
        kwargs["reasoning_effort"] = effort
    # Some OpenAI-compatible gateways reject tool-carrying calls unless the
    # request streams and also asks for usage. stream_usage is ChatOpenAI's
    # documented field for include_usage; without it every PM tool call fails
    # at such an endpoint.
    kwargs["streaming"] = True
    kwargs["stream_usage"] = True
    return ChatOpenAI(**kwargs)


async def memory_model_profile(state, store):
    """Freeze a session's public model identity before queuing its messages."""
    catalog = pm_model_catalog(store)
    selected = state.get("pm_model_selection")
    if selected is None:
        selected = await catalog.get_default()
    if selected is None:
        raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
    if not isinstance(selected, dict) or not isinstance(selected.get("provider_id"), str):
        raise ValueError("模型配置记录无效")
    provider = await catalog.get(selected["provider_id"])
    if not provider:
        raise ValueError("所选模型服务不存在")
    base_url = validate_model_base_url(provider.get("base_url"))
    endpoint_hash = sha256(base_url.encode("utf-8")).hexdigest()
    model = await configured_pm_model(catalog, selected, expected_endpoint_hash=endpoint_hash)
    return {"mode": "catalog", "endpoint_hash": endpoint_hash, "selection": {
        key: selected.get(key) for key in ("provider_id", "model_id", "reasoning_effort")
    }}, model


class PMModelSelectionMiddleware(AgentMiddleware):
    """Select a configured PM model without replacing governance instructions."""

    state_schema = PMModelSelectionState

    async def abefore_agent(self, state, runtime):
        """Make this run's selection available to the memory middleware."""
        return await self._select_for_run(state, runtime)

    async def abefore_model(self, state, runtime):
        """Apply selections on resumed runs that bypass the agent entry hook."""
        return await self._select_for_run(state, runtime)

    async def aafter_model(self, state, runtime):
        """模型调用后清除本轮切换标记。"""
        if state.get("pm_model_changed"):
            return {"pm_model_changed": False}

    async def _select_for_run(self, state, runtime):
        info = runtime.execution_info
        run_id = info.run_id if info else None
        if run_id is not None and state.get("pm_model_selection_run_id") == run_id:
            return None

        changes = await self._resolve_selection(state, runtime.store)
        if run_id is None and state.get("pm_model_changed"):
            # Direct graph calls can omit the run ID. Keep the notice until a model call completes.
            changes["pm_model_changed"] = True
        return {**changes, "pm_model_selection_run_id": run_id}

    async def _resolve_selection(self, state, store):
        configured = get_config().get("configurable", {})
        provider = configured.get("model_provider")
        model_name = configured.get("model_name")
        if configured.get("model_use_default") is True:
            if provider or model_name:
                raise ValueError("模型选择相互冲突")
            default = await pm_model_catalog(store).get_default() if store is not None else None
            if default is None:
                raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
            return {"pm_model_selection": default,
                    "pm_model_changed": default != state.get("pm_model_selection")}
        if bool(provider) != bool(model_name):
            raise ValueError("切换模型时必须同时提供模型服务和模型名称")
        if not provider:
            if state.get("pm_model_selection") is None and store is not None:
                default = await pm_model_catalog(store).get_default()
                if default is None:
                    raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
                return {"pm_model_selection": default, "pm_model_changed": False}
            return {"pm_model_changed": False}
        if not isinstance(provider, str) or not isinstance(model_name, str):
            raise ValueError("模型服务或名称无效")
        selected = {"provider_id": provider, "model_id": model_name,
                    "reasoning_effort": configured.get("reasoning_effort")}
        return {"pm_model_selection": selected,
                "pm_model_changed": selected != state.get("pm_model_selection")}

    async def awrap_model_call(self, request: ModelRequest, handler):
        """Resolve the enabled model; do not silently fall back on errors."""
        selected = request.state.get("pm_model_selection")
        if selected is None:
            return await handler(request)
        if request.runtime.store is None:
            raise ValueError("模型选择需要配置 Store")
        catalog = pm_model_catalog(request.runtime.store)
        model_instance = await configured_pm_model(catalog, selected)
        return await handler(request.override(model=model_instance))
