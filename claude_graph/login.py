"""The sandbox's Claude login: where its credentials file is, and whether it still works."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def credentials_path() -> Path:
    env = os.environ.get("CLAUDE_BOX_CONFIG_HOME")
    base = Path(env) if env else Path.home() / "Library" / "Application Support" / "claude-box"
    return base / ".credentials.json"


def login_verdict(path: Path | None = None) -> str:
    """ok | missing | expired | refresh-expired | no-expiry | unreadable | api-key.
    A corrupt file is `unreadable`, never an exception: this runs on every poll.
    """
    path = path or credentials_path()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api-key"
    if not path.is_file():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("credentials are not a JSON object")
        oauth = data.get("claudeAiOauth") or {}
        if not isinstance(oauth, dict):
            raise ValueError("claudeAiOauth is not a JSON object")
        refresh = _epoch_ms(oauth.get("refreshTokenExpiresAt"))
        expires = _epoch_ms(oauth.get("expiresAt"))
    except (OSError, ValueError):  # unreadable bytes, bad JSON, wrong shapes
        return "unreadable"
    if not oauth:
        return "ok"
    now = time.time()
    if refresh and refresh < now:
        return "refresh-expired"
    if expires is None:
        return "no-expiry"
    return "expired" if expires < now else "ok"


def _epoch_ms(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"not a timestamp: {value!r}")
    return value / 1000
