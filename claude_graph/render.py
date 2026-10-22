"""Turn a box's stream-json log into lines a person can read."""

from __future__ import annotations

import json
from typing import Any

_INPUT_KEYS = ("command", "file_path", "pattern", "path", "description", "prompt", "url")


def render_log(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("{"):
            try:
                lines.extend(render_event(json.loads(raw)))
            except json.JSONDecodeError:
                continue
        elif raw.strip():
            lines.append(f"· {raw}")
    return lines


def render_event(event: dict[str, Any]) -> list[str]:
    kind = event.get("type")
    if kind == "system":
        if event.get("subtype") not in (None, "init"):
            return []  # thinking_tokens and friends: progress noise, not content
        return [f"· session {event.get('session_id', '?')} · model {event.get('model', '?')}"]
    if kind == "assistant":
        return [line for block in _blocks(event) for line in _assistant_block(block)]
    if kind == "user":
        return [line for block in _blocks(event) for line in _tool_result(block)]
    if kind == "result":
        cost = event.get("total_cost_usd")
        head = f"■ {event.get('subtype', 'result')} · {event.get('num_turns', '?')} turns"
        if cost is not None:
            head += f" · ${cost:.4f}"
        text = event.get("result") or ""
        return [head, *text.splitlines()]
    return []


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _assistant_block(block: dict[str, Any]) -> list[str]:
    if block.get("type") == "text":
        return str(block.get("text", "")).splitlines()
    if block.get("type") == "tool_use":
        inp = block.get("input") or {}
        detail = next((str(inp[k]) for k in _INPUT_KEYS if k in inp), "")
        return [f"▶ {block.get('name', '?')} {_short(detail, 160)}".rstrip()]
    return []


def _tool_result(block: dict[str, Any]) -> list[str]:
    if block.get("type") != "tool_result":
        return []
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(_block_text(c) for c in content if isinstance(c, dict))
    lines = str(content or "").strip().splitlines()
    text = _short(lines[0] if lines else "", 160)
    return [f"✗ tool error: {text}"] if block.get("is_error") else [f"  ↳ {text}"]


def _block_text(block: dict[str, Any]) -> str:
    if block.get("type") == "tool_reference":
        return f"[tool {block.get('tool_name', '?')}]"
    return str(block.get("text", ""))


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
