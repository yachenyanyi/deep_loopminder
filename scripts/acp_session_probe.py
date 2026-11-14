"""Exercise real ACP adapters with independent connections; incurs model usage.

Run from the project root with: python -m scripts.acp_session_probe
The JSON report is written to the OS temporary directory unless --output is given.
No model responses, adapter arguments, credentials or environment values are saved.
"""

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
import uuid
from importlib import metadata
from pathlib import Path

from src.middlewares.acp.capabilities import negotiated_controls
from src.middlewares.acp.control import ACPWorker
from src.middlewares.agent.pm_settings import PMSettings, resolve_runtime_config
from src.middlewares.execution.security_policy import SecurityGrant

GRANTS = SecurityGrant(
    frozenset(
        {
            "worker.connect",
            "worker.new",
            "worker.ask",
            "worker.cancel",
            "worker.resume",
            "worker.close",
        }
    )
)
ALL_CONTROLS = {"ask", "cancel", "new", "resume", "close", "fork", "list", "steer"}


def _package_version(command):
    for arg in command:
        path = Path(arg)
        if path.suffix.lower() != ".js":
            continue
        for parent in path.parents:
            package = parent / "package.json"
            if package.is_file():
                try:
                    data = json.loads(package.read_text(encoding="utf-8"))
                    name, version = data.get("name"), data.get("version")
                    if all(
                        isinstance(x, str) and re.fullmatch(r"[\w@./+-]{1,120}", x)
                        for x in (name, version)
                    ):
                        return {"name": name, "version": version}
                except (OSError, ValueError):
                    pass
                break
    return {"name": None, "version": None}


def _initialization(worker):
    response = worker.initialized
    info = getattr(response, "agent_info", None)
    caps = getattr(response, "agent_capabilities", None)
    safe_info = {}
    for key in ("name", "title", "version"):
        value = getattr(info, key, None)
        if isinstance(value, str) and re.fullmatch(r"[\w .@/+()\-]{1,120}", value):
            safe_info[key] = value
    controls = negotiated_controls(response)
    session = getattr(caps, "session_capabilities", None)
    prompt = getattr(caps, "prompt_capabilities", None)
    return {
        "protocol_version": response.protocol_version,
        "agent_info": safe_info,
        "agent_capabilities": {
            "load_session": getattr(caps, "load_session", None),
            "session": {
                key: getattr(session, key, None) is not None
                for key in ("list", "resume", "close", "fork")
            },
            "prompt": {
                key: getattr(prompt, key, None)
                for key in ("image", "audio", "embedded_context")
            },
        },
        "client_controls": sorted(controls),
        "client_controls_not_advertised_or_supported": sorted(ALL_CONTROLS - controls),
        "scope": "Known protocol fields only; arbitrary extension metadata is excluded to avoid exposing sensitive values.",
    }


