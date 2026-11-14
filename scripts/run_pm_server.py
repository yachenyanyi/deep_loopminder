"""Start only the PM graphs with PM-specific settings and PostgreSQL."""

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main():
    """Reuse the official local server without changing other Agent graphs."""
    parser = argparse.ArgumentParser(description="PM-only local workbench")
    parser.add_argument("--port", type=int)
    parser.add_argument("--n-jobs-per-worker", type=int)
    args = parser.parse_args()
    os.environ["LOOPMINDER_PM_SETTINGS"] = "1"
    sys.path.insert(0, str(_ROOT))
    os.environ["PYTHONPATH"] = str(_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
    from src.middlewares.agent.pm_settings import PMSettings

    settings = PMSettings()
    runtime = settings.read().get("runtime", {})
    if runtime.get("data_dir"):
        os.environ["LOOPMINDER_DATA_DIR"] = runtime["data_dir"]
    config = json.loads((_ROOT / "langgraph.json").read_text(encoding="utf-8"))
    names = ("pm_agent", "pm_memory_agent", "acp_worker", "acp_control")
    graphs = {}
    for name in names:
        module, separator, symbol = config["graphs"][name].partition(":")
        if module.startswith("./"):
            module = module[2:].removesuffix(".py").replace("/", ".")
        graphs[name] = module + separator + symbol
    # The dev server stores its thread/run registry under its working directory.
    # PM must never share that directory with the original multi-Agent server.
    runtime_home = settings.root / "workspace" / "pm-runtime"
    runtime_home.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime_home)
    from langgraph_api.cli import run_server

    run_server(
        host="127.0.0.1",
        port=args.port or runtime.get("port", 2031),
        reload=False,
        graphs=graphs,
        n_jobs_per_worker=args.n_jobs_per_worker or runtime.get("n_jobs_per_worker", 10),
        open_browser=False,
        env=str(settings.env_path) if settings.env_path.exists() else {},
        store={"path": "src.middlewares.persistence.pm_runtime.generate_pm_store"},
        checkpointer={"path": "src.middlewares.persistence.pm_runtime.generate_pm_checkpointer"},
        http={"app": "src.deep_agents.agents.pm_agent.server:app"},
        allow_blocking=True,
        runtime_edition="inmem",
    )


if __name__ == "__main__":
    main()
