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
    titles: list[str] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    rename_ok: bool = True
    labels: list[tuple[int, str]] = field(default_factory=list)
    fail_label: bool = False

    def push(self, repo: Path, branch: str) -> None:
        self.calls.append("push")
        self.pushed.append(branch)
        if self.fail_push:
            raise PublishError("push rejected: no remote")

    def ensure_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> PullRequest:
        self.calls.append("ensure_pr")
        self.titles.append(title)
        self.bodies.append(body)
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

    def rename_branch(self, repo: Path, old: str, new: str) -> bool:
        self.calls.append("rename_branch")
        self.renamed.append((old, new))
        return self.rename_ok

    def update_pr(self, repo: Path, number: int, title: str, body: str) -> None:
        self.calls.append("update_pr")
        self.titles.append(title)
        self.bodies.append(body)

    def label_risk(self, repo: Path, number: int, risk: str) -> None:
        self.calls.append("label_risk")
        if self.fail_label:
            raise PublishError("label: HTTP 403")
        self.labels.append((number, risk))
