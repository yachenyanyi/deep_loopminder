"""Display and resume PM human decisions on the official thread."""

import asyncio
import json

import httpx
from langgraph_sdk import get_client
from starlette.responses import JSONResponse

from src.middlewares.project_state.persistence import ProjectStore

from .context import forbidden as _forbidden
from .context import get_store
from .context import local as _local
from .threads import project_thread


class CheckpointLostError(Exception):
    """The PM thread exists but its checkpoints are gone, so the run cannot resume."""


_CHECKPOINT_LOST_MESSAGE = "当前服务无法读取该 PM 会话或其检查点，无法恢复原 Run；请核对服务和会话记录。"


def _decisions_fingerprint(actions):
    """Hash the answerable decisions exactly as they were shown to the user.

    submit-decision accepts the fingerprint the page displayed; a mismatch
    means the pending set changed between render and submit (answered
    elsewhere, a new run replaced the interrupt, ...). Deciding on a stale
    view would send answers to the wrong questions, so the submit is refused
    with an explicit message instead.
    """
    import hashlib

    payload = json.dumps(actions, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _thread_interrupts(client, thread_id):
    """Read the pending human-in-the-loop interrupt from the official thread state.

    The batched HumanInTheLoopMiddleware raises one interrupt whose value is
    {action_requests, review_configs}; the official thread-state schema exposes
    it at tasks[].interrupts[] (falling back to the top-level interrupts[]).
    Interrupts of any other shape are counted but not interpreted: they are
    reported as pending yet not answerable, never guessed into decisions.

    Raises CheckpointLostError when the thread's checkpoints are gone. After a
    dev-server restart the state endpoint returns 200 with an empty snapshot
    (checkpoint: null) rather than 404, so both shapes must be recognized or a
    lost session would be reported as merely "no pending decision".

    The third return value binds the interrupt to its occurrence:
    {"interrupt_id", "checkpoint_id"}. Note the official interrupt id is a
    slot identity derived from the task namespace (langgraph types.py:
    _interrupt_id), so the same node position re-asking yields the same id —
    the checkpoint_id is what distinguishes one occurrence from the next.
    """
    async with asyncio.timeout(15):
        state = await client.threads.get_state(thread_id)
    checkpoint = state.get("checkpoint")
    if checkpoint is None and not (state.get("values") or {}):
        raise CheckpointLostError()
    interrupts = []
    for task in state.get("tasks") or []:
        if isinstance(task, dict):
            interrupts.extend(item for item in (task.get("interrupts") or []) if isinstance(item, dict))
    if not interrupts:
        interrupts = [item for item in (state.get("interrupts") or []) if isinstance(item, dict)]
    actions = []
    for item in interrupts:
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        requests, configs = value.get("action_requests"), value.get("review_configs")
        if not isinstance(requests, list) or not isinstance(configs, list):
            continue
        by_name = {c.get("action_name"): c for c in configs if isinstance(c, dict)}
        for req in requests:
            if not isinstance(req, dict):
                continue
            config = by_name.get(req.get("name")) or {}
            allowed = config.get("allowed_decisions")
            actions.append({
                "name": req.get("name"),
                "args": req.get("args") if isinstance(req.get("args"), dict) else {},
                "description": req.get("description") if isinstance(req.get("description"), str) else "",
                "allowed_decisions": list(allowed) if isinstance(allowed, list) else [],
            })
    # Bind to the occurrence: the interrupt's official id plus the checkpoint
    # it is parked on. Older SDK shapes may omit the id; then only the
    # checkpoint anchors the binding and the report must say so.
    interrupt_ids = [item.get("id") for item in interrupts if isinstance(item.get("id"), str) and item.get("id")]
    checkpoint_id = (checkpoint or {}).get("checkpoint_id") if isinstance(checkpoint, dict) else None
    occurrence = {"interrupt_id": interrupt_ids[0] if interrupt_ids else None,
                  "checkpoint_id": checkpoint_id}
    return actions, len(interrupts), occurrence


async def pending_decision(request):
    """Show the human decision this project's PM thread is officially waiting on."""
    if not _local(request):
        return _forbidden()
    project_id = request.query_params.get("project_id", "")
    store = await get_store()
    if not project_id or await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    client = get_client(url=None)
    try:
        thread_id = await project_thread(store, client, project_id, create_if_missing=False)
        actions, interrupt_count, occurrence = await _thread_interrupts(client, thread_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except CheckpointLostError:
        return JSONResponse({"error": _CHECKPOINT_LOST_MESSAGE}, status_code=409)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse({"error": _CHECKPOINT_LOST_MESSAGE}, status_code=409)
        return JSONResponse({"error": "无法读取会话状态，请检查官方服务。"}, status_code=503)
    except (httpx.HTTPError, TimeoutError):
        return JSONResponse({"error": "无法读取会话状态，请检查官方服务。"}, status_code=503)
    return JSONResponse({
        "pending": interrupt_count > 0,
        "answerable": bool(actions),
        "decisions": actions,
        "fingerprint": _decisions_fingerprint(actions) if actions else None,
        "interrupt_id": occurrence["interrupt_id"] if actions else None,
        "checkpoint_id": occurrence["checkpoint_id"] if actions else None,
        "binding_available": bool(actions and occurrence["checkpoint_id"]),
    })


async def submit_decision(request):
    """Resume the interrupted PM run with the human's answers via official Command(resume).

    Resume is a separate endpoint from chat and answers the exact interrupt read
    back from the thread state. When no interrupt is pending — including after a
    dev-server restart wiped the checkpoints — the request is refused rather
    than treated as, or turned into, a resume of a run that no longer exists.
    """
    if not _local(request):
        return _forbidden()
    try:
        data = await request.json()
        project_id, answers = data.get("project_id"), data.get("answers")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("请选择项目")
        if not isinstance(answers, list) or not answers or not all(
            isinstance(answer, str) and answer.strip() for answer in answers
        ):
            raise ValueError("请提供每个待决策问题的文字答复")
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    store = await get_store()
    if await ProjectStore(store).load(project_id) is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    client = get_client(url=None)
    # No outer project_write_lock here: project_thread already takes it and
    # the asyncio.Lock is not reentrant. The check-then-resume gap is closed by
    # the official server itself — a resume without a pending interrupt is
    # rejected there and surfaced as an error, never silently lost.
    try:
        thread_id = await project_thread(store, client, project_id, create_if_missing=False)
        actions, interrupt_count, occurrence = await _thread_interrupts(client, thread_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except CheckpointLostError:
        return JSONResponse({"error": _CHECKPOINT_LOST_MESSAGE}, status_code=409)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse({"error": _CHECKPOINT_LOST_MESSAGE}, status_code=409)
        return JSONResponse({"error": "无法读取会话状态，请检查官方服务。"}, status_code=503)
    except (httpx.HTTPError, TimeoutError):
        return JSONResponse({"error": "无法读取会话状态，请检查官方服务。"}, status_code=503)
    if not actions:
        if interrupt_count:
            return JSONResponse(
                {"error": "存在待处理的中断，但不是可在本界面答复的决策形式。"}, status_code=409
            )
        return JSONResponse(
            {"error": "当前没有待处理的人工决策；普通消息请通过对话发送，不会被当作决策恢复。"},
            status_code=409,
        )
    if len(answers) != len(actions):
        return JSONResponse(
            {"error": f"当前有 {len(actions)} 项待决策，需要 {len(actions)} 项答复。"}, status_code=400
        )
    fingerprint = data.get("fingerprint")
    current = _decisions_fingerprint(actions)
    if fingerprint is None or not isinstance(fingerprint, str) or not fingerprint:
        return JSONResponse(
            {"error": "提交必须绑定页面展示的待决策：请重新打开项目获取最新决策后提交。"}, status_code=400
        )
    if fingerprint != current:
        return JSONResponse(
            {"error": "待决策内容与页面展示的不一致（可能已被处理或发生变化）；请重新打开项目查看最新决策再提交。"},
            status_code=409,
        )
    # Content alone cannot tell one occurrence from another: the official
    # interrupt id is a slot identity that repeats across occurrences, so the
    # checkpoint it is parked on is part of the binding. An answer shown for
    # one occurrence must be refused when the thread now holds a different one.
    shown_interrupt = data.get("interrupt_id")
    shown_checkpoint = data.get("checkpoint_id")
    if (shown_interrupt or None) != (occurrence["interrupt_id"] or None) or (
        (shown_checkpoint or None) != (occurrence["checkpoint_id"] or None)
    ):
        return JSONResponse(
            {"error": "该答复所属的中断或检查点已变化（问题可能已被重新提出）；请重新打开项目查看最新决策再提交。"},
            status_code=409,
        )
    if occurrence["checkpoint_id"] is None:
        return JSONResponse(
            {"error": "官方服务未提供可核对的中断或检查点标识，无法确认答复针对当前中断；请重新打开项目后重试。"},
            status_code=409,
        )
    for action in actions:
        if action["allowed_decisions"] and "respond" not in action["allowed_decisions"]:
            return JSONResponse(
                {"error": f"决策 {action['name']} 不接受文字答复（允许：{'/'.join(action['allowed_decisions'])}）。"},
                status_code=400,
            )
    command = {"resume": {"decisions": [
        {"type": "respond", "message": answer.strip()} for answer in answers
    ]}}
    # Same project binding and trace flags as chat, so the resumed run stays
    # isolated to this project and keeps recording its trajectory. Model
    # options live inside "configurable", exactly where chat puts them.
    configurable = {
        "project_id": project_id, "project_state_single_writer": True, "workbench_trace": True,
    }
    if data.get("model_use_default") is True:
        configurable["model_use_default"] = True
    if data.get("model_use_legacy") is True:
        configurable["model_use_legacy"] = True
    for key in ("model_provider", "model_name", "reasoning_effort"):
        if data.get(key):
            configurable[key] = data[key]
    config = {"configurable": configurable}
    try:
        run = await client.runs.create(
            thread_id, "pm_agent", command=command, config=config, multitask_strategy="enqueue",
        )
    except (httpx.HTTPError, TimeoutError):
        return JSONResponse({"error": "无法启动恢复运行，请检查官方服务。"}, status_code=503)
    return JSONResponse({
        "resumed": True,
        "run_id": run.get("run_id") if isinstance(run, dict) else None,
        "thread_id": thread_id,
    })


