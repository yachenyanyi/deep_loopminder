"""Local workbench routes; project and model facts use the official Store."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from langgraph_sdk import get_client
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from src.middlewares.project_state.artifacts import content_hash
from src.middlewares.project_state.folders import ProjectFolders
from src.middlewares.project_state.models import ProjectSnapshot
from src.middlewares.project_state.persistence import ProjectStore
from src.middlewares.project_state.scope import directory_write_lock, project_write_lock
from src.middlewares.trajectory.middleware import TrajectoryRecorder
from src.middlewares.trajectory.paths import trajectory_directory
from src.middlewares.trajectory.web import read_events

from .context import ROOT as _ROOT
from .context import THREADS as _THREADS
from .context import WRITE_LOCK as _write_lock
from .context import forbidden as _forbidden
from .context import get_store
from .context import local as _local
from .context import run_config
from .decisions import pending_decision, submit_decision
from .threads import project_thread
from .settings_routes import (
    clef_settings,
    default_model,
    discover,
    providers,
    runtime_settings,
    worker_modes,
    worker_settings,
    workspace_settings,
)

_MODEL_CONFIGURATION_ERRORS = frozenset({
    "尚未配置默认模型，请在设置中选择已启用的模型",
    "切换模型时必须同时提供模型服务和模型名称",
    "模型服务或名称无效",
    "模型选择需要配置 Store",
    "所选模型服务不存在",
    "所选模型不存在或已禁用",
    "模型未配置所选思考等级",
    "所选模型服务未配置凭据",
    "模型选择相互冲突",
    "模型配置记录无效",
    "模型服务地址无效",
    "请输入不含凭据、参数或片段的 HTTP(S) API 地址",
    "非本机模型服务必须使用 HTTPS",
    "模型凭据配置无效",
})


def _check_project_location(directory):
    """Keep Worker projects separate from the running host and its data."""
    project = Path(directory).resolve(strict=True)
    base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
    if not base:
        raise ValueError("无法确定本机应用数据目录")
    protected = (Path(__file__).resolve().parents[2], (Path(base) / "DeepLoopMinder").resolve())
    if any(path.is_relative_to(project) or project.is_relative_to(path) for path in protected):
        raise ValueError("项目目录不能与当前宿主代码或应用数据重叠")


async def index(request):
    """Serve the local application shell."""
    if not _local(request):
        return _forbidden()
    return FileResponse(_ROOT / "index.html", headers={"Cache-Control": "no-store"})


async def javascript(request):
    """Serve the workbench script to the local workbench only."""
    if not _local(request):
        return _forbidden()
    return FileResponse(_ROOT / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


async def projects(request):
    """List projects and their bound directories."""
    if not _local(request):
        return _forbidden()
    store = await get_store()
    projects, offset = [], 0
    while True:
        page = await ProjectStore(store).list(limit=100, offset=offset)
        projects.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    folders = ProjectFolders(store)
    result = []
    for project in projects:
        folder = await folders.get(project.project_id)
        result.append({"project_id": project.project_id, "goal": project.goal,
                       "directory": folder["directory"] if folder else None})
    return JSONResponse(result)


async def create_project(request):
    """Create a project for an existing local folder, under the host writer lock."""
    if not _local(request):
        return _forbidden()
    try:
        data = await request.json()
        directory, goal = data.get("directory"), data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("请输入项目目标")
        store = await get_store()
        folders = ProjectFolders(store)
        _, directory_identity = await folders.canonical_directory(directory)
        async with _write_lock, directory_write_lock("project-folder-registry"):
            if (await folders.canonical_directory(directory))[1] != directory_identity:
                raise ValueError("项目文件夹身份已变化，请重新选择")
            await asyncio.to_thread(_check_project_location, directory)
            existing = await folders.find_directory(directory)
            if existing:
                return JSONResponse({"error": "该文件夹已关联项目", "project_id": existing["project_id"]}, status_code=409)
            overlap = await folders.find_overlapping_directory(directory)
            if overlap:
                return JSONResponse({"error": "该文件夹与已有项目目录重叠，请选择不重叠的文件夹。"}, status_code=409)
            project_id = "p-" + uuid.uuid4().hex
            # Save the discoverable project first. If the process dies before
            # binding, it remains visible as an unbound project and can be
            # completed using the normal bind operation. Binding first can
            # strand a directory behind an invisible project ID.
            await ProjectStore(store).save(ProjectSnapshot(project_id, goal.strip(), (), ()))
            try:
                binding = await folders.bind(project_id, directory)
            except Exception:
                return JSONResponse(
                    {"error": "项目已创建，但文件夹绑定未完成；请打开该项目重新绑定。",
                     "project_id": project_id}, status_code=503
                )
        return JSONResponse({"project_id": project_id, "goal": goal.strip(), "directory": binding["directory"]})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def bind_project(request):
    """Bind a previously unbound project to an existing local folder."""
    if not _local(request):
        return _forbidden()
    try:
        data = await request.json()
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("项目编号无效")
        store = await get_store()
        folders = ProjectFolders(store)
        _, directory_identity = await folders.canonical_directory(data.get("directory"))
        async with _write_lock, project_write_lock(project_id), directory_write_lock("project-folder-registry"):
            if (await folders.canonical_directory(data.get("directory")))[1] != directory_identity:
                raise ValueError("项目文件夹身份已变化，请重新选择")
            await asyncio.to_thread(_check_project_location, data.get("directory"))
            if await ProjectStore(store).load(project_id) is None:
                return JSONResponse({"error": "项目不存在"}, status_code=404)
            binding = await folders.bind(project_id, data.get("directory"))
        return JSONResponse(binding)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _select_folder():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="选择项目文件夹", mustexist=True)
    finally:
        root.destroy()


async def select_folder(request):
    """Ask the local user to select a directory using the OS dialog."""
    if not _local(request):
        return _forbidden()
    try:
        return JSONResponse({"directory": await asyncio.to_thread(_select_folder)})
    except Exception:
        return JSONResponse({"error": "无法打开文件夹选择器，请手动输入绝对路径"}, status_code=503)


def _confirm_local(title, message):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return messagebox.askyesno(title, message, parent=root)
    finally:
        root.destroy()


async def history(request):
    """Load the current PM thread without copying its checkpoint."""
    if not _local(request):
        return _forbidden()
    project_id = request.query_params.get("project_id", "")
    store = await get_store()
    if not project_id or await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    item = await store.aget(_THREADS, project_id)
    if not item:
        return JSONResponse({"messages": []})
    thread_id = item.value.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return JSONResponse({"error": "项目的 PM 会话记录不完整；请核对恢复，未创建新会话。"}, status_code=409)
    try:
        thread_id = await project_thread(store, get_client(url=None), project_id, create_if_missing=False)
        async with asyncio.timeout(15):
            snapshot = await get_client(url=None).threads.get_state(thread_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # 与 project_thread 的同一事实保持同一文案，避免同义漂移。
            return JSONResponse({"error": "已关联的 PM 会话在官方服务中不存在；请核对恢复，未创建新会话。"}, status_code=409)
        return JSONResponse({"error": "无法读取已关联的 PM 会话，请检查官方服务。"}, status_code=503)
    except (httpx.HTTPError, TimeoutError):
        return JSONResponse({"error": "无法读取已关联的 PM 会话，请检查官方服务。"}, status_code=503)
    if not isinstance(snapshot, dict):
        return JSONResponse({"error": "官方会话状态格式无效，请检查服务。"}, status_code=503)
    values = snapshot.get("values")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return JSONResponse({"error": "官方会话状态格式无效，请检查服务。"}, status_code=503)
    messages = values.get("messages")
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        return JSONResponse({"error": "官方会话消息格式无效，请检查服务。"}, status_code=503)
    selected = values.get("pm_model_selection")
    if selected is not None:
        if (not isinstance(selected, dict) or not isinstance(selected.get("provider_id"), str)
                or not isinstance(selected.get("model_id"), str)
                or (selected.get("reasoning_effort") is not None
                    and not isinstance(selected.get("reasoning_effort"), str))
                or values.get("pm_model_legacy") is True):
            return JSONResponse({"error": "会话模型选择记录无效，请检查会话状态。"}, status_code=409)
        selected = {key: selected.get(key) for key in ("provider_id", "model_id", "reasoning_effort")}
    return JSONResponse({"messages": [TrajectoryRecorder.clean_public(message) for message in messages],
                         "model_selection": TrajectoryRecorder.clean_public(selected),
                         "model_legacy": values.get("pm_model_legacy") is True,
                         "memory_model_error": TrajectoryRecorder.clean_public(values.get("pm_memory_model_error"))})


async def dispatch_status(request):
    """Expose the saved dispatch intent for manual reconciliation."""
    if not _local(request):
        return _forbidden()
    project_id = request.query_params.get("project_id", "")
    task_id = request.query_params.get("task_id", "")
    if not project_id or not task_id:
        return JSONResponse({"error": "项目和任务不能为空"}, status_code=400)
    store = await get_store()
    if await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    item = await store.aget(("deep_loopminder", "project_dispatch_intents", project_id), task_id)
    return JSONResponse({"intent": dict(item.value) if item else None})


async def _merged_mode_record(store, project_id: str, run_id: str) -> dict:
    """最末轮商讨的适配器回报，从 project_run_modes 权威记录合并。

    商讨门禁在派工返回时立即读回，可能早于 Worker 图的异步写入；这里以
    Run 为键重读，目标模式（用户保存的默认值）保持记录原值，只补回报字段。
    """
    item = await store.aget(("deep_loopminder", "project_run_modes", project_id), run_id)
    stored = item.value if item else {}
    return {"requested_mode": stored.get("requested_mode"),
            "reported_mode": stored.get("reported_mode"),
            "reported_mode_source": stored.get("reported_mode_source"),
            "confirmed_at": stored.get("confirmed_at"),
            "requested_mode_source": stored.get("requested_mode_source")}


async def project_status(request):
    """Present project facts, recorded decisions and actual validation state."""
    if not _local(request):
        return _forbidden()
    project_id = request.query_params.get("project_id", "")
    store = await get_store()
    snapshot = await ProjectStore(store).load(project_id) if project_id else None
    if snapshot is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from src.deep_agents.agents.pm_agent.discussion import DiscussionGate
    from src.middlewares.project_state.workflow import ProjectWorkflow

    gate = DiscussionGate(store)
    workflow = ProjectWorkflow(store)
    validation = {item["task_id"]: item for item in await workflow.review_status(snapshot)}
    tasks = []
    for task in snapshot.tasks:
        decision = await gate.latest(project_id, task.task_id)
        current = False
        if decision:
            try:
                await gate.require_pass(project_id, task.task_id, worker_agent_id=decision["worker_agent_id"])
                current = True
            except (ValueError, KeyError):
                pass
        turns = []
        for execution_ref in task.execution_refs:
            run_id = execution_ref.rsplit("/", 1)[-1]
            source = await store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
            binding = source.value if source else {}
            if binding.get("phase") != "discussion":
                continue
            if (binding.get("project_id") != project_id or binding.get("task_id") != task.task_id
                    or binding.get("reference") != execution_ref or binding.get("run_id") != run_id):
                turns.append({"unavailable": True})
                continue
            reference = binding.get("result_ref")
            turn = {"question": binding.get("discussion_question"), "run_id": run_id,
                    "status": "not_received", "included_in_decision": bool(
                        decision and reference and reference in decision.get("discussion_turn_refs", [])
                    )}
            if reference:
                try:
                    artifact = await workflow.artifacts.get(project_id, reference, task_id=task.task_id, kind="worker_result")
                    result = json.loads(artifact["content"])
                    if (result.get("run_id") != run_id or result.get("agent_id") != binding.get("agent_id")
                            or artifact.get("producer") != binding.get("agent_id")):
                        raise ValueError("invalid discussion source")
                    turn.update({"status": result.get("status", "unknown"), "result": result.get("result")})
                except (ValueError, TypeError, KeyError, AttributeError):
                    turn = {"unavailable": True}
            turns.append(turn)
        intent = await store.aget(("deep_loopminder", "project_dispatch_intents", project_id), task.task_id)
        tasks.append({
            "task_id": task.task_id, "title": task.title, "status": task.status.value,
            "owner_role": task.owner_role, "acceptance_criteria": list(task.acceptance_criteria),
            "discussion": ({"decision": decision.get("decision") if decision else "not_recorded",
                            "answer": decision.get("answer") if decision else None,
                            "created_at": decision.get("created_at") if decision else None,
                            "current_pass": current,
                            "pm_confirmation": decision.get("pm_confirmation") if decision else None,
                            "mode_record": (await _merged_mode_record(store, project_id, turns[-1]["run_id"])
                                            if turns else (decision.get("mode_record") if decision else None)),
                            "content_digest_scope": decision.get("project_content_digest_scope") if decision else None,
                            "turns": turns} if decision or turns else None),
            "dispatch": dict(intent.value) if intent else None,
            "validation": validation.get(task.task_id),
            "independent_codex_review": await workflow.codex_review_status(project_id, task.task_id),
        })
    quarantines = await store.asearch(("deep_loopminder", "project_worker_quarantine", project_id), limit=100)
    return JSONResponse({"project_id": project_id, "goal": TrajectoryRecorder.clean_public(snapshot.goal),
                         "tasks": [TrajectoryRecorder.clean_public(task) for task in tasks],
                         "unconfirmed_workers": [{"agent_id": item.key, "task_id": item.value.get("task_id")}
                                                 for item in quarantines]})


async def stop_unconfirmed_worker(request):
    """Ask the recorded supervisor to stop; never infer shutdown from absence."""
    if not _local(request):
        return _forbidden()
    data = await request.json()
    project_id, agent_id = data.get("project_id"), data.get("agent_id")
    if not isinstance(project_id, str) or not project_id or not isinstance(agent_id, str) or not agent_id:
        return JSONResponse({"error": "项目和负责人不能为空"}, status_code=400)
    store = await get_store()
    namespace = ("deep_loopminder", "project_worker_quarantine", project_id)
    item = await store.aget(namespace, agent_id)
    if item is None:
        return JSONResponse({"error": "没有待核对的 Worker"}, status_code=404)
    value = dict(item.value)
    from src.middlewares.acp import server as acp_server
    from src.middlewares.acp.correlation import WorkerRef

    try:
        ref = WorkerRef(**value["worker_ref"])
        pool = acp_server.project_worker_pool()
        if ref.project_id != project_id or ref.thread_id != agent_id or pool is None:
            raise ValueError("Worker 来源不匹配")
        await pool.reconcile_shutdown(ref, value["intent_id"], value["ticket"])
    except (ValueError, KeyError, TypeError, OSError):
        return JSONResponse({"error": "无法确认进程已停止；项目仍保持阻断。"}, status_code=409)
    async with project_write_lock(project_id):
        current = await store.aget(namespace, agent_id)
        if current is None:
            return JSONResponse({"status": "confirmed_stopped"})
        if current.value.get("intent_id") != value.get("intent_id"):
            return JSONResponse({"error": "Worker 状态已变化，请重新核对。"}, status_code=409)
        await store.adelete(namespace, agent_id)
    return JSONResponse({"status": "confirmed_stopped"})


async def human_validation(request):
    """Record explicit local-human acceptance separately from Codex review."""
    if not _local(request):
        return _forbidden()
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "验收请求无效"}, status_code=400)
    project_id, task_id, criteria = data.get("project_id"), data.get("task_id"), data.get("criteria")
    if not isinstance(project_id, str) or not project_id or not isinstance(task_id, str) or not task_id or not isinstance(criteria, dict):
        return JSONResponse({"error": "项目、任务和验收结果不能为空"}, status_code=400)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    store = await get_store()
    workflow = ProjectWorkflow(store)
    try:
        _, task = await workflow.task(project_id, task_id)
        if set(criteria) != set(task.acceptance_criteria) or any(type(value) is not bool for value in criteria.values()):
            raise ValueError("必须逐条填写当前验收标准")
        initial_review = await workflow.codex_review_status(project_id, task_id)
        if initial_review.get("status") != "pass":
            raise ValueError("当前交付缺少通过的独立 Codex 审查")
        lines = [f"{'通过' if criteria[item] else '未通过'}：{TrajectoryRecorder.clean_public(item)}" for item in task.acceptance_criteria]
        confirmed = await asyncio.to_thread(_confirm_local, "确认任务验收", f"任务：{TrajectoryRecorder.clean_public(task.title)}\n\n" + "\n".join(lines) + "\n\n确认记录以上验收结果？这一步不是人工批准。")
        if not confirmed:
            return JSONResponse({"status": "cancelled"})
        async with project_write_lock(project_id):
            _, current = await workflow.task(project_id, task_id)
            if current.acceptance_criteria != task.acceptance_criteria:
                raise ValueError("验收标准已变化，请重新核对")
            if await store.asearch(("deep_loopminder", "project_worker_quarantine", project_id), limit=1):
                raise ValueError("Worker 尚未确认停止")
            review = await workflow.codex_review_status(project_id, task_id)
            if review.get("status") != "pass" or review.get("review_id") != initial_review.get("review_id"):
                raise ValueError("审查结论或交付已变化，请重新核对")
            run_ids = (review["developer_run_id"], review["review_run_id"])
            evidence = []
            for run_id in run_ids:
                source = await store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
                if source is None or source.value.get("reference") not in current.execution_refs:
                    raise ValueError("任务含未知的 Run 来源")
                evidence.append(source.value.get("result_ref"))
            if not evidence or any(not ref for ref in evidence):
                raise ValueError("最新 Run 尚未全部领取结果")
            reference = await workflow._record_validation(
                project_id, task_id, validator_id="local-human", criteria=criteria,
                evidence_refs=tuple(evidence), required_run_ids=run_ids,
            )
        return JSONResponse({"status": "recorded", "validation_ref": reference})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "无法取得本机确认或记录验收。"}, status_code=503)


async def human_approval(request):
    """Record a separately confirmed local-human approval or rejection."""
    if not _local(request):
        return _forbidden()
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "审批请求无效"}, status_code=400)
    project_id, task_id, approved = data.get("project_id"), data.get("task_id"), data.get("approved")
    if not isinstance(project_id, str) or not project_id or not isinstance(task_id, str) or not task_id or type(approved) is not bool:
        return JSONResponse({"error": "项目、任务和明确决定不能为空"}, status_code=400)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    store = await get_store()
    workflow = ProjectWorkflow(store)
    try:
        _, task = await workflow.task(project_id, task_id)
        initial_review = await workflow.codex_review_status(project_id, task_id)
        if initial_review.get("status") != "pass":
            raise ValueError("当前交付缺少通过的独立 Codex 审查")
        choice = "批准" if approved else "拒绝"
        confirmed = await asyncio.to_thread(_confirm_local, "确认人工决定", f"任务：{TrajectoryRecorder.clean_public(task.title)}\n\n确认{choice}当前已验收的交付？")
        if not confirmed:
            return JSONResponse({"status": "cancelled"})
        async with project_write_lock(project_id):
            if await store.asearch(("deep_loopminder", "project_worker_quarantine", project_id), limit=1):
                raise ValueError("Worker 尚未确认停止")
            review = await workflow.codex_review_status(project_id, task_id)
            if review.get("status") != "pass" or review.get("review_id") != initial_review.get("review_id"):
                raise ValueError("审查结论或交付已变化，请重新核对")
            decision_id = uuid.uuid4().hex
            decision_ref = "local-human-decision://" + decision_id
            await store.aput(("deep_loopminder", "human_decisions", project_id), decision_id,
                             {"task_id": task_id, "actor_id": "local-human", "approved": approved}, index=False)
            await workflow._record_approval(project_id, task_id, actor_id="local-human",
                                            decision_ref=decision_ref, approved=approved)
        return JSONResponse({"status": "recorded", "approved": approved})
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "无法取得本机确认或记录人工决定。"}, status_code=503)


async def worker_usage(request):
    """Read only Worker metrics reported in this project's official checkpoints."""
    if not _local(request):
        return _forbidden()
    project_id = request.query_params.get("project_id", "")
    store = await get_store()
    if not project_id or await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    parent = await store.aget(_THREADS, project_id)
    if not parent:
        return JSONResponse({"workers": []})
    owner = parent.value["thread_id"]
    client = get_client(url=None)
    from src.middlewares.acp import server as acp_server
    from src.middlewares.acp.correlation import WorkerRef

    parent_state = await client.threads.get_state(owner)
    state = parent_state.get("values", {})
    workers = []
    for worker_id, tracked in state.get("async_tasks", {}).items():
        worker = {"agent_id": worker_id, "role": tracked.get("agent_name"),
                  "run_id": tracked.get("run_id"), "context_usage": None,
                  "compaction_visibility": "unknown"}
        history = await client.threads.get_history(worker_id, metadata={"run_id": tracked.get("run_id")}, limit=1)
        values = history[0].get("values", {}) if history else {}
        ref = values.get("worker_ref") or {}
        if values.get("owner_thread_id") != owner or ref.get("project_id") != project_id:
            continue
        pool = acp_server.project_worker_pool()
        if pool is not None:
            try:
                live = pool.observe_live(WorkerRef(**ref))
            except (ValueError, TypeError):
                live = None
            if live:
                worker.update(live)
        for message in reversed(values.get("messages", [])):
            extra = message.get("additional_kwargs", {})
            if extra.get("acp_run_id") == tracked.get("run_id"):
                if worker["context_usage"] is None:
                    worker["context_usage"] = extra.get("context_usage")
                    worker["compaction_visibility"] = extra.get("compaction_visibility", "unknown")
                break
        workers.append(worker)
    return JSONResponse({"workers": workers})


