"""Host-side Slack seam: announce an approved pull request in one channel."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Protocol

from claude_graph.state import TaskState

MAX_BULLETS = 8


class SlackError(Exception):
    pass


class Slack(Protocol):
    def post(self, text: str) -> None: ...


class WebhookSlack:
    """An Incoming Webhook; it posts to the channel it was created for."""

    def __init__(self, url: str, timeout_s: int = 15) -> None:
        self.url, self.timeout_s = url, timeout_s

    def post(self, text: str) -> None:
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s):
                pass
        except urllib.error.HTTPError as e:
            raise SlackError(f"slack webhook: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SlackError(f"slack webhook: {e}") from None


def announcement(state: TaskState, limit: int = 2000) -> str:
    """`<pr url|title>` plus the PR body's "What" bullets; the coder's summary when there
    are none."""
    title = state.get("pr_title") or state["request"].strip().splitlines()[0][:70]
    head = f"<{state['pr_url']}|{_mrkdwn(title)}>"
    bullets = _what_bullets(state.get("pr_body", ""))
    if bullets:
        text = head + "\n" + "\n".join(f"• {_mrkdwn(b)}" for b in bullets[:MAX_BULLETS])
    else:
        summary = _first_paragraph(state.get("coder_summary", ""))
        text = f"{head} — {_mrkdwn(summary)}" if summary else head
    return text[:limit]


def _what_bullets(body: str) -> list[str]:
    inside, bullets, current = False, [], ""
    for line in body.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower() == "what"
            continue
        if not inside:
            continue
        if re.match(r"^\s*[-*]\s+", line):
            if current:
                bullets.append(current)
            current = re.sub(r"^\s*[-*]\s+", "", line).strip()
        elif current and line.strip():  # a wrapped bullet
            current += " " + line.strip()
        elif current:
            bullets.append(current)
            current = ""
    return bullets + ([current] if current else [])


def _first_paragraph(text: str, limit: int = 500) -> str:
    for block in re.split(r"\n\s*\n", text.strip()):
        flat = " ".join(block.split())
        if flat:
            return flat[:limit]
    return ""


def _mrkdwn(text: str) -> str:
    """Slack's three escapes, bold and links from markdown; code spans pass through."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
