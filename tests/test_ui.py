import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_graph.ui.server import UiContext, serve

FAKE_BOX = Path(__file__).parent / "fake_claude_box.sh"
QA_LOG = (
    "worktree=/tmp/wt/t1\nbox=claude-box-graph-t1-qa-1\n"
    + json.dumps({"type": "system", "session_id": "s1", "model": "opus"})
    + "\n"
    + json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}]
            },
        }
    )
    + "\n"
    + json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 3,
            "total_cost_usd": 0.02,
            "result": "ok",
        }
    )
    + "\n"
)


def make_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env=env)
    base = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(path), "checkout", "-qb", "claude-graph/t1"], check=True)
    (path / "a.txt").write_text("one\ntwo\n")
    subprocess.run(["git", "-C", str(path), "commit", "-qam", "add two"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "main"], check=True)
    return base


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BOX_CONFIG_HOME", str(tmp_path / "nocreds"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_ARGV", str(tmp_path / "argv"))
    state = tmp_path / "state"
    (state / "tasks").mkdir(parents=True)
    (state / "logs" / "t1").mkdir(parents=True)
    base = make_repo(tmp_path / "repo")
    (state / "tasks" / "t1.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "status": "running",
                "request": "add route",
                "repo": str(tmp_path / "repo"),
                "branch": "claude-graph/t1",
                "base_sha": base,
                "worktree": str(tmp_path / "wt"),
                "iteration": 1,
                "max_iterations": 3,
                "updated_at": "2026-10-21T10:00:00+00:00",
                "steps": [
                    {
                        "role": "pm",
                        "attempt": 1,
                        "parsed_ok": True,
                        "cost_usd": 0.1,
                        "duration_s": 30,
                        "exit_code": 0,
                        "box": "claude-box-graph-t1-pm-1",
                        "log": "x",
                    }
                ],
            }
        )
    )
    (state / "logs" / "t1" / "01-pm.meta.json").write_text(
        json.dumps(
            {
                "role": "pm",
                "attempt": 1,
                "box": "claude-box-graph-t1-pm-1",
                "model": "fable",
                "started": (datetime.now(UTC) - timedelta(seconds=300)).isoformat(
                    timespec="seconds"
                ),
                "finished": (datetime.now(UTC) - timedelta(seconds=270)).isoformat(
                    timespec="seconds"
                ),
                "exit_code": 0,
                "duration_s": 30,
                "cost_usd": 0.1,
                "log": "01-pm.log",
            }
        )
    )
    (state / "logs" / "t1" / "01-qa.meta.json").write_text(
        json.dumps(
            {
                "role": "qa",
                "attempt": 1,
                "box": "claude-box-graph-t1-qa-1",
                "model": "opus",
                "started": (datetime.now(UTC) - timedelta(seconds=120)).isoformat(
                    timespec="seconds"
                ),
                "log": "01-qa.log",
            }
        )
    )
    (state / "logs" / "t1" / "01-qa.log").write_text(QA_LOG)
    return state, tmp_path


@pytest.fixture
def alive(world):
    """An orchestrator that is alive: a sleeping child whose pid is on record for t1."""
    state, _ = world
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "claude_graph"])
    (state / "tasks" / "t1.pid").write_text(str(proc.pid))
    yield proc
    proc.kill()
    proc.wait()


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, "timed out waiting"
        time.sleep(0.02)


