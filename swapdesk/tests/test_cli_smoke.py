from __future__ import annotations

import http.client
import threading
from pathlib import Path

from swapdesk.cli import main
from swapdesk.config import work_paths
from swapdesk.report.server import create_server


def test_demo_runs_end_to_end(tmp_path: Path, capsys) -> None:
    work = tmp_path / "workspace"
    code = main(["demo", "--workdir", str(work)])
    output = capsys.readouterr().out
    assert code == 0
    assert "[verify] PASS" in output
    assert "tasks=3" in output
    assert "executable=True" in output
    demo_root = work
    assert (demo_root / "ledger.sqlite3").exists()
    reports = sorted((demo_root / "reports").glob("run-*.html"))
    assert len(reports) == 1
    assert (demo_root / "runs").exists()


def test_serve_only_get_and_blocks_traversal(tmp_path: Path) -> None:
    path = tmp_path / "ws"
    assert main(["demo", "--workdir", str(path)]) == 0
    paths = work_paths(path)
    report = next(paths.reports.glob("run-*.html"))
    httpd = create_server(paths, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    assert host == "127.0.0.1"
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert report.name in body
        conn.request("GET", f"/reports/{report.name}")
        response = conn.getresponse()
        assert response.status == 200
        assert b"swapdesk" in response.read()
        conn.request("GET", "/reports/%2e%2e%2fsecret.md")
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.request("POST", "/reports/" + report.name)
        response = conn.getresponse()
        assert response.status == 405
        response.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_blocked_plan_exit_code(tmp_path: Path) -> None:
    code = main(["plan", "--workdir", str(tmp_path / "out"),
                 "--data-dir", str(Path("data").resolve()),
                 "--request", "req-002.yaml"])
    assert code == 3
