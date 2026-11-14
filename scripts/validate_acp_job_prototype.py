"""Run isolated Windows Job launcher checks; creates only temporary processes/files."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if os.name == "nt":
    import win32api
    import win32con
    import win32event
    import win32process

LAUNCHER = Path(__file__).resolve().parents[1] / "src" / "middlewares" / "acp" / "job.py"


@unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
class JobPrototypeTests(unittest.TestCase):
    """Exercise stdio, process-tree cleanup, crashes, and startup failure."""

    def _command(self, status, code):
        return [
            sys.executable,
            str(LAUNCHER),
            "--status",
            str(status),
            "--",
            sys.executable,
            "-u",
            "-c",
            code,
        ]

    def test_stdio_and_descendants_stop(self):
        """Keep standard streams usable and confirm descendants exit."""
        code = (
            "import subprocess,sys; "
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],'
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            'print(child.pid,flush=True); print("reply:"+sys.stdin.readline().strip(),flush=True)'
        )
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "state.json"
            process = subprocess.Popen(
                self._command(status, code),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                child_pid = int(process.stdout.readline())
                child = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child_pid)
                try:
                    process.stdin.write("hello\n")
                    process.stdin.flush()
                    self.assertEqual(process.stdout.readline().strip(), "reply:hello")
                    self.assertEqual(process.wait(timeout=15), 0, process.stderr.read())
                    self.assertEqual(
                        win32event.WaitForSingleObject(child, 5000),
                        win32event.WAIT_OBJECT_0,
                    )
                    self.assertEqual(
                        json.loads(status.read_text(encoding="utf-8"))["state"],
                        "stopped_verified",
                    )
                finally:
                    child.Close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_killed_launcher_does_not_forge_stop_proof(self):
        """Do not write a verified status when the launcher is killed."""
        code = (
            "import subprocess,sys,time; "
            'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],'
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(child.pid,flush=True); time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "state.json"
            process = subprocess.Popen(
                self._command(status, code),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                child_pid = int(process.stdout.readline())
                child = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child_pid)
                try:
                    # The target executes only after assignment and the assigned record is written.
                    state = json.loads(status.read_text(encoding="utf-8"))
                    launcher = win32api.OpenProcess(
                        win32con.PROCESS_TERMINATE, False, state["launcher_pid"]
                    )
                    try:
                        win32process.TerminateProcess(launcher, 1)
                    finally:
                        launcher.Close()
                    process.wait(timeout=10)
                    self.assertEqual(
                        win32event.WaitForSingleObject(child, 5000),
                        win32event.WAIT_OBJECT_0,
                    )
                    self.assertNotEqual(
                        json.loads(status.read_text(encoding="utf-8"))["state"],
                        "stopped_verified",
                    )
                finally:
                    child.Close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_start_failure_is_unknown(self):
        """A failed spawn leaves an unknown status without leaking arguments."""
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "state.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(LAUNCHER),
                    "--status",
                    str(status),
                    "--",
                    str(Path(directory) / "missing-adapter.exe"),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(
                json.loads(status.read_text(encoding="utf-8"))["state"], "stop_unknown"
            )
            self.assertEqual(process.stdout, "")
            self.assertNotIn("missing-adapter.exe", process.stderr)

    def test_sdk_sessions_and_verified_close(self):
        """The real ACP client retains session routing through the Job owner."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.middlewares.acp.control import ACPWorker
        from src.middlewares.execution.security_policy import SecurityGrant

        adapter = '''import json, sys, uuid
for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        result = {"sessionId": str(uuid.uuid4())}
    elif method == "session/prompt":
        session = params["sessionId"]
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "ACK:" + session}}}}), flush=True)
        result = {"stopReason": "end_turn"}
    else:
        result = {}
    if "id" in request:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "adapter.py"
            source.write_text(adapter, encoding="utf-8")
            status = root / "execution.json"
            grants = SecurityGrant(frozenset({"worker.connect", "worker.new", "worker.ask", "worker.cancel"}))

            async def scenario():
                updates = []

                async def observe(event):
                    updates.append((event.session_id, event.update.content.text))

                async with ACPWorker("mock", [sys.executable, str(source)], root,
                                     grants=lambda _: grants, on_update=observe,
                                     timeout=10, job_status=status) as worker:
                    first = await worker.new(project_id="p", task_id="a", thread_id="a", role="test")
                    second = await worker.new(project_id="p", task_id="b", thread_id="b", role="test")
                    await asyncio.gather(worker.control(first, "ask", {"text": "a"}),
                                         worker.control(second, "ask", {"text": "b"}))
                    self.assertEqual(set(updates), {(first.session_id, "ACK:" + first.session_id),
                                                    (second.session_id, "ACK:" + second.session_id)})
                self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["state"], "stopped_verified")
                with self.assertRaises(ValueError):
                    async with ACPWorker("mock", [sys.executable, str(source)], root,
                                         grants=lambda _: grants, job_status=status):
                        pass
                status.write_text('{"state":"stopped_verified","execution_id":"other"}', encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    await worker.__aexit__(None, None, None)

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
