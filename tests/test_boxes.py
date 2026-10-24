import json
import subprocess
from pathlib import Path

import pytest

from claude_graph.boxes import ClaudeBoxRunner

FAKE = Path(__file__).parent / "fake_claude_box.sh"
SANDBOX_RUNNER = Path(__file__).resolve().parents[1] / "sandbox" / "claude-box"


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


def test_ctrl_c_stops_the_box_and_closes_its_record(tmp_path, argv_file, monkeypatch):
    import subprocess as sp

    from claude_graph import boxes as mod

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    real_run = sp.run
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, **kw: real_run(argv, **kw) if argv[1] == "stop" else interrupted(),
    )
    with pytest.raises(KeyboardInterrupt):
        run_coder(tmp_path)
    stop = calls(tmp_path / "argv.stop")[0]
    assert stop[2:] == ["stop", "claude-box-graph-t1-coder-1"]
    meta = json.loads((tmp_path / "logs" / "t1" / "01-coder.meta.json").read_text())
    assert meta["exit_code"] == 130 and meta["interrupted"] is True and "finished" in meta


def test_a_runner_that_cannot_start_is_a_failed_box_not_a_crash(tmp_path):
    dud = tmp_path / "dud.sh"
    dud.write_text("#!/bin/sh\n")  # no exec bit: Popen raises PermissionError
    r = ClaudeBoxRunner(claude_box=dud, engine="fake", log_dir=tmp_path / "logs").run(
        role="coder",
        task_id="t1",
        attempt=1,
        repo=tmp_path,
        system_prompt="S",
        prompt="P",
        model="opus",
        effort="high",
    )
    assert r.exit_code == 127 and r.result_text is None
    assert "dud.sh" in r.log_path.read_text()
    meta = json.loads((tmp_path / "logs" / "t1" / "01-coder.meta.json").read_text())
    assert "finished" in meta and meta["exit_code"] == 127


@pytest.mark.skipif(not SANDBOX_RUNNER.is_file(), reason="sandbox submodule not checked out")
def test_contract_with_the_real_runner_under_its_fake_engine(tmp_path, monkeypatch):
    """Pins the runner's `headless` output contract against the real script (fake engine)."""
    root = tmp_path / "root"
    repo = root / "demo"
    repo.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    for var, sub in {
        "HOME": "home",
        "CLAUDE_BOX_ROOT": "root",
        "CLAUDE_BOX_STATE": "state",
        "CLAUDE_BOX_CONFIG_HOME": "config",
        "CLAUDE_BOX_FAKE_STATE": "fake",
    }.items():
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(var, str(tmp_path / sub))
    (tmp_path / "config" / ".credentials.json").write_text('{"fake": true}\n')
    runner = ClaudeBoxRunner(claude_box=SANDBOX_RUNNER, engine="fake", log_dir=tmp_path / "logs")
    r = runner.run(
        role="coder",
        task_id="ct1",
        attempt=1,
        repo=repo,
        system_prompt="SYS-MARKER",
        prompt="PROMPT-MARKER",
        model="opus",
        effort="high",
    )
    assert r.exit_code == 0, r.log_path.read_text()
    assert r.branch == "claude-graph/ct1" and r.base_sha == head
    assert r.worktree is not None
    assert r.worktree.resolve() == (root / ".claude-box-runs" / "ct1").resolve()
    assert r.box == "claude-box-graph-ct1-coder-1"
    argv = (tmp_path / "fake" / "argv.log").read_text()
    assert "claude-box-graph-ct1-coder-1" in argv and "PROMPT-MARKER" in argv
    assert "SYS-MARKER" in argv and "opus" in argv
    runner.remove_worktree(task_id="ct1", repo=repo)  # the removal path exits 0 too


def test_stop_survives_a_vanished_runner(tmp_path):
    runner = ClaudeBoxRunner(claude_box=tmp_path / "gone", engine="fake", log_dir=tmp_path)
    runner._stop("claude-box-graph-t1-coder-1")  # must not raise: it runs inside Ctrl-C handling


def test_read_log_keeps_a_result_whose_text_holds_a_unicode_line_separator(tmp_path):
    from claude_graph.boxes import read_log

    log = tmp_path / "x.log"
    event = {"type": "result", "result": "para one\u2028para two", "total_cost_usd": 0.5}
    log.write_text(
        "box=claude-box-graph-t1-coder-1\n" + json.dumps(event, ensure_ascii=False) + "\n"
    )
    r = read_log(log, exit_code=0, duration_s=1.0)
    assert r.result_text == "para one\u2028para two" and r.cost_usd == 0.5


def test_remove_worktree_surfaces_the_runner_error(tmp_path):
    stub = tmp_path / "stub"
    stub.write_text("#!/bin/sh\necho 'worktree dir still present' >&2\nexit 1\n")
    stub.chmod(0o755)
    runner = ClaudeBoxRunner(claude_box=stub, engine="fake", log_dir=tmp_path)
    with pytest.raises(RuntimeError, match="worktree dir still present"):
        runner.remove_worktree(task_id="t1", repo=tmp_path)
