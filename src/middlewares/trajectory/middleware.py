"""官方AgentMiddleware轨迹采集；记录投影，不代管业务状态或Run调度。"""

import asyncio
import json
import os
import re
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

_request_identity = ContextVar("trajectory_request_identity", default=None)


class TrajectoryStopped(RuntimeError):
    """严格观察已停止，不得把这个异常转成模型可继续重试的普通工具返回。"""


def tool_errors(value, *, nested=False):
    """识别真实错误信封、ToolMessage和Command，不对普通文本做关键词猜测。"""
    if isinstance(value, Command):
        return tool_errors((value.update or {}).get("messages", []))
    if isinstance(value, ToolMessage):
        if value.status == "error":
            return [value.content]
        return tool_errors(value.content)
    if isinstance(value, str):
        try:
            return tool_errors(json.loads(value), nested=nested)
        except (ValueError, TypeError):
            return []
    if isinstance(value, (list, tuple)):
        return [error for item in value for error in tool_errors(item, nested=nested)]
    if isinstance(value, dict):
        if value.get("ok") is False or value.get("fatal") is True:
            return [value]
        if value.get("status") in ("error", "failed") or value.get("errors"):
            return [value]
        if value.get("error"):
            return [value["error"]]
        if value.get("ok") is True:
            # 成功查询返回的历史Worker失败是数据，不是这次工具调用失败。
            # 新Worker执行异常仍由ACP事件与宿主官方Run状态单独触发停止。
            return []
        if any(
            isinstance(value.get(key), int) and value[key] != 0
            for key in ("exit_code", "exitCode")
        ):
            return [value]
        if value.get("type") == "text":
            return tool_errors(value.get("text"), nested=nested)
        # 工具正文里的历史失败、客户报错、JSON制品都是数据，不能递归当成调用失败。
        # ACP的公开通知有协议嵌套，因此只在其桥接入口显式启用深层检查。
        if nested:
            return [
                error
                for item in value.values()
                for error in tool_errors(item, nested=True)
            ]
        return []
    return []


class TrajectoryRecorder:
    """一个部署进程共享一个记录器，追加JSONL并保存第一处错误现场。"""

    def __init__(self, directory: str | Path):
        """事件目录由宿主指定，不从模型参数获取路径。"""
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "trajectory.jsonl"
        self._lock = threading.RLock()
        self._rounds = {}
        self._inputs = set()
        self._sequence = 0
        self._secret_values = [
            value
            for key, value in os.environ.items()
            if re.search(r"(?:KEY|TOKEN|PASSWORD|SECRET)$", key) and len(value) >= 8
        ]

    def clean(self, value):
        """先脱敏再落盘；不记录隐藏思考，长内容明确标注截断。"""
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {
                str(key): (
                    "[已脱敏]"
                    if re.search(
                        r"(?:api[_-]?key|password|passwd|secret|authorization|access[_-]?token|refresh[_-]?token|cookie)$",
                        str(key),
                        re.I,
                    )
                    else self.clean(item)
                )
                for key, item in list(value.items())[:150]
                if key
                not in {"reasoning", "reasoning_content", "thinking", "signature"}
                or (key == "reasoning" and isinstance(item, int))
            }
        if isinstance(value, (list, tuple)):
            items = [
                self.clean(item)
                for item in value[:100]
                if not (
                    isinstance(item, dict)
                    and item.get("type")
                    in {"thinking", "reasoning", "redacted_thinking"}
                )
            ]
            return items + (
                [f"[其余{len(value) - 100}项已截断]"] if len(value) > 100 else []
            )
        if isinstance(value, str):
            try:
                structured = json.loads(value)
            except (ValueError, TypeError):
                structured = None
            if isinstance(structured, (dict, list)):
                return json.dumps(self.clean(structured), ensure_ascii=False)
            for secret in self._secret_values:
                value = value.replace(secret, "[已脱敏]")
            value = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[已脱敏]", value)
            value = re.sub(
                r"(?i)((?:api[_-]?key|password|passwd|secret|access_token|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
                r"\1[已脱敏]",
                value,
            )
            value = re.sub(
                r"(postgres(?:ql)?://[^\s:/]+:)[^@\s]+@", r"\1[已脱敏]@", value
            )
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已脱敏]", value)
            return value[:16000] + (
                f"\n[内容截断，原长{len(value)}字符]" if len(value) > 16000 else ""
            )
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.clean(str(value))

    def emit(
        self,
        kind,
        phase,
        name,
        *,
        agent="pm",
        span_id=None,
        runtime=None,
        duration_ms=None,
        data=None,
        **identity,
    ):
        """写入公开可见事件，不序列化整个runtime或认证配置。"""
        info = getattr(runtime, "execution_info", None)
        row = {
            "schema_version": 1,
            "id": uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "phase": phase,
            "name": name,
            "agent": agent,
            "span_id": span_id or uuid4().hex,
            "thread_id": str(
                getattr(info, "thread_id", "") or identity.get("thread_id", "")
            ),
            "run_id": str(getattr(info, "run_id", "") or identity.get("run_id", "")),
            "duration_ms": duration_ms,
            "data": data,
        }
        row = self.clean(row)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            # seq是本进程的写入顺序；网页仍以文件游标排序，兼容旧日志和进程重启。
            self._sequence += 1
            row["seq"] = self._sequence
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def fail(self, event):
        """保存首错，不覆盖更早的错误；观察器负责取消官方Run。"""
        with self._lock:
            try:
                with (self.directory / "first-error.json").open(
                    "x", encoding="utf-8"
                ) as stream:
                    json.dump(event, stream, ensure_ascii=False, indent=2)
            except FileExistsError:
                pass

    def check_stop(self):
        """阻止首错或人工停止之后继续调用模型/工具。"""
        if (self.directory / "first-error.json").exists() or (
            self.directory / "stop-request.json"
        ).exists():
            raise TrajectoryStopped("轨迹监控已请求停止；保留现场，不再调用模型或工具")

    def next_round(self, runtime, agent):
        """轮次只用于轨迹展示，不构成运行步骤上限。"""
        info = runtime.execution_info
        key = (str(info.thread_id), str(info.run_id), agent)
        with self._lock:
            self._rounds[key] = self._rounds.get(key, 0) + 1
            return self._rounds[key]

    def native(self, provider, notification, *, stop_on_error=False):
        """ACP公开通知桥接；增量通知用原生toolCallId关联同一工具。"""
        value = notification.model_dump(mode="json", by_alias=True, exclude_none=True)
        update = value.get("update", {})
        category = update.get("sessionUpdate", "notification")
        if category in {"agent_thought_chunk", "reasoning_chunk"}:
            return
        errors = tool_errors(update, nested=True)
        phase = "error" if update.get("status") == "failed" or errors else "message"
        row = self.emit(
            "native",
            phase,
            update.get("title") or category,
            agent=provider,
            thread_id=value.get("sessionId", ""),
            span_id=update.get("toolCallId"),
            data=update,
        )
        if stop_on_error and phase == "error":
            self.fail(row)


