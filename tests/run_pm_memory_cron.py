"""只验证整理凭据和真实官方 Cron；不重跑压缩、交接和查询。"""

# ruff: noqa: E402, T201
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph_sdk import get_client

from src.middlewares.memory.pm_agent_memory import PMMemory, get_pm_memory_store
from src.middlewares.memory.pm_agent_memory.files import memory_files
from src.middlewares.project_state import ProjectSnapshot, ProjectStore

load_dotenv()
EVIDENCE = Path("docs/progress/evidence/pm-memory-cron-20261015.json")


async def memory_agent():
    """复用免费模型测试工厂，额外保留协议失败的实际响应用于诊断。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from langchain_core.callbacks import BaseCallbackHandler
    from run_pm_memory_lifecycle import memory_agent as original_factory
    from run_pm_memory_lifecycle import test_model

    class Trace(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            for generations in response.generations:
                for generation in generations:
                    message = generation.message
                    with Path(".local-acp/pm-cron-model-trace.jsonl").open(
                        "a", encoding="utf-8"
                    ) as output:
                        output.write(
                            json.dumps(
                                {
                                    "content": message.content,
                                    "tool_calls": message.tool_calls,
                                    "metadata": message.response_metadata,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )

    test_model().callbacks = [Trace()]
    return await original_factory()


async def main():
    provider = await get_pm_memory_store()
    if provider.durability != "durable":
        raise RuntimeError("必须使用真实 PostgreSQL 测试库")
    client = get_client(url="http://127.0.0.1:2024", timeout=600)
    memory = PMMemory(provider.store, client=client)
    project = "pm-cron-fix-" + uuid4().hex[:12]
    thread = memory.thread(project)
    previous = (
        json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else None
    )
    evidence = {
        "previous_attempt": previous,
        "project_id": project,
        "thread_id": thread,
        "model": "minimax/minimax-m3.1-flash:free",
        "store": provider.backend,
        "fixture": "人工构造测试输入，模型负责真实整理",
        "status": "running",
    }
    cron = None

    def save():
        EVIDENCE.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(evidence, ensure_ascii=False), flush=True)

    async def state_after(run):
        await asyncio.wait_for(client.runs.join(thread, run["run_id"]), timeout=600)
        finished = await client.runs.get(thread, run["run_id"])
        if finished["status"] != "success":
            raise RuntimeError("官方整理 Run 失败：" + finished["status"])
        return (await client.threads.get_state(thread))["values"]

    try:
        await ProjectStore(provider.store).save(
            ProjectSnapshot(
                project,
                "验证定时整理不重复处理相同资料",
                ("仅测试资料",),
                (),
            )
        )
        await memory.observe(
            project,
            "pm",
            [
                HumanMessage(
                    content="PM_CRON_FIX_20261015：已确认记忆整理使用官方 Run 排队；没有新资料时跳过重复工作。"
                )
            ],
        )
        source = await memory.capture(
            project,
            "pm",
            [],
            purpose="整理测试的固定依据",
            observation="官方 Run 排队；相同资料跳过重复工作。",
        )
        await memory_files(provider.store, project).awrite(
            "/daily/fixture.md",
            "# 待整理候选\n\nPM_CRON_FIX_20261015：官方 Run 排队；相同资料跳过重复工作。\n来源："
            + source["bookmark"],
        )
        run = await client.runs.create(
            thread,
            "pm_memory_agent",
            input={"project_id": project},
            config={"configurable": {"project_id": project}},
            if_not_exists="create",
            multitask_strategy="enqueue",
        )
        evidence["consolidation_run_id"] = run["run_id"]
        save()
        state = await state_after(run)
        namespace = ("deep_loopminder", "pm_memory_consolidations", project)
        receipt = await provider.store.aget(namespace, "latest")
        if not receipt:
            raise RuntimeError("成功整理后没有输入凭据")
        evidence["consolidation"] = {
            "outcome": state.get("outcome"),
            "origin": state.get("outcome_origin"),
            "receipt_source_id": receipt.value["source_id"],
        }
        save()
        before = {r["run_id"] for r in await client.runs.list(thread, limit=30)}
        sources_before = {
            r.key
            for r in await provider.store.asearch(memory.namespace(project), limit=100)
        }
        cron = await client.crons.create_for_thread(
            thread,
            "pm_memory_agent",
            schedule="* * * * *",
            input={"project_id": project},
            config={"configurable": {"project_id": project}},
        )
        evidence["cron_id"] = cron["cron_id"]
        save()
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            fresh = [
                r
                for r in await client.runs.list(thread, limit=30)
                if r["run_id"] not in before
            ]
            if fresh:
                # 观察到首次触发就删除 Cron，避免等待模型期间再次排队。
                await client.crons.delete(cron["cron_id"])
                cron = None
                evidence["cron_deleted"] = True
                scheduled = fresh[0]
                evidence["scheduled_run_id"] = scheduled["run_id"]
                state = await state_after(scheduled)
                after = await provider.store.aget(namespace, "latest")
                sources_after = {
                    r.key
                    for r in await provider.store.asearch(
                        memory.namespace(project), limit=100
                    )
                }
                evidence["checks"] = {
                    "official_scheduled_success": True,
                    "verified_noop": state.get("outcome") == "ignore"
                    and state.get("outcome_origin") == "unchanged_inputs",
                    "receipt_unchanged": after.value == receipt.value,
                    "no_extra_source": sources_before == sources_after,
                }
                if not all(evidence["checks"].values()):
                    raise RuntimeError("定时整理校验未通过")
                evidence["status"] = "passed"
                save()
                break
            await asyncio.sleep(3)
        else:
            raise TimeoutError("150秒内没有观察到定时 Run")
    except Exception as exc:
        evidence.update(
            status="failed", error_type=type(exc).__name__, error=str(exc)[:300]
        )
        save()
        raise SystemExit(1) from None
    finally:
        if cron:
            await client.crons.delete(cron["cron_id"])
            evidence["cron_deleted"] = True
            save()
        from src.deep_agents.db import cleanup_postgres

        await cleanup_postgres()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