async def _matching_dispatches(project_id, task_id, intent, candidate_agent_id=None):
    """Return only runs whose official checkpoint proves this exact intent."""
    worker_id = intent.get("worker_agent_id") or candidate_agent_id
    if candidate_agent_id is not None and (not isinstance(candidate_agent_id, str) or not candidate_agent_id.strip()
                                           or len(candidate_agent_id) > 128):
        raise ValueError("候选会话编号无效")
    if intent.get("worker_agent_id") and candidate_agent_id and candidate_agent_id != worker_id:
        raise ValueError("候选会话与派工意图不一致")
    if not worker_id or not intent.get("intent_id"):
        return []
    history = (await get_store()).aget(
        ("deep_loopminder", "project_dispatch_intents", project_id, "history"), intent["intent_id"]
    )
    claimed_run_id = history.value.get("claimed_run_id") if history else None
    if not claimed_run_id:
        return []
    matches, offset = [], 0
    client = get_client(url=None)
    # A missing checkpoint does not prove absence. The bounded scan can only
    # establish existence of a run, never that an unobserved run doesn't exist.
    while offset < 1000:
        runs = await client.runs.list(worker_id, limit=100, offset=offset)
        for run in runs:
            snapshots = await client.threads.get_history(worker_id, metadata={"run_id": run["run_id"]}, limit=1)
            values = snapshots[0].get("values", {}) if snapshots else {}
            envelope, ref = values.get("request") or {}, values.get("worker_ref") or {}
            if (run["run_id"] == claimed_run_id
                    and envelope.get("pm_dispatch_id") == intent["intent_id"]
                    and envelope.get("pm_phase") == intent.get("phase")
                    and isinstance(envelope.get("message"), str)
                    and content_hash(envelope["message"]) == intent.get("message_digest")
                    and values.get("owner_thread_id") == intent.get("owner_thread")
                    and ref.get("project_id") == project_id and ref.get("task_id") == task_id
                    and ref.get("thread_id") == worker_id
                    and (intent.get("phase") == "execution" or ref.get("isolation") == "native-readonly")):
                matches.append({"agent_id": worker_id, "run_id": run["run_id"], "status": run["status"]})
        if len(runs) < 100:
            break
        offset += len(runs)
    return matches


