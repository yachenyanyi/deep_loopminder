"""Run a WSL ACP adapter inside a read-only filesystem and filtered network.

This is a process supervisor, not an ACP implementation. It is deliberately
not enabled by default: the host must verify its dependencies and confinement
before advertising a read-only Worker. ACP protocol traffic stays on stdio.
"""

import argparse
import ctypes
import errno
import ipaddress
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit


def _relay(left, right):
    sockets = [left, right]
    while True:
        ready, _, _ = select.select(sockets, [], [], 60)
        if not ready:
            continue
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


def _connect_public(host):
    for family, kind, proto, _, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(address[0].split("%", 1)[0])
        if not ip.is_global:
            continue
        connection = socket.socket(family, kind, proto)
        connection.settimeout(15)
        try:
            connection.connect(address)
            connection.settimeout(None)
            return connection
        except OSError:
            connection.close()
    raise OSError("no public upstream address available")


def _proxy_connection(client, allowed_host):
    with client:
        client.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = client.recv(1024)
            if not chunk:
                return
            data += chunk
        header, sep, extra = data.partition(b"\r\n\r\n")
        if not sep or extra:
            return
        first = header.split(b"\r\n", 1)[0].split(b" ")
        expected = (allowed_host + ":443").encode("ascii")
        if len(first) != 3 or first[0] != b"CONNECT" or first[1].lower() != expected:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream = _connect_public(allowed_host)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            _relay(client, upstream)


def _serve_outer(server, allowed_host):
    while True:
        try:
            client, _ = server.accept()
        except OSError:
            return
        threading.Thread(target=_proxy_connection, args=(client, allowed_host), daemon=True).start()


def _serve_control(server, process, stopping):
    try:
        client, _ = server.accept()
    except OSError:
        return
    with client:
        client.settimeout(5)
        if client.recv(16) != b"stop\n":
            return
        stopping.set()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        client.sendall(b"STOPPED\n")


def _bridge_client(client, proxy_socket):
    with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
        try:
            upstream.connect(proxy_socket)
            _relay(client, upstream)
        except OSError:
            pass


def _bridge(proxy_socket, port, ready_fd):
    # A Worker must not be able to inspect or alter this trusted bridge.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "cannot protect proxy process")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(32)
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        while True:
            client, _ = server.accept()
            threading.Thread(target=_bridge_client, args=(client, proxy_socket), daemon=True).start()


