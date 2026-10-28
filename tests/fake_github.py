"""A GitHub seam that records calls and answers from a script."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from claude_graph.github import Checks, PublishError, PullRequest


@dataclass
class FakeGitHub:
    checks: tuple[str, str] = ("pass", "build: pass https://ci/1")
    fail_push: bool = False
    calls: list[str] = field(default_factory=list)
    prs: dict[str, PullRequest] = field(default_factory=dict)

    def push(self, repo: Path, branch: str) -> None:
        self.calls.append("push")
        if self.fail_push:
            raise PublishError("push rejected: no remote")

    def ensure_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> PullRequest:
        self.calls.append("ensure_pr")
        if branch not in self.prs:
            self.prs[branch] = PullRequest(
                url=f"https://github.com/o/r/pull/{len(self.prs) + 7}", number=len(self.prs) + 7
            )
        return self.prs[branch]

    def wait_checks(self, repo: Path, number: int, timeout_s: int) -> Checks:
        self.calls.append("wait_checks")
        return Checks(status=self.checks[0], summary=self.checks[1])

    def mark_ready(self, repo: Path, number: int) -> None:
        self.calls.append("mark_ready")