async def reconcile_dispatch(request):
    """Find proven matching official Runs without concluding absence or retrying."""
    if not _local(request):
        return _forbidden()
    data = await request.json()
    project_id, task_id = data.get("project_id"), data.get("task_id")
    if not isinstance(project_id, str) or not project_id or not isinstance(task_id, str) or not task_id:
        return JSONResponse({"error": "项目和任务不能为空"}, status_code=400)
    store = await get_store()
    if await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    item = await store.aget(("deep_loopminder", "project_dispatch_intents", project_id), task_id)
    intent = item.value if item else None
    if not intent or intent.get("status") != "pending":
        return JSONResponse({"error": "没有待核对的派工意图"}, status_code=409)
    try:
        matches = await _matching_dispatches(project_id, task_id, intent, data.get("agent_id"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"status": "unknown", "matches": [], "error": "无法读取官方 Run，请稍后核对。"}, status_code=503)
    return JSONResponse({"status": "matched" if matches else "unknown", "matches": matches})


async def attach_reconciled_dispatch(request):
    """Attach an independently verified Run without creating or restarting it."""
    if not _local(request):
        return _forbidden()
    data = await request.json()
    project_id, task_id, run_id = (data.get(key) for key in ("project_id", "task_id", "run_id"))
    if not all(isinstance(value, str) and value for value in (project_id, task_id, run_id)):
        return JSONResponse({"error": "项目、任务和 Run 编号不能为空"}, status_code=400)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    store = await get_store()
    namespace = ("deep_loopminder", "project_dispatch_intents", project_id)
    item = await store.aget(namespace, task_id)
    intent = item.value if item else None
    if not intent or intent.get("status") != "pending":
        return JSONResponse({"error": "没有待核对的派工意图"}, status_code=409)
    try:
        matches = await _matching_dispatches(project_id, task_id, intent, data.get("agent_id"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "无法读取官方 Run，未修改项目记录。"}, status_code=503)
    if len(matches) != 1 or matches[0]["run_id"] != run_id:
        return JSONResponse({"error": "无法唯一验证该 Run，未修改项目记录。"}, status_code=409)
    async with _write_lock, project_write_lock(project_id):
        workflow = ProjectWorkflow(store)
        try:
            snapshot, task = await workflow.task(project_id, task_id)
            if await workflow.input_version(snapshot, task) != intent.get("input_version"):
                raise ValueError("任务输入已变化，需人工处理旧 Run。")
            # Recheck the pending intent after the external lookup.
            current = await store.aget(namespace, task_id)
            if not current or current.value.get("intent_id") != intent["intent_id"] or current.value.get("status") != "pending":
                raise ValueError("派工状态已变化，请重新核对。")
            phase = intent.get("phase")
            enforcement = None
            if phase in {"discussion", "review"}:
                from src.middlewares.execution.security_policy import (
                    EnforcementCapability,
                    EnforcementFact,
                )

                if not intent.get("readonly_mechanism"):
                    raise ValueError("缺少执行边界来源，不能恢复")
                # Rebuild the boundary exactly as it was recorded. Upgrading a
                # writable-mode discussion to ENFORCEABLE here would fabricate
                # a read-only guarantee the original dispatch never had.
                recorded = intent.get("readonly_capability")
                capability = (EnforcementCapability(recorded)
                              if recorded in {item.value for item in EnforcementCapability}
                              else EnforcementCapability.PARTIAL)
                enforcement = EnforcementFact(
                    surface="native_acp_permission_mode",
                    capability=capability,
                    enforced_capabilities=(frozenset({"worker.readonly"})
                                           if capability is EnforcementCapability.ENFORCEABLE
                                           else frozenset()),
                    mechanism=intent["readonly_mechanism"],
                    gap=intent.get("readonly_gap"),
                )
            agent_id = matches[0]["agent_id"]
            # Carry the mode record the original dispatch persisted so the
            # existing execution binding is not seen as conflicting.
            await workflow.record_start(project_id, task_id, intent["owner_thread"], agent_id, run_id,
                                        phase=phase, readonly_enforcement=enforcement, dispatch_id=intent["intent_id"],
                                        discussion_question=intent.get("discussion_question"),
                                        expected_review_subject=intent.get("expected_review_subject"),
                                        mode_record=intent.get("mode_record"))
            await workflow.complete_dispatch(project_id, task_id, agent_id=agent_id, run_id=run_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"status": "confirmed", "run_id": run_id})


