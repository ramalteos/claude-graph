"""Localhost dashboard: one state call, log tails, diffs, and token-gated actions."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from claude_graph.boxes import split_lines
from claude_graph.render import render_log
from claude_graph.state import TASK_ID
from claude_graph.ui.actions import Actions
from claude_graph.ui.data import Store

LOG_NAME = re.compile(r"^\d{2}-[a-z]+\.log\Z")
INDEX = Path(__file__).parent / "index.html"
MAX_BODY = 64 * 1024  # an action request is a few hundred bytes
DRAIN_MAX = 1 << 20  # drained before a 413 so the client sees the status, not a reset
DRAIN_MAX = 1 << 20  # read this much of an oversized body before answering 413, so the client
# sees the status instead of a reset; beyond that the connection is simply closed
LOG_CHUNK = 1 << 20  # per /api/log answer; the page keeps asking from the returned offset


@dataclass
class UiContext:
    state_dir: Path
    claude_box: Path | None
    engine: str
    actions: bool = False
    runner_argv: list[str] | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def __post_init__(self) -> None:
        self.store = Store(self.state_dir, self.claude_box, self.engine)
        self.act = Actions(self.store, self.claude_box, self.engine, self.runner_argv)

    def log(self, task_id: str, name: str, offset: int, raw: bool) -> dict | None:
        path = self.state_dir / "logs" / task_id / name
        if not path.is_file():
            return None
        size = path.stat().st_size
        reset = offset > size  # the log was rewritten shorter (same attempt re-run): start over
        if reset:
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(LOG_CHUNK)
        cut = chunk.rfind(b"\n") + 1  # whole lines only; the tail is re-read next time
        if cut == 0 and len(chunk) == LOG_CHUNK:
            cut = len(chunk)  # one line longer than a chunk: pass it on rather than stall
        text = chunk[:cut].decode("utf-8", errors="replace")
        lines = split_lines(text) if raw else render_log(text)
        return {"offset": offset + cut, "lines": lines, "size": size, "reset": reset}


def make_handler(ctx: UiContext) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # a client that stops mid-body cannot pin a thread forever

        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if not _host_ok(self.headers.get("Host", ""), self.server.server_port):
                return self._send(403, {"error": "forbidden host"})
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    page = INDEX.read_text(encoding="utf-8").replace(
                        "__ACTION_TOKEN__", ctx.token if ctx.actions else ""
                    )
                    return self._send_html(page)
                if url.path == "/api/state":
                    return self._send(200, ctx.store.state())
                if url.path == "/api/task":
                    task_id = _one(q, "task")
                    if not TASK_ID.match(task_id):
                        return self._send(400, {"error": "bad task id"})
                    doc = ctx.store.document(task_id)
                    return self._send(200, doc) if doc else self._send(404, {"error": "no task"})
                if url.path == "/api/diff":
                    task_id = _one(q, "task")
                    if not TASK_ID.match(task_id):
                        return self._send(400, {"error": "bad task id"})
                    return self._send_text(ctx.store.diff(task_id))
                if url.path == "/api/log":
                    task_id, name = _one(q, "task"), _one(q, "name")
                    if not TASK_ID.match(task_id) or not LOG_NAME.match(name):
                        return self._send(400, {"error": "bad task id or log name"})
                    offset = max(0, int(_one(q, "offset") or 0))
                    log = ctx.log(task_id, name, offset, _one(q, "raw") == "1")
                    return self._send(200, log) if log else self._send(404, {"error": "no log"})
                return self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001 — a dashboard must not drop the connection
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self) -> None:  # noqa: N802
            if not _host_ok(self.headers.get("Host", ""), self.server.server_port):
                return self._send(403, {"ok": False, "output": "forbidden host"})
            if urlparse(self.path).path != "/api/action":
                return self._send(404, {"ok": False, "output": "not found"})
            if not ctx.actions:
                return self._send(
                    403,
                    {
                        "ok": False,
                        "output": "actions are off — start with: claude-graph ui --actions",
                    },
                )
            given = (self.headers.get("X-CG-Token") or "").encode("latin-1", "replace")
            if not secrets.compare_digest(given, ctx.token.encode()):
                return self._send(403, {"ok": False, "output": "bad token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    return self._send(400, {"ok": False, "output": "bad body"})
                if length > MAX_BODY:
                    self._drain(length)
                    return self._send(413, {"ok": False, "output": "body too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"ok": False, "output": "bad body"})
            return self._send(
                200, ctx.act.do(str(body.get("action", "")), str(body.get("arg", "")))
            )

        def _drain(self, length: int) -> None:
            left = min(length, DRAIN_MAX)
            while left > 0:
                chunk = self.rfile.read(min(left, 64 * 1024))
                if not chunk:
                    break
                left -= len(chunk)

        def _send(self, status: int, payload: object) -> None:
            self._raw(status, json.dumps(payload).encode(), "application/json")

        def _send_text(self, text: str) -> None:
            self._raw(200, text.encode(), "text/plain; charset=utf-8")

        def _send_html(self, html: str) -> None:
            self._raw(
                200,
                html.encode(),
                "text/html; charset=utf-8",
                csp="default-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )

        def _raw(self, status: int, body: bytes, ctype: str, csp: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "X-Frame-Options", "DENY"
            )  # a framed dashboard could be clicked for you
            if csp:
                self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _one(q: dict[str, list[str]], key: str) -> str:
    return (q.get(key) or [""])[0]


def _host_ok(host: str, port: int) -> bool:
    return host.strip().lower() in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")


def serve(ctx: UiContext, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(ctx))
