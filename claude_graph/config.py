"""Where claude-graph keeps state, and the per-role model/effort settings a repo can set."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENGINE = "container"  # Apple `container`; CLAUDE_BOX_ENGINE semantics

DEFAULT_MODELS = {role: "opus" for role in ("pm", "architect", "coder", "qa", "reviewer")}
DEFAULT_EFFORT = "xhigh"


@dataclass(frozen=True)
class Settings:
    models: dict[str, str] = field(default_factory=dict)
    efforts: dict[str, str] = field(default_factory=dict)
    slack_channel: str = ""  # where the reviewer announces an approved PR; empty = nowhere

    def model(self, role: str) -> str:
        return self.models.get(role, DEFAULT_MODELS[role])

    def effort(self, role: str) -> str:
        return self.efforts.get(role, DEFAULT_EFFORT)


def state_dir() -> Path:
    env = os.environ.get("CLAUDE_GRAPH_STATE")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "claude-graph"


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def find_claude_box() -> Path | None:
    """First match wins: $CLAUDE_GRAPH_RUNNER; `abox`/`claude-box` on PATH; the `sandbox/`
    submodule; a sibling clone. The engine is passed per call, so any of them works.
    """
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
    Shape: `[models] coder = "sonnet"`, `[efforts] qa = "low"`, `[slack] channel = "..."`.
    """
    for path in (repo / "claude-graph.toml", Path.home() / ".config/claude-graph/config.toml"):
        if path.is_file():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return Settings(
                models=dict(data.get("models", {})),
                efforts=dict(data.get("efforts", {})),
                slack_channel=str(data.get("slack", {}).get("channel", "")).lstrip("#"),
            )
    return Settings()