class _ArgCompare(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def _restrict_syscalls():
    """Deny local-socket access and namespace/ptrace escape in descendants."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "cannot set no_new_privs")
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise OSError("cannot initialize seccomp")
    denied = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
    try:
        number = lib.seccomp_syscall_resolve_name(b"socket")
        # socket's domain is an int: compare the low 32 bits as the kernel
        # does, even when a raw syscall supplies nonzero upper bits.
        comparison = _ArgCompare(0, 7, 0xFFFFFFFF, socket.AF_UNIX)  # SCMP_CMP_MASKED_EQ
        if number < 0 or lib.seccomp_rule_add(ctx, denied, number, 1, comparison) != 0:
            raise OSError("cannot restrict local sockets")
        # WSL requires the initial namespace to retain NET_ADMIN until the
        # loopback bridge is configured. Descendants must not create further
        # namespaces after capabilities are dropped. ENOSYS permits libc to
        # fall back from clone3 to the filtered clone syscall.
        clone3 = lib.seccomp_syscall_resolve_name(b"clone3")
        if clone3 >= 0 and lib.seccomp_rule_add(ctx, 0x00050000 | errno.ENOSYS, clone3, 0) != 0:
            raise OSError("cannot restrict clone3")
        clone = lib.seccomp_syscall_resolve_name(b"clone")
        if clone < 0:
            raise OSError("cannot restrict namespace creation")
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                     0x10000000, 0x20000000, 0x40000000):
            comparison = _ArgCompare(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            if lib.seccomp_rule_add(ctx, denied, clone, 1, comparison) != 0:
                raise OSError("cannot restrict namespace creation")
        for name in (b"ptrace", b"pidfd_getfd", b"process_vm_writev", b"io_uring_setup", b"mount", b"umount2", b"setns", b"unshare", b"bpf"):
            number = lib.seccomp_syscall_resolve_name(name)
            if number >= 0 and lib.seccomp_rule_add(ctx, denied, number, 0) != 0:
                raise OSError("cannot install syscall restriction")
        if lib.seccomp_load(ctx) != 0:
            raise OSError("cannot load syscall restrictions")
    finally:
        lib.seccomp_release(ctx)


def _inner(port, probe=False, agent_kind="claude"):
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    reader, writer = os.pipe()
    bridge = subprocess.Popen(
        ["/usr/bin/setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
         "/usr/bin/python3", "/supervisor.py", "bridge", "--port", str(port), "--ready-fd", str(writer)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, pass_fds=(writer,),
    )
    os.close(writer)
    try:
        if os.read(reader, 1) != b"1":
            raise RuntimeError("filtered proxy did not start")
    finally:
        os.close(reader)
    # Drop every capability before the model-controlled process is started.
    command = ["setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
               "/usr/bin/python3", "/supervisor.py", "agent", "--port", str(port), "--agent-kind", agent_kind]
    if probe:
        command.append("--probe")
    os.execv("/usr/bin/setpriv", command)


def _agent(port, probe=False, agent_kind="claude"):
    _restrict_syscalls()
    if probe:
        try:
            fd = os.open("/project/loopminder-write-probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
                raise
        else:
            os.close(fd)
            raise RuntimeError("project filesystem is writable")
        try:
            local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise
        else:
            local.close()
            raise RuntimeError("local sockets are not restricted")
        routes = subprocess.run(["/usr/sbin/ip", "-4", "route", "show"], check=True,
                                capture_output=True, text=True).stdout.strip()
        if routes:
            raise RuntimeError("unexpected external network route")
        print("LOOPMINDER_READONLY_PREFLIGHT_OK", flush=True)
        return
    env = dict(os.environ)
    proxy = f"http://127.0.0.1:{port}"
    env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
                "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1", "HOME": "/home/worker",
                "PATH": "/runtime/node/bin:/usr/bin:/bin", "TMPDIR": "/tmp"})
    if agent_kind == "codex":
        # Codex's own nested sandbox cannot create namespaces under this outer
        # sandbox. The outer OS boundary remains mandatory and read-only.
        env["INITIAL_AGENT_MODE"] = "agent-full-access"
        env["CODEX_HOME"] = "/home/worker/.codex"
        env["DEFAULT_AUTH_REQUEST"] = '{"methodId":"api-key"}'
        env["MODEL_PROVIDER"] = "loopminder"
        env["NO_BROWSER"] = "1"
        adapter = "codex-acp"
    else:
        adapter = "claude-agent-acp"
    os.execve("/runtime/node/bin/node", ["node", f"/runtime/adapter/node_modules/@agentclientprotocol/{adapter}/dist/index.js"], env)


def _supervise(project, runtime, home, endpoint, probe=False, execution=False, control=None, agent_kind="claude", env_keys=""):
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("read-only Worker requires an HTTPS provider on port 443")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("local provider is not supported by the read-only boundary")
    project, runtime, home = (str(Path(path).resolve(strict=True)) for path in (project, runtime, home))
    if os.path.commonpath((project, home)) in {project, home}:
        raise ValueError("session home must be separate from the project")
    if os.path.commonpath((project, runtime)) in {project, runtime}:
        raise ValueError("worker runtime must be separate from the project")
    if os.path.commonpath((runtime, home)) in {runtime, home}:
        raise ValueError("worker runtime must be separate from the writable session home")
    if agent_kind not in {"claude", "codex"} or (agent_kind == "codex" and execution):
        raise ValueError("this Worker cannot enter the requested execution mode")
    if probe and execution:
        raise ValueError("the read-only preflight cannot run with project writes enabled")
    if not probe and not control:
        raise ValueError("a private process control socket is required")
    script = str(Path(__file__).resolve(strict=True))
    allowed_names = ({"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_CONFIG"}
                     if agent_kind == "codex" else {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "DISABLE_AUTOUPDATER"})
    requested = set(filter(None, env_keys.split(",")))
    if any(name not in allowed_names and not (agent_kind == "claude" and name.startswith("ANTHROPIC_")) for name in requested):
        raise ValueError("unrecognized Worker environment name")
    allowed_env = {name: value for name, value in os.environ.items()
                   if name in requested}
    allowed_env.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/home/worker"})
    with tempfile.TemporaryDirectory(prefix="loopminder-proxy-") as temp:
        path = str(Path(temp) / "proxy.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(32)
            threading.Thread(target=_serve_outer, args=(server, host), daemon=True).start()
            args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
                    "--unshare-ipc", "--unshare-net", "--unshare-uts",
                    "--cap-drop", "ALL", "--cap-add", "CAP_NET_ADMIN", "--cap-add", "CAP_SETPCAP",
                    "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
                    "--ro-bind-try", "/lib64", "/lib64", "--ro-bind", "/bin", "/bin",
                    "--ro-bind", "/etc/ssl", "/etc/ssl", "--proc", "/proc", "--dev", "/dev",
                    "--tmpfs", "/tmp", "--dir", "/run", "--dir", "/home", "--dir", "/home/worker",
                    "--bind" if execution else "--ro-bind", project, "/project", "--ro-bind", runtime, "/runtime",
                    "--bind", home, "/home/worker", "--ro-bind", script, "/supervisor.py",
                    "--ro-bind", path, "/run/proxy.sock", "--chdir", "/project", "--",
                    "/usr/bin/python3", "/supervisor.py", "inner", "--port", "18743", "--agent-kind", agent_kind]
            if probe:
                args.append("--probe")
            if probe:
                return subprocess.run(args, env=allowed_env, check=False).returncode
            control_path = Path(control)
            if not control_path.is_absolute() or control_path.exists():
                raise ValueError("invalid or already used control socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control_server:
                control_server.bind(str(control_path))
                os.chmod(control_path, 0o600)
                control_server.listen(1)
                try:
                    process = subprocess.Popen(args, env=allowed_env)
                    stopping = threading.Event()
                    controller = threading.Thread(target=_serve_control, args=(control_server, process, stopping), daemon=True)
                    controller.start()
                    status = process.wait()
                    if stopping.is_set():
                        controller.join(timeout=15)
                    return status
                finally:
                    try:
                        control_path.unlink()
                    except FileNotFoundError:
                        pass


def _stop(control):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(25)
        connection.connect(control)
        connection.sendall(b"stop\n")
        if connection.recv(32) != b"STOPPED\n":
            raise RuntimeError("worker shutdown was not confirmed")


def main():
    """Launch one supervised ACP connection or one internal helper."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["supervise", "inner", "bridge", "agent", "stop"])
    parser.add_argument("--project")
    parser.add_argument("--runtime")
    parser.add_argument("--home")
    parser.add_argument("--endpoint")
    parser.add_argument("--port", type=int, default=18743)
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--execution", action="store_true")
    parser.add_argument("--control")
    parser.add_argument("--agent-kind", choices=["claude", "codex"], default="claude")
    parser.add_argument("--env-keys", default="")
    args = parser.parse_args()
    if args.action == "supervise":
        return _supervise(args.project, args.runtime, args.home, args.endpoint, args.probe, args.execution, args.control, args.agent_kind, args.env_keys)
    if args.action == "stop":
        return _stop(args.control)
    if args.action == "inner":
        return _inner(args.port, args.probe, args.agent_kind)
    if args.action == "bridge":
        return _bridge("/run/proxy.sock", args.port, args.ready_fd)
    return _agent(args.port, args.probe, args.agent_kind)


if __name__ == "__main__":
    sys.exit(main())
