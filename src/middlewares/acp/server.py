"""Configure ACP connections using the official Agent Server application lifespan."""

import asyncio
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette

from src.middlewares.approval.config import ApprovalProviderConfig, resolve_provider
from src.middlewares.approval.builtin import AllowlistProvider
from src.middlewares.agent.model_catalog import WindowsCredentials
from src.middlewares.execution.security_policy import EnforcementCapability, EnforcementFact, SecurityGrant
from src.runtime.routing.models import WorkerCandidate

from .capabilities import worker_candidate
from .control import ACPWorker
from .graph import create_control_graph, create_worker_graph
from src.workbench.app import routes as workbench_routes

_workers: dict[str, ACPWorker] = {}
_candidates = []
_worker_errors = {}
_active_managed_revision = {}
# This boundary must be independently exercised and reviewed before release.
# A configuration flag or a successful startup probe alone is not an audit.
_WSL_BOUNDARY_AUDITED = False


def _worker_settings_path():
    base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "DeepLoopMinder" / "worker-settings.json"


def _validate_managed_worker(value, provider_id="claude", *, check_runtime=True):
    if provider_id not in {"claude", "codex"}:
        raise ValueError("未知的 Worker 提供方")
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("负责人配置无效")
    if not value["enabled"]:
        return
    distro, endpoint, runtime, model = (value.get(key) for key in ("distro", "endpoint", "runtime_dir", "model"))
    if not isinstance(distro, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", distro):
        raise ValueError("WSL 发行版名称无效")
    if not isinstance(endpoint, str):
        raise ValueError("负责人 API 地址无效")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("负责人 API 地址必须是无凭据和参数的 HTTPS 地址")
    if not isinstance(runtime, str) or not Path(runtime).is_absolute():
        raise ValueError("请选择已安装的本机只读运行环境目录")
    adapter = "claude-agent-acp" if provider_id == "claude" else "codex-acp"
    if check_runtime and (not (Path(runtime) / "node" / "bin" / "node").is_file() or not (
        Path(runtime) / "adapter" / "node_modules" / "@agentclientprotocol" / adapter / "dist" / "index.js"
    ).is_file()):
        raise ValueError("运行环境缺少 Linux Node 或 Claude ACP")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("请输入负责人模型名称")


def _load_managed_workers():
    path = _worker_settings_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if isinstance(value, dict) and "enabled" in value:
        value = {"claude": value}
    if not isinstance(value, dict) or any(key not in {"claude", "codex"} for key in value):
        raise ValueError("Worker 配置无效")
    for provider, settings in value.items():
        _validate_managed_worker(settings, provider, check_runtime=False)
    return value


def load_managed_worker_settings(provider_id="claude"):
    """Load public local Worker settings; credentials are stored separately."""
    value = _load_managed_workers().get(provider_id)
    if value is None:
        return None
    return {key: value.get(key) for key in ("enabled", "distro", "endpoint", "runtime_dir", "model", "revision")}


async def save_managed_worker_settings(value, token=None, provider_id="claude"):
    """Persist public settings atomically and keep provider credentials in Windows."""
    _validate_managed_worker(value, provider_id)
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise ValueError("负责人凭据无效")
    if token is not None and not value["enabled"]:
        raise ValueError("启用负责人后才能保存凭据")
    previous = await asyncio.to_thread(load_managed_worker_settings, provider_id)
    if value["enabled"]:
        public = {key: value.get(key) for key in ("enabled", "distro", "endpoint", "runtime_dir", "model")}
    else:
        public = {key: (previous or {}).get(key) for key in ("distro", "endpoint", "runtime_dir", "model")}
        public["enabled"] = False
    endpoint = public.get("endpoint") or ""
    credentials = WindowsCredentials("DeepLoopMinder/worker/")
    identity = provider_id + "/" + hashlib.sha256(endpoint.encode()).hexdigest()
    inherited_token = (os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
                       if provider_id == "claude" else os.getenv("CODEX_API_KEY") or os.getenv("OPENAI_API_KEY"))
    if (public["enabled"] and previous and previous.get("endpoint") != endpoint and token is None
            and previous.get("endpoint") and (inherited_token
                or await credentials.get(provider_id + "/" + hashlib.sha256(previous["endpoint"].encode()).hexdigest()))):
        raise ValueError("更换负责人 API 地址时请重新提供凭据")
    if token is not None:
        await credentials.put(identity, token)
    changed = not previous or any(previous.get(key) != public.get(key) for key in public) or token is not None
    public["revision"] = uuid.uuid4().hex if changed else previous.get("revision")

    def write():
        path = _worker_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        all_settings = _load_managed_workers()
        all_settings[provider_id] = public
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(all_settings, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    await asyncio.to_thread(write)
    return public


class _ProjectWorkerPool:
    """Own SDK connections bound to one provider and one project directory."""

    def __init__(self, stack, settings):
        self._stack = stack
        self._settings = {entry["provider_id"]: entry for entry in settings}
        supervisor = Path(__file__).with_name("wsl_readonly.py")
        self._supervisor_digest = hashlib.sha256(supervisor.read_bytes()).hexdigest()
        self._versions = {provider: self._version(provider) for provider in self._settings}
        self._connections = {}
        self._readonly_connections = {}
        self._readonly_stacks = {}
        self._execution_connections = {}
        self._execution_stacks = {}
        self._lock = asyncio.Lock()

    def _version(self, provider):
        entry = self._settings[provider]
        inherited = {name: value for name, value in os.environ.items()
                     if name.startswith("ANTHROPIC_") or name in {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "DISABLE_AUTOUPDATER"}}
        if provider == "codex":
            inherited = {name: value for name, value in os.environ.items()
                         if name in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_CONFIG"}}
        effective_env = (entry.get("env") or {}) if entry.get("managed") else (
            {**inherited, **(entry.get("env") or {})} if provider in {"claude", "codex"} else entry.get("env") or {}
        )
        versioned = {"settings": entry, "effective_env": effective_env, "supervisor": self._supervisor_digest}
        return hashlib.sha256(json.dumps(versioned, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def get(self, provider_id, directory):
        from pathlib import Path

        cwd = await asyncio.to_thread(lambda: str(Path(directory).resolve(strict=True)))
        key = (provider_id, cwd)
        async with self._lock:
            worker = self._connections.get(key)
            if worker is not None and worker.process.returncode is None:
                return worker
            settings = self._settings.get(provider_id)
            if settings is None:
                raise ValueError("Worker 提供方未配置")
            grant = SecurityGrant(frozenset(settings["grants"]))
            worker = await self._stack.enter_async_context(
                ACPWorker(
                    provider_id,
                    settings["command"],
                    cwd,
                    grants=lambda _ref, grant=grant: grant,
                    approval=(
                        resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval") else None
                    ),
                    env=settings.get("env"),
                    timeout=settings.get("timeout", 180),
                )
            )
            self._connections[key] = worker
            return worker

    def observe_live(self, ref):
        """Read a bound live session only; never start or resume one for display."""
        if ref.isolation == "wsl-readonly":
            worker = self._readonly_connections.get((ref.provider_id, ref.cwd, ref.config_version))
            if worker is None:
                worker = self._execution_connections.get((ref.provider_id, ref.cwd, ref.config_version, ref.thread_id))
        else:
            worker = self._connections.get((ref.provider_id, ref.cwd))
        if worker is None or worker.process.returncode is not None:
            return None
        try:
            observed = worker.observe(ref)
        except (ValueError, PermissionError, KeyError):
            return None
        return {"context_usage": observed.get("context_usage"),
                "compaction_visibility": observed.get("compaction_visibility", "unknown")}

    async def _wsl_path(self, distro, path):
        executable = str(Path(os.environ["SystemRoot"]) / "System32" / "wsl.exe")
        process = await asyncio.create_subprocess_exec(
            executable, "-d", distro, "--", "/usr/bin/wslpath", "-a", str(path).replace("\\", "/"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("无法转换 WSL 路径") from None
        if process.returncode or not output.strip():
            raise ValueError("无法转换 WSL 路径")
        return output.decode("utf-8").strip()

    def _supervisor_directory(self):
        base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
        if not base:
            raise ValueError("无法确定本机应用数据目录")
        return (Path(base) / "DeepLoopMinder" / "acp-supervisors").resolve()

    async def _staged_supervisor(self, *, project, home, runtime):
        """Stage outside every Worker-writable mount before launching WSL."""
        directory = self._supervisor_directory()
        for path in (project, home, runtime):
            if directory.is_relative_to(path) or path.is_relative_to(directory):
                raise ValueError("可信监督器目录不能与项目、会话或运行环境重叠")
        source = Path(__file__).with_name("wsl_readonly.py")
        data = await asyncio.to_thread(source.read_bytes)
        if hashlib.sha256(data).hexdigest() != self._supervisor_digest:
            raise ValueError("只读执行器已变化，请重启宿主后重新验证")

        def stage():
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (self._supervisor_digest + ".py")
            if target.exists() or target.is_symlink():
                info = target.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                        or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or hashlib.sha256(target.read_bytes()).hexdigest() != self._supervisor_digest):
                    raise ValueError("可信监督器副本已损坏")
                return target.resolve(strict=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=directory, delete=False) as file:
                    temporary = Path(file.name)
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                # Competing local hosts stage identical content; a changed
                # existing copy is never overwritten silently.
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    info = target.lstat()
                    if (stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or hashlib.sha256(target.read_bytes()).hexdigest() != self._supervisor_digest):
                        raise ValueError("可信监督器副本已损坏")
                return target.resolve(strict=True)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return await asyncio.to_thread(stage)

    async def get_readonly(self, provider_id, directory, *, ephemeral_home=None):
        """Start only a locally preflighted WSL read-only ACP connection."""
        if not _WSL_BOUNDARY_AUDITED:
            raise ValueError("只读执行边界尚待独立安全验证，Worker 暂不可用")
        if os.name != "nt":
            raise ValueError("WSL 只读连接仅适用于 Windows 宿主")
        supervisor = Path(__file__).with_name("wsl_readonly.py")
        if hashlib.sha256(await asyncio.to_thread(supervisor.read_bytes)).hexdigest() != self._supervisor_digest:
            raise ValueError("只读执行器已变化，请重启宿主后重新验证")
        cwd = await asyncio.to_thread(lambda: str(Path(directory).resolve(strict=True)))
        key = (provider_id, cwd, self._versions.get(provider_id))
        async with self._lock:
            if provider_id in self._settings and self._version(provider_id) != self._versions[provider_id]:
                raise ValueError("Worker 配置已变化，请重启宿主后重新验证")
            existing = self._readonly_connections.get(key)
            if existing is not None and existing.process.returncode is None:
                return existing
            settings = self._settings.get(provider_id) or {}
            policy = settings.get("readonly") or {}
            if policy.get("kind") != "wsl-bubblewrap":
                raise ValueError("Worker 未配置可验证的只读运行环境")
            if provider_id not in {"claude", "codex"} or policy.get("agent_kind") != provider_id:
                raise ValueError("只读运行器与 Worker 提供方不匹配")
            distro, endpoint = policy.get("distro"), policy.get("endpoint")
            runtime_path = policy.get("runtime_dir")
            if (not isinstance(distro, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", distro)
                    or not isinstance(endpoint, str) or not isinstance(runtime_path, str)
                    or not Path(runtime_path).is_absolute()):
                raise ValueError("只读 Worker 配置不完整")
            runtime = await asyncio.to_thread(lambda: Path(runtime_path).resolve(strict=True))
            appdata = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
            if not appdata:
                raise ValueError("无法确定本机应用数据目录")
            identity = hashlib.sha256((provider_id + "\0" + os.path.normcase(cwd)).encode()).hexdigest()
            home = (Path(ephemeral_home) if ephemeral_home is not None else
                    Path(appdata) / "DeepLoopMinder" / "acp-readonly" / identity)
            await asyncio.to_thread(home.mkdir, parents=True, exist_ok=True)
            home = await asyncio.to_thread(home.resolve, strict=True)
            project = Path(cwd)
            application = Path(__file__).resolve().parents[3]
            app_root = (Path(appdata) / "DeepLoopMinder").resolve()
            for protected in (application, app_root):
                if protected.is_relative_to(project) or project.is_relative_to(protected):
                    raise ValueError("项目目录不能与当前宿主代码或应用数据重叠")
            if home.is_relative_to(project) or project.is_relative_to(home):
                raise ValueError("Worker 会话目录不能位于项目内或包含项目")
            if runtime.is_relative_to(project) or project.is_relative_to(runtime):
                raise ValueError("Worker 运行环境必须与项目目录分开，请在设置中选择项目外的运行环境")
            if runtime.is_relative_to(home) or home.is_relative_to(runtime):
                raise ValueError("Worker 运行环境必须与可写会话目录分开")
            script = await self._staged_supervisor(project=project, home=home, runtime=runtime)
            script_linux, runtime_linux, project_linux, home_linux = await asyncio.gather(
                self._wsl_path(distro, script), self._wsl_path(distro, runtime),
                self._wsl_path(distro, project), self._wsl_path(distro, home),
            )
            env = ({**os.environ, **(settings.get("env") or {})} if not settings.get("managed")
                   else dict(settings.get("env") or {}))
            allowed = [name for name in env if (
                (provider_id == "claude" and (name.startswith("ANTHROPIC_")
                                               or name in {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "DISABLE_AUTOUPDATER"}))
                or (provider_id == "codex" and name in {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_CONFIG"})
            )]
            env["WSLENV"] = ":".join(allowed)
            executable = str(Path(os.environ["SystemRoot"]) / "System32" / "wsl.exe")
            control = "/tmp/loopminder-control-" + uuid.uuid4().hex + ".sock"
            command = [executable, "-d", distro, "--", "/usr/bin/python3", "-I", script_linux,
                       "supervise", "--project", project_linux, "--runtime", runtime_linux,
                       "--home", home_linux, "--endpoint", endpoint, "--control", control, "--agent-kind", provider_id,
                       "--env-keys", ",".join(allowed)]
            # Probe only disposable directories. A failed or partial preflight
            # cannot produce a trusted enforcement fact.
            with tempfile.TemporaryDirectory(prefix="dlm-ro-probe-", dir=home.parent) as probe_project, \
                    tempfile.TemporaryDirectory(prefix="dlm-ro-home-", dir=home.parent) as probe_home:
                probe_project_linux, probe_home_linux = await asyncio.gather(
                    self._wsl_path(distro, probe_project), self._wsl_path(distro, probe_home)
                )
                probe = [executable, "-d", distro, "--", "/usr/bin/python3", "-I", script_linux,
                         "supervise", "--project", probe_project_linux, "--runtime", runtime_linux,
                         "--home", probe_home_linux, "--endpoint", endpoint, "--probe", "--agent-kind", provider_id,
                         "--env-keys", ",".join(allowed)]
                process = await asyncio.create_subprocess_exec(
                    *probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env={**os.environ, **env},
                )
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise ValueError("只读环境安全检查超时") from None
                if process.returncode or output.strip() != b"LOOPMINDER_READONLY_PREFLIGHT_OK":
                    raise ValueError("只读环境安全检查未通过")
            enforcement = EnforcementFact(
                surface="wsl_acp_process", capability=EnforcementCapability.ENFORCEABLE,
                enforced_capabilities=frozenset({"worker.readonly"}),
                mechanism="WSL bubblewrap read-only mount, isolated network and local-socket seccomp",
            )
            grant = SecurityGrant(frozenset(settings.get("grants", ())))
            connection_stack = AsyncExitStack()
            await connection_stack.__aenter__()
            try:
                worker = await connection_stack.enter_async_context(ACPWorker(
                    provider_id, command, cwd, grants=lambda _ref, grant=grant: grant,
                    approval=AllowlistProvider(allowed_tools=["acp.read", "acp.search", "acp.execute"]),
                    env={**(settings.get("env") or {}), "WSLENV": env["WSLENV"]},
                    timeout=settings.get("timeout", 180), native_cwd="/project", readonly_enforcement=enforcement,
                    config_version=self._versions[provider_id], suppress_stderr=True,
                ))
            except BaseException:
                await connection_stack.aclose()
                raise
            worker._sandbox_control = (distro, script_linux, control)
            self._stack.push_async_callback(connection_stack.aclose)
            self._readonly_connections[key] = worker
            self._readonly_stacks[key] = connection_stack
            return worker

    async def _stop_sandbox(self, worker):
        distro, script, control = worker._sandbox_control
        executable = str(Path(os.environ["SystemRoot"]) / "System32" / "wsl.exe")
        process = await asyncio.create_subprocess_exec(
            executable, "-d", distro, "--", "/usr/bin/python3", "-I", script, "stop", "--control", control,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("无法确认 Worker 进程已停止") from None
        if process.returncode:
            raise ValueError("无法确认 Worker 进程已停止")

    async def get_execution(self, ref, intent_id):
        """Switch an idle verified WSL session to a bounded writable process."""
        if ref.isolation != "wsl-readonly":
            raise ValueError("开发必须续接经过验证的只读负责人会话")
        if ref.provider_id != "claude":
            raise ValueError("该 Worker 只能用于只读审查，不能切换到开发")
        supervisor = Path(__file__).with_name("wsl_readonly.py")
        if hashlib.sha256(await asyncio.to_thread(supervisor.read_bytes)).hexdigest() != self._supervisor_digest:
            raise ValueError("执行器已变化，不能恢复原负责人会话")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
            raise ValueError("开发请求缺少有效的派工意图")
        key = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
        if ref.config_version != self._versions.get(ref.provider_id):
            raise ValueError("Worker 配置已变化，不能恢复原负责人会话")
        if self._version(ref.provider_id) != ref.config_version:
            raise ValueError("Worker 环境已变化，不能恢复原负责人会话")
        existing = self._execution_connections.get(key)
        if existing is not None:
            if existing.process is None or existing.process.returncode is not None:
                raise ValueError("上次 Worker 进程的退出尚未确认")
            return existing
        readonly = await self.get_readonly(ref.provider_id, ref.cwd)
        async with self._lock:
            existing = self._execution_connections.get(key)
            if existing is not None:
                if existing.process is None or existing.process.returncode is not None:
                    raise ValueError("上次 Worker 进程的退出尚未确认")
                return existing
            if any(lock.locked() for lock in readonly._prompt_locks.values()):
                raise ValueError("负责人还有正在运行的只读请求，不能切换到开发")
            await self._stop_sandbox(readonly)
            readonly_key = (ref.provider_id, ref.cwd, ref.config_version)
            await self._readonly_stacks.pop(readonly_key).aclose()
            self._readonly_connections.pop(readonly_key, None)
            distro, script, _ = readonly._sandbox_control
            control = "/tmp/loopminder-exec-" + intent_id + ".sock"
            command = list(readonly.command)
            control_index = command.index("--control") + 1
            command[control_index] = control
            command.append("--execution")
            stack = AsyncExitStack()
            await stack.__aenter__()
            settings = self._settings[ref.provider_id]
            approval = resolve_provider(ApprovalProviderConfig(**settings["approval"])) if settings.get("approval") else None
            worker = ACPWorker(
                ref.provider_id, command, ref.cwd, grants=readonly.grants, approval=approval,
                env=readonly.env, timeout=readonly.timeout, native_cwd=readonly.native_cwd,
                session_isolation=ref.isolation, config_version=ref.config_version, suppress_stderr=True,
            )
            worker._sandbox_control = (distro, script, control)
            self._stack.push_async_callback(stack.aclose)
            # Keep the supervisor identity even if startup fails: a child may
            # already exist and only a confirmed shutdown can clear quarantine.
            self._execution_connections[key], self._execution_stacks[key] = worker, stack
            await stack.enter_async_context(worker)
            return worker

    async def release_execution(self, ref):
        """Stop the writable process and its children after its official Run."""
        key = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
        async with self._lock:
            worker = self._execution_connections.get(key)
            if worker is None:
                raise ValueError("无法确认 Worker 进程已停止；项目保持隔离等待核对")
            await self._stop_sandbox(worker)
            await self._execution_stacks.pop(key).aclose()
            self._execution_connections.pop(key, None)

    def execution_ticket(self, ref, intent_id):
        """Identify only the supervisor belonging to this authorized Run."""
        if (ref.isolation != "wsl-readonly" or ref.config_version != self._versions.get(ref.provider_id)
                or not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id)):
            raise ValueError("无法建立可信的 Worker 进程标识")
        policy = self._settings[ref.provider_id].get("readonly") or {}
        distro = policy.get("distro")
        if not isinstance(distro, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", distro):
            raise ValueError("Worker WSL 配置无效")
        return {"distro": distro, "control": "/tmp/loopminder-exec-" + intent_id + ".sock",
                "supervisor_digest": self._supervisor_digest}

    async def reconcile_shutdown(self, ref, intent_id, ticket):
        """Stop a recorded supervisor; absence of a reply remains unknown."""
        if (not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id)
                or ticket.get("control") != "/tmp/loopminder-exec-" + intent_id + ".sock"
                or not isinstance(ticket.get("distro"), str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", ticket["distro"])
                or not isinstance(ticket.get("supervisor_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", ticket["supervisor_digest"])):
            raise ValueError("Worker 进程标识无效")
        directory = self._supervisor_directory()
        project = await asyncio.to_thread(lambda: Path(ref.cwd).resolve(strict=True))
        if directory.is_relative_to(project) or project.is_relative_to(directory):
            raise ValueError("可信监督器目录与项目重叠")
        staged = directory / (ticket["supervisor_digest"] + ".py")
        info = await asyncio.to_thread(staged.lstat)
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("可信监督器副本不安全")
        data = await asyncio.to_thread(staged.read_bytes)
        if hashlib.sha256(data).hexdigest() != ticket["supervisor_digest"]:
            raise ValueError("可信监督器副本已变化")
        script = await self._wsl_path(ticket["distro"], staged.resolve(strict=True))
        from types import SimpleNamespace

        async with self._lock:
            await self._stop_sandbox(SimpleNamespace(_sandbox_control=(ticket["distro"], script, ticket["control"])))
            key = (ref.provider_id, ref.cwd, ref.config_version, ref.thread_id)
            stack = self._execution_stacks.pop(key, None)
            if stack is not None:
                await stack.aclose()
            self._execution_connections.pop(key, None)


_project_workers = None


@asynccontextmanager
async def lifespan(app):
    """Let the ACP SDK own process setup/teardown inside Starlette's lifespan."""
    global _project_workers, _active_managed_revision
    async with AsyncExitStack() as stack:
        # Register cleanup before initialization so startup failures also clear
        # the live graph references, after the SDK resources have been closed.
        stack.callback(_workers.clear)
        stack.callback(_candidates.clear)
        stack.callback(_worker_errors.clear)
        configured_workers = json.loads(os.getenv("ACP_WORKERS_JSON") or "[]")
        managed_workers = await asyncio.to_thread(_load_managed_workers)
        _active_managed_revision = {name: value.get("revision") for name, value in managed_workers.items()}
        if managed_workers:
            configured_workers = [entry for entry in configured_workers if entry.get("provider_id") not in managed_workers]
            for provider, managed in managed_workers.items():
                if not managed["enabled"]:
                    continue
                identity = provider + "/" + hashlib.sha256(managed["endpoint"].encode()).hexdigest()
                try:
                    token = await WindowsCredentials("DeepLoopMinder/worker/").get(identity)
                except Exception:
                    token = None
                    _worker_errors[provider] = "无法读取本机 Worker 凭据"
                token = token or (os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
                                  if provider == "claude" else os.getenv("CODEX_API_KEY") or os.getenv("OPENAI_API_KEY"))
                if provider == "claude":
                    worker_env = {"ANTHROPIC_BASE_URL": managed["endpoint"], "ANTHROPIC_MODEL": managed["model"]}
                else:
                    worker_env = {"CODEX_CONFIG": json.dumps({
                        "model": managed["model"], "model_provider": "loopminder",
                        "model_providers": {"loopminder": {"name": "Loopminder", "base_url": managed["endpoint"],
                                                          "env_key": "OPENAI_API_KEY", "wire_api": "responses"}},
                    })}
                if token:
                    worker_env["ANTHROPIC_AUTH_TOKEN" if provider == "claude" else "OPENAI_API_KEY"] = token
                else:
                    _worker_errors[provider] = "Worker 凭据未配置"
                configured_workers.append({
                    "provider_id": provider, "roles": ["developer" if provider == "claude" else "reviewer"], "command": [],
                    "capabilities": (["repo.read", "repo.write", "files.read", "files.write", "shell.execute", "test.execute"]
                                     if provider == "claude" else ["repo.read", "files.read", "shell.execute", "test.execute"]),
                    "grants": ["worker.connect", "worker.new", "worker.ask", "worker.cancel", "worker.resume",
                               "worker.observe", "worker.steer", "external_tool.read", "external_tool.search",
                               "external_tool.edit", "external_tool.execute", "external_tool.fetch", "external_tool.other"],
                    "env": worker_env, "revision": managed.get("revision"), "timeout": 300,
                    "approval": {"use": "src.middlewares.approval.builtin:YamlPolicyProvider", "config": {}},
                    "readonly": {"kind": "wsl-bubblewrap", "agent_kind": provider, "distro": managed["distro"],
                                 "endpoint": managed["endpoint"], "runtime_dir": managed["runtime_dir"]},
                    "managed": True,
                })
        _project_workers = _ProjectWorkerPool(stack, configured_workers)
        bootstrap = None
        for settings in configured_workers:
            provider = settings["provider_id"]
            if provider in _workers:
                raise ValueError(f"duplicate ACP provider: {provider}")
            if settings.get("managed"):
                try:
                    if provider in _worker_errors:
                        raise ValueError(_worker_errors[provider])
                    if bootstrap is None:
                        bootstrap = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="loopminder-acp-bootstrap-")))
                    bootstrap_project = bootstrap / (provider + "-project")
                    bootstrap_home = bootstrap / (provider + "-home")
                    await asyncio.to_thread(bootstrap_project.mkdir, parents=True, exist_ok=True)
                    worker = await _project_workers.get_readonly(provider, str(bootstrap_project), ephemeral_home=bootstrap_home)
                except Exception:
                    _worker_errors.setdefault(provider, "负责人只读环境不可用或安全检查未通过")
                    _candidates.append(WorkerCandidate(provider, "external_acp", frozenset(settings["roles"]),
                                                       frozenset(settings.get("capabilities", ())), False))
                    continue
                _workers[provider] = worker
                _candidates.append(worker_candidate(provider, frozenset(settings["roles"]), worker.initialized,
                                                    business_capabilities=frozenset(settings.get("capabilities", ()))))
                continue
            grant = SecurityGrant(frozenset(settings["grants"]))
            worker = await stack.enter_async_context(
                ACPWorker(
                    provider,
                    settings["command"],
                    settings["cwd"],
                    grants=lambda _ref, grant=grant: grant,
                    approval=(
                        resolve_provider(ApprovalProviderConfig(**settings["approval"]))
                        if settings.get("approval")
                        else None
                    ),
                    env=settings.get("env"),
                    timeout=settings.get("timeout", 180),
                )
            )
            _workers[provider] = worker
            _candidates.append(
                worker_candidate(
                    provider,
                    frozenset(settings["roles"]),
                    worker.initialized,
                    business_capabilities=frozenset(settings.get("capabilities", ())),
                )
            )
        yield


app = Starlette(routes=workbench_routes, lifespan=lifespan)


def worker_catalog():
    """返回当前连接的角色/能力事实，不读取或暴露认证配置。"""
    return tuple(
        replace(candidate, runtime_available=bool(
            (worker := _workers.get(candidate.provider_id)) is not None
            and worker.process.returncode is None
        ))
        for candidate in _candidates
    )


def acp_worker():
    """Return the role-routed ACP worker graph registered in langgraph.json."""
    return create_worker_graph(_workers, _candidates, project_workers=_project_workers)


def acp_control():
    """Return the one-shot control graph registered in langgraph.json."""
    return create_control_graph(_workers, project_workers=_project_workers)
