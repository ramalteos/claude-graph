"""Where claude-graph keeps state, and the per-role model/effort settings a repo can set."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from claude_graph.graph import Settings

DEFAULT_ENGINE = "container"  # Apple `container`; CLAUDE_BOX_ENGINE semantics


def state_dir() -> Path:
    env = os.environ.get("CLAUDE_GRAPH_STATE")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "claude-graph"


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def find_claude_box() -> Path | None:
    """The sandbox runner, first match wins: $CLAUDE_GRAPH_RUNNER; `abox` or `claude-box` on
    PATH; the `sandbox/` submodule of this repo; a sibling clone next to this repo. The engine
    is passed explicitly on every call, so any of them works."""
    env = os.environ.get("CLAUDE_GRAPH_RUNNER")
    if env:
        return Path(env).expanduser()
    for name in ("abox", "claude-box"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(d) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    for repo in (_PACKAGE_ROOT / "sandbox", _PACKAGE_ROOT.parent / "claude-box"):
        for name in ("abox", "claude-box"):
            if (repo / name).is_file():
                return repo / name
    return None


def load_settings(repo: Path) -> Settings:
    """`<repo>/claude-graph.toml`, else `~/.config/claude-graph/config.toml`, else defaults.

    Shape: `[models] coder = "sonnet"` and `[efforts] qa = "low"`, keyed by role.
    """
    for path in (repo / "claude-graph.toml", Path.home() / ".config/claude-graph/config.toml"):
        if path.is_file():
            data = tomllib.loads(path.read_text())
            return Settings(
                models=dict(data.get("models", {})), efforts=dict(data.get("efforts", {}))
            )
    return Settings()
