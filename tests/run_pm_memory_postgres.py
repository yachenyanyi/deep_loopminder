"""命令测试：验证 PM 记忆在 PostgreSQL 中保存，并由独立进程读回。"""

# 命令入口先设置项目路径；输出仅含测试状态，不包含数据库口令。
# ruff: noqa: E402, T201

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.middlewares.memory.pm_agent_memory import PMMemory, get_pm_memory_store
from src.middlewares.memory.pm_agent_memory.files import (
    memory_candidates,
    memory_files,
    read_document,
)
from src.middlewares.project_state import ProjectSnapshot, ProjectStore

EVIDENCE = Path("docs/progress/evidence/pm-memory-postgres-20261014.json")
MARKER = "PM_PG_20261014"


async def test_storage(read_only=False):
    """写入模拟记忆制品，或在新进程读取并检查全部持久化数据。"""
    provider = await get_pm_memory_store()
    if provider.durability != "durable":
        raise RuntimeError("PostgreSQL connection failed; refusing an in-memory test")
    memory = PMMemory(provider.store)
    try:
        if read_only:
            evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
            project_id = evidence["project_id"]
            source = await memory.source(project_id, evidence["source_id"])
            project = await ProjectStore(provider.store).load(project_id)
            body = await read_document(provider.store, project_id, evidence["destination"])
            matches = await memory_candidates(provider.store, project_id, MARKER)
            checks = {
                "project_read_after_restart": project is not None,
                "source_read_after_restart": MARKER in source["observation"],
                "document_read_after_restart": MARKER in body,
                "search_after_restart": any(row["path"] == evidence["destination"] for row in matches),
            }
            if not all(checks.values()):
                raise RuntimeError("Cross-process storage checks failed")
            evidence["read_checks"] = checks
            evidence["read_pid"] = os.getpid()
            evidence["status"] = "passed"
            EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Independent process read:", json.dumps(checks))
            return

        project_id = "pm-pg-test-" + uuid4().hex[:12]
        await ProjectStore(provider.store).save(ProjectSnapshot(
            project_id, "独立 PostgreSQL 持久化测试", (), (),
        ))
        observation = f"{MARKER}：仅验证存储，不是正式业务决定。"
        source = await memory.capture(
            project_id, "postgres-test-thread", [HumanMessage(content=observation)],
            purpose="PostgreSQL 跨进程持久化测试", observation=observation,
        )
        # 此文档是确定性的存储测试输入，不声称由真实模型生成。
        body = f"# PostgreSQL 测试记录\n\n{observation}\n\n来源：{source['bookmark']}\n"
        result = await memory_files(provider.store, project_id).awrite(
            source["destination"].removeprefix("/memories"), body,
        )
        if result.error:
            raise RuntimeError(result.error)
        await memory._index_documents({**source, "baseline": {}})
        evidence = {
            "created_at": datetime.now(UTC).isoformat(), "project_id": project_id,
            "source_id": source["id"], "destination": source["destination"],
            "database": "deep_loopminder_test", "backend": provider.backend,
            "durability": provider.durability, "write_pid": os.getpid(),
            "model_execution": False, "document_fixture": "deterministic_test_input",
            "status": "written",
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("PostgreSQL write: passed; project:", project_id)
    finally:
        from src.deep_agents.db import cleanup_postgres
        await cleanup_postgres()


if __name__ == "__main__":
    # psycopg 异步连接在 Windows 上要求 Selector，不能使用默认 Proactor。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    is_reader = "--read-only" in sys.argv
    asyncio.run(test_storage(read_only=is_reader))
    if not is_reader:
        # 写入连接关闭后，再启动独立 Python 进程验证，防止把内存共享当持久化。
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--read-only"], check=True)