class TrajectoryMiddleware(AgentMiddleware):
    """独立可插拔的异步轨迹中间件，放在middleware列表最前方观察最终工具返回。"""

    def __init__(
        self, recorder: TrajectoryRecorder, *, agent="pm", stop_on_error=False
    ):
        """采集默认不改变控制流；宿主可显式开启首错停止。"""
        self.recorder = recorder
        self.agent = agent
        self.stop_on_error = stop_on_error

    def request_capture(self):
        """放在业务中间件最后，记录提示词/工具经过注入后的实际ModelRequest。"""
        return TrajectoryRequestCapture(self)

    def capture_request(self, request, span, round_number, scope):
        """前后两处用相同span，网页用内层快照替换外层，保留原事件供审计。"""
        # 使用官方工具转换器保留调用时的参数契约，不另造Schema推导器。
        schemas = [convert_to_openai_tool(tool) for tool in request.tools]
        model = request.model
        model_name = getattr(model, "model_name", None) or getattr(model, "model", None)
        self.recorder.emit(
            "context",
            "message",
            "本轮可见上下文",
            agent=self.agent,
            span_id=span,
            runtime=request.runtime,
            data={
                "round": round_number,
                "message_count": len(request.messages),
                "system": request.system_message,
                "messages": request.messages,
                "tools": schemas,
                "config": {
                    "model": model_name or type(model).__name__,
                    "settings": request.model_settings,
                },
                "snapshot_scope": scope,
                "available_tools": [
                    tool.get("name", "工具") if isinstance(tool, dict) else tool.name
                    for tool in request.tools
                ],
            },
        )

    async def abefore_agent(self, state, runtime):
        """一次官方图调用是一轮；不把一次模型请求误当成用户轮次。"""
        self.recorder.emit(
            "turn", "start", "轮次开始", agent=self.agent, runtime=runtime
        )

    async def aafter_agent(self, state, runtime):
        """仅正常到达官方after_agent钩子时记结束，异常仍由模型/工具事件呈现。"""
        self.recorder.emit("turn", "end", "轮次结束", agent=self.agent, runtime=runtime)

    async def abefore_model(self, state, runtime):
        """捕获ToolNode在工具包装器外生成的参数校验错误，阻止下一次模型调用。"""
        if self.stop_on_error:
            self.recorder.check_stop()
            # 仅检查上一轮工具响应，避免把历史会话错误当成本次新错误。
            for message in reversed(state.get("messages", [])):
                if not isinstance(message, ToolMessage):
                    break
                errors = tool_errors(message)
                if errors:
                    event = self.recorder.emit(
                        "tool",
                        "error",
                        message.name or "工具参数/返回错误",
                        agent=self.agent,
                        runtime=runtime,
                        span_id=message.tool_call_id,
                        data={"errors": errors},
                    )
                    self.recorder.fail(event)
                    self.recorder.check_stop()

    async def awrap_model_call(self, request, handler):
        """记录输入/上下文、模型耗时、公开输出、工具请求及实际token用量。"""
        if self.stop_on_error:
            self.recorder.check_stop()
        runtime = request.runtime
        round_number = self.recorder.next_round(runtime, self.agent)
        span = uuid4().hex
        started = time.monotonic()
        messages = request.messages
        inputs = [message for message in messages if message.type == "human"]
        for message in inputs:
            key = (
                str(runtime.execution_info.thread_id),
                message.id or str(message.content),
            )
            with self.recorder._lock:
                if key in self.recorder._inputs:
                    continue
                self.recorder._inputs.add(key)
            is_summary = message.additional_kwargs.get("lc_source") == "summarization"
            self.recorder.emit(
                "compaction" if is_summary else "input",
                "message",
                "官方压缩摘要" if is_summary else "用户输入",
                agent=self.agent,
                runtime=runtime,
                data=message,
            )
        self.capture_request(request, span, round_number, "trajectory_wrapper_request")
        self.recorder.emit(
            "model",
            "start",
            "模型调用",
            agent=self.agent,
            span_id=span,
            runtime=runtime,
            data={"round": round_number},
        )
        token = _request_identity.set((span, round_number))
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            self.recorder.emit(
                "model",
                "cancelled",
                "模型取消",
                agent=self.agent,
                span_id=span,
                runtime=runtime,
            )
            raise
        except Exception as error:
            event = self.recorder.emit(
                "model",
                "error",
                "模型异常",
                agent=self.agent,
                span_id=span,
                runtime=runtime,
                duration_ms=(time.monotonic() - started) * 1000,
                data={"type": type(error).__name__, "error": str(error)},
            )
            if self.stop_on_error:
                self.recorder.fail(event)
            raise
        finally:
            _request_identity.reset(token)
        invalid = [
            call
            for message in result.result
            for call in getattr(message, "invalid_tool_calls", [])
        ]
        self.recorder.emit(
            "model",
            "error" if invalid else "end",
            "模型响应",
            agent=self.agent,
            span_id=span,
            runtime=runtime,
            duration_ms=(time.monotonic() - started) * 1000,
            data={
                "round": round_number,
                "messages": result.result,
                "invalid_tool_calls": invalid,
            },
        )
        if invalid and self.stop_on_error:
            event = self.recorder.emit(
                "model",
                "error",
                "模型生成无效工具参数",
                agent=self.agent,
                runtime=runtime,
                span_id=span,
                data={"invalid_tool_calls": invalid},
            )
            self.recorder.fail(event)
            self.recorder.check_stop()
        return result

    async def awrap_tool_call(self, request, handler):
        """记录最终工具结果；严格模式拒绝让Agent在首错后自行继续试错。"""
        if self.stop_on_error:
            self.recorder.check_stop()
        call = request.tool_call
        started = time.monotonic()
        self.recorder.emit(
            "tool",
            "start",
            call["name"],
            agent=self.agent,
            span_id=call["id"],
            runtime=request.runtime,
            data={"args": call["args"]},
        )
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            self.recorder.emit(
                "tool",
                "cancelled",
                call["name"],
                agent=self.agent,
                span_id=call["id"],
                runtime=request.runtime,
            )
            raise
        except Exception as error:
            event = self.recorder.emit(
                "tool",
                "error",
                call["name"],
                agent=self.agent,
                span_id=call["id"],
                runtime=request.runtime,
                duration_ms=(time.monotonic() - started) * 1000,
                data={"type": type(error).__name__, "error": str(error)},
            )
            if self.stop_on_error:
                self.recorder.fail(event)
            raise
        errors = tool_errors(result)
        value = result.update if isinstance(result, Command) else result
        event = self.recorder.emit(
            "tool",
            "error" if errors else "end",
            call["name"],
            agent=self.agent,
            span_id=call["id"],
            runtime=request.runtime,
            duration_ms=(time.monotonic() - started) * 1000,
            data={"result": value, "errors": errors},
        )
        if errors and self.stop_on_error:
            self.recorder.fail(event)
            self.recorder.check_stop()
        return result


class TrajectoryRequestCapture(AgentMiddleware):
    """轨迹中间件的内层采集端；只观察公开请求，不修改请求或模型返回。"""

    def __init__(self, observer):
        """复用外层记录器和请求标识，不建立第二份运行状态。"""
        self.observer = observer

    async def awrap_model_call(self, request, handler):
        """只在外层请求范围内记录，异步并发调用通过ContextVar隔离。"""
        identity = _request_identity.get()
        if identity is not None:
            span, round_number = identity
            self.observer.capture_request(request, span, round_number, "model_request")
        return await handler(request)
