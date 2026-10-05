"""Local ACP smoke runner; no existing Agent assembly is changed."""

# ruff: noqa: T201 -- This command's interface is console/JSON output.

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
from pathlib import Path

# Existing middleware imports print model setup messages; keep protocol/demo
# JSON on stdout and all such diagnostics on stderr.
with contextlib.redirect_stdout(sys.stderr):
    from src.middlewares.approval.decision import ApprovalDecision, RiskLevel
    from src.middlewares.execution.security_policy import SecurityGrant

    from . import ACPWorker, WorkerRef


def _arguments():
    parser = argparse.ArgumentParser(
        description="Control local ACP workers through the official Python SDK."
    )
    parser.add_argument(
        "--provider",
        default="codex",
        help="Trusted provider identity; built-ins: codex, claude",
    )
    parser.add_argument(
        "--command-json",
        help='Override with JSON argv, e.g. ["node", "D:/adapter/dist/index.js"]',
    )
    parser.add_argument("--cwd", required=True, help="Worker project directory")
    parser.add_argument("--project-id", default="local-smoke")
    parser.add_argument("--task-id", default="smoke-1")
    parser.add_argument("--thread-id", default="local-1")
    parser.add_argument("--role", default="developer")
    parser.add_argument(
        "--action",
        choices=[
            "initialize",
            "new",
            "prompt",
            "observe",
            "list",
            "cancel",
            "close",
            "mode",
            "config",
            "authenticate",
        ],
        default="prompt",
    )
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--ref-in", type=Path, help="Load an existing domain-correlation JSON"
    )
    parser.add_argument(
        "--ref-out",
        type=Path,
        help="Save domain references for later native session loading",
    )
    parser.add_argument(
        "--permissions", choices=["deny", "read", "ask"], default="deny"
    )
    parser.add_argument("--mode-id")
    parser.add_argument("--config-id")
    parser.add_argument(
        "--value-json", help="JSON string or boolean for the advertised config option"
    )
    parser.add_argument("--auth-method")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--cancel-after",
        type=float,
        help="Cancel an in-flight prompt after this many seconds",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print native session/update notifications and official responses as JSON",
    )
    return parser.parse_args()


class _LocalPolicy:
    """Explicit smoke-runner policy; production callers supply their own #20 policy."""

    name = "local_acp_smoke"

    def __init__(self, mode):
        self.mode = mode

    async def aevaluate(self, request):
        if self.mode == "ask":
            return ApprovalDecision.needs_approval(
                RiskLevel.MEDIUM, "Confirm the native ACP tool request"
            )
        if self.mode == "read" and request.tool_name in {"acp.read", "acp.search"}:
            return ApprovalDecision.allowed()
        return ApprovalDecision.blocked()


def _command(args):
    if args.command_json:
        command = json.loads(args.command_json)
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(value, str) or not value for value in command)
        ):
            raise ValueError("--command-json must be a non-empty JSON argv array")
        return command
    packages = {"codex": "codex-acp", "claude": "claude-agent-acp"}
    if args.provider not in packages:
        raise ValueError("custom providers require --command-json")
    root = Path(__file__).resolve().parents[3]
    script = (
        root
        / ".local-acp"
        / "node_modules"
        / "@agentclientprotocol"
        / packages[args.provider]
        / "dist"
        / "index.js"
    )
    node = shutil.which("node")
    if not node or not script.is_file():
        raise FileNotFoundError(
            "Install Node and run: npm install --prefix .local-acp @agentclientprotocol/codex-acp @agentclientprotocol/claude-agent-acp"
        )
    # Direct Node invocation works on Windows too; no .cmd shim or shell quoting.
    return [node, str(script)]


