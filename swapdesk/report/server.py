"""Local read-only report server bound exclusively to loopback."""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from ..config import DEFAULT_HOST, DEFAULT_PORT, WorkPaths, work_paths
from ..errors import ErrorCode, SwapDeskError
from ..io.fs import list_files, safe_join

INDEX_CSS = """
body { font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif; margin:0;
  background:#f6f7f8; color:#1f252b; }
main { max-width:860px; margin:0 auto; padding:36px 20px; }
h1 { font-size:1.45rem; }
ul { list-style:none; padding:0; }
li { background:#fff; border:1px solid #d9dfe5; border-radius:8px;
  margin:8px 0; padding:10px 14px; }
a { color:#1f5f8b; text-decoration:none; }
a:hover { text-decoration:underline; }
.meta { color:#65717c; font-size:.85rem; }
"""


def _listing(reports: Path) -> bytes:
    files = list_files(reports, (".html", ".md"))
    items = "".join(
        f'<li><a href="/reports/{quote(path.name)}">{html.escape(path.name)}</a>'
        f'<div class="meta">{path.stat().st_size} bytes</div></li>'
        for path in files
    )
    if not items:
        items = "<li>暂无报告。</li>"
    page = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>swapdesk 报告</title><style>" + INDEX_CSS + "</style></head>"
        "<body><main><h1>swapdesk 本地只读报告</h1>"
        "<p class=\"meta\">仅监听 127.0.0.1，仅允许 GET。</p><ul>"
        + items + "</ul></main></body></html>"
    )
    return page.encode("utf-8")


class ReportRequestHandler(BaseHTTPRequestHandler):
    server_version = "swapdesk-report"
    sys_version = ""

    @property
    def reports_dir(self) -> Path:
        return Path(self.server.reports_dir)  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        try:
            raw = unquote(urlsplit(self.path).path)
            if raw in ("/", "/index.html", "/reports", "/reports/"):
                self._send(200, _listing(self.reports_dir),
                           "text/html; charset=utf-8")
                return
            if raw == "/healthz":
                self._json(200, {"status": "ok", "readonly": True})
                return
            prefix = "/reports/"
            if not raw.startswith(prefix):
                self._json(404, {"error": ErrorCode.HTTP_NOT_FOUND})
                return
            name = raw[len(prefix):]
            if (not name or name != Path(name).name or "\\" in name
                    or "\x00" in name or name in (".", "..")):
                self._json(403, {"error": ErrorCode.PATH_ESCAPE})
                return
            target = safe_join(self.reports_dir, name)
            if not target.exists() or not target.is_file():
                self._json(404, {"error": ErrorCode.HTTP_NOT_FOUND})
                return
            if target.suffix.lower() not in (".html", ".md"):
                self._json(404, {"error": ErrorCode.HTTP_NOT_FOUND})
                return
            body = target.read_bytes()
            content_type = ("text/html; charset=utf-8" if target.suffix == ".html"
                            else "text/markdown; charset=utf-8")
            self._send(200, body, content_type)
        except SwapDeskError:
            self._json(403, {"error": ErrorCode.PATH_ESCAPE})
        except (BrokenPipeError, ConnectionResetError):
            return

    def _method_not_allowed(self) -> None:
        self._json(405, {"error": ErrorCode.HTTP_METHOD,
                         "allowed": ["GET"]})

    do_HEAD = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed


def create_server(paths: WorkPaths | str | Path | None = None,
                  port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if isinstance(paths, WorkPaths):
        workspace = paths
    else:
        workspace = work_paths(paths)
    workspace.ensure()
    handler = ReportRequestHandler
    httpd = ThreadingHTTPServer((DEFAULT_HOST, port), handler)
    httpd.reports_dir = str(workspace.reports)  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def serve(paths: WorkPaths | str | Path | None = None,
          port: int = DEFAULT_PORT) -> None:
    httpd = create_server(paths, port)
    host, actual_port = httpd.server_address[0], httpd.server_address[1]
    print(f"swapdesk 报告服务：{host}:{actual_port}（仅 GET，只读，按 Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
