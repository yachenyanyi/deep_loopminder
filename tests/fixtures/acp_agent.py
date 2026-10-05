"""Real stdio ACP fixture using the official SDK; it never invokes a model."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from acp import PROTOCOL_VERSION, RequestError, run_agent, text_block
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AuthenticateResponse,
    CloseSessionResponse,
    ForkSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionForkCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    ToolCallUpdate,
)


class FixtureAgent:
    def __init__(self, profile, state_file):
        self.full = profile == "full"
        self.state_file = Path(state_file)
        self.sessions = (
            json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        )
        self.cancelled = {}
        self.model = "small"

    def on_connect(self, client):
        self.client = client

    def metadata(self):
        return {
            "modes": SessionModeState(
                current_mode_id="safe",
                available_modes=[
                    SessionMode(id="safe", name="Safe"),
                    SessionMode(id="edit", name="Edit"),
                ],
            ),
            "config_options": [
                SessionConfigOptionSelect(
                    id="model",
                    name="Model",
                    type="select",
                    current_value=self.model,
                    options=[
                        SessionConfigSelectOption(value="small", name="Small"),
                        SessionConfigSelectOption(value="large", name="Large"),
                    ],
                )
            ],
        }

    def save(self):
        self.state_file.write_text(json.dumps(self.sessions))

    async def initialize(self, protocol_version, **kwargs):
        sessions = (
            SessionCapabilities(
                list=SessionListCapabilities(),
                close=SessionCloseCapabilities(),
                fork=SessionForkCapabilities(),
            )
            if self.full
            else SessionCapabilities()
        )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(
                name="fixture-full" if self.full else "fixture-basic", version="1"
            ),
            agent_capabilities=AgentCapabilities(
                load_session=self.full, session_capabilities=sessions
            ),
        )

    async def new_session(self, cwd, mcp_servers, **kwargs):
        session_id = uuid4().hex
        self.sessions[session_id] = {"cwd": cwd, "history": []}
        self.save()
        return NewSessionResponse(session_id=session_id, **self.metadata())

    async def load_session(self, cwd, session_id, mcp_servers, **kwargs):
        if not self.full:
            raise RequestError.method_not_found("session/load")
        if session_id not in self.sessions:
            raise RequestError.resource_not_found(session_id)
        return LoadSessionResponse(**self.metadata())

    async def prompt(self, session_id, prompt, **kwargs):
        text = "".join(block.text for block in prompt if block.type == "text")
        self.sessions[session_id]["history"].append(text)
        self.save()
        if text == "block":
            event = self.cancelled.setdefault(session_id, asyncio.Event())
            await self.client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk", content=text_block("started")
                ),
            )
            await event.wait()
            return PromptResponse(stop_reason="cancelled")
        if text.startswith("permission:"):
            kind = text.split(":")[1]
            options = [
                PermissionOption(
                    option_id="permanent", name="Always", kind="allow_always"
                )
            ]
            if kind != "always":
                options.append(
                    PermissionOption(option_id="once", name="Once", kind="allow_once")
                )
            permission = await self.client.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id="test-tool",
                    kind="read" if kind == "always" else kind,
                    title="Fixture operation",
                    raw_input={"path": "sample.txt"},
                ),
                options=options,
            )
            text = permission.outcome.model_dump_json(by_alias=True, exclude_none=True)
        elif text == "history":
            text = "|".join(self.sessions[session_id]["history"])
        await self.client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk", content=text_block(text)
            ),
            **{"fixture.marker": "preserved"},
        )
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):
        self.cancelled.setdefault(session_id, asyncio.Event()).set()

    async def list_sessions(self, cwd=None, cursor=None, **kwargs):
        return ListSessionsResponse(
            sessions=[
                SessionInfo(session_id=key, cwd=value["cwd"], title="fixture")
                for key, value in self.sessions.items()
                if cwd is None or cwd == value["cwd"]
            ]
        )

    async def set_session_mode(self, session_id, mode_id, **kwargs):
        return SetSessionModeResponse()

    async def set_config_option(self, config_id, session_id, value, **kwargs):
        self.model = value
        return SetSessionConfigOptionResponse(
            config_options=self.metadata()["config_options"]
        )

    async def fork_session(self, session_id, cwd, mcp_servers, **kwargs):
        response = await self.new_session(cwd, mcp_servers)
        self.sessions[response.session_id]["history"] = list(
            self.sessions[session_id]["history"]
        )
        self.save()
        return ForkSessionResponse(session_id=response.session_id, **self.metadata())

    async def close_session(self, session_id, **kwargs):
        return CloseSessionResponse()

    async def authenticate(self, method_id, **kwargs):
        return AuthenticateResponse()


if __name__ == "__main__":
    asyncio.run(
        run_agent(FixtureAgent(sys.argv[1], sys.argv[2]), use_unstable_protocol=True)
    )