def _failure(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return {
            "status": "failed",
            "reason": "timeout",
            "error_type": type(exc).__name__,
        }
    from src.middlewares.acp.capabilities import UnsupportedControl

    if isinstance(exc, UnsupportedControl):
        return {
            "status": "unsupported",
            "reason": "adapter did not advertise the requested operation",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "failed",
        "reason": "adapter or SDK request failed; details omitted to protect credentials",
        "error_type": type(exc).__name__,
    }


async def _ask(worker, ref, text):
    chunks = []

    async def sink(event):
        update = event.update
        if (
            update.session_update == "agent_message_chunk"
            and getattr(update.content, "type", None) == "text"
        ):
            chunks.append(update.content.text)

    response = await worker.control(ref, "ask", {"text": text}, on_update=sink)
    return {"stop_reason": response.stop_reason, "text": "".join(chunks)}


def _check_answer(result, own, foreign=None):
    if isinstance(result, BaseException):
        return _failure(result)
    found = own in result["text"]
    crossed = bool(foreign and foreign in result["text"])
    return {
        "status": "passed"
        if result["stop_reason"] == "end_turn" and found and not crossed
        else "failed",
        "stop_reason": result["stop_reason"],
        "own_marker_seen": found,
        "foreign_marker_seen": crossed,
    }


async def _probe(provider, config, settings):
    report = {"provider": provider, "status": "incomplete", "limitations": []}
    if config is None:
        report.update(
            {
                "status": "unavailable",
                "reason": "No matching adapter is configured in settings.json.",
            }
        )
        return report
    active = {}
    events = {"A": [], "B": []}
    phase = "configuration"
    try:
        entry = resolve_runtime_config(config, settings)
        command = entry["command"]
        report["adapter"] = _package_version(command)
        with tempfile.TemporaryDirectory(prefix="dlm_acp_probe_") as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "b").mkdir()

            def build(label):
                async def observe(event):
                    update = event.update
                    content = getattr(update, "content", None)
                    text = (
                        getattr(content, "text", "")
                        if getattr(content, "type", None) == "text"
                        else ""
                    )
                    events[label].append(
                        (
                            time.monotonic(),
                            event.session_id,
                            update.session_update,
                            text,
                        )
                    )

                return ACPWorker(
                    provider,
                    command,
                    root / label.lower(),
                    grants=lambda _: GRANTS,
                    env=entry.get("env"),
                    timeout=70,
                    suppress_stderr=True,
                    on_update=observe,
                )

            async def open_worker(label):
                worker = build(label)
                await worker.__aenter__()
                active[label] = worker
                return worker

            async def close_worker(label):
                worker = active.pop(label)
                await worker.__aexit__(None, None, None)

            try:
                phase = "initialize"
                a, b = await open_worker("A"), await open_worker("B")
                report["initialize"] = {
                    "A": _initialization(a),
                    "B": _initialization(b),
                }
                phase = "session_creation"
                ref_a, ref_b = await asyncio.gather(
                    a.new(
                        project_id="probe", task_id="a", thread_id="a", role="tester"
                    ),
                    b.new(
                        project_id="probe", task_id="b", thread_id="b", role="tester"
                    ),
                )
                report["sessions"] = {
                    "A": {"session_id": ref_a.session_id},
                    "B": {"session_id": ref_b.session_id},
                    "distinct_session_ids": ref_a.session_id != ref_b.session_id,
                    "distinct_processes": a.process.pid != b.process.pid,
                    "distinct_working_directories": a.cwd != b.cwd,
                }
                markers = {
                    key: key + uuid.uuid4().hex[:10].upper()
                    for key in ("A", "B", "OLD", "NEW", "LIVE", "B2", "B3")
                }
                prompt = "不要调用任何工具。请只回复这个代号："
                phase = "independent_connections"
                first = await asyncio.gather(
                    _ask(a, ref_a, prompt + markers["A"]),
                    _ask(b, ref_b, prompt + markers["B"]),
                    return_exceptions=True,
                )
                report["independent_connections"] = {
                    "A": _check_answer(first[0], markers["A"], markers["B"]),
                    "B": _check_answer(first[1], markers["B"], markers["A"]),
                }
                phase = "cancellation"
                pending_a = asyncio.create_task(
                    _ask(
                        a,
                        ref_a,
                        "不要调用工具。先写代号 "
                        + markers["OLD"]
                        + "，然后逐个写出从 1 到 1000 的数字。",
                    )
                )
                await asyncio.sleep(0.3)
                pending_b = asyncio.create_task(
                    _ask(b, ref_b, prompt + markers["LIVE"])
                )
                await asyncio.sleep(0.4)
                a_in_flight = not pending_a.done()
                b_in_flight = not pending_b.done()
                try:
                    await a.control(ref_a, "cancel")
                    cancel_result = await asyncio.wait_for(pending_a, 25)
                    stop = cancel_result["stop_reason"]
                    report["cancellation"] = {
                        "status": "passed" if stop == "cancelled" else "inconclusive",
                        "A_stop_reason": stop,
                        "reason": None
                        if stop == "cancelled"
                        else "A completed or returned another stop reason before cancellation took effect.",
                    }
                except Exception as exc:
                    report["cancellation"] = _failure(exc)
                    pending_a.cancel()
                    await asyncio.gather(pending_a, return_exceptions=True)
                report["cancellation"]["A_in_flight_when_cancelled"] = a_in_flight
                report["cancellation"]["B_in_flight_when_cancelled"] = b_in_flight
                try:
                    b_result = await asyncio.wait_for(pending_b, 70)
                    report["cancellation"]["B_response"] = _check_answer(
                        b_result, markers["LIVE"]
                    )
                except Exception as exc:
                    report["cancellation"]["B_response"] = _failure(exc)
                phase = "late_events"
                before_new = time.monotonic()
                try:
                    new = await _ask(a, ref_a, prompt + markers["NEW"])
                    check = _check_answer(new, markers["NEW"], markers["OLD"])
                    response_end = time.monotonic()
                    await asyncio.sleep(2)
                    late = [event for event in events["A"] if event[0] >= before_new]
                    report["late_events"] = {
                        "status": "inconclusive"
                        if check["status"] == "passed"
                        else "failed",
                        "new_response": check,
                        "old_marker_seen_in_observed_events": markers["OLD"]
                        in "".join(e[3] for e in late),
                        "old_marker_seen_before_new_turn": markers["OLD"]
                        in "".join(e[3] for e in events["A"] if e[0] < before_new),
                        "post_response_observation_seconds": 2,
                        "events_after_new_response": sum(
                            e[0] >= response_end for e in late
                        ),
                        "event_types_after_new_response": sorted(
                            {e[2] for e in late if e[0] >= response_end}
                        ),
                        "reason": "ACP session updates have no turn identity, so later events cannot be conclusively attributed to a turn.",
                    }
                    if report["late_events"]["old_marker_seen_in_observed_events"]:
                        report["late_events"]["status"] = "failed"
                except Exception as exc:
                    report["late_events"] = _failure(exc)
                phase = "close_isolation"
                try:
                    await a.control(ref_a, "close")
                    session_close = {"status": "passed"}
                except Exception as exc:
                    session_close = _failure(exc)
                try:
                    await close_worker("A")
                    connection_close = {"status": "passed"}
                except Exception as exc:
                    connection_close = _failure(exc)
                checks = []
                for key in ("B2", "B3"):
                    try:
                        checks.append(
                            _check_answer(
                                await _ask(b, ref_b, prompt + markers[key]),
                                markers[key],
                            )
                        )
                    except Exception as exc:
                        checks.append(_failure(exc))
                report["close_isolation"] = {
                    "A_session_close": session_close,
                    "A_connection_close": connection_close,
                    "B_followup_turns": checks,
                }
                phase = "resume"
                await close_worker("B")
                fresh_b = await open_worker("B")
                report["resume"] = {"session_id": ref_b.session_id}
                try:
                    await fresh_b.resume(ref_b)
                    report["resume"]["load"] = {
                        "status": "passed",
                        "same_session_id": ref_b.session_id in fresh_b._refs,
                    }
                    resumed = []
                    try:
                        remembered = await _ask(
                            fresh_b,
                            ref_b,
                            "不要调用工具。请只回复我最初让你记住的代号。",
                        )
                        resumed.append(_check_answer(remembered, markers["B"]))
                    except Exception as exc:
                        resumed.append(_failure(exc))
                    try:
                        fresh_marker = "RESUME" + uuid.uuid4().hex[:8].upper()
                        resumed.append(
                            _check_answer(
                                await _ask(fresh_b, ref_b, prompt + fresh_marker),
                                fresh_marker,
                            )
                        )
                    except Exception as exc:
                        resumed.append(_failure(exc))
                    report["resume"]["followup_turns"] = resumed
                except Exception as exc:
                    report["resume"]["load"] = _failure(exc)
                report["notifications"] = {
                    label: {
                        "event_count": len(items),
                        "mismatched_session_id_count": sum(
                            e[1]
                            != (ref_a.session_id if label == "A" else ref_b.session_id)
                            for e in items
                        ),
                    }
                    for label, items in events.items()
                }
                report["limitations"].append(
                    report["late_events"].get(
                        "reason", "Late event attribution was not verified."
                    )
                )
                if not b_in_flight:
                    report["limitations"].append(
                        "B had already finished at the moment A was cancelled; interruption of an in-flight B was not verified."
                    )
                report["status"] = "completed"
            finally:
                for label in list(active):
                    try:
                        await close_worker(label)
                    except Exception as exc:
                        report.setdefault("cleanup", {})[label] = _failure(exc)
    except Exception as exc:
        report["error"] = {"phase": phase, **_failure(exc)}
    return report


async def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("claude", "codex"),
        help="Test one provider; defaults to both.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="New JSON report path; existing files are never overwritten.",
    )
    args = parser.parse_args()
    try:
        sdk_version = metadata.version("agent-client-protocol")
    except metadata.PackageNotFoundError:
        sdk_version = None
    settings = PMSettings(Path.cwd())
    entries = settings.read().get("runtime", {}).get("acp_workers", [])
    providers = [args.provider] if args.provider else ["claude", "codex"]
    report = {
        "python_acp_sdk_version": sdk_version,
        "test": "two_independent_acp_workers",
        "sensitive_output_policy": "No adapter arguments, credentials, environment values, prompts, or model responses are recorded.",
        "adapters": [],
    }
    for provider in providers:
        sys.stderr.write(
            f"Testing {provider} with two independent ACP connections...\n"
        )
        config = next(
            (entry for entry in entries if entry.get("provider_id") == provider), None
        )
        report["adapters"].append(await _probe(provider, config, settings))
    output = args.output or (
        Path(tempfile.gettempdir()) / f"acp-session-report-{uuid.uuid4().hex}.json"
    )
    with output.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
        file.write("\n")
    sys.stdout.write(str(output.resolve()) + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
