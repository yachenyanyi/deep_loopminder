"""命令实测：使用 OpenRouter 免费路由，验证 PM 记忆图的记录与修订。"""

# 命令脚本需要输出进度，并先配置项目路径再导入源码。
# ruff: noqa: E402, T201

import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from src.middlewares.memory.pm_agent_memory import PMMemory, get_pm_memory_store
from src.middlewares.memory.pm_agent_memory import graph as memory_graph
from src.middlewares.memory.pm_agent_memory.files import (
    memory_candidates,
    memory_files,
    read_document,
)
from src.middlewares.project_state import ProjectSnapshot, ProjectStore

TEST_DATE = os.getenv("PM_MEMORY_TEST_DATE", "20261013")
TEST_VARIANT = os.getenv("PM_MEMORY_TEST_VARIANT", "")
EVIDENCE = Path(f"docs/progress/evidence/pm-memory-live-{TEST_DATE}{TEST_VARIANT}.json")
DOCUMENT = EVIDENCE.with_suffix(".md")
MARKER = f"PMMEM_{TEST_DATE}"


class CommandTrace(BaseCallbackHandler):
    """只记录工具名和响应模型标识，不输出认证信息或完整请求。"""

    def __init__(self):
        self.tools = []
        self.models = []
        self.responses = []

    def on_tool_start(self, serialized, input_str, **kwargs):
        name = serialized.get("name", "unknown")
        self.tools.append(name)
        print("tool:", name, flush=True)

    def on_llm_end(self, response, **kwargs):
        for group in response.generations:
            for generation in group:
                message = generation.message
                self.responses.append({
                    "text": message.text[:800],
                    "tool_calls": [call["name"] for call in message.tool_calls],
                    "finish_reason": message.response_metadata.get("finish_reason"),
                    "usage": message.usage_metadata,
                })
                metadata = message.response_metadata
                model = metadata.get("model_name") or metadata.get("model")
                if model:
                    self.models.append(model)
                    print("response_model:", model, flush=True)


