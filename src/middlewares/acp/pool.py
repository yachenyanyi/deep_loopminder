"""Own ACP connections bound to one provider and one project directory.

Workers run as native Windows processes. Read-only phases are bounded by the
adapter's own permission mode and the host's grant/approval checks, not by an
operating-system read-only mount. Records state exactly that, so callers never
confuse a permission boundary with an OS-level guarantee.
"""

import asyncio
import hashlib
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from src.middlewares.approval.builtin import AllowlistProvider
from src.middlewares.approval.config import ApprovalProviderConfig, resolve_provider
from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
    SecurityGrant,
)

from .control import ACPWorker

# Native Windows Workers rely on the adapter's own mode and permission rules
# (Claude Code permission modes, Codex approval/sandbox policy) plus host
# grants. That is a permission boundary, not an OS-level read-only guarantee.
NATIVE_READONLY_MECHANISM = (
    "native Windows ACP adapter bounded by its own permission mode, "
    "host grants and approval callbacks"
)
READONLY_ISOLATION = "native-readonly"

# Modes whose effect is "do not modify the project". The adapter still reports
# the actual value. This list does not restrict what a user may choose; it only
# decides whether the host must warn that a chosen mode can modify the project.
READONLY_MODES = {
    # Claude's default mode still asks to edit and execute, so it is not a
    # read-only mode here. Only plan performs no project writes.
    "claude": frozenset({"plan"}),
    "codex": frozenset({"read-only"}),
}


def mode_can_write(provider_id, mode_id):
    """Report whether a mode is outside the known read-only set.

    Unknown modes are treated as writable: an unrecognised value must not be
    presented as safe. This is a warning signal, not a permission gate.
    """
    return mode_id not in READONLY_MODES.get(provider_id, frozenset())


