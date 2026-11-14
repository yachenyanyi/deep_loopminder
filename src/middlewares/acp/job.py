"""Windows Job owner for one ACP stdio adapter process tree.

The status file describes only this invocation; missing confirmation is unknown.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def write_status(path, state, **details):
    """Replace the observation atomically without exposing adapter arguments."""
    payload = {"state": state, "execution_id": path.stem, "launcher_pid": os.getpid(), **details}
    fd, temporary = tempfile.mkstemp(prefix=".acp-job-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def witness(status, job_name, ready_name, parent_pid, parent_created):
    """Keep the Job observable and reconcile it if its owner exits unexpectedly."""
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    parent = job = ready = None
    try:
        parent = win32api.OpenProcess(
            win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, parent_pid
        )
        if win32process.GetProcessTimes(parent)["CreationTime"].isoformat() != parent_created:
            raise ValueError("Job owner identity changed")
        job = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY | win32job.JOB_OBJECT_TERMINATE, False, job_name)
        ready = win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, ready_name)
        win32event.SetEvent(ready)
        ready.Close()
        ready = None
        win32event.WaitForSingleObject(parent, win32event.INFINITE)
        try:
            with status.open(encoding="utf-8") as stream:
                last = json.load(stream)
        except (OSError, ValueError):
            return 1
        if not isinstance(last, dict) or last.get("execution_id") != status.stem:
            return 1
        if last.get("state") == "stopped_verified":
            return 0
        # Before assignment a suspended target might exist outside the Job.
        # Error records may be recovered only when the owner recorded target
        # identity after successful assignment.
        if last.get("state") not in {"assigned", "running", "stop_unknown"}:
            return 1
        if (not isinstance(last.get("adapter_pid"), int) or last["adapter_pid"] <= 0
                or not isinstance(last.get("adapter_creation_time"), str)
                or not last["adapter_creation_time"]):
            return 1
        win32job.TerminateJobObject(job, 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)
            if info["ActiveProcesses"] == 0:
                identity = {key: last[key] for key in ("adapter_pid", "adapter_creation_time") if key in last}
                write_status(status, "stopped_verified", source="witness", launcher_pid=parent_pid,
                             witness_pid=os.getpid(), **identity)
                return 0
            time.sleep(0.05)
        return 1
    except Exception:
        # Missing, unreadable or unverifiable evidence is never promoted to stopped.
        return 1
    finally:
        for handle in (ready, job, parent):
            if handle is not None:
                handle.Close()


def run(status, command):
    """Run one adapter under a unique kill-on-close Job."""
    import _winapi

    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    write_status(status, "starting")
    job = ready = None
    process = thread = None
    witness_process = None
    duplicates = []
    assigned = False
    identity = {}
    stage = "job_setup"
    try:
        job_name = "DeepLoopMinderWorker-" + uuid.uuid4().hex
        ready_name = "Local\\DeepLoopMinderReady-" + uuid.uuid4().hex
        job = win32job.CreateJobObject(None, job_name)
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )

        # A second process holds a Job handle before any target is created.
        # This allows it to prove whole-Job shutdown after this owner dies.
        stage = "witness_start"
        ready = win32event.CreateEvent(None, True, False, ready_name)
        parent_created = win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"].isoformat()
        witness_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--witness", "--status", str(status),
             "--job", job_name, "--ready", ready_name, "--parent", str(os.getpid()),
             "--parent-created", parent_created],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=win32con.CREATE_NO_WINDOW,
        )
        if win32event.WaitForSingleObject(ready, 5000) != win32event.WAIT_OBJECT_0:
            raise RuntimeError("Job witness did not establish ownership")
        ready.Close()
        ready = None
        if witness_process.poll() is not None:
            raise RuntimeError("Job witness exited during startup")

        # Copy only our standard streams as inheritable handles for the target.
        stage = "adapter_start"
        current = win32api.GetCurrentProcess()
        for standard in (
            -10,
            -11,
            -12,
        ):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = win32api.GetStdHandle(standard)
            duplicates.append(
                win32api.DuplicateHandle(
                    current, handle, current, 0, True, win32con.DUPLICATE_SAME_ACCESS
                )
            )
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= win32process.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = map(int, duplicates)
        # Match subprocess.Popen's explicit handle list: no unrelated host
        # handles, including the Job handle, reach the adapter.
        startup.lpAttributeList["handle_list"] = list(map(int, duplicates))
        process_handle, thread_handle, pid, _ = _winapi.CreateProcess(
            None,
            subprocess.list2cmdline(command),
            None,
            None,
            True,
            win32process.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
            None,
            None,
            startup,
        )
        process, thread = pywintypes.HANDLE(process_handle), pywintypes.HANDLE(thread_handle)
        stage = "job_assignment"
        win32job.AssignProcessToJobObject(job, process)
        assigned = True
        created = win32process.GetProcessTimes(process)["CreationTime"].isoformat()
        identity = {"adapter_pid": pid, "adapter_creation_time": created, "witness_pid": witness_process.pid}
        # Persist before execution. A crash before assignment can leave a suspended child.
        stage = "status_write"
        write_status(status, "assigned", **identity)
        stage = "adapter_resume"
        win32process.ResumeThread(thread)
        thread.Close()
        thread = None
        stage = "status_write"
        write_status(status, "running", **identity)
        stage = "adapter_wait"
        win32event.WaitForSingleObject(process, win32event.INFINITE)
        exit_code = win32process.GetExitCodeProcess(process)
        # Once the adapter exits, its remaining descendants cannot keep this run alive.
        stage = "job_termination"
        win32job.TerminateJobObject(job, 1)
        stage = "job_drain"
        deadline = time.monotonic() + 10
        while True:
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectBasicAccountingInformation
            )
            if info["ActiveProcesses"] == 0:
                write_status(
                    status, "stopped_verified", exit_code=exit_code, **identity
                )
                return exit_code
            if time.monotonic() >= deadline:
                raise TimeoutError("Job process count did not reach zero")
            time.sleep(0.05)
    except BaseException as exc:
        # Fail closed: do not claim quiescence if the verification path failed.
        if process is not None and not assigned:
            try:
                win32process.TerminateProcess(process, 1)
            except pywintypes.error:
                pass
        try:
            write_status(
                status, "stop_unknown", stage=stage, error_type=type(exc).__name__, **identity
            )
        except OSError:
            pass
        raise
    finally:
        if ready is not None:
            ready.Close()
        if thread is not None:
            thread.Close()
        if process is not None:
            process.Close()
        for handle in duplicates:
            handle.Close()
        if job is not None:
            job.Close()


def main():
    """Run the ACP Job owner from the command line."""
    if len(sys.argv) > 1 and sys.argv[1] == "--witness":
        parser = argparse.ArgumentParser(description="Observe one ACP Job owner")
        parser.add_argument("--status", type=Path, required=True)
        parser.add_argument("--job", required=True)
        parser.add_argument("--ready", required=True)
        parser.add_argument("--parent", type=int, required=True)
        parser.add_argument("--parent-created", required=True)
        args = parser.parse_args(sys.argv[2:])
        return witness(args.status, args.job, args.ready, args.parent, args.parent_created)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows is required")
    if not args.status.is_absolute() or not args.status.parent.is_dir():
        parser.error("--status must be absolute and its parent must exist")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an adapter command is required after --")
    try:
        return run(args.status, command)
    except Exception as exc:
        sys.stderr.write(f"ACP Job owner failed: {type(exc).__name__}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
