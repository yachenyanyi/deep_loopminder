"""SkillsBench真实PM→ClaudeACP单题试跑，保留原始输入与断言并映射Windows路径。"""

# ruff: noqa: E402, T201
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langgraph_sdk import get_client

load_dotenv(ROOT / ".env")
from run_pm_project_flow import ModelTrace
from run_pm_project_flow import acp_control as acp_control
from run_pm_project_flow import acp_worker as acp_worker
from run_pm_project_flow import app as app


class Trace(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        path = Path(os.environ["PM_FLOW_DIRECTORY"]) / "pm-tools.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(request.tool_call, ensure_ascii=False) + "\n")
        return await handler(request)


async def benchmark_pm():
    from src.models.llm import get_default_model

    get_default_model().callbacks = [ModelTrace()]
    module = importlib.import_module("src.deep_agents.agents.pm_agent.agent")
    original = module.assemble_pm_user_middleware
    module.assemble_pm_user_middleware = lambda *a, **kw: (*original(*a, **kw), Trace())
    try:
        return await module.pm_agent()
    finally:
        module.assemble_pm_user_middleware = original


async def main():
    from urllib.parse import urlparse

    if (
        urlparse(os.getenv("LANGGRAPH_POSTGRES_URI", "")).path
        != "/deep_loopminder_test"
    ):
        raise RuntimeError("仅允许独立测试数据库")
    identity = uuid4().hex[:10]
    directory = ROOT / "workspace/benchmarks/runs" / ("citation-" + identity)
    directory.mkdir(parents=True)
    task = ROOT / "workspace/benchmarks/skillsbench/tasks/citation-check"
    evidence = {
        "task": "citation-check",
        "dataset": "SkillsBench v1.1",
        "skill_mode": "未注入SkillsBench技能，保留本机Claude配置；非严格no-skill隔离",
        "upstream_commit": "b63b7b2850226b6aa4fb5929a8c1ac7bc4d9a6af",
        "model": "minimax/minimax-m3.1-flash:free",
        "worker_model": "minimax/minimax-m3.1-flash:free",
        "runner": "真实PM/本机ClaudeACP，原始输入、原版pytest断言，仅映射文件路径；非榜单标准运行",
        "directory": str(directory),
        "platform": "Windows本机适配，非Linux标准容器运行",
        "steps": {},
        "status": "running",
    }
    public = ROOT / "docs/progress/evidence/skillsbench-citation-20261020.json"

    def save(step, value):
        evidence["steps"][step] = value
        public.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"step": step, **value}, ensure_ascii=False), flush=True)

    process = None
    log = (directory / "server.log").open("w", encoding="utf-8")
    try:
        workdir = directory / "work"
        workdir.mkdir()
        shutil.copy2(task / "environment/test.bib", workdir / "test.bib")
        original_hash = hashlib.sha256((workdir / "test.bib").read_bytes()).hexdigest()
        (workdir / ".claude").mkdir()
        (workdir / ".claude/settings.json").write_text(
            json.dumps(
                {
                    "permissions": {
                        "defaultMode": "acceptEdits",
                        "allow": [
                            "Read",
                            "Write",
                            "Edit",
                            "Bash",
                            "Glob",
                            "Grep",
                            "WebFetch",
                            "WebSearch",
                        ],
                        "deny": ["Agent", "Task"],
                    }
                }
            ),
            encoding="utf-8",
        )
        native = json.loads(
            (Path.home() / ".claude/settings.json").read_text(encoding="utf-8-sig")
        ).get("env", {})
        worker_env = {
            k: v
            for k, v in native.items()
            if k in {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"}
        }
        for key in [
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        ]:
            worker_env[key] = evidence["worker_model"]
        worker_env.update(
            CLAUDE_CODE_MAX_OUTPUT_TOKENS="32768",
            IS_SANDBOX="1",
            DISABLE_AUTOUPDATER="1",
            HTTP_PROXY="http://127.0.0.1:7897",
            HTTPS_PROXY="http://127.0.0.1:7897",
            NO_PROXY="localhost,127.0.0.1",
        )
        command = [
            "J:/dev_soft/node/node.exe",
            "C:/Users/User/AppData/Local/npm-cache/_npx/d820eb7d96bc2600/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js",
        ]
        worker_env["CLAUDE_CODE_EXECUTABLE"] = "C:/Users/User/.local/bin/claude.exe"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        evidence["url"] = url
        env = dict(os.environ)
        env.update(
            PM_FLOW_DIRECTORY=str(directory),
            LANGGRAPH_API_URL=url,
            PYTHONUTF8="1",
            LANGSMITH_TRACING="false",
        )
        env["PYTHONPATH"] = str(ROOT / "tests") + os.pathsep + str(ROOT)
        env["ACP_SUBAGENTS_JSON"] = json.dumps(
            [
                {
                    "name": "developer",
                    "description": "执行隔离工作环境内的文件和检索任务",
                    "graph_id": "acp_worker",
                }
            ]
        )
        env["ACP_WORKERS_JSON"] = json.dumps(
            [
                {
                    "provider_id": "claude",
                    "roles": ["developer"],
                    "capabilities": [
                        "files.read",
                        "files.write",
                        "shell.execute",
                        "web.search",
                    ],
                    "cwd": str(workdir),
                    "command": command,
                    "env": worker_env,
                    "timeout": 900,
                    "grants": [
                        "worker.connect",
                        "worker.new",
                        "worker.resume",
                        "worker.ask",
                        "worker.observe",
                        "worker.cancel",
                        "external_tool.read",
                        "external_tool.search",
                        "external_tool.edit",
                        "external_tool.execute",
                        "external_tool.fetch",
                        "external_tool.other",
                    ],
                    "approval": {
                        "use": "src.middlewares.approval.builtin:AllowlistProvider",
                        "config": {
                            "allowed_tools": [
                                "acp.read",
                                "acp.search",
                                "acp.edit",
                                "acp.execute",
                                "acp.fetch",
                                "acp.other",
                            ]
                        },
                    },
                }
            ]
        )
        config = directory / "langgraph.json"
        config.write_text(
            json.dumps(
                {
                    "dependencies": [str(ROOT)],
                    "graphs": {
                        "pm_agent": "run_pm_skillsbench:benchmark_pm",
                        "pm_memory_agent": "src.middlewares.memory.pm_agent_memory.graph:pm_memory_agent",
                        "acp_worker": "run_pm_skillsbench:acp_worker",
                        "acp_control": "run_pm_skillsbench:acp_control",
                    },
                    "http": {"app": "run_pm_skillsbench:app"},
                    "store": {
                        "path": "src.middlewares.memory.pm_agent_memory.provider.generate_pm_store"
                    },
                }
            ),
            encoding="utf-8",
        )
        bootstrap = """import sys,threading,uvicorn
original=uvicorn.run
def run(app,**kwargs):
 server=uvicorn.Server(uvicorn.Config(app,**kwargs))
 def stop():
  sys.stdin.readline();server.should_exit=True
 threading.Thread(target=stop,daemon=True).start();server.run()
uvicorn.run=run
from langgraph_cli.cli import cli
cli()
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X",
            "utf8",
            "-c",
            bootstrap,
            "dev",
            "--config",
            str(config),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-browser",
            "--no-reload",
            "--n-jobs-per-worker",
            "4",
            "--allow-blocking",
            cwd=directory,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import httpx

        async with httpx.AsyncClient() as http:
            for _ in range(120):
                if process.returncode is not None:
                    raise RuntimeError("Server启动失败，详见隔离日志")
                try:
                    if (await http.get(url + "/ok", timeout=1)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise TimeoutError("Server启动超时")
        save("ready", {"server": True, "oracle_hidden": True, "verifier_hidden": True})
        client = get_client(url=url, timeout=1000)
        thread = (await client.threads.create())["thread_id"]
        evidence["thread_id"] = thread
        task_text = (
            (task / "task.md").read_text(encoding="utf-8").split("---", 2)[2].strip()
        )
        task_text = task_text.replace(
            "/root/test.bib", (workdir / "test.bib").as_posix()
        ).replace("/root/answer.json", (workdir / "answer.json").as_posix())
        prompt = (
            "请作为PM实际执行下述客户交付需求，使用项目状态和ACP工具组织并完成工作。"
            "这是独立测试项目，project_id由宿主提供。你没有直接文件执行工具，实际文件工作交给developer。"
            "请创建项目，创建真实TaskBrief制品，再创建task_id=solve_citations、owner_role=developer的任务，"
            "required_capabilities=['files.read','files.write','shell.execute','web.search']，验收标准只有一条：原版SkillsBench验证器通过。"
            "任务按ready、assigned、in_progress逐步推进，再spawn_agent(task_name=solve_citations,agent_type=developer)。"
            "每次只执行一个写工具；领取Worker结果后读取交付制品核对，告知客户结果。"
            "外部验证由宿主在你结束后执行，未收到可信验收时不得自报passed、approved或done；不请求人工审批。"
            "不要创建重复Worker，不要读取参考答案或评分器，不修改任务输入。"
            "下面是完整原始客户需求，原样保存到TaskBrief并交给Worker：\n" + task_text
        )
        run = await client.runs.create(
            thread,
            "pm_agent",
            input={"messages": [{"role": "user", "content": prompt}]},
            config={
                "configurable": {
                    "project_id": "skillsbench-" + identity,
                    "project_state_single_writer": True,
                },
                "recursion_limit": 300,
            },
        )
        save("pm_started", {"run_id": run["run_id"]})
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = await client.runs.get(thread, run["run_id"])
            if current["status"] not in {"pending", "running"}:
                break
            await asyncio.sleep(3)
        else:
            await client.runs.cancel(
                thread, run["run_id"], action="interrupt", wait=True
            )
            raise TimeoutError("原题900秒执行预算耗尽")
        state = await client.threads.get_state(thread)
        (directory / "pm-checkpoint.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        save(
            "pm_finished",
            {
                "status": current["status"],
                "answer": state["values"].get("messages", [{}])[-1].get("content", ""),
                "async_tasks": state["values"].get("async_tasks", {}),
            },
        )
        answer = workdir / "answer.json"
        if answer.exists():
            shutil.copy2(answer, directory / "answer.json")
        save(
            "delivery",
            {
                "answer_file_exists": answer.exists(),
                "input_unchanged": hashlib.sha256(
                    (workdir / "test.bib").read_bytes()
                ).hexdigest()
                == original_hash,
            },
        )
        # 停止真实Worker后才载入评分器；保持原版测试函数，仅映射ANSWER_FILE。
        process.stdin.write(b"\n")
        await process.stdin.drain()
        await asyncio.wait_for(process.wait(), 40)
        process = None
        scoring = """import sys,pytest
from pathlib import Path
class Paths:
 def pytest_collection_modifyitems(self,items):
  for item in items:
   item.module.ANSWER_FILE=Path(sys.argv[2])
sys.exit(pytest.main([sys.argv[1],'-q','-p','no:cacheprovider','--confcutdir='+str(Path(sys.argv[1]).parent)],plugins=[Paths()]))
"""
        verified = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-c",
                scoring,
                str(task / "verifier/test_outputs.py"),
                str(answer),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        (directory / "verifier.log").write_text(
            verified.stdout + "\n" + verified.stderr, encoding="utf-8"
        )
        save(
            "verification",
            {
                "reward": 1 if verified.returncode == 0 else 0,
                "verifier_exit": verified.returncode,
                "output": verified.stdout[-1800:],
            },
        )
        evidence["status"] = "passed" if verified.returncode == 0 else "failed"
    except Exception as exc:
        evidence["status"] = "error"
        save("error", {"type": type(exc).__name__, "message": str(exc)[:600]})
    finally:
        if process and process.returncode is None:
            process.stdin.write(b"\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 40)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()
        evidence["server_stopped"] = process is None or process.returncode is not None
        from src.deep_agents.db import cleanup_postgres

        await cleanup_postgres()
        public.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "server_stopped": evidence["server_stopped"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
