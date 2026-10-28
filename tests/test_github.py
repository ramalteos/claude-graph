import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_graph.github import GhGitHub, PublishError

FAKE_GH = Path(__file__).parent / "fake_gh.sh"


@pytest.fixture
def world(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(FAKE_GH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "ghstate"))
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(bare)], check=True)
    subprocess.run([*git, "branch", "claude-graph/t1"], check=True)
    return repo, bare, tmp_path


def argv_log(tmp_path):
    return (tmp_path / "ghstate" / "argv.log").read_text().splitlines()


def test_push_publishes_the_branch_to_origin(world):
    repo, bare, _ = world
    GhGitHub().push(repo, "claude-graph/t1")
    out = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "refs/heads/claude-graph/t1"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0


def test_push_failure_is_a_publish_error(world, tmp_path):
    repo, _, _ = world
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "origin", str(tmp_path / "nope")], check=True
    )
    with pytest.raises(PublishError, match="nope"):
        GhGitHub().push(repo, "claude-graph/t1")


def test_ensure_pr_creates_a_draft_once(world):
    repo, _, tmp = world
    gh = GhGitHub()
    pr = gh.ensure_pr(repo, "claude-graph/t1", "main", "add x", "body")
    assert pr.number == 7 and pr.url.endswith("/pull/7")
    again = gh.ensure_pr(repo, "claude-graph/t1", "main", "add x", "body")
    assert again == pr
    creates = [a for a in argv_log(tmp) if a.startswith("pr create")]
    assert len(creates) == 1
    assert (
        "--draft" in creates[0]
        and "--base main" in creates[0]
        and "--head claude-graph/t1" in creates[0]
    )


def test_wait_checks_reports_none_fail_pass_and_timeout(world, monkeypatch):
    repo, _, tmp = world
    gh = GhGitHub()
    assert gh.wait_checks(repo, 7, timeout_s=5).status == "none"
    checks = tmp / "checks.json"
    checks.write_text(
        json.dumps(
            [
                {"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "https://ci/1"},
                {"name": "e2e", "state": "FAILURE", "bucket": "fail", "link": "https://ci/2"},
            ]
        )
    )
    monkeypatch.setenv("FAKE_GH_CHECKS", str(checks))
    c = gh.wait_checks(repo, 7, timeout_s=5)
    assert (
        c.status == "fail" and "e2e: fail https://ci/2" in c.summary and "unit: pass" in c.summary
    )
    checks.write_text(
        json.dumps([{"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "https://ci/1"}])
    )
    assert gh.wait_checks(repo, 7, timeout_s=5).status == "pass"
    monkeypatch.setenv("FAKE_GH_SLEEP", "5")
    c = gh.wait_checks(repo, 7, timeout_s=1)
    assert c.status == "timeout" and "1 s" in c.summary


def test_mark_ready_calls_gh(world):
    repo, _, tmp = world
    GhGitHub().mark_ready(repo, 7)
    assert "pr ready 7" in argv_log(tmp)


def test_update_pr_edits_title_and_body(world):
    repo, _, tmp = world
    GhGitHub().update_pr(repo, 7, "feat(x): add y", "## What")
    assert any(a.startswith("pr edit 7 --title feat(x): add y --body") for a in argv_log(tmp))
