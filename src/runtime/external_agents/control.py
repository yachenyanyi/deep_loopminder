"""Thin ACP SDK calls with current grants and existing approval policy."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    RequestPermissionResponse,
    SessionNotification,
)

from src.middlewares.approval.decision import ApprovalDecision
from src.middlewares.approval.provider import ApprovalProvider
from src.middlewares.approval.request import ApprovalRequest
from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
    SecurityDecision,
    SecurityGrant,
    authorize,
)

from .capabilities import UnsupportedControl, negotiated_controls
from .correlation import WorkerRef

GrantSource = Callable[[WorkerRef | None], SecurityGrant]
UpdateSink = Callable[[SessionNotification], Awaitable[None]]
HumanReview = Callable[[ApprovalRequest, ApprovalDecision], Awaitable[bool]]


class _Client:
    """Handle native ACP notifications and permission requests only."""

    def __init__(self, worker):
        self.worker = worker

    async def session_update(self, session_id, update, **meta):
        notification = SessionNotification(
            session_id=session_id, update=update, field_meta=meta or None
        )
        self.worker._update_metadata(session_id, update)
        if self.worker.on_update:
            await self.worker.on_update(notification)

    async def request_permission(self, session_id, tool_call, options, **meta):
        denied = RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        ref = self.worker._refs.get(session_id)
        if ref is None or self.worker.approval is None:
            return denied
        capability = f"external_tool.{tool_call.kind or 'other'}"
        try:
            self.worker._authorize(capability, ref)
            raw = tool_call.raw_input
            request = ApprovalRequest(
                tool_name=f"acp.{tool_call.kind or 'other'}",
                tool_input=raw if isinstance(raw, dict) else {"raw_input": raw},
                agent_id=ref.provider_id,
                thread_id=ref.thread_id,
                context={
                    "worker_ref": ref.to_dict(),
                    "tool_call": tool_call.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "acp_meta": meta,
                },
            )
            decision = await self.worker.approval.aevaluate(request)
            reviewed = False
            if decision.needs_interrupt:
                if self.worker.review is None or not await self.worker.review(
                    request, decision
                ):
                    return denied
                reviewed = True
                # Recheck current authorization and policy after human latency.
                decision = await self.worker.approval.aevaluate(request)
            self.worker._authorize(capability, ref)
            if not decision.allow and not (reviewed and decision.needs_interrupt):
                return denied
            once = next(
                (option for option in options if option.kind == "allow_once"), None
            )
            if once is None:
                return denied
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=once.option_id)
            )
        except Exception:
            # Policy failures must never silently authorize native tool use.
            return denied


class ACPWorker:
    """Use one SDK-managed connection to control any compatible ACP adapter.

    This class stores only live domain bindings and latest official metadata.
    It does not own a session store, event log, transport or provider lifecycle.
    """

    def __init__(
        self,
        provider_id: str,
        command: Sequence[str],
        cwd: str | Path,
        *,
        grants: GrantSource,
        approval: ApprovalProvider | None = None,
        review: HumanReview | None = None,
        on_update: UpdateSink | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 60,
    ):
        """Configure trusted launch arguments and dynamic policy callbacks."""
        if not provider_id.strip() or not command or timeout <= 0:
            raise ValueError("provider_id, command and positive timeout are required")
        self.provider_id = provider_id
        self.command = tuple(command)
        self.cwd = str(Path(cwd).resolve(strict=True))
        if not Path(self.cwd).is_dir():
            raise ValueError("cwd must be a directory")
        self.grants, self.approval, self.review = grants, approval, review
        self.on_update = on_update
        self.env, self.timeout = env, timeout
        self.initialized = None
        self.connection = None
        self.process = None
        self._transport = None
        self._refs: dict[str, WorkerRef] = {}
        self._metadata: dict[str, object] = {}
        self._prompt_locks: dict[str, asyncio.Lock] = {}

    def _authorize(self, capability: str, ref: WorkerRef | None = None) -> None:
        result = authorize(
            required_capability=capability,
            effective_grant=self.grants(ref),
            enforcement=EnforcementFact(
                surface="acp_dispatch",
                capability=EnforcementCapability.ENFORCEABLE,
                enforced_capabilities=frozenset({capability}),
                mechanism="grant check before official SDK dispatch/permission response",
            ),
        )
        if result.decision is not SecurityDecision.ALLOW:
            raise PermissionError(f"{capability}: {result.reason}")

    async def __aenter__(self):
        """Let the official SDK spawn, frame and clean up the adapter process."""
        self._authorize("worker.connect")
        self._transport = spawn_agent_process(
            _Client(self),
            *self.command,
            cwd=self.cwd,
            env={**os.environ, **(self.env or {})},
            transport_kwargs={"stderr": None},
            use_unstable_protocol=True,
        )
        self.connection, self.process = await self._transport.__aenter__()
        try:
            self.initialized = await self._call(
                self.connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_info=Implementation(name="deep-loopminder", version="0.0.1"),
                    client_capabilities=ClientCapabilities(),
                )
            )
            if self.initialized.protocol_version != PROTOCOL_VERSION:
                raise UnsupportedControl(
                    "adapter negotiated an unsupported ACP protocol version"
                )
        except BaseException:
            await self._transport.__aexit__(None, None, None)
            self.connection = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        """Close the SDK connection; provider persistence remains provider-owned."""
        try:
            return await self._transport.__aexit__(exc_type, exc, traceback)
        finally:
            self.connection = None
            self._refs.clear()
            self._metadata.clear()

    async def _call(self, request):
        return await asyncio.wait_for(request, timeout=self.timeout)

    def _bound(self, ref: WorkerRef) -> None:
        if self.connection is None:
            raise RuntimeError("ACP worker is not connected")
        if ref.provider_id != self.provider_id or ref.cwd != self.cwd:
            raise ValueError("worker ref provider/cwd does not match this connection")
        if self._refs.get(ref.session_id) != ref:
            raise ValueError("worker ref must be created or resumed on this connection")

    def _update_metadata(self, session_id, update):
        response = self._metadata.get(session_id)
        if response is None:
            return
        if update.session_update == "config_option_update":
            self._metadata[session_id] = response.model_copy(
                update={"config_options": update.config_options}
            )
        elif update.session_update == "current_mode_update" and response.modes:
            self._metadata[session_id] = response.model_copy(
                update={
                    "modes": response.modes.model_copy(
                        update={"current_mode_id": update.current_mode_id}
                    )
                }
            )

    async def new(
        self,
        *,
        project_id: str,
        task_id: str,
        thread_id: str,
        role: str,
        handoff_id: str | None = None,
        mcp_servers: list | None = None,
    ) -> WorkerRef:
        """Create a native session and associate explicit business references."""
        provisional = WorkerRef(
            self.provider_id,
            "pending",
            project_id,
            task_id,
            thread_id,
            role,
            self.cwd,
            handoff_id,
        )
        if self.connection is None:
            raise RuntimeError("ACP worker is not connected")
        self._authorize("worker.new", provisional)
        response = await self._call(
            self.connection.new_session(cwd=self.cwd, mcp_servers=mcp_servers or [])
        )
        ref = replace(provisional, session_id=response.session_id)
        self._refs[ref.session_id], self._metadata[ref.session_id] = ref, response
        return ref

    async def resume(self, ref: WorkerRef, *, mcp_servers: list | None = None):
        """Load/resume native provider history without copying it into DeepLoop."""
        if self.connection is None:
            raise RuntimeError("ACP worker is not connected")
        if ref.provider_id != self.provider_id or ref.cwd != self.cwd:
            raise ValueError("worker ref provider/cwd does not match this connection")
        self._authorize("worker.resume", ref)
        if "resume" not in negotiated_controls(self.initialized):
            raise UnsupportedControl("adapter does not advertise session load/resume")
        self._refs[ref.session_id] = ref
        try:
            if self.initialized.agent_capabilities.load_session:
                request = self.connection.load_session(
                    session_id=ref.session_id,
                    cwd=self.cwd,
                    mcp_servers=mcp_servers or [],
                )
            else:
                request = self.connection.resume_session(
                    session_id=ref.session_id,
                    cwd=self.cwd,
                    mcp_servers=mcp_servers or [],
                )
            response = await self._call(request)
            self._metadata[ref.session_id] = response
            return response
        except BaseException:
            self._refs.pop(ref.session_id, None)
            raise

    def _controls(self, ref: WorkerRef) -> set[str]:
        response = self._metadata[ref.session_id]
        controls = set(negotiated_controls(self.initialized)) - {"new", "list"}
        if response.modes:
            controls.add("set_mode")
        if response.config_options:
            controls.add("set_config")
        return controls

    def observe(self, ref: WorkerRef) -> dict:
        """Return correlation and official metadata, not guessed worker progress."""
        self._bound(ref)
        self._authorize("worker.observe", ref)
        response = self._metadata[ref.session_id]
        return {
            "worker_ref": ref.to_dict(),
            "initialize": self.initialized.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "session": response.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "supported_controls": sorted(self._controls(ref)),
            "compaction_visibility": "unknown",
            "connection_process_returncode": self.process.returncode,
        }

    async def control(self, ref: WorkerRef, action: str, payload: dict | None = None):
        """Dispatch negotiated native operations after checking current grants."""
        self._bound(ref)
        payload = payload or {}
        action = "cancel" if action == "interrupt" else action
        if action not in self._controls(ref):
            raise UnsupportedControl(f"{action} unsupported; use an advertised action")
        self._authorize(f"worker.{action}", ref)
        if action == "ask":
            blocks = payload.get("prompt")
            if blocks is None:
                text = payload.get("text", "")
                if not text.strip():
                    raise ValueError(
                        "ask requires non-empty text or native ACP prompt blocks"
                    )
                blocks = [text_block(text)]
            lock = self._prompt_locks.setdefault(ref.session_id, asyncio.Lock())
            if lock.locked():
                raise RuntimeError(
                    "a prompt is already in flight; cancel it or wait before asking again"
                )
            async with lock:
                try:
                    return await self._call(
                        self.connection.prompt(session_id=ref.session_id, prompt=blocks)
                    )
                except (TimeoutError, asyncio.CancelledError):
                    # A local timeout must not silently leave a turn running.
                    self._authorize("worker.cancel", ref)
                    await self._call(self.connection.cancel(session_id=ref.session_id))
                    raise
        if action == "cancel":
            return await self._call(self.connection.cancel(session_id=ref.session_id))
        if action == "resume":
            return await self.resume(ref, mcp_servers=payload.get("mcp_servers"))
        if action == "set_mode":
            modes = self._metadata[ref.session_id].modes
            if payload.get("mode_id") not in {
                mode.id for mode in modes.available_modes
            }:
                raise UnsupportedControl("mode_id was not advertised by the session")
            response = await self._call(
                self.connection.set_session_mode(
                    session_id=ref.session_id, mode_id=payload["mode_id"]
                )
            )
            current = self._metadata[ref.session_id]
            self._metadata[ref.session_id] = current.model_copy(
                update={
                    "modes": current.modes.model_copy(
                        update={"current_mode_id": payload["mode_id"]}
                    )
                }
            )
            return response
        if action == "set_config":
            ids = {
                option.id for option in self._metadata[ref.session_id].config_options
            }
            if payload.get("config_id") not in ids:
                raise UnsupportedControl("config_id was not advertised by the session")
            response = await self._call(
                self.connection.set_config_option(
                    session_id=ref.session_id,
                    config_id=payload["config_id"],
                    value=payload["value"],
                )
            )
            current = self._metadata[ref.session_id]
            self._metadata[ref.session_id] = current.model_copy(
                update={"config_options": response.config_options}
            )
            return response
        if action == "close":
            response = await self._call(
                self.connection.close_session(session_id=ref.session_id)
            )
            self._refs.pop(ref.session_id, None)
            self._metadata.pop(ref.session_id, None)
            return response
        if action == "fork":
            response = await self._call(
                self.connection.fork_session(
                    session_id=ref.session_id,
                    cwd=self.cwd,
                    mcp_servers=payload.get("mcp_servers", []),
                )
            )
            child = replace(
                ref,
                session_id=response.session_id,
                task_id=payload.get("task_id", ref.task_id),
                thread_id=payload.get("thread_id", ref.thread_id),
            )
            self._refs[child.session_id], self._metadata[child.session_id] = (
                child,
                response,
            )
            return child
        raise UnsupportedControl(f"use list_sessions() for the {action} query")

    async def list_sessions(self, *, cursor: str | None = None):
        """Read the adapter's native session catalog, with its native cursor."""
        self._authorize("worker.list")
        if "list" not in negotiated_controls(self.initialized):
            raise UnsupportedControl("adapter does not advertise session/list")
        return await self._call(
            self.connection.list_sessions(cwd=self.cwd, cursor=cursor)
        )

    async def authenticate(self, method_id: str):
        """Explicitly invoke an advertised adapter login method, never handle tokens."""
        self._authorize("worker.authenticate")
        if method_id not in {
            method.id for method in self.initialized.auth_methods or []
        }:
            raise UnsupportedControl("auth method was not advertised by the adapter")
        return await self._call(self.connection.authenticate(method_id=method_id))