class ProjectWorkerPool:
    """Own SDK connections bound to one provider and one project directory."""

    def __init__(self, stack, settings):
        """Register provider settings; connections start lazily per directory."""
        self._stack = stack
        self._settings = {entry["provider_id"]: entry for entry in settings}
        self._versions = {provider: self._version(provider) for provider in self._settings}
        self._connections = {}
        self._readonly_connections = {}
        self._execution_connections = {}
        self._lock = asyncio.Lock()

    def _version(self, provider):
        """Fingerprint the settings that would change a running Worker."""
        entry = self._settings[provider]
        inherited = {name: value for name, value in os.environ.items()
                     if name.startswith("ANTHROPIC_") or name in {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "DISABLE_AUTOUPDATER"}}
        if provider == "codex":
            inherited = {name: value for name, value in os.environ.items()
                         if name in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_CONFIG"}}
        effective_env = (entry.get("env") or {}) if entry.get("managed") else (
            {**inherited, **(entry.get("env") or {})} if provider in {"claude", "codex"} else entry.get("env") or {}
        )
        versioned = {"settings": entry, "effective_env": effective_env}
        return hashlib.sha256(json.dumps(versioned, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def configured(self, provider_id):
        """Report whether the provider has any usable settings."""
        return provider_id in self._settings

    async def get(self, provider_id, directory):
        """Return a plain live connection for unexplored, non-governed use."""
        cwd = await asyncio.to_thread(lambda: str(Path(directory).resolve(strict=True)))
        key = (provider_id, cwd)
        async with self._lock:
            worker = self._connections.get(key)
            if worker is not None and worker.process.returncode is None:
                return worker
            settings = self._settings.get(provider_id)
            if settings is None:
                raise ValueError("Worker 提供方未配置")
            grant = SecurityGrant(frozenset(settings["grants"]))
            worker = await self._stack.enter_async_context(
                ACPWorker(
                    provider_id,
                    self._command(settings),
                    cwd,
                    grants=lambda _ref, grant=grant: grant,
                    approval=(
                        resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval") else None
                    ),
                    env=settings.get("env"),
                    timeout=settings.get("timeout", 180),
                )
            )
            self._connections[key] = worker
            return worker

    @staticmethod
    def _command(settings):
        """Resolve the adapter command; managed workers get it from the runtime."""
        command = settings.get("command")
        if command:
            return tuple(command)
        runtime = settings.get("runtime_dir")
        adapter = "claude-agent-acp" if settings.get("provider_id") == "claude" else "codex-acp"
        if not isinstance(runtime, str) or not runtime:
            raise ValueError("Worker 运行环境未配置")
        entry = Path(runtime) / "node_modules" / "@agentclientprotocol" / adapter / "dist" / "index.js"
        if not entry.is_file():
            raise ValueError(f"运行环境缺少 {adapter} 适配器")
        node = settings.get("node") or "node"
        return (node, str(entry))

    async def get_readonly(self, provider_id, directory, *, mode_id=None, ephemeral_home=None):
        """Start the native adapter for a discussion or review phase.

        The boundary fact describes the adapter's own permission mode and host
        checks. A mode that can modify the project is reported as PARTIAL with
        an explicit gap; it is never presented as a read-only guarantee. When
        the caller does not name the mode yet, the fact says it was not
        confirmed rather than guessing a value.
        """
        if os.name != "nt":
            raise ValueError("原生 Worker 仅适用于 Windows 宿主")
        settings = self._settings.get(provider_id)
        if settings is None:
            raise ValueError("Worker 提供方未配置")
        # A writable mode is allowed when the user chose it; the warning is
        # carried in the enforcement fact and surfaced by the workbench, not
        # turned into a silent refusal.
        cwd = await asyncio.to_thread(lambda: str(Path(directory).resolve(strict=True)))
        # The mode is part of the key: a cached connection carries the boundary
        # fact of the mode it was opened under, so reusing it after a mode
        # change would record a stale (possibly read-only) capability.
        key = (provider_id, cwd, self._versions.get(provider_id), mode_id)
        async with self._lock:
            if self._version(provider_id) != self._versions[provider_id]:
                raise ValueError("Worker 配置已变化，请重启宿主后重新验证")
            existing = self._readonly_connections.get(key)
            if existing is not None and existing.process.returncode is None:
                return existing
            # A saved mode change leaves the previous mode's connection unused.
            # Close it now instead of keeping an idle adapter process for the
            # rest of the host's lifetime. A connection with an in-flight
            # prompt is left alone; it is closed when its turn ends.
            stale = [item for item in self._readonly_connections
                     if item[:3] == (provider_id, cwd, self._versions.get(provider_id))
                     and item != key]
            for item in stale:
                worker = self._readonly_connections.get(item)
                if worker is None or any(lock.locked() for lock in worker._prompt_locks.values()):
                    continue
                try:
                    await worker.__aexit__(None, None, None)
                except Exception:
                    # Keep the entry; a stale connection that will not close is
                    # not silently forgotten.
                    continue
                self._readonly_connections.pop(item, None)
            grant = SecurityGrant(frozenset(settings["grants"]))
            worker = await self._stack.enter_async_context(
                ACPWorker(
                    provider_id,
                    self._command(settings),
                    cwd,
                    grants=lambda _ref, grant=grant: grant,
                    # Always use the configured policy. A bare allowlist would
                    # silently turn every approval-gated request into a denial
                    # and hide which tools were refused.
                    approval=(
                        resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval") else AllowlistProvider(
                            allowed_tools=["acp.read", "acp.search"]
                        )
                    ),
                    env=self._worker_env(settings),
                    timeout=settings.get("timeout", 180),
                    session_isolation=READONLY_ISOLATION,
                    config_version=self._versions[provider_id],
                    readonly_enforcement=self._readonly_fact(
                        provider_id, mode_id or "unconfirmed"
                    ),
                )
            )
            self._readonly_connections[key] = worker
            return worker

    @staticmethod
    def _worker_env(settings):
        """Provide Worker-only environment without mutating the host process."""
        return settings.get("env")

    @staticmethod
    def _readonly_fact(provider_id, mode_id):
        """Describe the permission boundary honestly; it is not OS-level.

        A writable mode is reported as PARTIAL with an explicit gap so no
        record, UI or later check can read it as a read-only guarantee.
        """
        confirmed = mode_id != "unconfirmed"
        writable = mode_can_write(provider_id, mode_id)
        if confirmed and not writable:
            capability, gap = EnforcementCapability.ENFORCEABLE, None
        elif confirmed:
            capability = EnforcementCapability.PARTIAL
            gap = (f"mode {mode_id} can modify the project; discussion safety relies on "
                   "the confirmation and Clef gate, not on this boundary")
        else:
            # The caller starts the connection before the discussion mode is
            # known. Claiming read-only here would be a guess.
            capability = EnforcementCapability.PARTIAL
            gap = "permission mode not yet confirmed for this phase"
        return EnforcementFact(
            surface="native_acp_permission_mode",
            capability=capability,
            # Only an actually read-only mode enforces the read-only capability.
            enforced_capabilities=(frozenset() if writable
                                   else frozenset({"worker.readonly"})),
            mechanism=f"{NATIVE_READONLY_MECHANISM} (mode={mode_id})",
            gap=gap,
        )

    def observe_live(self, ref):
        """Read a bound live session only; never start or resume one for display."""
        if ref.isolation == READONLY_ISOLATION:
            worker = next((value for key, value in self._readonly_connections.items()
                           if key[:3] == (ref.provider_id, ref.cwd, ref.config_version)), None)
            if worker is None:
                worker = self._execution_connections.get((ref.provider_id, ref.cwd, ref.config_version, ref.thread_id))
        else:
            worker = self._connections.get((ref.provider_id, ref.cwd))
        if worker is None or worker.process.returncode is not None:
            return None
        try:
            observed = worker.observe(ref)
        except (ValueError, PermissionError, KeyError):
            return None
        return {"context_usage": observed.get("context_usage"),
                "compaction_visibility": observed.get("compaction_visibility", "unknown")}

    async def get_execution(self, ref, intent_id):
        """Continue an idle read-only session as a writable developer session."""
        if ref.isolation != READONLY_ISOLATION:
            raise ValueError("开发必须续接经过商讨的负责人会话")
        if ref.provider_id != "claude":
            raise ValueError("该 Worker 只能用于只读审查，不能切换到开发")
        if not isinstance(intent_id, str) or not re_fullmatch_hex(intent_id):
            raise ValueError("开发请求缺少有效的派工意图")
        if ref.config_version != self._versions.get(ref.provider_id):
            raise ValueError("Worker 配置已变化，不能恢复原负责人会话")
        if self._version(ref.provider_id) != ref.config_version:
            raise ValueError("Worker 环境已变化，不能恢复原负责人会话")
        execution_key = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
        async with self._lock:
            existing = self._execution_connections.get(execution_key)
            if existing is not None:
                if existing.process is None or existing.process.returncode is not None:
                    raise ValueError("上次 Worker 进程的退出尚未确认")
                return existing
            # Close every read-only connection for this project/version, not
            # just one: connections are cached per mode, so several may exist
            # and each owns an adapter process holding the same session.
            readonly_keys = [item for item in self._readonly_connections
                             if item[:3] == (ref.provider_id, ref.cwd, ref.config_version)]
            readonly = [self._readonly_connections[item] for item in readonly_keys]
            if any(lock.locked() for worker in readonly
                   for lock in worker._prompt_locks.values()):
                raise ValueError("负责人还有正在运行的只读请求，不能切换到开发")
            # Close every connection even if one fails; a failure keeps that
            # entry so a later attempt can retry it instead of losing it.
            failure = None
            for item, worker in zip(readonly_keys, readonly):
                try:
                    await worker.__aexit__(None, None, None)
                except Exception as exc:
                    failure = failure or exc
                else:
                    self._readonly_connections.pop(item, None)
            if failure is not None:
                raise ValueError("无法确认部分只读 Worker 已停止；保持隔离等待核对") from failure
            settings = self._settings[ref.provider_id]
            approval = (resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval") else None)
            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                worker = await stack.enter_async_context(ACPWorker(
                    ref.provider_id, self._command(settings), ref.cwd,
                    grants=lambda _ref, grant=SecurityGrant(frozenset(settings["grants"])): grant,
                    approval=approval, env=self._worker_env(settings),
                    timeout=settings.get("timeout", 180), session_isolation=ref.isolation,
                    config_version=ref.config_version,
                ))
            except BaseException:
                await stack.aclose()
                raise
            self._stack.push_async_callback(stack.aclose)
            self._execution_connections[execution_key] = worker
            return worker

    async def release_execution(self, ref):
        """Close the writable session once its official Run has finished.

        Raises when no live connection is tracked: after a host restart the
        adapter child may still exist, and silence is not proof that it
        stopped. Callers must keep the quarantine until it is reconciled.
        """
        key = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
        async with self._lock:
            worker = self._execution_connections.get(key)
            if worker is None:
                raise ValueError("无法确认 Worker 进程已停止；项目保持隔离等待核对")
            try:
                await worker.__aexit__(None, None, None)
            except Exception as exc:
                # Keep the entry so a later reconcile can retry the close.
                raise ValueError("无法确认 Worker 进程已退出；项目保持隔离等待核对") from exc
            if worker.process is not None and worker.process.returncode is None:
                raise ValueError("无法确认 Worker 进程已退出；项目保持隔离等待核对")
            self._execution_connections.pop(key, None)

    def execution_ticket(self, ref, intent_id):
        """Identify the writable session belonging to this authorized Run."""
        if (ref.isolation != READONLY_ISOLATION
                or ref.config_version != self._versions.get(ref.provider_id)
                or not isinstance(intent_id, str) or not re_fullmatch_hex(intent_id)):
            raise ValueError("无法建立可信的 Worker 进程标识")
        return {"provider_id": ref.provider_id, "cwd": ref.cwd,
                "config_version": ref.config_version, "thread_id": ref.thread_id,
                "intent_id": intent_id}

    async def reconcile_shutdown(self, ref, intent_id, ticket):
        """Close a recorded session; a missing process needs no further proof."""
        expected = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
        if (ticket.get("intent_id") != intent_id
                or (ticket.get("provider_id"), ticket.get("cwd"), ticket.get("config_version"),
                    ticket.get("thread_id")) != expected):
            raise ValueError("Worker 进程标识无效")
        await self.release_execution(ref)


def re_fullmatch_hex(value):
    """Accept the 32-character hex identifier used for dispatch intents."""
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)
