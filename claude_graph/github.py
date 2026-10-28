"""Host-side GitHub seam: push the task branch, keep one draft PR per branch, read its checks."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

CiStatus = Literal["pass", "fail", "none", "timeout"]
# risk:<level> labels, created in a repo on first use. Colours as in the Alteos monorepo.
RISK_LABELS = {
    "low": ("0e8a16", "small, reversible, no behaviour change beyond the request"),
    "medium": ("fbca04", "a behaviour change on an existing path; rolled back by a revert"),
    "high": ("b60205", "schema, auth, money, data deletion, infra or shared code"),
}


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int


@dataclass(frozen=True)
class Checks:
    status: CiStatus
    summary: str


class GitHub(Protocol):
    def push(self, repo: Path, branch: str) -> None: ...

    def ensure_pr(
        self, repo: Path, branch: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    def wait_checks(self, repo: Path, number: int, timeout_s: int) -> Checks: ...

    def mark_ready(self, repo: Path, number: int) -> None: ...

    def update_pr(self, repo: Path, number: int, title: str, body: str) -> None: ...

    def rename_branch(self, repo: Path, old: str, new: str) -> bool: ...

    def label_risk(self, repo: Path, number: int, risk: str) -> None: ...


@dataclass(frozen=True)
class GhGitHub:
    """`git push` plus the `gh` CLI, run on the host with the host's login."""

    gh: str = "gh"

    def push(self, repo: Path, branch: str) -> None:
        _run(["git", "-C", str(repo), "push", "-u", "origin", branch], timeout=300)

    def ensure_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> PullRequest:
        listed = _run(
            [
                self.gh,
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url,number",
                "--limit",
                "1",
            ],
            cwd=repo,
            timeout=60,
        )
        found = json.loads(listed or "[]")
        if found:
            return PullRequest(url=found[0]["url"], number=int(found[0]["number"]))
        out = _run(
            [
                self.gh,
                "pr",
                "create",
                "--draft",
                "--head",
                branch,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repo,
            timeout=120,
        )
        url = out.strip().splitlines()[-1]
        return PullRequest(url=url, number=int(url.rstrip("/").rsplit("/", 1)[1]))

    def wait_checks(self, repo: Path, number: int, timeout_s: int) -> Checks:
        try:
            subprocess.run(
                [self.gh, "pr", "checks", str(number), "--watch"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Checks("timeout", f"CI still running after {timeout_s} s on PR #{number}")
        except OSError as e:
            raise PublishError(f"{self.gh}: {e}") from None
        proc = subprocess.run(
            [self.gh, "pr", "checks", str(number), "--json", "name,state,bucket,link"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        try:
            checks = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            checks = []
        if not checks:
            return Checks("none", "no CI checks reported on the pull request")
        lines = [
            f"{c.get('name')}: {c.get('bucket') or c.get('state')} {c.get('link') or ''}".rstrip()
            for c in checks
        ]
        buckets = {c.get("bucket") for c in checks}
        if buckets & {"fail", "cancel"}:
            status: CiStatus = "fail"
        elif buckets <= {"pass", "skipping"}:
            status = "pass"
        else:
            status = "timeout"  # something still pending when the watch ended
        return Checks(status, "\n".join(lines))

    def rename_branch(self, repo: Path, old: str, new: str) -> bool:
        """`git branch -m`; False when the name is taken or the old branch is missing."""
        proc = subprocess.run(
            ["git", "-C", str(repo), "branch", "-m", old, new],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0

    def update_pr(self, repo: Path, number: int, title: str, body: str) -> None:
        _run(
            [self.gh, "pr", "edit", str(number), "--title", title, "--body", body],
            cwd=repo,
            timeout=60,
        )

    def label_risk(self, repo: Path, number: int, risk: str) -> None:
        """`risk:<level>` on the PR and the other levels off; missing labels are created."""
        listed = _run(
            [self.gh, "label", "list", "--json", "name", "--limit", "200"], cwd=repo, timeout=60
        )
        have = {label["name"] for label in json.loads(listed or "[]")}
        for level, (color, description) in RISK_LABELS.items():
            if f"risk:{level}" not in have:
                _run(
                    [self.gh, "label", "create", f"risk:{level}", "-c", color, "-d", description],
                    cwd=repo,
                    timeout=60,
                )
        argv = [self.gh, "pr", "edit", str(number), "--add-label", f"risk:{risk}"]
        for level in RISK_LABELS:
            if level != risk:
                argv += ["--remove-label", f"risk:{level}"]
        _run(argv, cwd=repo, timeout=60)

    def mark_ready(self, repo: Path, number: int) -> None:
        subprocess.run(
            [self.gh, "pr", "ready", str(number)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int) -> str:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PublishError(f"{' '.join(argv[:3])}: {e}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise PublishError(detail or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout
