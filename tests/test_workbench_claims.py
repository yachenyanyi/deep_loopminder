"""宿主触发的领取：Worker Run 结束且 PM 空闲时唤醒 PM 领取结果。

官方唤醒只覆盖 deep-agent 子代理；acp_worker 是普通 StateGraph，空闲 PM 的
领取由 workbench.claims 观察循环补位。这里先用真实官方
AsyncSubagentsMiddleware 验证忙时通知对普通子代理同样投递（官方文档承诺
"忙时在下一次模型调用前送达"），再用假 SDK 客户端验证观察循环的触发与
克制规则。
"""

import asyncio

import pytest
from langchain_core.messages import get_notification_metadata

from src.workbench.claims import ClaimWatcher, _claim_message, _outcome, _poll_seconds


class FakeRuns:
    """runs.get 按预设状态回答；runs.create 记录调用。"""

    def __init__(self, statuses):
        self.statuses = statuses
        self.created = []

    async def get(self, thread_id, run_id):
        if (thread_id, run_id) not in self.statuses:
            raise RuntimeError("missing run")
        return {"status": self.statuses[(thread_id, run_id)]}

    async def create(self, thread_id, assistant_id, *, input=None, config=None, multitask_strategy=None):
        self.created.append({
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "config": config,
            "multitask_strategy": multitask_strategy,
        })
        return {"run_id": "claim-run-1"}


class FakeThreads:
    """threads.get / get_state 按 PM 线程预设回答；未知线程一律 404。"""

    def __init__(self, statuses, states):
        self.statuses = statuses
        self.states = states

    async def get(self, thread_id):
        if thread_id not in self.statuses:
            raise RuntimeError("404")
        return {"thread_id": thread_id, "status": self.statuses[thread_id]}

    async def get_state(self, thread_id):
        if thread_id not in self.states:
            raise RuntimeError("404")
        return {"values": self.states[thread_id]}


class FakeClient:
    def __init__(self, runs, threads):
        self.runs = runs
        self.threads = threads


class FakeBinding:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class FakeStore:
    def __init__(self, bindings):
        self.bindings = bindings

    async def asearch(self, namespace, *, limit=100, offset=0):
        return self.bindings[offset:offset + limit]


def _task(status="running", run_id="run-1"):
    return {
        "task_id": "worker-thread-1",
        "agent_name": "developer",
        "thread_id": "worker-thread-1",
        "run_id": run_id,
        "status": status,
    }


def _values(delivered=False):
    return {
        "async_tasks": {"worker-thread-1": _task()},
        "acp_delivered": {"run-1": True} if delivered else {},
        "acp_task_names": {"worker-thread-1": "inspect"},
    }


def _watcher(store, pm_status="idle"):
    runs = FakeRuns({("worker-thread-1", "run-1"): "success"})
    client = FakeClient(runs, FakeThreads(
        {"pm-thread-1": pm_status, "worker-thread-1": "idle"}, {"pm-thread-1": _values()},
    ))
    return ClaimWatcher(store=store), client


def test_official_busy_pull_delivers_notification_for_plain_subagent():
    """真实官方中间件：普通子代理任务从 running 变 success 时，忙时拉取注入官方通知。"""
    from deepagents.middleware.async_subagents import AsyncSubAgentMiddleware

    middleware = AsyncSubAgentMiddleware(
        async_subagents=[{"name": "developer", "description": "开发", "graph_id": "acp_worker"}]
    )

    class StubSDK:
        async def runs_get(self, thread_id, run_id):
            return {"run_id": run_id, "status": "success", "created_at": "2026-11-14T00:00:00Z"}

        async def threads_get(self, thread_id):
            return {"thread_id": thread_id, "status": "idle"}

    class StubClients:
        def get_async(self, agent_name):
            return _StubClient()

    class _StubClient:
        class runs:
            @staticmethod
            async def get(thread_id, run_id):
                return await StubSDK().runs_get(thread_id, run_id)

        class threads:
            @staticmethod
            async def get(thread_id):
                return await StubSDK().threads_get(thread_id)

    middleware._clients = StubClients()
    task = _task(status="running")

    async def scenario():
        return await middleware._apull({"worker-thread-1": task})

    update = asyncio.run(scenario())
    messages = update.get("messages") or []
    assert len(messages) == 1
    message = messages[0]
    if isinstance(message, dict):
        extra = message["additional_kwargs"]
        source, event = extra.get("lc_source"), extra.get("lc_data")
    else:
        metadata = get_notification_metadata(message)
        source, event = metadata["source"], metadata["data"]
    assert source == "async_subagent"
    assert event["task_id"] == "worker-thread-1"
    assert event["agent_name"] == "developer"
    assert event["status"] == "success"
    assert update["async_tasks"]["worker-thread-1"]["status"] == "success"


def test_idle_pm_with_finished_unclaimed_run_wakes_pm():
    """PM 空闲 + Run 终态 + 未领取：创建携带项目 configurable 的领取 Run。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    watcher, client = _watcher(store)

    woken = asyncio.run(watcher.poll_once(client))

    assert woken == [("pm-thread-1", "run-1")]
    assert len(client.runs.created) == 1
    call = client.runs.created[0]
    assert call["thread_id"] == "pm-thread-1"
    assert call["assistant_id"] == "pm_agent"
    assert call["multitask_strategy"] == "enqueue"
    assert call["config"]["configurable"] == {
        "project_id": "demo",
        "project_state_single_writer": True,
        "workbench_trace": True,
    }
    content = call["input"]["messages"][0]["content"]
    assert "[宿主通知]" in content and "inspect" in content and "run-1" in content
    assert "wait_agent" in content and "success" in content


@pytest.mark.parametrize("pm_status", ["busy", "interrupted"])
def test_busy_or_interrupted_pm_thread_is_not_disturbed(pm_status):
    """官方同一条规则：忙碌或等待人工决策的父会话不被新 Run 惊扰。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    watcher, client = _watcher(store, pm_status=pm_status)

    assert asyncio.run(watcher.poll_once(client)) == []
    assert client.runs.created == []


def test_claimed_running_waiting_and_busy_tasks_are_skipped():
    """已领取、仍在运行、等待输入、线程上有新工作的任务都不触发。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    runs = FakeRuns({("worker-thread-1", "run-1"): "success"})
    threads = FakeThreads(
        {"pm-thread-1": "idle", "worker-thread-1": "idle"},
        {"pm-thread-1": _values(delivered=True)},
    )
    watcher = ClaimWatcher(store=store)

    # 已领取：直接跳过，不发起 fresh 查询。
    assert asyncio.run(watcher.poll_once(FakeClient(runs, threads))) == []
    assert runs.created == []

    # 仍在运行：fresh 状态非终态。
    threads.states["pm-thread-1"] = _values()
    runs.statuses[("worker-thread-1", "run-1")] = "running"
    assert asyncio.run(watcher.poll_once(FakeClient(runs, threads))) == []
    assert runs.created == []

    # 负责人在等输入：Run 成功结束但 Worker 线程 interrupted，没有最终结果。
    threads.states["pm-thread-1"]["async_tasks"]["worker-thread-1"]["status"] = "waiting"
    runs.statuses[("worker-thread-1", "run-1")] = "success"
    threads.statuses["worker-thread-1"] = "interrupted"
    assert asyncio.run(watcher.poll_once(FakeClient(runs, threads))) == []
    assert runs.created == []

    # 线程上还有更新的 Run 在工作：同样没有可领取的最终结果。
    threads.states["pm-thread-1"]["async_tasks"]["worker-thread-1"]["status"] = "running"
    threads.statuses["worker-thread-1"] = "busy"
    assert asyncio.run(watcher.poll_once(FakeClient(runs, threads))) == []
    assert runs.created == []


def test_each_run_wakes_pm_once_per_process():
    """同一 run_id 只触发一次，即使 PM 迟迟未领取。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    watcher, client = _watcher(store)

    assert asyncio.run(watcher.poll_once(client)) == [("pm-thread-1", "run-1")]
    assert asyncio.run(watcher.poll_once(client)) == []
    assert len(client.runs.created) == 1


def test_wake_failure_stays_retryable():
    """创建领取 Run 失败不标记已触发；服务恢复后下一轮再试。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    watcher, client = _watcher(store)

    async def failing_create(*args, **kwargs):
        raise RuntimeError("server busy")

    client.runs.create = failing_create
    assert asyncio.run(watcher.poll_once(client)) == []

    runs = FakeRuns({("worker-thread-1", "run-1"): "success"})
    healthy = FakeClient(runs, FakeThreads(
        {"pm-thread-1": "idle", "worker-thread-1": "idle"}, {"pm-thread-1": _values()},
    ))
    assert asyncio.run(watcher.poll_once(healthy)) == [("pm-thread-1", "run-1")]
    assert len(runs.created) == 1


def test_unreadable_pm_thread_is_skipped():
    """开发服务重启后 PM 会话 404：跳过该绑定，不报错不创建。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "gone-thread"})])
    watcher, client = _watcher(store)

    assert asyncio.run(watcher.poll_once(client)) == []
    assert client.runs.created == []


def test_multiple_finished_runs_share_one_wake():
    """一次轮询把所有新完成的任务合并进同一条宿主通知。"""
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    values = _values()
    values["async_tasks"]["worker-thread-2"] = _task(run_id="run-2")
    values["async_tasks"]["worker-thread-2"]["thread_id"] = "worker-thread-2"
    values["acp_task_names"]["worker-thread-2"] = "review"
    runs = FakeRuns({
        ("worker-thread-1", "run-1"): "success",
        ("worker-thread-2", "run-2"): "error",
    })
    client = FakeClient(runs, FakeThreads(
        {"pm-thread-1": "idle", "worker-thread-1": "idle", "worker-thread-2": "idle"},
        {"pm-thread-1": values},
    ))
    watcher = ClaimWatcher(store=store)

    woken = asyncio.run(watcher.poll_once(client))

    assert sorted(woken) == [("pm-thread-1", "run-1"), ("pm-thread-1", "run-2")]
    assert len(runs.created) == 1
    content = runs.created[0]["input"]["messages"][0]["content"]
    assert "run-1" in content and "run-2" in content and "error" in content


def test_notification_marked_task_terminal_but_unclaimed_still_wakes_pm():
    """官方忙时通知已把缓存标成终态、wait_agent 没跑：结果不能一直无人领取。

    缓存状态只是过滤线索，不是裁决；权威门槛是 fresh runs.get 的终态。
    去重行为不变：触发一次后不再重复通知。
    """
    store = FakeStore([FakeBinding("demo", {"thread_id": "pm-thread-1"})])
    runs = FakeRuns({("worker-thread-1", "run-1"): "success"})
    client = FakeClient(runs, FakeThreads(
        {"pm-thread-1": "idle", "worker-thread-1": "idle"},
        {"pm-thread-1": _values()},
    ))
    # 官方 _apull 已把缓存状态写成 success（终态），acp_delivered 无记录。
    client.threads.states["pm-thread-1"]["async_tasks"]["worker-thread-1"]["status"] = "success"
    watcher = ClaimWatcher(store=store)

    assert asyncio.run(watcher.poll_once(client)) == [("pm-thread-1", "run-1")]
    assert len(runs.created) == 1

    # 保留既有去重：同一 run_id 不再触发第二条通知。
    assert asyncio.run(watcher.poll_once(client)) == []
    assert len(runs.created) == 1


def test_outcome_mirrors_official_task_status():
    """领取视角的状态映射与官方 _task_status 同向。"""
    assert _outcome("success", "idle") == "success"
    assert _outcome("success", "error") == "error"
    assert _outcome("success", "interrupted") is None  # 负责人在等输入
    assert _outcome("success", "busy") is None  # 线程上有更新 Run
    assert _outcome("interrupted", "idle") == "cancelled"
    assert _outcome("error", None) == "error"
    assert _outcome("running", None) is None
    assert _outcome("pending", None) is None


def test_claim_message_names_task_and_stays_a_status_notice():
    """宿主通知写明别名、run_id 和状态，并声明自己不是新业务指令。"""
    text = _claim_message(
        [("worker-thread-1", _task(), "success")], {"worker-thread-1": "inspect"}
    )
    assert text.startswith("[宿主通知]")
    assert "inspect" in text and "run-1" in text and "success" in text
    assert "wait_agent" in text and "不是新的业务指令" in text


def test_poll_seconds_rejects_invalid_configuration(monkeypatch):
    """非法轮询间隔回退 5 秒，不让配置错误杀死守护任务。"""
    monkeypatch.setenv("PM_CLAIM_POLL_SECONDS", "nonsense")
    assert _poll_seconds() == 5.0
    monkeypatch.setenv("PM_CLAIM_POLL_SECONDS", "0")
    assert _poll_seconds() == 5.0
    monkeypatch.setenv("PM_CLAIM_POLL_SECONDS", "2.5")
    assert _poll_seconds() == 2.5
