"""Probe configured ACP adapters with live model calls in a temporary directory.

Run from the project root: python -m scripts.acp_session_probe [--provider claude]
Pass --job to launch the adapter through the experimental Windows Job wrapper.
The probe incurs model usage and does not modify the project settings.
"""

import argparse
import asyncio
import sys
import tempfile
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


async def _ask(worker, ref, message):
    chunks = []

    async def on_update(event):
        update = event.update
        if (
            update.session_update == "agent_message_chunk"
            and getattr(update.content, "type", None) == "text"
        ):
            chunks.append(update.content.text)

    response = await worker.control(ref, "ask", {"text": message}, on_update=on_update)
    return response.stop_reason, "".join(chunks)


async def _probe(config, settings, use_job):
    entry = resolve_runtime_config(config, settings)
    provider = config["provider_id"]
    result = {"provider": provider, "transport": "job" if use_job else "direct"}
    with tempfile.TemporaryDirectory(prefix="dlm_acp_verify_") as directory:

        def worker():
            command = entry["command"]
            if use_job:
                status = str(Path(directory) / "job-status.json")
                launcher = str(Path(__file__).resolve().parents[1] / "src" / "middlewares" / "acp" / "job.py")
                command = [sys.executable, launcher, "--status", status, "--", *command]
            return ACPWorker(
                provider,
                command,
                directory,
                grants=lambda _: GRANTS,
                env=entry.get("env"),
                timeout=45,
                suppress_stderr=True,
            )

        async with worker() as current:
            controls = negotiated_controls(current.initialized)
            result["controls"] = sorted(controls)
            a = await current.new(
                project_id="experiment", task_id="a", thread_id="a", role="tester"
            )
            b = await current.new(
                project_id="experiment", task_id="b", thread_id="b", role="tester"
            )
            result["distinct_sessions"] = a.session_id != b.session_id
            prompt = "不要调用工具或访问文件。请只回复代号："
            answers = await asyncio.gather(
                _ask(current, a, prompt + "AURORA736"),
                _ask(current, b, prompt + "BAMBOO842"),
                return_exceptions=True,
            )
            for name, answer, marker, other in zip(
                ("a", "b"),
                answers,
                ("AURORA736", "BAMBOO842"),
                ("BAMBOO842", "AURORA736"),
            ):
                if isinstance(answer, BaseException):
                    result[name] = {"error": type(answer).__name__}
                else:
                    result[name] = {
                        "stop": answer[0],
                        "own_marker": marker in answer[1],
                        "foreign_marker": other in answer[1],
                    }
            pending = asyncio.create_task(
                _ask(current, a, "不要调用工具。请从 1 数到 500。")
            )
            await asyncio.sleep(0.5)
            try:
                await current.control(a, "cancel")
                stop, _ = await asyncio.wait_for(pending, 15)
                result["cancel_stop"] = stop
            except Exception as exc:
                result["cancel_error"] = type(exc).__name__
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            try:
                stop, answer = await _ask(current, a, prompt + "CEDAR953")
                result["after_cancel"] = {
                    "stop": stop,
                    "own_marker": "CEDAR953" in answer,
                }
            except Exception as exc:
                result["after_cancel"] = {"error": type(exc).__name__}
            if "close" in controls:
                try:
                    await current.control(a, "close")
                    result["session_close"] = True
                except Exception as exc:
                    result["session_close"] = type(exc).__name__
            else:
                result["session_close"] = "not_advertised"
            stop, answer = await _ask(current, b, prompt + "MAPLE381")
            result["other_after_close"] = {
                "stop": stop,
                "own_marker": "MAPLE381" in answer,
            }
        async with worker() as restarted:
            try:
                await restarted.resume(b)
                stop, answer = await _ask(
                    restarted, b, "不要调用工具。请只回复我最初给你的代号。"
                )
                result["resume"] = {
                    "stop": stop,
                    "remembered_marker": "BAMBOO842" in answer,
                }
            except Exception as exc:
                result["resume"] = {"error": type(exc).__name__}
        if use_job:
            import json

            status_path = Path(directory) / "job-status.json"
            result["job_final_state"] = json.loads(
                status_path.read_text(encoding="utf-8")
            )["state"]
    return result


async def _main():
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("claude", "codex"))
    parser.add_argument("--job", action="store_true")
    args = parser.parse_args()
    settings = PMSettings(Path.cwd())
    entries = settings.read().get("runtime", {}).get("acp_workers", [])
    entries = [
        entry
        for entry in entries
        if not args.provider or entry.get("provider_id") == args.provider
    ]
    if not entries:
        raise SystemExit("No matching ACP worker configured")
    for entry in entries:
        try:
            result = await _probe(entry, settings, args.job)
        except Exception as exc:
            result = {"provider": entry.get("provider_id"), "error": type(exc).__name__}
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_main())
