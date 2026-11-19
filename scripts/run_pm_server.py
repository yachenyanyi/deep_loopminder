"""Start only the PM graphs with PM-specific settings and PostgreSQL."""

import argparse
import json
import os
import socket
import sys
import time
import webbrowser
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

_ROOT = Path(__file__).resolve().parents[1]
_HOST = "127.0.0.1"


def _open_workbench(url):
    """Wait for the local workbench before opening the default browser."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        return
    if not webbrowser.open(url):
        print(f"Open the workbench in your browser: {url}", flush=True)


def main():
    """Reuse the official local server without changing other Agent graphs."""
    parser = argparse.ArgumentParser(description="PM-only local workbench")
    parser.add_argument("--port", type=int)
    parser.add_argument("--n-jobs-per-worker", type=int)
    parser.add_argument("--open-workbench", action="store_true", help="Open the local workbench after startup")
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

    port = args.port or runtime.get("port", 2031)
    url = f"http://{_HOST}:{port}/workbench"
    print(f"PM workbench: {url}", flush=True)
    if args.open_workbench:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            port_in_use = probe.connect_ex((_HOST, port)) == 0
        if not port_in_use:
            Thread(target=_open_workbench, args=(url,), daemon=True).start()

    run_server(
        host=_HOST,
        port=port,
        reload=False,
        graphs=graphs,
        n_jobs_per_worker=args.n_jobs_per_worker or runtime.get("n_jobs_per_worker", 10),
        open_browser=False,
        env=str(settings.env_path) if settings.env_path.exists() else {},
        store=config["store"],
        checkpointer=config["checkpointer"],
        http=config["http"],
        allow_blocking=True,
        runtime_edition="inmem",
    )


if __name__ == "__main__":
    main()
