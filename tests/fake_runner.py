"""A BoxRunner that replays scripted results per role and records every call."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_graph.boxes import BoxResult


def report(payload: dict[str, Any], prefix: str = "Done.\n") -> str:
    return f"{prefix}```json\n{json.dumps(payload)}\n```"


def result(
    text: str | None, *, exit_code: int = 0, is_error: bool = False, meta: bool = True
) -> BoxResult:
    return BoxResult(
        exit_code=exit_code,
        result_text=text,
        is_error=is_error,
        duration_s=1.5,
        cost_usd=0.01,
        log_path=Path("/tmp/fake.log"),
        box="claude-box-graph-t1-x-1",
        worktree=Path("/tmp/wt/t1") if meta else None,
        branch="claude-graph/t1" if meta else None,
        base_sha="abc123" if meta else None,
    )


@dataclass
class FakeRunner:
    scripts: dict[str, list[BoxResult]]
    calls: list[dict[str, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def run(self, **kw: Any) -> BoxResult:
        self.calls.append(kw)
        queue = self.scripts.get(kw["role"], [])
        if not queue:
            raise AssertionError(f"no scripted result left for {kw['role']}")
        return queue.pop(0)

    def remove_worktree(self, *, task_id: str, repo: Path) -> None:
        self.removed.append(task_id)

    def roles(self) -> list[str]:
        return [c["role"] for c in self.calls]
