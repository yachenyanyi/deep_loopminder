"""Run isolated Windows Job launcher checks; creates only temporary processes/files."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

if os.name == "nt":
    import win32api
    import win32con
    import win32event
    import win32job
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

    def test_killed_launcher_is_reconciled_by_witness(self):
        """A surviving witness stops the Job and records proof after owner death."""
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
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        state = json.loads(status.read_text(encoding="utf-8"))
                        if state.get("state") == "stopped_verified":
                            break
                        time.sleep(0.05)
                    self.assertEqual(state.get("state"), "stopped_verified")
                    self.assertEqual(state.get("source"), "witness")
                    self.assertEqual(state.get("execution_id"), status.stem)
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

    def test_witness_cannot_confirm_before_job_assignment(self):
        """Owner death during startup cannot prove the target was assigned."""
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "state.json"
            status.write_text(json.dumps({"state": "starting", "execution_id": status.stem}), encoding="utf-8")
            job_name = "DeepLoopMinderTest-" + uuid.uuid4().hex
            ready_name = "Local\\DeepLoopMinderTest-" + uuid.uuid4().hex
            job = win32job.CreateJobObject(None, job_name)
            ready = win32event.CreateEvent(None, True, False, ready_name)
            owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=win32con.CREATE_NO_WINDOW)
            owner_handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, owner.pid)
            created = win32process.GetProcessTimes(owner_handle)["CreationTime"].isoformat()
            owner_handle.Close()
            watcher = subprocess.Popen([sys.executable, str(LAUNCHER), "--witness", "--status", str(status),
                                        "--job", job_name, "--ready", ready_name, "--parent", str(owner.pid),
                                        "--parent-created", created], creationflags=win32con.CREATE_NO_WINDOW)
            try:
                self.assertEqual(win32event.WaitForSingleObject(ready, 5000), win32event.WAIT_OBJECT_0)
                owner.kill()
                owner.wait(timeout=5)
                self.assertNotEqual(watcher.wait(timeout=10), 0)
                self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["state"], "starting")
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                if watcher.poll() is None:
                    watcher.kill()
                    watcher.wait(timeout=5)
                ready.Close()
                job.Close()

    def test_runner_and_witness_death_stays_unconfirmed(self):
        """Losing both observers cannot produce a false shutdown proof."""
        code = 'import time; print("ready", flush=True); time.sleep(60)'
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "state.json"
            process = subprocess.Popen(self._command(status, code), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                state = json.loads(status.read_text(encoding="utf-8"))
                watcher = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE,
                                              False, state["witness_pid"])
                try:
                    win32process.TerminateProcess(watcher, 1)
                    self.assertEqual(win32event.WaitForSingleObject(watcher, 5000), win32event.WAIT_OBJECT_0)
                finally:
                    watcher.Close()
                process.kill()
                process.wait(timeout=5)
                self.assertNotEqual(json.loads(status.read_text(encoding="utf-8"))["state"], "stopped_verified")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_verified_witness_allows_record_reconciliation(self):
        """A real recovered Job proof clears only its matching PM record."""
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from langgraph.store.memory import InMemoryStore

        from src.deep_agents.agents.pm_agent.collaboration import (
            reconcile_verified_jobs,
        )

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LOOPMINDER_DATA_DIR": directory}):
            job_dir = Path(directory) / "DeepLoopMinder" / "worker-jobs"
            job_dir.mkdir(parents=True)
            execution_id = uuid.uuid4().hex
            status = job_dir / (execution_id + ".json")
            code = 'import time; print("ready", flush=True); time.sleep(60)'
            process = subprocess.Popen(self._command(status, code), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.kill()
                process.wait(timeout=5)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    state = json.loads(status.read_text(encoding="utf-8"))
                    if state.get("state") == "stopped_verified":
                        break
                    time.sleep(0.05)
                self.assertEqual(state.get("source"), "witness")

                async def check():
                    store = InMemoryStore()
                    namespace = ("deep_loopminder", "project_worker_quarantine", "project")
                    record = {"project_id": "project", "worker_ref": {"project_id": "project", "thread_id": "worker"},
                              "execution_id": execution_id, "job_status": str(status)}
                    await store.aput(namespace, "worker", record)
                    await reconcile_verified_jobs(store, "project")
                    self.assertIsNone(await store.aget(namespace, "worker"))

                asyncio.run(check())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_unconfirmed_records_explain_the_reason(self):
        """Missing, mismatched and failed observations remain distinguishable."""
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.deep_agents.agents.pm_agent.collaboration import job_observation

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LOOPMINDER_DATA_DIR": directory}):
            job_dir = Path(directory) / "DeepLoopMinder" / "worker-jobs"
            job_dir.mkdir(parents=True)
            execution_id = uuid.uuid4().hex
            status = job_dir / (execution_id + ".json")
            record = {"execution_id": execution_id, "job_status": str(status)}
            self.assertIn("不存在", job_observation(record)["reason"])
            status.write_text("{bad json", encoding="utf-8")
            self.assertIn("格式无效", job_observation(record)["reason"])
            status.write_text(json.dumps({"state": "stopped_verified", "execution_id": "wrong"}), encoding="utf-8")
            self.assertIn("身份不匹配", job_observation(record)["reason"])
            status.write_text(json.dumps({"state": "stop_unknown", "execution_id": execution_id,
                                          "stage": "job_assignment"}), encoding="utf-8")
            self.assertIn("分配进程组", job_observation(record)["reason"])

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
