"""独立本地轨迹网页：只读取事件文件，停止请求交由宿主取消官方Run。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .middleware import TrajectoryRecorder


def read_events(path: Path, after: int = 0, limit: int = 300):
    """按字节游标增量读取完整行，写到一半的行留到下次，不扫描全部历史。"""
    if after < 0 or not 1 <= limit <= 1000:
        raise ValueError("游标或数量不合法")
    if not path.exists():
        return {"events": [], "next": 0, "has_more": False}
    events = []
    with path.open("rb") as stream:
        if after > path.stat().st_size:
            raise ValueError("事件文件已更换，请从头读取")
        stream.seek(after)
        while len(events) < limit:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            events.append(json.loads(line))
        cursor = stream.tell()
    return {"events": events, "next": cursor, "has_more": cursor < path.stat().st_size}


def read_json(path):
    """运行状态可能正在原子提交之前写入，暂时不可读时不伪造状态。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def create_server(directory: str | Path, port=0):
    """仅监听本机；目录由宿主绑定，不接受HTTP文件路径参数。"""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    redactor = TrajectoryRecorder(directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            """轮询不刷屏，不把请求头和数据写进日志。"""

        def reply(self, status, body, content_type="application/json; charset=utf-8"):
            """统一响应，HTML仅运行本地随包脚本，不渲染事件中的HTML。"""
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            """阻止跨站控制和DNS重绑定；没有跨源开放。"""
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in allowed:
                self.reply(403, {"error": "仅允许本机地址"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers["Host"]:
                self.reply(403, {"error": "不接受跨站请求"})
                return False
            return True

        def do_GET(self):
            """提供页面、增量事件、状态和已经脱敏的JSONL导出。"""
            if not self.local_request():
                return
            url = urlparse(self.path)
            if url.path == "/":
                return self.reply(
                    200,
                    Path(__file__).with_name("dashboard.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            if url.path == "/api/events":
                try:
                    query = parse_qs(url.query)
                    result = read_events(
                        directory / "trajectory.jsonl",
                        int(query.get("after", ["0"])[0]),
                    )
                except (ValueError, UnicodeError):
                    return self.reply(400, {"error": "事件游标或文件内容无效"})
                return self.reply(200, result)
            if url.path == "/api/status":
                return self.reply(
                    200,
                    redactor.clean(
                        {
                            "run": read_json(directory / "run.json"),
                            "first_error": read_json(directory / "first-error.json"),
                            "stop_requested": (
                                directory / "stop-request.json"
                            ).exists(),
                        }
                    ),
                )
            if url.path == "/api/export":
                path = directory / "trajectory.jsonl"
                return self.reply(
                    200,
                    path.read_bytes() if path.exists() else b"",
                    "application/x-ndjson; charset=utf-8",
                )
            return self.reply(404, {"error": "不存在的接口"})

        def do_POST(self):
            """只接受停止信号，不直接管理Agent/原生进程或签发业务批准。"""
            if not self.local_request():
                return
            if self.path != "/api/stop":
                return self.reply(404, {"error": "不存在的接口"})
            try:
                with (directory / "stop-request.json").open(
                    "x", encoding="utf-8"
                ) as stream:
                    json.dump(
                        {"source": "local_trajectory_viewer", "action": "stop"}, stream
                    )
            except FileExistsError:
                pass
            self.reply(
                202,
                {
                    "status": "stop_requested",
                    "message": "已发送停止请求，等待宿主确认取消",
                },
            )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    """命令行启动独立网页；Agent服务结束后仍可查看历史。"""
    parser = argparse.ArgumentParser(description="本地Agent运行轨迹")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = create_server(args.directory, args.port)
    info = {
        "url": f"http://127.0.0.1:{server.server_port}",
        "directory": str(Path(args.directory).resolve()),
    }
    temporary = Path(args.directory, "monitor.json.tmp")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    temporary.replace(Path(args.directory, "monitor.json"))
    print(json.dumps(info), flush=True)  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
