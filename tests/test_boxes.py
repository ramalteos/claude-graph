import json
from pathlib import Path

import pytest

from claude_graph.boxes import ClaudeBoxRunner

FAKE = Path(__file__).parent / "fake_claude_box.sh"


@pytest.fixture
def argv_file(tmp_path, monkeypatch):
    path = tmp_path / "argv"
    monkeypatch.setenv("FAKE_ARGV", str(path))
    return path


def runner(tmp_path, **kw) -> ClaudeBoxRunner:
    return ClaudeBoxRunner(claude_box=FAKE, engine="fake", log_dir=tmp_path / "logs", **kw)


def run_coder(tmp_path, **kw):
    return runner(tmp_path, **kw).run(
        role="coder",
        task_id="t1",
        attempt=1,
        repo=tmp_path,
        system_prompt="SYS",
        prompt="PROMPT\nline 2",
        model="opus",
        effort="high",
    )


def calls(path: Path) -> list[list[str]]:
    return [c.splitlines() for c in path.read_text().split("===\n") if c]


def after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_argv_and_result(tmp_path, argv_file, monkeypatch):
    monkeypatch.setenv("FAKE_RESULT", json.dumps('done\n```json\n{"summary": "x"}\n```'))
    r = run_coder(tmp_path)
    argv = calls(tmp_path / "argv.headless")[0]
    assert argv[0] == "ENGINE=fake"
    assert argv[1] == "STDIN=eof"
    assert argv[2:4] == ["headless", str(tmp_path)]
    assert after(argv, "--name") == "graph-t1-coder-1"
    assert after(argv, "--worktree") == "t1"
    assert after(argv, "--branch") == "claude-graph/t1"
    assert after(argv, "--model") == "opus"
    assert after(argv, "--effort") == "high"
    assert Path(after(argv, "--prompt-file")).read_text() == "PROMPT\nline 2"
    assert Path(after(argv, "--system-prompt-file")).read_text() == "SYS"
    assert r.exit_code == 0
    assert r.result_text == 'done\n```json\n{"summary": "x"}\n```'
    assert r.is_error is False
    assert r.cost_usd == 0.0123
    assert (r.worktree, r.branch, r.base_sha) == (Path("/tmp/wt/t1"), "claude-graph/t1", "abc123")
    assert r.box == "claude-box-graph-t1-coder-1"
    assert r.log_path == tmp_path / "logs" / "t1" / "01-coder.log"
    assert "a note on stderr" in r.log_path.read_text()


def test_no_result_event_means_no_text(tmp_path, argv_file):
    r = run_coder(tmp_path)
    assert r.exit_code == 0
    assert r.result_text is None
    assert r.cost_usd is None


def test_exit_code_propagates(tmp_path, argv_file, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "3")
    assert run_coder(tmp_path).exit_code == 3


def test_timeout_stops_the_box(tmp_path, argv_file, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    r = run_coder(tmp_path, timeout_s=1)
    assert r.exit_code == 124
    assert r.result_text is None
    stop = calls(tmp_path / "argv.stop")[0]
    assert stop[2:] == ["stop", "claude-box-graph-t1-coder-1"]


def test_remove_worktree(tmp_path, argv_file):
    runner(tmp_path).remove_worktree(task_id="t1", repo=tmp_path)
    argv = calls(tmp_path / "argv.headless")[0]
    assert argv[2:] == ["headless", str(tmp_path), "--worktree", "t1", "--remove"]


def test_meta_sidecar_is_written_before_and_after(tmp_path, argv_file, monkeypatch):
    monkeypatch.setenv("FAKE_RESULT", json.dumps("x"))
    r = run_coder(tmp_path)
    meta = json.loads(r.log_path.with_suffix(".meta.json").read_text())
    assert meta["role"] == "coder" and meta["attempt"] == 1
    assert meta["box"] == "claude-box-graph-t1-coder-1"
    assert meta["model"] == "opus" and meta["log"] == "01-coder.log"
    assert meta["started"] <= meta["finished"]
    assert meta["exit_code"] == 0 and meta["cost_usd"] == 0.0123