async def main():
    """在独立项目执行两个真实模型任务，并保存可复核证据。"""
    if "--read-only" in sys.argv:
        provider = await get_pm_memory_store()
        if provider.durability != "durable":
            raise RuntimeError("Cross-process checks require PostgreSQL")
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        try:
            project_id = evidence["project_id"]
            step = evidence["steps"][-1] if evidence["steps"] else {
                "source_id": evidence["failed_source_id"], "destination": evidence["failed_destination"],
            }
            source = await PMMemory(provider.store).source(project_id, step["source_id"])
            checks = {"source_persisted": source["project_id"] == project_id}
            try:
                body = await read_document(provider.store, project_id, step["destination"])
                rows = await memory_candidates(provider.store, project_id, MARKER)
                checks.update(document_persisted=MARKER in body,
                              search_persisted=any(row["path"] == step["destination"] for row in rows))
                DOCUMENT.write_text(body, encoding="utf-8")
                if len(evidence["steps"]) == 2:
                    # 从实际数据库正文重新检查方案，允许正常 Markdown 中的空格。
                    compact = re.sub(r"\s+", "", body)
                    meta = await provider.store.aget(
                        ("deep_loopminder", "pm_memory_documents", project_id),
                        step["destination"].removeprefix("/memories"),
                    )
                    checks.update(
                        revised_plan_persisted="方案B" in compact,
                        old_plan_revoked="方案A已作废" in compact,
                        revision_reason_persisted="后台排队" in compact,
                        version_persisted=meta is not None and meta.value["version"] >= 2,
                        both_sources_indexed=meta is not None and all(
                            f"/memories/bookmarks/{row['source_id']}.json" in meta.value["source_refs"]
                            for row in evidence["steps"]
                        ),
                    )
                    evidence["steps"][-1]["checks"]["corrected_plan_recorded"] = checks["revised_plan_persisted"]
                    if all(checks.values()) and all(
                        all(row["checks"].values()) for row in evidence["steps"]
                    ):
                        evidence["status"] = "passed"
                        evidence["verification_note"] = "方案名称允许空格，结果以独立进程实际读回为准"
            except FileNotFoundError:
                checks.update(document_persisted=False, search_persisted=False)
            evidence["cross_process_checks"] = checks
            evidence["read_pid"] = os.getpid()
            EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Cross-process checks:", json.dumps(checks), flush=True)
        finally:
            from src.deep_agents.db import cleanup_postgres
            await cleanup_postgres()
        return

    revision_only = "--revision-only" in sys.argv
    previous = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else None
    previous_body = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.exists() else None
    trace = CommandTrace()
    from src.models.llm import get_default_model

    model = get_default_model()
    model.callbacks = [trace]
    api_key = model.openai_api_key.get_secret_value()  # 仅用于异常脱敏，不输出认证信息。
    # 使用生产存储提供方，只隐藏初始化日志以防连接信息出现在证据里。
    with contextlib.redirect_stdout(io.StringIO()):
        provider = await get_pm_memory_store()
    if "--require-postgres" in sys.argv and provider.durability != "durable":
        raise RuntimeError("PostgreSQL required: refusing memory fallback")
    project_id = previous["project_id"] if revision_only and previous else "pm-memory-live-" + uuid4().hex[:12]
    memory = PMMemory(provider.store, model)
    graph = await memory_graph.pm_memory_agent()
    await ProjectStore(provider.store).save(ProjectSnapshot(
        project_id, "独立测试：验证记忆可以记录事实并修订方案", (), (),
    ))
    evidence = {
        "created_at": datetime.now(UTC).isoformat(), "project_id": project_id,
        "write_pid": os.getpid(),
        "requested_model": "minimax/minimax-m3.1-flash:free", "store_backend": provider.backend,
        "durability": provider.durability, "execution": "direct_graph_ainvoke",
        "steps": [], "status": "running",
        "previous_attempt": previous, "revision_only": revision_only,
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        evidence["tool_calls"] = trace.tools
        evidence["response_models"] = list(dict.fromkeys(trace.models))
        evidence["responses"] = trace.responses
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint()
    print("project:", project_id, "storage:", provider.backend, provider.durability, flush=True)
    messages = []
    commands = [
        f"这是独立记忆测试。标记 {MARKER} 必须原样记录。当前采用方案A，原因是先验证最小流程。此信息是测试事实，不是正式项目决定。",
        f"更正 {MARKER} 的测试决定：方案A已作废，现在改为方案B，原因是方案B支持后台排队。请保留旧决定及更正原因，不要把方案A当作当前有效方案。",
    ]
    try:
        for number, command in enumerate(commands, 1):
            if number == 1 and revision_only:
                if not previous or not previous_body or not previous["steps"]:
                    raise RuntimeError("Revision-only mode needs the first recording evidence")
                original = previous["steps"][0]
                messages.append(HumanMessage(content=command))
                await memory.capture(project_id, "test-thread", messages,
                    purpose="恢复此前已验证的来源快照", source_id=original["source_id"])
                files = memory_files(provider.store, project_id)
                await files.awrite(original["destination"].removeprefix("/memories"), previous_body)
                await provider.store.aput(("deep_loopminder", "pm_memory_documents", project_id),
                    original["destination"].removeprefix("/memories"), original["metadata"], index=False)
                evidence["steps"].append({**original, "replayed_fixture": True})
                checkpoint()
                continue
            print("step:", number, "record" if number == 1 else "revise", flush=True)
            messages.append(HumanMessage(content=command))
            await memory.observe(project_id, "test-thread", messages)
            source = await memory.capture(
                project_id, "test-thread", messages,
                purpose="真实命令测试：记录" if number == 1 else "真实命令测试：修订",
                observation=command, focus="用中文准确记录测试标记和方案变化，保留来源引用。",
            )
            result = await asyncio.wait_for(graph.ainvoke(
                {"project_id": project_id, "source_id": source["id"]},
                {"configurable": {"project_id": project_id}, "callbacks": [trace]},
            ), timeout=360)
            body = await read_document(provider.store, project_id, source["destination"])
            meta = await provider.store.aget(
                ("deep_loopminder", "pm_memory_documents", project_id),
                source["destination"].removeprefix("/memories"),
            )
            matches = await memory_candidates(provider.store, project_id, MARKER)
            checks = {
                "readable": bool(body.strip()), "marker_recorded": MARKER in body,
                "source_indexed": meta is not None and source["bookmark"] in meta.value["source_refs"],
                "real_newlines": "\n" in body and "\\n" not in body,
                "searchable": any(row["path"] == source["destination"] for row in matches),
            }
            if number == 2:
                checks["corrected_plan_recorded"] = "方案B" in re.sub(r"\s+", "", body)
                checks["version_incremented"] = meta is not None and meta.value["version"] >= 2
            evidence["steps"].append({
                "step": number, "input": command, "source_id": source["id"],
                "destination": source["destination"], "outcome": result["outcome"],
                "outcome_origin": result.get("outcome_origin", "model"),
                "checks": checks, "metadata": meta.value if meta else None,
            })
            DOCUMENT.write_text(body, encoding="utf-8")
            checkpoint()
            print("checks:", json.dumps(checks), flush=True)
            if not all(checks.values()):
                raise RuntimeError("Memory verification failed; inspect saved evidence")
        evidence["status"] = "passed"
        checkpoint()
        print("RESULT: passed", flush=True)
    except Exception as exc:
        # 失败也读回实际文件，区分未写入与写入后结果协议失败。
        try:
            body = await read_document(provider.store, project_id, source["destination"])
            evidence["failure_document"] = body
            evidence["failure_has_plan_b"] = "方案B" in body
        except Exception:
            evidence["failure_document"] = None
        evidence["status"] = "failed"
        evidence["failed_source_id"] = source["id"]
        evidence["failed_destination"] = source["destination"]
        evidence["error_type"] = type(exc).__name__
        evidence["error"] = str(exc).replace(api_key, "[redacted]")[:1500]
        checkpoint()
        print("RESULT: failed", type(exc).__name__, evidence["error"], flush=True)
        raise SystemExit(1) from None
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            from src.deep_agents.db import cleanup_postgres
            await cleanup_postgres()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    exit_code = 0
    try:
        asyncio.run(main())
    except SystemExit as exc:
        exit_code = exc.code
    if "--require-postgres" in sys.argv and "--read-only" not in sys.argv:
        # 不管模型是否完成协议，都用新进程检查实际留下的数据。
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--read-only"])
        if result.returncode:
            exit_code = result.returncode
    raise SystemExit(exit_code)
