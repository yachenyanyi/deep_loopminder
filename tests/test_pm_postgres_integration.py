"""Opt-in real PostgreSQL test using a uniquely named temporary schema."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from dotenv import dotenv_values, set_key

pytestmark = pytest.mark.skipif(os.getenv("RUN_PM_POSTGRES_TEST") != "1", reason="explicit real PostgreSQL probe")


def test_pm_store_and_settings_migrate_in_an_isolated_schema(tmp_path):
    import psycopg
    from psycopg import sql

    root = Path(__file__).resolve().parents[1]
    uri = dotenv_values(root / ".env", interpolate=False).get("LANGGRAPH_POSTGRES_URI")
    if not uri:
        pytest.skip("No existing PostgreSQL URI")
    parts = urlsplit(uri)
    schema = "pm_probe_" + uuid.uuid4().hex
    query = parse_qsl(parts.query, keep_blank_values=True) + [("options", "-csearch_path=" + schema)]
    isolated = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    with psycopg.connect(uri, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            (tmp_path / ".env").write_text("", encoding="utf-8")
            set_key(str(tmp_path / ".env"), "PM_POSTGRES_URI", isolated)
            (tmp_path / "settings.json").write_text("", encoding="utf-8")
            (tmp_path / "project").mkdir()
            (tmp_path / "appdata").mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            environment = os.environ.copy()
            environment.update(LOOPMINDER_PM_CONFIG_ROOT=str(tmp_path),
                               LOCALAPPDATA=str(tmp_path / "appdata"), PYTHONUTF8="1")
            for name in ("LANGGRAPH_POSTGRES_URI", "PM_POSTGRES_URI", "LOOPMINDER_DATA_DIR"):
                environment.pop(name, None)
            log_path = tmp_path / "server.log"
            with log_path.open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(
                    [sys.executable, str(root / "scripts/run_pm_server.py"), "--port", str(port)],
                    cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                try:
                    base = f"http://127.0.0.1:{port}"
                    state = None
                    for _ in range(100):
                        if proc.poll() is not None:
                            break
                        try:
                            with urllib.request.urlopen(base + "/workbench/api/runtime-settings", timeout=1) as response:
                                state = json.load(response)
                            break
                        except (OSError, urllib.error.URLError):
                            time.sleep(0.2)
                    assert state is not None and state["database"]["store_ready"] is True, "PM PostgreSQL startup failed"
                    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["migration"]["legacy_imported"]
                    body = json.dumps({"directory": str(tmp_path / "project"), "goal": "isolated PM probe"}).encode()
                    request = urllib.request.Request(base + "/workbench/api/projects", data=body,
                                                     headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request) as response:
                        assert response.status == 200
                    with psycopg.connect(isolated) as connection:
                        assert connection.execute("SELECT count(*) FROM store").fetchone()[0] > 0
                    assert (tmp_path / "workspace/pm-runtime/.langgraph_api").is_dir()
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
        finally:
            assert schema.startswith("pm_probe_") and len(schema) == 41
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
