"""实际测试记忆查询、官方后台交接、压缩和自动整理。"""

# 此文件同时提供独立测试图工厂与命令测试入口，测试进度写入证据文件。
# ruff: noqa: E402, T201

import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dotenv import load_dotenv
from langchain.agents import create_agent
from langgraph_sdk import get_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemory,
    PMMemorySummarizationMiddleware,
    get_pm_memory_store,
)
from src.middlewares.memory.pm_agent_memory import graph as memory_graph
from src.middlewares.memory.pm_agent_memory.files import (
    document_paths,
    latest_handoff,
    memory_context,
    read_document,
)
from src.middlewares.memory.pm_agent_memory.middleware import memory_search
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    ProjectStore,
)

EVIDENCE = Path("docs/progress/evidence/pm-memory-lifecycle-20261014.json")
MARKER = "PM_LIFECYCLE_20261014"
AUTO_MARKER = "AUTO_PM_20261014"


def test_model():
    """复用生产默认模型，不单独覆盖测试模型参数。"""
    from src.models.llm import get_default_model

    return get_default_model()


async def memory_agent():
    """直接复用生产记忆图与模型配置。"""
    return await memory_graph.pm_memory_agent()


async def pm_agent_probe():
    """测试图复用实际 PM 记忆钩子，以较低阈值触发官方压缩。"""
    store = (await get_pm_memory_store()).store
    jobs = PMMemory(store, test_model())
    return create_agent(
        test_model(), system_prompt="用中文简短回答当前任务的下一步，依据项目状态和最新交接。"
        "测试对话不要主动提交额外记忆任务；由中间件自动管理。",
        middleware=[
            ProjectStateMiddleware(), PMAgentMemoryMiddleware(jobs, context_token_limit=30000),
            PMMemorySummarizationMiddleware(jobs, test_model(), token_threshold=1200, keep_messages=2),
        ], store=store,
    )


