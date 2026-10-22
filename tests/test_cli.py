import json
import subprocess
from pathlib import Path

import pytest

from claude_graph.cli import main

FAKE = Path(__file__).parent / "fake_claude_box.sh"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_GRAPH_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv"))
    monkeypatch.setenv(
        "FAKE_RESULT",
        json.dumps(
            'ok\n```json\n{"plan":"p","summary":"s","passed":true,"report":"r",'
            '"verdict":"approve","notes":"n"}\n```'
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return tmp_path, repo


def run_cli(*args):
    return main([str(a) for a in args])


def test_rejects_task_ids_with_path_characters(env, capsys):
    tmp, repo = env
    rc = run_cli("run", repo, "do it", "--task-id", "../x", "--engine", "fake", "--runner", FAKE)
    assert rc == 2
    assert "task id must match" in capsys.readouterr().err
    assert not (tmp / "argv.headless").exists()


def test_rejects_a_non_repo(env, capsys):
    tmp, _ = env
    rc = run_cli("run", tmp, "do it", "--engine", "fake", "--runner", FAKE)
    assert rc == 2
    assert "not a git repo" in capsys.readouterr().err


def test_end_to_end_with_the_fake_runner(env, capsys):
    tmp, repo = env
    rc = run_cli(
        "run",
        repo,
        "add a health route",
        "--task-id",
        "t1",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "task t1: done" in out
    assert "branch:     claude-graph/t1" in out
    assert (tmp / "state" / "tasks" / "t1.json").is_file()
    logs = sorted(p.name for p in (tmp / "state" / "logs" / "t1").glob("*.log"))
    assert logs == ["01-architect.log", "01-coder.log", "01-pm.log", "01-qa.log", "01-reviewer.log"]
    argv = (tmp / "argv.headless").read_text()
    assert "ENGINE=fake" in argv


def test_resume_of_a_finished_task_launches_nothing(env, capsys):
    tmp, repo = env
    run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    (tmp / "argv.headless").unlink()
    rc = run_cli("run", repo, "--resume", "t1", "--engine", "fake", "--runner", FAKE)
    assert rc == 0
    assert "task t1: done" in capsys.readouterr().out
    assert not (tmp / "argv.headless").exists()


def test_duplicate_task_id_is_refused(env, capsys):
    tmp, repo = env
    run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    rc = run_cli("run", repo, "again", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    assert rc == 2
    assert "already exists" in capsys.readouterr().err


def test_resume_unknown_task(env, capsys):
    _, repo = env
    rc = run_cli("run", repo, "--resume", "nope", "--engine", "fake", "--runner", FAKE)
    assert rc == 2
    assert "no such task" in capsys.readouterr().err


def test_status_and_logs(env, capsys):
    _, repo = env
    run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    capsys.readouterr()
    assert run_cli("status", "t1") == 0
    assert "task t1: done" in capsys.readouterr().out
    assert run_cli("logs", "t1") == 0
    assert "01-qa.log" in capsys.readouterr().out
    assert run_cli("logs", "t1", "qa") == 0
    assert '"type":"result"' in capsys.readouterr().out
    assert run_cli("logs", "t1", "devops") == 2


def test_failed_run_exits_one(env, capsys, monkeypatch):
    _, repo = env
    monkeypatch.setenv("FAKE_EXIT", "1")
    rc = run_cli("run", repo, "x", "--task-id", "t2", "--engine", "fake", "--runner", FAKE)
    assert rc == 1
    assert "task t2: failed" in capsys.readouterr().out


def test_settings_from_repo_toml(env):
    tmp, repo = env
    (repo / "claude-graph.toml").write_text('[models]\ncoder = "sonnet"\n[efforts]\nqa = "low"\n')
    run_cli("run", repo, "x", "--task-id", "t3", "--engine", "fake", "--runner", FAKE)
    calls = [c.splitlines() for c in (tmp / "argv.headless").read_text().split("===\n") if c]
    by_role = {c[c.index("--name") + 1].split("-")[2]: c for c in calls}
    assert by_role["coder"][by_role["coder"].index("--model") + 1] == "sonnet"
    assert by_role["coder"][by_role["coder"].index("--effort") + 1] == "high"
    assert by_role["qa"][by_role["qa"].index("--model") + 1] == "opus"
    assert by_role["qa"][by_role["qa"].index("--effort") + 1] == "low"


def test_snapshot_carries_updated_at_and_ui_parser(env):
    tmp, repo = env
    run_cli("run", repo, "x", "--task-id", "t9", "--engine", "fake", "--runner", FAKE)
    snap = json.loads((tmp / "state" / "tasks" / "t9.json").read_text())
    assert snap["status"] == "done" and "updated_at" in snap
    from claude_graph.cli import parser

    args = parser().parse_args(["ui", "--port", "0"])
    assert args.port == 0 and args.func.__name__ == "cmd_ui"


def test_run_from_inside_the_repo_needs_no_path(env, capsys, monkeypatch):
    tmp, repo = env
    monkeypatch.chdir(repo)
    rc = run_cli(
        "run", "add", "a", "route", "--task-id", "t4", "--engine", "fake", "--runner", FAKE
    )
    assert rc == 0
    snap = json.loads((tmp / "state" / "tasks" / "t4.json").read_text())
    assert snap["request"] == "add a route" and snap["repo"] == str(repo.resolve())


def test_run_outside_a_repo_without_a_path_is_refused(env, capsys, monkeypatch):
    tmp, _ = env
    monkeypatch.chdir(tmp)
    assert run_cli("run", "do it", "--engine", "fake", "--runner", FAKE) == 2
    assert "not a git repo" in capsys.readouterr().err