async def _run(args):
    async def stream(notification):
        if args.json:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": notification.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            update = notification.update
            if (
                update.session_update == "agent_message_chunk"
                and update.content.type == "text"
            ):
                print(update.content.text, end="", flush=True)
            elif update.session_update in {"tool_call", "tool_call_update"}:
                print(
                    f"\n[{update.session_update}] {getattr(update, 'title', '')} {getattr(update, 'status', '')}",
                    file=sys.stderr,
                    flush=True,
                )

    async def review(request, decision):
        if not sys.stdin.isatty():
            return False
        print("\nACP permission request (allow once):", file=sys.stderr)
        print(
            json.dumps(request.context["tool_call"], ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        print("Approve? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = await asyncio.to_thread(sys.stdin.readline)
        return answer.strip().casefold() == "y"

    controls = {
        "worker.connect",
        "worker.new",
        "worker.observe",
        "worker.ask",
        "worker.cancel",
        "worker.resume",
        "worker.list",
        "worker.close",
        "worker.set_mode",
        "worker.set_config",
        "worker.authenticate",
    }
    kinds = (
        {
            "read",
            "edit",
            "delete",
            "move",
            "search",
            "execute",
            "think",
            "fetch",
            "switch_mode",
            "other",
        }
        if args.permissions == "ask"
        else ({"read", "search"} if args.permissions == "read" else set())
    )
    grant = SecurityGrant(
        frozenset(controls | {f"external_tool.{kind}" for kind in kinds})
    )
    async with ACPWorker(
        args.provider,
        _command(args),
        args.cwd,
        grants=lambda _ref: grant,
        approval=_LocalPolicy(args.permissions),
        review=review,
        on_update=stream,
        timeout=args.timeout,
    ) as worker:
        if args.action == "initialize":
            return worker.initialized
        if args.action == "authenticate":
            if not args.auth_method:
                raise ValueError(
                    "--auth-method is required; initialize lists advertised methods"
                )
            return await worker.authenticate(args.auth_method)
        if args.action == "list":
            return await worker.list_sessions()
        if args.ref_in:
            ref = WorkerRef(**json.loads(args.ref_in.read_text(encoding="utf-8")))
            await worker.resume(ref)
        else:
            if args.action in {"observe", "cancel", "close"}:
                raise ValueError("this action requires --ref-in")
            ref = await worker.new(
                project_id=args.project_id,
                task_id=args.task_id,
                thread_id=args.thread_id,
                role=args.role,
            )
        if args.ref_out:
            args.ref_out.parent.mkdir(parents=True, exist_ok=True)
            args.ref_out.write_text(
                json.dumps(ref.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(f"\nACP session: {ref.session_id}", file=sys.stderr)
        if args.action == "new":
            return ref.to_dict()
        if args.action == "observe":
            return worker.observe(ref)
        if args.action == "mode":
            return await worker.control(ref, "set_mode", {"mode_id": args.mode_id})
        if args.action == "config":
            if not args.config_id or args.value_json is None:
                raise ValueError("--config-id and --value-json are required")
            return await worker.control(
                ref,
                "set_config",
                {"config_id": args.config_id, "value": json.loads(args.value_json)},
            )
        if args.action in {"cancel", "close"}:
            return await worker.control(ref, args.action)
        if args.prompt and args.prompt_file:
            raise ValueError("use --prompt or --prompt-file, not both")
        text = (
            args.prompt_file.read_text(encoding="utf-8")
            if args.prompt_file
            else args.prompt
        )
        if not text:
            raise ValueError("--prompt or --prompt-file is required")
        if args.cancel_after is not None and args.cancel_after <= 0:
            raise ValueError("--cancel-after must be positive")
        prompt = asyncio.create_task(worker.control(ref, "ask", {"text": text}))

        async def cancel_later():
            await asyncio.sleep(args.cancel_after)
            if not prompt.done():
                await worker.control(ref, "cancel")

        timer = asyncio.create_task(cancel_later()) if args.cancel_after else None
        try:
            return await prompt
        finally:
            if timer:
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer


def main():
    """Run one explicit local command and print its official result."""
    try:
        result = asyncio.run(_run(_arguments()))
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        print(json.dumps(result, ensure_ascii=False))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
