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
    job = None
    process = thread = None
    duplicates = []
    assigned = False
    identity = {}
    try:
        job = win32job.CreateJobObject(None, "DeepLoopMinderPrototype-" + uuid.uuid4().hex)
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )

        # Copy only our standard streams as inheritable handles for the target.
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
        win32job.AssignProcessToJobObject(job, process)
        assigned = True
        created = win32process.GetProcessTimes(process)["CreationTime"].isoformat()
        identity = {"adapter_pid": pid, "adapter_creation_time": created}
        # Persist before execution. A crash before assignment can leave a suspended child.
        write_status(status, "assigned", **identity)
        win32process.ResumeThread(thread)
        thread.Close()
        thread = None
        write_status(status, "running", **identity)
        win32event.WaitForSingleObject(process, win32event.INFINITE)
        exit_code = win32process.GetExitCodeProcess(process)
        # Once the adapter exits, its remaining descendants cannot keep this run alive.
        win32job.TerminateJobObject(job, 1)
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
                status, "stop_unknown", error_type=type(exc).__name__, **identity
            )
        except OSError:
            pass
        raise
    finally:
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
