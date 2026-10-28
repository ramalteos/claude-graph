import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_graph.cli import main, parser

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
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(Path(__file__).parent / "fake_gh.sh")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "ghstate"))
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
    assert "branch:     t1" in out
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
    assert by_role["coder"][by_role["coder"].index("--effort") + 1] == "xhigh"
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


def test_a_request_starting_with_a_plain_directory_name_is_not_a_repo(env, monkeypatch):
    tmp, repo = env
    (repo / "docs").mkdir()
    monkeypatch.chdir(repo)
    rc = run_cli(
        "run", "docs", "fix", "typo", "--task-id", "t5", "--engine", "fake", "--runner", FAKE
    )
    assert rc == 0
    snap = json.loads((tmp / "state" / "tasks" / "t5.json").read_text())
    assert snap["request"] == "docs fix typo" and snap["repo"] == str(repo.resolve())


def test_sigterm_closes_the_box_record_and_exits_130(env):
    tmp, repo = env
    argv = [sys.executable, "-m", "claude_graph.cli", "run", str(repo), "x", "--task-id", "tsig"]
    argv += ["--engine", "fake", "--runner", str(FAKE)]
    proc = subprocess.Popen(
        argv,
        env={**os.environ, "FAKE_SLEEP": "20"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    meta = tmp / "state" / "logs" / "tsig" / "01-pm.meta.json"
    started = tmp / "argv.headless"  # the fake records its argv before anything else
    try:
        deadline = time.time() + 15
        while not (meta.exists() and started.exists()) and time.time() < deadline:
            time.sleep(0.05)
        assert meta.exists() and started.exists()
        proc.terminate()
        _, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, err
    body = json.loads(meta.read_text())
    assert body.get("interrupted") and "finished" in body
    assert "claude-box-graph-tsig-pm-1" in (tmp / "argv.stop").read_text()
    assert not (tmp / "state" / "tasks" / "tsig.pid").exists()
    assert "claude-graph run --resume tsig" in err


def test_a_path_to_a_plain_directory_is_still_refused(env, capsys, monkeypatch):
    tmp, repo = env
    (repo / "docs").mkdir()
    monkeypatch.chdir(repo)
    rc = run_cli("run", "./docs", "fix", "typo", "--engine", "fake", "--runner", FAKE)
    assert rc == 2
    assert "not a git repo" in capsys.readouterr().err


def test_the_module_runs_as_main_like_the_dashboard_spawns_it():
    proc = subprocess.run(
        [sys.executable, "-m", "claude_graph.cli", "--help"], capture_output=True, text=True
    )
    assert proc.returncode == 0 and "usage: claude-graph" in proc.stdout


def test_unknown_engine_is_rejected_up_front(capsys):
    with pytest.raises(SystemExit) as e:
        parser().parse_args(["run", "x", "--engine", "bogus"])
    assert e.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_a_runner_that_is_not_executable_is_refused(env, capsys):
    tmp, repo = env
    dud = tmp / "dud.sh"
    dud.write_text("#!/bin/sh\n")  # mode 644: a file, not a program
    rc = run_cli("run", repo, "x", "--engine", "fake", "--runner", dud)
    assert rc == 2 and "not executable" in capsys.readouterr().err


def test_term_and_hangup_become_interrupts_but_nohup_is_respected():
    import signal

    from claude_graph import cli

    old_hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # what nohup leaves behind
    term_before = signal.getsignal(signal.SIGTERM)  # whatever the host (CI wrapper, plugin) set
    try:
        previous = cli._interrupt_on_signals()
        try:
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            term = signal.getsignal(signal.SIGTERM)
            with pytest.raises(KeyboardInterrupt):
                term(signal.SIGTERM, None)
        finally:
            cli._restore_signals(previous)
        assert signal.getsignal(signal.SIGTERM) is term_before
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        previous = cli._interrupt_on_signals()
        try:
            with pytest.raises(KeyboardInterrupt):
                signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        finally:
            cli._restore_signals(previous)
    finally:
        signal.signal(signal.SIGHUP, old_hup)


def test_a_tilde_word_for_an_unknown_user_is_request_text(env, monkeypatch):
    tmp, repo = env
    monkeypatch.chdir(repo)
    rc = run_cli(
        "run", "~50%", "faster", "please", "--task-id", "t6", "--engine", "fake", "--runner", FAKE
    )
    assert rc == 0
    snap = json.loads((tmp / "state" / "tasks" / "t6.json").read_text())
    assert snap["request"] == "~50% faster please"


def test_restoring_a_handler_python_never_saw_is_a_no_op():
    import signal

    from claude_graph import cli

    before = signal.getsignal(signal.SIGTERM)
    cli._restore_signals({signal.SIGTERM: None})
    assert signal.getsignal(signal.SIGTERM) is before


def _names(tmp):
    argv = (tmp / "argv.headless").read_text()
    return [ln for ln in argv.splitlines() if ln.startswith("graph-")]


def test_resume_after_a_failed_box_retries_that_role(env, capsys, monkeypatch):
    tmp, repo = env
    monkeypatch.setenv("FAKE_EXIT", "1")
    assert run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE) == 1
    assert "task t1: failed" in capsys.readouterr().out
    monkeypatch.delenv("FAKE_EXIT")
    rc = run_cli("run", repo, "--resume", "t1", "--engine", "fake", "--runner", FAKE)
    assert rc == 0, capsys.readouterr()
    assert "task t1: done" in capsys.readouterr().out
    assert _names(tmp)[:3] == ["graph-t1-pm-1", "graph-t1-pm-2", "graph-t1-architect-1"]


def test_resume_after_exhaustion_needs_a_higher_cap(env, capsys, monkeypatch):
    tmp, repo = env
    failing = json.dumps(
        '```json\n{"spec":"s","acceptance_criteria":[],"plan":"p","summary":"s","passed":false,"report":"r"}\n```'
    )
    monkeypatch.setenv("FAKE_RESULT", failing)
    rc = run_cli(
        "run",
        repo,
        "x",
        "--task-id",
        "t1",
        "--max-iterations",
        "1",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    assert rc == 1 and "task t1: exhausted" in capsys.readouterr().out
    rc = run_cli("run", repo, "--resume", "t1", "--engine", "fake", "--runner", FAKE)
    assert rc == 2 and "--max-iterations" in capsys.readouterr().err
    passing = json.dumps(
        '```json\n{"summary":"s","passed":true,"report":"r","verdict":"approve","notes":"n"}\n```'
    )
    monkeypatch.setenv("FAKE_RESULT", passing)
    rc = run_cli(
        "run", repo, "--resume", "t1", "--max-iterations", "2", "--engine", "fake", "--runner", FAKE
    )
    assert rc == 0, capsys.readouterr()
    snap = json.loads((tmp / "state" / "tasks" / "t1.json").read_text())
    assert snap["status"] == "done" and snap["iteration"] == 2 and snap["max_iterations"] == 2
    assert _names(tmp).count("graph-t1-coder-2") == 1


def test_a_request_starting_with_a_dash_works_after_a_double_dash(env):
    tmp, repo = env
    rc = run_cli(
        "run", "--engine", "fake", "--runner", FAKE, "--task-id", "t7", "--", repo, "-weird", "req"
    )
    assert rc == 0
    snap = json.loads((tmp / "state" / "tasks" / "t7.json").read_text())
    assert snap["request"] == "-weird req"


def test_resume_after_an_architect_failure_reruns_only_the_architect(env, capsys, monkeypatch):
    tmp, repo = env
    monkeypatch.setenv("FAKE_FAIL_ROLE", "architect")
    assert run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE) == 1
    capsys.readouterr()
    monkeypatch.delenv("FAKE_FAIL_ROLE")
    assert run_cli("run", repo, "--resume", "t1", "--engine", "fake", "--runner", FAKE) == 0
    names = _names(tmp)
    assert names.count("graph-t1-pm-1") == 1 and "graph-t1-pm-2" not in names
    assert names[:4] == [
        "graph-t1-pm-1",
        "graph-t1-architect-1",
        "graph-t1-architect-2",
        "graph-t1-coder-1",
    ]


def test_resume_of_an_interrupted_run_can_raise_the_cap(env):
    tmp, repo = env
    argv = [sys.executable, "-m", "claude_graph.cli", "run", str(repo), "x", "--task-id", "tcap"]
    argv += ["--max-iterations", "1", "--engine", "fake", "--runner", str(FAKE)]
    proc = subprocess.Popen(
        argv, env={**os.environ, "FAKE_SLEEP": "20"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.time() + 15
        while not (tmp / "argv.headless").exists() and time.time() < deadline:
            time.sleep(0.05)
        proc.terminate()
        proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130
    rc = run_cli(
        "run",
        repo,
        "--resume",
        "tcap",
        "--max-iterations",
        "4",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    assert rc == 0
    snap = json.loads((tmp / "state" / "tasks" / "tcap.json").read_text())
    assert snap["status"] == "done" and snap["max_iterations"] == 4


@pytest.mark.parametrize("bad", ["0", "-1", "21", "x"])
def test_max_iterations_outside_1_to_20_is_a_usage_error(capsys, bad):
    with pytest.raises(SystemExit) as e:
        parser().parse_args(["run", "x", "--max-iterations", bad])
    assert e.value.code == 2 and "max-iterations" in capsys.readouterr().err


def test_a_run_opens_a_draft_pr_against_the_checked_out_branch(env, capsys):
    tmp, repo = env
    rc = run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "pr:         https://github.com/o/r/pull/7" in out and "ci:         none" in out
    log = (tmp / "ghstate" / "argv.log").read_text()
    assert "pr create --draft --head t1 --base main" in log
    assert "pr ready 7" in log
    snap = json.loads((tmp / "state" / "tasks" / "t1.json").read_text())
    assert snap["base_branch"] == "main" and snap["publish"] is True


def test_no_publish_keeps_github_out_of_it(env, capsys):
    tmp, repo = env
    rc = run_cli(
        "run", repo, "x", "--task-id", "t1", "--no-publish", "--engine", "fake", "--runner", FAKE
    )
    assert rc == 0
    assert not (tmp / "ghstate" / "argv.log").exists()
    assert "pr:" not in capsys.readouterr().out


def test_resume_publish_decision():
    from claude_graph.cli import _resume_publish

    legacy = {"task_id": "t", "status": "running"}  # created before the publish step existed
    assert _resume_publish(legacy, force_on=False, force_off=False) is True
    assert _resume_publish(legacy, force_on=False, force_off=True) is False
    local = {**legacy, "publish": False}
    assert _resume_publish(local, force_on=False, force_off=False) is False
    assert _resume_publish(local, force_on=True, force_off=False) is True
    assert _resume_publish({**legacy, "publish": True}, force_on=False, force_off=False) is True


def test_resume_with_publish_opens_the_pr_for_a_local_task(env, capsys, monkeypatch):
    tmp, repo = env
    failing = json.dumps(
        '```json\n{"spec":"s","acceptance_criteria":[],"plan":"p","summary":"s","passed":false,"report":"r"}\n```'
    )
    monkeypatch.setenv("FAKE_RESULT", failing)
    rc = run_cli(
        "run",
        repo,
        "x",
        "--task-id",
        "t1",
        "--no-publish",
        "--max-iterations",
        "1",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    assert rc == 1 and not (tmp / "ghstate" / "argv.log").exists()
    passing = json.dumps(
        '```json\n{"summary":"s","passed":true,"report":"r","verdict":"approve","notes":"n"}\n```'
    )
    monkeypatch.setenv("FAKE_RESULT", passing)
    rc = run_cli(
        "run",
        repo,
        "--resume",
        "t1",
        "--publish",
        "--max-iterations",
        "2",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    assert rc == 0, capsys.readouterr()
    snap = json.loads((tmp / "state" / "tasks" / "t1.json").read_text())
    assert snap["publish"] is True and snap["base_branch"] == "main"
    assert snap["pr_url"].endswith("/pull/7") and snap["status"] == "done"
    assert "pr create --draft --head t1 --base main" in (tmp / "ghstate" / "argv.log").read_text()


def test_resume_refuses_a_task_another_orchestrator_is_driving(env, capsys):
    tmp, repo = env
    run_cli(
        "run",
        repo,
        "x",
        "--task-id",
        "t1",
        "--max-iterations",
        "1",
        "--no-publish",
        "--engine",
        "fake",
        "--runner",
        FAKE,
    )
    capsys.readouterr()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "claude_graph"])
    try:
        (tmp / "state" / "tasks" / "t1.pid").write_text(str(other.pid))
        rc = run_cli(
            "run",
            repo,
            "--resume",
            "t1",
            "--max-iterations",
            "2",
            "--engine",
            "fake",
            "--runner",
            FAKE,
        )
        err = capsys.readouterr().err
        assert rc == 2 and "already running" in err and str(other.pid) in err
    finally:
        other.kill()
        other.wait()


def test_an_orchestrator_releases_only_its_own_pid_file(tmp_path):
    from claude_graph.cli import _release_pid

    pid_file = tmp_path / "t1.pid"
    pid_file.write_text("424242")  # another orchestrator's
    _release_pid(pid_file)
    assert pid_file.read_text() == "424242"
    pid_file.write_text(str(os.getpid()))
    _release_pid(pid_file)
    assert not pid_file.exists()


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        (
            "Check partnership service and suggest me an api which can be optimized "
            "and create the api",
            "check-partnership-service-suggest",
        ),
        ("Add a /health endpoint that reports DB status", "add-health-endpoint-reports-db"),
        (
            "Fix: NPE in PartnerService.getById() — see DPT-1234",
            "fix-npe-partnerservice-getbyid",
        ),
        ("implement this https://versicorp.atlassian.net/browse/DPT-9894", "implement-dpt-9894"),
        ("   ", "task"),
        ("Ünïcödé café ñ", "unicode-cafe-n"),
    ],
)
def test_default_task_id_is_a_slug_of_the_request(text, slug):
    from claude_graph.cli import _slug
    from claude_graph.state import MAX_TASK_ID

    assert _slug(text) == slug
    assert len(_slug("x" * 200)) == MAX_TASK_ID


def test_a_repeated_request_gets_a_numbered_task_id(env, capsys):
    tmp, repo = env
    for _ in range(2):
        assert (
            run_cli(
                "run", repo, "add a route", "--no-publish", "--engine", "fake", "--runner", FAKE
            )
            == 0
        )
    ids = sorted(p.stem for p in (tmp / "state" / "tasks").glob("*.json"))
    assert ids == ["add-route", "add-route-2"]
    assert "branch:     add-route-2" in capsys.readouterr().out


def test_a_task_id_never_collides_with_an_existing_branch(env, capsys):
    tmp, repo = env
    subprocess.run(["git", "-C", str(repo), "branch", "add-route"], check=True)
    assert (
        run_cli("run", repo, "add a route", "--no-publish", "--engine", "fake", "--runner", FAKE)
        == 0
    )
    assert "branch:     add-route-2" in capsys.readouterr().out


def test_the_webhook_env_var_announces_the_approved_pr(env, capsys, monkeypatch):
    from fake_slack import webhook_server

    tmp, repo = env
    with webhook_server() as (url, received):
        monkeypatch.setenv("CLAUDE_GRAPH_SLACK_WEBHOOK", url)
        rc = run_cli("run", repo, "x", "--task-id", "t1", "--engine", "fake", "--runner", FAKE)
    assert rc == 0
    assert len(received) == 1 and "pull/7" in received[0]["text"]
    assert "slack:      posted" in capsys.readouterr().out