def start(ctx):
    server = serve(ctx, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def ui(world):
    state, tmp = world
    server, base = start(UiContext(state_dir=state, claude_box=None, engine="fake"))
    yield base, state, tmp
    server.shutdown()
    server.server_close()


@pytest.fixture
def ui_actions(world):
    state, tmp = world
    fake_runner = tmp / "fake_runner.sh"
    fake_runner.write_text(
        "#!/usr/bin/env bash\n"
        # write-then-rename: a test polling for the file never reads it half-written
        'printf "%s\\n" "$@" > "$FAKE_RUNNER_OUT.tmp"\n'
        'mv "$FAKE_RUNNER_OUT.tmp" "$FAKE_RUNNER_OUT"\n'
        "sleep 0.2\n"
    )
    fake_runner.chmod(0o755)
    os.environ["FAKE_RUNNER_OUT"] = str(tmp / "runner.argv")
    ctx = UiContext(
        state_dir=state,
        claude_box=FAKE_BOX,
        engine="fake",
        actions=True,
        runner_argv=[str(fake_runner)],
    )
    server, base = start(ctx)
    yield base, state, tmp, ctx
    server.shutdown()
    server.server_close()


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()


def status_of(url, host=None):
    try:
        return get(url, host)[0]
    except urllib.error.HTTPError as e:
        return e.code


def post(base, action, arg, token):
    req = urllib.request.Request(
        base + "/api/action",
        method="POST",
        data=json.dumps({"action": action, "arg": arg}).encode(),
        headers={"Content-Type": "application/json", "X-CG-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- reads ---------------------------------------------------------------------------


def test_index_without_actions_has_no_token(ui):
    base, _, _ = ui
    status, body = get(base + "/")
    assert status == 200 and "<title>claude-graph</title>" in body
    assert 'ACTION_TOKEN = ""' in body


def test_state_has_every_section(ui, alive):
    base, _, tmp = ui
    s = json.loads(get(base + "/api/state")[1])
    assert s["api_version"] == 1 and s["engine"] == "fake"
    assert s["login"] == "missing"
    t = s["tasks"][0]
    assert t["task_id"] == "t1" and t["status"] == "running"
    assert t["running"]["role"] == "qa" and t["running"]["box"] == "claude-box-graph-t1-qa-1"
    assert t["cost"] == 0.1 and t["progress"] == {
        "commits": 1,
        "shortstat": "1 file changed, 1 insertion(+)",
        "branch_exists": True,
    }
    assert "qa is in a box" in t["next_action"]
    assert t["pid"] == alive.pid
    assert [b["role"] for b in s["boxes"]] == ["qa"]
    assert s["boxes"][0]["elapsed_s"] > 0
    assert s["attention"][0]["kind"] == "login"
    assert [e["role"] for e in s["timeline"]] == ["qa", "pm"]
    stats = {r["role"]: r for r in s["role_stats"]}
    assert stats["pm"]["runs"] == 1 and stats["pm"]["avg_s"] == 30 and stats["qa"]["runs"] == 0
    assert s["spend"]["total"] == 0.1
    assert s["logs"][0]["name"] == "01-qa.log"
    assert s["config"]["models"]["pm"] == "opus" and s["config"]["roles"][0] == "pm"


def test_dead_orchestrator_shows_as_stopped_with_resume_hint(ui):
    base, state, _ = ui  # no pid file: the orchestrator is gone, the qa record is still open
    s = json.loads(get(base + "/api/state")[1])
    t = s["tasks"][0]
    assert t["status"] == "stopped" and t["running"] is None
    assert s["boxes"] == []  # a stale record is not "in flight"
    assert any(a["kind"] == "task" and "resume" in a["text"] for a in s["attention"])


def test_task_document_and_diff(ui):
    base, _, _ = ui
    d = json.loads(get(base + "/api/task?task=t1")[1])
    assert d["request"] == "add route" and d["steps"][0]["role"] == "pm"
    text = get(base + "/api/diff?task=t1")[1]
    assert "add two" in text and "+two" in text and "1 file changed" in text


def test_log_tail_by_offset(ui):
    base, state, _ = ui
    first = json.loads(get(base + "/api/log?task=t1&name=01-qa.log")[1])
    assert first["lines"][3:] == ["▶ Bash pytest", "■ success · 3 turns · $0.0200", "ok"]
    with (state / "logs" / "t1" / "01-qa.log").open("a") as f:
        f.write("note=more\npartial")
    more = json.loads(get(base + f"/api/log?task=t1&name=01-qa.log&offset={first['offset']}")[1])
    assert more["lines"] == ["· note=more"]
    raw = json.loads(get(base + "/api/log?task=t1&name=01-qa.log&raw=1")[1])
    assert raw["lines"][0] == "worktree=/tmp/wt/t1"


def test_refusals(ui):
    base, _, _ = ui
    assert status_of(base + "/api/task?task=nope") == 404
    assert status_of(base + "/api/task?task=../x") == 400
    assert status_of(base + "/api/log?task=t1&name=nope.log") == 400
    assert status_of(base + "/api/diff?task=..") == 400
    assert status_of(base + "/other") == 404
    assert status_of(base + "/api/state", host="evil.example:80") == 403
    assert post(base, "purge", "", "x")[0] == 403  # actions off


# --- actions -------------------------------------------------------------------------


def test_page_carries_token_and_rejects_bad_ones(ui_actions):
    base, _, _, ctx = ui_actions
    assert f'ACTION_TOKEN = "{ctx.token}"' in get(base + "/")[1]
    assert post(base, "purge", "", "wrong")[0] == 403
    assert post(base, "nonsense", "", ctx.token) == (
        200,
        {
            "ok": False,
            "output": "unsupported action 'nonsense'; supported: run, resume, stop-box, "
            "cancel, remove-worktree, delete-task, purge",
        },
    )


def test_run_spawns_the_orchestrator(ui_actions):
    base, state, tmp, ctx = ui_actions
    spec = {"repo": str(tmp / "repo"), "request": "do it", "task_id": "t2", "max_iterations": 2}
    status, out = post(base, "run", json.dumps(spec), ctx.token)
    assert out["ok"], out
    wait_for(lambda: (tmp / "runner.argv").exists())
    argv = (tmp / "runner.argv").read_text().splitlines()
    assert argv[0] == "run" and argv[-3:] == ["--", str((tmp / "repo").resolve()), "do it"]
    assert argv == [
        "run",
        "--task-id",
        "t2",
        "--max-iterations",
        "2",
        "--engine",
        "fake",
        "--runner",
        str(FAKE_BOX),
        "--",
        str((tmp / "repo").resolve()),
        "do it",
    ]
    assert (state / "runs" / "t2.out").exists()


def test_run_validation(ui_actions):
    base, _, tmp, ctx = ui_actions
    assert not post(base, "run", json.dumps({"repo": str(tmp), "request": "x"}), ctx.token)[1]["ok"]
    assert not post(base, "run", json.dumps({"repo": str(tmp / "repo"), "request": ""}), ctx.token)[
        1
    ]["ok"]
    assert not post(
        base,
        "run",
        json.dumps({"repo": str(tmp / "repo"), "request": "x", "task_id": "t1"}),
        ctx.token,
    )[1]["ok"]
    assert not post(
        base,
        "run",
        json.dumps({"repo": str(tmp / "repo"), "request": "x", "task_id": "../x"}),
        ctx.token,
    )[1]["ok"]
    assert not post(base, "run", "not json", ctx.token)[1]["ok"]


def test_resume_and_stop_box_and_cancel(ui_actions):
    base, state, tmp, ctx = ui_actions
    _, out = post(base, "resume", "t1", ctx.token)
    assert out["ok"], out
    wait_for(lambda: (tmp / "runner.argv").exists())
    argv = (tmp / "runner.argv").read_text().splitlines()
    assert argv[0] == "run" and argv[argv.index("--resume") + 1] == "t1"
    assert argv[-2:] == ["--", str(tmp / "repo")]
    _, out = post(base, "stop-box", "claude-box-graph-t1-qa-1", ctx.token)
    assert out["ok"], out
    assert (tmp / "argv.stop").read_text().splitlines()[2:4] == ["stop", "claude-box-graph-t1-qa-1"]
    assert not post(base, "stop-box", "evil; rm -rf /", ctx.token)[1]["ok"]
    _, out = post(base, "cancel", "t1", ctx.token)
    assert out["ok"] and "claude-box-graph-t1-qa-1" in out["output"], out


def test_delete_refuses_running_then_deletes(ui_actions, alive):
    base, state, tmp, ctx = ui_actions
    _, out = post(base, "delete-task", "t1", ctx.token)
    assert not out["ok"] and "still running" in out["output"]
    alive.kill()
    alive.wait()
    _, out = post(base, "remove-worktree", "t1", ctx.token)
    assert out["ok"], out
    assert (tmp / "argv.headless").read_text().splitlines()[2:] == [
        "headless",
        str(tmp / "repo"),
        "--worktree",
        "t1",
        "--remove",
        "===",
    ]
    _, out = post(base, "delete-task", "t1", ctx.token)
    assert out["ok"], out
    assert not (state / "tasks" / "t1.json").exists() and not (state / "logs" / "t1").exists()
    assert json.loads(get(base + "/api/state")[1])["tasks"] == []


def test_delete_after_a_dead_orchestrator_despite_an_open_box_record(ui_actions):
    base, state, tmp, ctx = ui_actions  # t1: no pid file, 01-qa.meta.json never got "finished"
    _, out = post(base, "delete-task", "t1", ctx.token)
    assert out["ok"], out
    assert not (state / "tasks" / "t1.json").exists()


def test_purge_keeps_running_tasks(ui_actions, alive):
    base, state, _, ctx = ui_actions
    (state / "tasks" / "t3.json").write_text(
        json.dumps({"task_id": "t3", "status": "done", "steps": []})
    )
    _, out = post(base, "purge", "", ctx.token)
    assert out["ok"] and "purged 1 task(s); kept running: t1" == out["output"]
    assert not (state / "tasks" / "t3.json").exists() and (state / "tasks" / "t1.json").exists()


def test_cancel_kills_the_orchestrator_and_stops_its_box(ui_actions, alive):
    base, state, tmp, ctx = ui_actions
    _, out = post(base, "cancel", "t1", ctx.token)
    assert out["ok"], out
    assert f"SIGTERM to orchestrator pid {alive.pid}" in out["output"]
    alive.wait(timeout=5)
    assert alive.returncode is not None
    assert "claude-box-graph-t1-qa-1" in (tmp / "argv.stop").read_text()


def test_resume_refuses_a_live_task(ui_actions, alive):
    base, _, _, ctx = ui_actions
    _, out = post(base, "resume", "t1", ctx.token)
    assert not out["ok"] and "already running" in out["output"]


def test_a_pid_reused_by_a_foreign_process_is_stale(world, alive):
    from claude_graph.ui.data import Store

    state, _ = world
    assert Store(state, None, "fake").pid("t1") == alive.pid
    other = subprocess.Popen(["sleep", "60"])  # alive, same user, not an orchestrator
    try:
        (state / "tasks" / "t1.pid").write_text(str(other.pid))
        assert Store(state, None, "fake").pid("t1") is None
    finally:
        other.kill()
        other.wait()


def test_state_reads_each_snapshot_once(world, monkeypatch):
    from claude_graph.ui import data

    state, _ = world
    reads: list[str] = []
    original = data.Store.snapshot

    def counting(self, task_id):
        reads.append(task_id)
        return original(self, task_id)

    monkeypatch.setattr(data.Store, "snapshot", counting)
    data.Store(state, None, "fake").state()
    assert reads == ["t1"]


def failing_runner(tmp_path: Path) -> Path:
    stub = tmp_path / "failing-runner"
    stub.write_text("#!/bin/sh\necho 'engine says no' >&2\nexit 1\n")
    stub.chmod(0o755)
    return stub


def test_delete_reports_a_stale_box_it_could_not_stop(world, tmp_path):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world  # t1: dead orchestrator, 01-qa.meta.json still open
    stub = failing_runner(tmp_path)
    out = Actions(Store(state, stub, "fake"), stub, "fake")._delete_task("t1")
    assert out["ok"] and "deleted t1" in out["output"]
    assert "could not stop" in out["output"] and "engine says no" in out["output"]
    assert "claude-box-graph-t1-qa-1" in out["output"] and "stopped stale box" not in out["output"]


def test_cancel_reports_a_stop_that_failed(world, tmp_path):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world
    stub = failing_runner(tmp_path)
    out = Actions(Store(state, stub, "fake"), stub, "fake")._cancel("t1")
    assert not out["ok"]
    assert "could not stop" in out["output"] and "engine says no" in out["output"]
    assert "stopped claude-box" not in out["output"]


def test_cancel_survives_an_orchestrator_that_just_exited(world, monkeypatch):
    from claude_graph.ui import actions
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, tmp = world
    monkeypatch.setattr(Store, "pid", lambda self, task_id: 4242)  # alive at the check …

    def exited(pid, sig):  # … gone by the time the signal is sent; never signal a real pid here
        raise ProcessLookupError(pid)

    monkeypatch.setattr(actions.os, "kill", exited)
    out = Actions(Store(state, FAKE_BOX, "fake"), FAKE_BOX, "fake")._cancel("t1")
    assert out["ok"], out
    assert "already gone" in out["output"]
    assert (
        "claude-box-graph-t1-qa-1" in (tmp / "argv.stop").read_text()
    )  # the box was still stopped


def test_delete_rechecks_the_orchestrator_after_stopping_boxes(world, monkeypatch):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world
    answers = iter([None, 4242])  # dead at the first check, resumed by someone meanwhile
    monkeypatch.setattr(Store, "pid", lambda self, task_id: next(answers, 4242))
    out = Actions(Store(state, FAKE_BOX, "fake"), FAKE_BOX, "fake")._delete_task("t1")
    assert not out["ok"] and "still running" in out["output"]
    assert (state / "tasks" / "t1.json").exists()


def test_purge_surfaces_boxes_it_could_not_stop(world, tmp_path):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world
    stub = failing_runner(tmp_path)
    out = Actions(Store(state, stub, "fake"), stub, "fake")._purge("")
    assert out["ok"] and "purged 1 task(s)" in out["output"]
    assert "could not stop" in out["output"] and "claude-box-graph-t1-qa-1" in out["output"]
    assert not (state / "tasks" / "t1.json").exists()


def test_spawn_leaves_no_open_file_behind(world):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, tmp = world
    acts = Actions(
        Store(state, None, "fake"), None, "fake", runner_argv=[sys.executable, "-c", "0"]
    )
    before = len(os.listdir("/dev/fd"))
    for _ in range(3):
        assert acts._run(json.dumps({"repo": str(tmp / "repo"), "request": "x"}))["ok"]
    assert len(os.listdir("/dev/fd")) == before


def test_attention_names_the_configured_runner(world):
    from claude_graph.ui.data import Store

    state, _ = world
    text = Store(state, Path("/x/claude-box"), "fake").state()["attention"][0]["text"]
    assert "`claude-box <repo>`" in text
    text = Store(state, None, "fake").state()["attention"][0]["text"]
    assert "`abox <repo>`" in text


def test_a_vanished_runner_is_an_action_error_not_an_exception(world, tmp_path):
    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world
    gone = tmp_path / "gone-runner"
    out = Actions(Store(state, gone, "fake"), gone, "fake")._stop_box("claude-box-graph-t1-qa-1")
    assert not out["ok"] and "gone-runner" in out["output"]


def test_a_zombie_orchestrator_is_stale(world):
    """Exited but unreaped (the dashboard may reap late): answers `kill -0`, is not running."""
    from claude_graph.ui.data import Store

    state, _ = world
    zombie = subprocess.Popen([sys.executable, "-c", "pass", "claude_graph"])
    try:
        os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)  # exited, still unreaped
        (state / "tasks" / "t1.pid").write_text(str(zombie.pid))
        assert Store(state, None, "fake").pid("t1") is None
    finally:
        zombie.wait()


def test_a_request_starting_with_a_dash_is_passed_after_a_double_dash(ui_actions):
    base, state, tmp, ctx = ui_actions
    spec = {"repo": str(tmp / "repo"), "request": "-weird request"}
    assert post(base, "run", json.dumps(spec), ctx.token)[1]["ok"]
    wait_for(lambda: (tmp / "runner.argv").exists())
    argv = (tmp / "runner.argv").read_text().splitlines()
    assert argv[-3:] == ["--", str((tmp / "repo").resolve()), "-weird request"]


def test_resume_action_refuses_an_exhausted_task(ui_actions):
    base, state, tmp, ctx = ui_actions
    snap = json.loads((state / "tasks" / "t1.json").read_text())
    snap["status"] = "exhausted"
    (state / "tasks" / "t1.json").write_text(json.dumps(snap))
    _, out = post(base, "resume", "t1", ctx.token)
    assert not out["ok"] and "--max-iterations" in out["output"]


def test_negative_and_oversized_post_bodies_are_refused(ui_actions):
    import http.client

    base, _, _, ctx = ui_actions
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/api/action")
    conn.putheader("Host", f"{host}:{port}")
    conn.putheader("X-CG-Token", ctx.token)
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400  # must answer, not block reading to EOF
    conn.close()
    big = json.dumps({"action": "purge", "arg": "x" * 70_000}).encode()
    req = urllib.request.Request(
        base + "/api/action",
        data=big,
        method="POST",
        headers={"Content-Type": "application/json", "X-CG-Token": ctx.token},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        assert e.code == 413
    else:
        raise AssertionError("an oversized body was accepted")


def test_log_tail_is_chunked_and_catches_up(ui):
    base, state, tmp = ui
    big = state / "logs" / "t1" / "01-qa.log"
    big.write_text(("x" * 99 + "\n") * 14_000)  # 1.4 MB
    with urllib.request.urlopen(base + "/api/log?task=t1&name=01-qa.log&offset=0&raw=1") as r:
        first = json.loads(r.read())
    assert 0 < first["offset"] < first["size"]
    with urllib.request.urlopen(
        base + f"/api/log?task=t1&name=01-qa.log&offset={first['offset']}&raw=1"
    ) as r:
        second = json.loads(r.read())
    assert second["offset"] == second["size"]
    assert len(first["lines"]) + len(second["lines"]) == 14_000


def test_log_tail_resets_when_the_file_shrank(ui):
    base, state, tmp = ui
    size = (state / "logs" / "t1" / "01-qa.log").stat().st_size
    with urllib.request.urlopen(
        base + f"/api/log?task=t1&name=01-qa.log&offset={size + 500}&raw=1"
    ) as r:
        out = json.loads(r.read())
    assert out["reset"] is True and out["offset"] == size and out["lines"]


def test_the_page_cannot_be_framed(ui):
    base, state, tmp = ui
    with urllib.request.urlopen(base + "/") as r:
        headers = r.headers
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_forget_reports_a_locked_checkpoint_db_instead_of_claiming_success(world):
    import sqlite3

    from claude_graph.ui.actions import Actions
    from claude_graph.ui.data import Store

    state, _ = world
    db = state / "checkpoints.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        conn.execute("CREATE TABLE writes (thread_id TEXT)")
        conn.execute("INSERT INTO checkpoints VALUES ('t1')")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        out = Actions(Store(state, None, "fake"), None, "fake")._delete_task("t1")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert out["ok"] and "locked" in out["output"]
    assert "checkpoints," not in out["output"] and not out["output"].endswith("checkpoints.")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1


def test_a_non_ascii_token_header_is_a_json_403_not_a_crash(ui_actions):
    base, _, _, ctx = ui_actions
    req = urllib.request.Request(
        base + "/api/action",
        data=b'{"action":"purge","arg":""}',
        method="POST",
        headers={"Content-Type": "application/json", "X-CG-Token": "caf\u00e9"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        assert e.code == 403 and json.loads(e.read())["output"] == "bad token"
    else:
        raise AssertionError("a bad token was accepted")


def test_task_views_carry_the_pull_request_and_ci(world):
    from claude_graph.ui.data import Store

    state, _ = world
    snap = json.loads((state / "tasks" / "t1.json").read_text())
    snap.update(
        pr_url="https://github.com/o/r/pull/7",
        pr_number=7,
        ci_status="pass",
        ci_summary="unit: pass",
    )
    (state / "tasks" / "t1.json").write_text(json.dumps(snap))
    store = Store(state, None, "fake")
    task = store.tasks()[0]
    assert task["pr_url"].endswith("/pull/7") and task["ci_status"] == "pass"
    doc = store.document("t1")
    assert doc["pr_url"].endswith("/pull/7") and doc["ci_summary"] == "unit: pass"


def test_state_lists_pull_requests_newest_first(world):
    from claude_graph.ui.data import Store

    state, _ = world
    snap = json.loads((state / "tasks" / "t1.json").read_text())
    snap.update(
        pr_url="https://github.com/o/r/pull/7",
        pr_number=7,
        ci_status="fail",
        review_verdict="changes",
    )
    (state / "tasks" / "t1.json").write_text(json.dumps(snap))
    (state / "tasks" / "t2.json").write_text(
        json.dumps(
            {
                "task_id": "t2",
                "status": "done",
                "steps": [],
                "updated_at": "2026-10-28T10:00:00+00:00",
            }
        )
    )
    (state / "tasks" / "t3.json").write_text(
        json.dumps(
            {
                "task_id": "t3",
                "status": "done",
                "steps": [],
                "updated_at": "2026-10-28T11:00:00+00:00",
                "pr_url": "https://github.com/o/r/pull/9",
                "pr_number": 9,
                "ci_status": "pass",
                "review_verdict": "approve",
            }
        )
    )
    prs = Store(state, None, "fake").state()["prs"]
    assert [p["task_id"] for p in prs] == ["t3", "t1"]  # t2 has no PR
    assert prs[0] == {
        "task_id": "t3",
        "pr_url": "https://github.com/o/r/pull/9",
        "pr_number": 9,
        "ci_status": "pass",
        "review_verdict": "approve",
        "status": "done",
        "updated_at": "2026-10-28T11:00:00+00:00",
    }
