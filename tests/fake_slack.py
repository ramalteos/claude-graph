"""A Slack seam that records posts, and a local webhook endpoint for the CLI tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_graph.slack import SlackError


@dataclass
class FakeSlack:
    fail: bool = False
    posts: list[str] = field(default_factory=list)

    def post(self, text: str) -> None:
        if self.fail:
            raise SlackError("slack webhook: HTTP 500")
        self.posts.append(text)


@contextmanager
def webhook_server(status: int = 200) -> Iterator[tuple[str, list[dict]]]:
    """Yields (url, received): every POST body, parsed as JSON, lands in `received`."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", received
    finally:
        server.shutdown()
        server.server_close()