async def main():
    """调用真实工具和官方服务，每完成一项就保存可复核结果。"""
    provider = await get_pm_memory_store()
    if provider.durability != "durable":
        raise RuntimeError("PostgreSQL is required")
    client = get_client(url="http://127.0.0.1:2024", timeout=600)
    jobs = PMMemory(provider.store, test_model(), client=client)
    previous = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else None
    evidence = {"previous_attempt": previous, "created_at": datetime.now(UTC).isoformat(), "model": "minimax/minimax-m3.1-flash:free",
                "store": provider.backend, "steps": {}, "status": "running"}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)

    def save(name, data):
        evidence["steps"][name] = data
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, json.dumps(data, ensure_ascii=False)[:900], flush=True)

    async def run(thread_id, graph_id, input_value, project_id):
        created = await client.runs.create(thread_id, graph_id, input=input_value,
            config={"configurable": {"project_id": project_id}}, multitask_strategy="enqueue")
        await asyncio.wait_for(client.runs.join(thread_id, created["run_id"]), timeout=600)
        finished = await client.runs.get(thread_id, created["run_id"])
        if finished["status"] != "success":
            raise RuntimeError(f"Official run failed: {finished['run_id']} {finished['status']}")
        state = await client.threads.get_state(thread_id)
        return created["run_id"], state["values"]

    try:
        # 使用此前真实模型已写入的数据，避免再次生成同一普通记忆。
        if previous and all(previous.get("steps", {}).get("lookup", {}).get("checks", {}).values()) and previous.get("steps", {}).get("lookup"):
            save("lookup", {**previous["steps"]["lookup"], "reused_verified_result": True})
        else:
            prior = json.loads(Path("docs/progress/evidence/pm-memory-live-20261014-guided.json").read_text(encoding="utf-8"))
            query_project = prior["project_id"]
            middleware = PMAgentMemoryMiddleware(jobs)
            tools = {tool.name: tool for tool in middleware.tools}
            runtime = SimpleNamespace(config={"configurable": {"project_id": query_project, "thread_id": "lookup"}},
                                      store=provider.store, state={"messages": []})
            result = await memory_search.coroutine(query="PMMEM_20261014", runtime=runtime)
            doc = json.loads(await tools["pm_read_memory"].coroutine(path=prior["steps"][-1]["destination"], runtime=runtime))
            compact = re.sub(r"\s+", "", doc["content"])
            source_id = prior["steps"][-1]["source_id"]
            first = json.loads(await tools["pm_read_bookmark"].coroutine(job_id=source_id, runtime=runtime, limit=400))
            second = json.loads(await tools["pm_read_bookmark"].coroutine(job_id=source_id, runtime=runtime, offset=400, limit=400))
            full = json.loads(await tools["pm_read_bookmark"].coroutine(job_id=source_id, runtime=runtime, limit=20000))
            isolation = False
            wrong = SimpleNamespace(config={"configurable": {"project_id": "wrong-project", "thread_id": "lookup"}}, store=provider.store)
            try:
                await tools["pm_read_bookmark"].coroutine(job_id=source_id, runtime=wrong)
            except KeyError:
                isolation = True
            checks = {"search_found": "PMMEM_20261014" in result, "read_revised_plan": "方案B" in compact,
                      "bookmark_has_original_input": "方案A" in full["content"],
                      "pagination_matches": first["content"] + second["content"] == full["content"][:800],
                      "project_isolation": isolation}
            save("lookup", {"checks": checks, "project_id": query_project, "source_id": source_id})
            if not all(checks.values()):
                raise RuntimeError("Lookup checks failed")

        verified_compression = previous.get("steps", {}).get("compression_handoff") if previous else None
        if verified_compression and all(verified_compression["checks"].values()):
            project_id, thread_id = previous["project_id"], previous["thread_id"]
            evidence.update(project_id=project_id, thread_id=thread_id)
            save("compression_handoff", {**verified_compression, "reused_verified_result": True})
        else:
            project_id = "pm-lifecycle-" + uuid4().hex[:12]
            thread_id = (await client.threads.create())["thread_id"]
            evidence.update(project_id=project_id, thread_id=thread_id)
            await ProjectStore(provider.store).save(ProjectSnapshot(
                project_id, f"{MARKER}：验证重启后恢复工作", ("只使用测试数据", "下一步必须验证重启读回"), (),
            ))
            filler = "测试历史讨论：先讨论实现细节，再保留真实依据，不把尝试当完成。" * 160
            messages = [
                {"role": "user", "content": f"{MARKER} 当前方案A已作废，采用方案B。下一步验证重启读回。" + filler},
                {"role": "assistant", "content": "已理解：方案B是当前方案，目标是验证恢复。"},
                {"role": "user", "content": filler},
                {"role": "assistant", "content": "整理历史依据后继续验证。"},
                {"role": "user", "content": f"{MARKER} 请简短说明当前方案和下一步。不要调用工具。"},
            ]
            save("compression_started", {"input_messages": len(messages), "project_id": project_id})
            run_id, values = await run(thread_id, "pm_lifecycle_probe", {"messages": messages}, project_id)
            output_messages = values["messages"]
            handoff = await latest_handoff(provider.store, project_id, thread_id)
            body = await read_document(provider.store, project_id, handoff) if handoff else ""
            summaries = [m for m in output_messages if m.get("additional_kwargs", {}).get("lc_source") == "summarization"]
            answer = output_messages[-1].get("content", "")
            if not isinstance(answer, str):
                answer = json.dumps(answer, ensure_ascii=False)
            orientation = await memory_context(provider.store, project_id, thread_id)
            checks = {"handoff_readable": bool(body.strip()), "handoff_has_marker": MARKER in body,
                      "handoff_has_next_step": "重启" in body,
                      "official_summary_present": bool(summaries),
                      "messages_reduced": len(output_messages) < len(messages) + 1,
                      "handoff_in_next_context": handoff is not None and handoff in orientation,
                      "answer_retains_next_step": "重启" in answer}
            Path("docs/progress/evidence/pm-memory-lifecycle-20261014-handoff.md").write_text(body, encoding="utf-8")
            save("compression_handoff", {"run_id": run_id, "checks": checks, "handoff": handoff,
                 "output_messages": len(output_messages), "answer": answer, "summary": summaries})
            if not all(checks.values()):
                raise RuntimeError("Compression/handoff checks failed")

        # 新一轮显式事件由真实 after_agent 钩子自动提交，不手动调用 submit。
        event = {"id": uuid4().hex, "kind": "milestone_completed", "observation":
                 f"{AUTO_MARKER}：测试里已完成重启读回验证，请保留此事件的原始依据。"}
        run_id, _ = await run(thread_id, "pm_lifecycle_probe", {
            "messages": [{"role": "user", "content": event["observation"] + " 简短回复即可，不调用工具。"}],
            "pm_memory_events": [event],
        }, project_id)
        sources = await provider.store.asearch(jobs.namespace(project_id),
            filter={"purpose": "Review PM event: milestone_completed"}, limit=20)
        if not sources:
            raise RuntimeError("Automatic event did not submit a source")
        automatic = sources[-1].value
        finished = await asyncio.wait_for(jobs.wait(project_id, automatic["id"]), timeout=600)
        daily = await read_document(provider.store, project_id, automatic["destination"])
        save("automatic_event", {"pm_run_id": run_id, "memory_run_id": finished["run_id"],
             "source_id": automatic["id"], "checks": {"official_run_success": finished["status"] == "success",
             "event_recorded": AUTO_MARKER in daily}})
        if AUTO_MARKER not in daily:
            raise RuntimeError("Automatic event not recorded")

        # 无 source_id 调用当前记忆图，测试与定期调度共用的综合整理入口。
        run_id, state = await run(jobs.thread(project_id), "pm_memory_agent", {"project_id": project_id}, project_id)
        sources = await provider.store.asearch(jobs.namespace(project_id), filter={"purpose": "Consolidate project cognition"}, limit=20)
        source = sources[-1].value if sources else {}
        consolidated = await read_document(provider.store, project_id, source["destination"]) if source else ""
        Path("docs/progress/evidence/pm-memory-lifecycle-20261014-consolidated.md").write_text(consolidated, encoding="utf-8")
        save("consolidation", {"run_id": run_id, "outcome": state.get("outcome"),
             "origin": state.get("outcome_origin"), "checks": {"consolidation_flag": source.get("consolidation") is True,
             "document_readable": bool(consolidated.strip())}, "document_map": await document_paths(provider.store, project_id)})
        if source.get("consolidation") is not True or not consolidated.strip():
            raise RuntimeError("Consolidation entry checks failed")

        cron = None
        try:
            before_runs = await client.runs.list(jobs.thread(project_id), limit=30)
            before_ids = {row["run_id"] for row in before_runs}
            cron = await client.crons.create_for_thread(jobs.thread(project_id), "pm_memory_agent",
                schedule="* * * * *", input={"project_id": project_id},
                config={"configurable": {"project_id": project_id}})
            save("cron_api", {"supported": True, "cron_id": cron["cron_id"], "execution_verified": False})
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                runs = await client.runs.list(jobs.thread(project_id), limit=30)
                fresh = [row for row in runs if row["run_id"] not in before_ids]
                if fresh:
                    scheduled = fresh[0]
                    await asyncio.wait_for(client.runs.join(jobs.thread(project_id), scheduled["run_id"]), timeout=600)
                    finished = await client.runs.get(jobs.thread(project_id), scheduled["run_id"])
                    save("cron_api", {"supported": True, "cron_id": cron["cron_id"],
                        "scheduled_run_id": scheduled["run_id"], "status": finished["status"],
                        "execution_verified": finished["status"] == "success"})
                    break
                await asyncio.sleep(5)
            else:
                save("cron_api", {"supported": True, "cron_id": cron["cron_id"],
                                  "execution_verified": False, "error": "Scheduled run not observed within 150s"})
        except Exception as exc:
            save("cron_api", {"supported": False, "error_type": type(exc).__name__,
                              "error": str(exc)[:400], "execution_verified": False})
        finally:
            if cron:
                await client.crons.delete(cron["cron_id"])
                evidence["steps"]["cron_api"]["deleted"] = True
        evidence["status"] = ("passed" if evidence["steps"]["cron_api"].get("execution_verified")
                              else "passed_except_unverified_cron_execution")
        save("completed", {"status": evidence["status"]})
    except Exception as exc:
        evidence["status"] = "failed"
        save("failure", {"type": type(exc).__name__, "error": str(exc)[:1200]})
        raise SystemExit(1) from None
    finally:
        from src.deep_agents.db import cleanup_postgres
        await cleanup_postgres()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