async def chat(request):
    """Stream an official PM run using the shared local project-writer lock."""
    if not _local(request):
        return _forbidden()
    try:
        data = await request.json()
        project_id, text = data.get("project_id"), data.get("message")
        if not isinstance(project_id, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("请选择项目并输入消息")
        store = await get_store()
        if await ProjectStore(store).load(project_id) is None:
            raise ValueError("项目不存在")
        client = get_client(url=None)
        thread_id = await project_thread(store, client, project_id)
        config = run_config(project_id, data)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    async def events():
        try:
            async for event in client.runs.stream(
                thread_id, "pm_agent", input={"messages": [{"role": "user", "content": text}]},
                config=config, stream_mode=["updates"], multitask_strategy="enqueue",
            ):
                yield "data: " + json.dumps({"event": event.event, "data": TrajectoryRecorder.clean_public(event.data)}, ensure_ascii=False, default=str) + "\n\n"
        except Exception as exc:
            # Only expose explicitly controlled configuration messages. Other
            # exceptions may contain request data or credentials.
            detail = str(exc)
            message = detail if detail in _MODEL_CONFIGURATION_ERRORS else "运行失败，请查看服务日志"
            yield "data: " + json.dumps({"event": "error", "data": {"message": message}}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def _trace_project(request):
    project_id = request.query_params.get("project_id", "")
    if not project_id or await ProjectStore(await get_store()).load(project_id) is None:
        raise ValueError("项目不存在")
    return project_id


async def trajectory(request):
    """Embed the existing trace viewer using project-scoped local endpoints."""
    if not _local(request):
        return _forbidden()
    try:
        project_id = await _trace_project(request)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    html = await asyncio.to_thread((_ROOT.parent / "middlewares" / "trajectory" / "dashboard.html").read_text, encoding="utf-8")
    query = "?project_id=" + quote(project_id, safe="")
    html = html.replace('"/api/events?after="', json.dumps("/workbench/api/trace/events" + query + "&after="))
    html = html.replace('"/api/status"', json.dumps("/workbench/api/trace/status" + query))
    html = html.replace('"/api/export"', json.dumps("/workbench/api/trace/export" + query))
    html = html.replace('<button id="stop" class="danger">停止运行</button>', '<button id="stop" class="danger" disabled title="当前工作台尚未接入安全的完整取消">停止运行</button>')
    html = html.replace('$("stop").disabled = !!state.stop_requested || !!isFinished();', '$("stop").disabled = true;')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def trajectory_events(request):
    """Read a bounded chunk from this project's recorded events."""
    if not _local(request):
        return _forbidden()
    try:
        project_id = await _trace_project(request)
        after = int(request.query_params.get("after", "0"))
        result = await asyncio.to_thread(read_events, trajectory_directory(project_id) / "trajectory.jsonl", after)
        return JSONResponse(result)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def trajectory_status(request):
    """Show the latest official run status, not a fabricated completion state."""
    if not _local(request):
        return _forbidden()
    try:
        project_id = await _trace_project(request)
        item = (await get_store()).aget(_THREADS, project_id)
        runs = await get_client(url=None).runs.list(item.value["thread_id"], limit=1) if item else []
        return JSONResponse({"run": {"status": runs[0]["status"]} if runs else None, "first_error": None, "stop_requested": False})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def trajectory_export(request):
    """Export only this project's already-redacted trace file."""
    if not _local(request):
        return _forbidden()
    try:
        project_id = await _trace_project(request)
        path = trajectory_directory(project_id) / "trajectory.jsonl"
        if not path.exists():
            return Response(b"", media_type="application/x-ndjson")
        return FileResponse(path, media_type="application/x-ndjson")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def memory_recovery(request):
    """Inspect commits and reconcile only a provably untouched attempt."""
    if not _local(request):
        return _forbidden()
    try:
        project_id = request.query_params.get("project_id", "")
        store = await get_store()
        if not project_id or await ProjectStore(store).load(project_id) is None:
            raise ValueError("项目不存在")
        from src.middlewares.memory.pm_agent_memory.jobs import PMMemory
        rollback = request.method == "POST" and request.query_params.get("action") == "rollback"
        data = await request.json() if rollback else {}
        journal_digest = data.get("journal_digest") if isinstance(data, dict) else None
        if rollback and (not isinstance(journal_digest, str) or len(journal_digest) != 64):
            raise ValueError("请先核对待处理的记忆写回记录")
        if rollback and not await asyncio.to_thread(
            _confirm_local, "恢复记忆写回", "将未完成的本次记忆写回恢复到提交前状态。确认继续？"
        ):
            return JSONResponse({"status": "cancelled", "files": []})
        result = await PMMemory(store).inspect_commit(project_id, reconcile=request.method == "POST" and not rollback,
                                                      rollback=rollback, expected_journal=journal_digest)
        return JSONResponse(result)
    except (ValueError, RuntimeError, TypeError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


routes = [
    Route("/workbench", index),
    Route("/workbench/", index),
    Route("/workbench/assets/app.js", javascript),
    Route("/workbench/api/projects", projects, methods=["GET"]),
    Route("/workbench/api/projects", create_project, methods=["POST"]),
    Route("/workbench/api/projects/bind", bind_project, methods=["POST"]),
    Route("/workbench/api/select-folder", select_folder, methods=["POST"]),
    Route("/workbench/api/providers", providers, methods=["GET", "POST"]),
    Route("/workbench/api/providers/discover", discover, methods=["POST"]),
    Route("/workbench/api/default-model", default_model, methods=["GET", "POST"]),
    Route("/workbench/api/clef", clef_settings, methods=["GET", "POST"]),
    Route("/workbench/api/worker-settings", worker_settings, methods=["GET", "POST"]),
    Route("/workbench/api/worker-modes", worker_modes, methods=["GET", "POST"]),
    Route("/workbench/api/workspace-settings", workspace_settings, methods=["GET", "POST"]),
    Route("/workbench/api/runtime-settings", runtime_settings, methods=["GET", "POST"]),
    Route("/workbench/api/history", history, methods=["GET"]),
    Route("/workbench/api/dispatch-status", dispatch_status, methods=["GET"]),
    Route("/workbench/api/project-status", project_status, methods=["GET"]),
    Route("/workbench/api/memory-recovery", memory_recovery, methods=["GET", "POST"]),
    Route("/workbench/api/stop-unconfirmed-worker", stop_unconfirmed_worker, methods=["POST"]),
    Route("/workbench/api/human-validation", human_validation, methods=["POST"]),
    Route("/workbench/api/human-approval", human_approval, methods=["POST"]),
    Route("/workbench/api/worker-usage", worker_usage, methods=["GET"]),
    Route("/workbench/api/reconcile-dispatch", reconcile_dispatch, methods=["POST"]),
    Route("/workbench/api/attach-reconciled-dispatch", attach_reconciled_dispatch, methods=["POST"]),
    Route("/workbench/api/chat", chat, methods=["POST"]),
    Route("/workbench/api/pending-decision", pending_decision, methods=["GET"]),
    Route("/workbench/api/submit-decision", submit_decision, methods=["POST"]),
    Route("/workbench/trajectory", trajectory),
    Route("/workbench/api/trace/events", trajectory_events),
    Route("/workbench/api/trace/status", trajectory_status),
    Route("/workbench/api/trace/export", trajectory_export),
]
