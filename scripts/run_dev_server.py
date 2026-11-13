"""本机开发服务启动入口。

为什么不用 ``langgraph dev``：本机安装的 langgraph-cli 在 ``dev`` 命令里构造
``run_server(...)`` 调用时没有传 ``checkpointer``（langgraph_cli/cli.py 的 dev
只转发了 store/http/auth/ui/webhooks），langgraph.json 里的 checkpointer 配置
被静默丢弃，PM 会话检查点退回内存。上游 ``langgraph_api.cli:main`` 读该配置，
但 ``langgraph`` 命令不经过它。

这里按 run_server 的公开签名把 langgraph.json 的 checkpointer 一起传入，
其余参数与 ``langgraph dev`` 的默认行为一致；命令行参数也保持一致，便于
与既有启动方式互换。
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main():
    """Start the local dev server, forwarding langgraph.json's checkpointer."""
    parser = argparse.ArgumentParser(description="PM dev server with official checkpointer wiring")
    parser.add_argument("--port", type=int, default=2030)
    parser.add_argument("--config", default=str(_ROOT / "langgraph.json"))
    parser.add_argument("--n-jobs-per-worker", type=int, default=10)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sys.path.insert(0, str(_ROOT))

    from langgraph_api.cli import run_server

    run_server(
        host="127.0.0.1",
        port=args.port,
        reload=False,
        graphs=config.get("graphs", {}),
        n_jobs_per_worker=args.n_jobs_per_worker,
        open_browser=False,
        env=config.get("env"),
        store=config.get("store"),
        auth=config.get("auth"),
        http=config.get("http"),
        ui=config.get("ui"),
        ui_config=config.get("ui_config"),
        webhooks=config.get("webhooks"),
        checkpointer=config.get("checkpointer"),
        allow_blocking=True,
        runtime_edition="inmem",
    )


if __name__ == "__main__":
    main()
