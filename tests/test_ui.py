import json
import os
import subprocess
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
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$FAKE_RUNNER_OUT"\nsleep 0.2\n'
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


def test_state_has_every_section(ui):
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
    assert [b["role"] for b in s["boxes"]] == ["qa"]
    assert s["boxes"][0]["elapsed_s"] > 0
    assert s["attention"][0]["kind"] == "login"
    assert [e["role"] for e in s["timeline"]] == ["qa", "pm"]
    stats = {r["role"]: r for r in s["role_stats"]}
    assert stats["pm"]["runs"] == 1 and stats["pm"]["avg_s"] == 30 and stats["qa"]["runs"] == 0
    assert s["spend"]["total"] == 0.1
    assert s["logs"][0]["name"] == "01-qa.log"
    assert s["config"]["models"]["pm"] == "fable" and s["config"]["roles"][0] == "pm"


def test_dead_orchestrator_shows_as_stopped_with_resume_hint(ui):
    base, state, _ = ui
    (state / "logs" / "t1" / "01-qa.meta.json").write_text(
        json.dumps({"role": "qa", "attempt": 1, "started": "x", "finished": "y", "exit_code": 0})
    )
    s = json.loads(get(base + "/api/state")[1])
    assert s["tasks"][0]["status"] == "stopped"
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
    time.sleep(0.4)
    argv = (tmp / "runner.argv").read_text().splitlines()
    assert argv[:3] == ["run", str((tmp / "repo").resolve()), "do it"]
    assert argv[3:] == [
        "--task-id",
        "t2",
        "--max-iterations",
        "2",
        "--engine",
        "fake",
        "--runner",
        str(FAKE_BOX),
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
    time.sleep(0.4)
    assert (tmp / "runner.argv").read_text().splitlines()[:4] == [
        "run",
        str(tmp / "repo"),
        "--resume",
        "t1",
    ]
    _, out = post(base, "stop-box", "claude-box-graph-t1-qa-1", ctx.token)
    assert out["ok"], out
    assert (tmp / "argv.stop").read_text().splitlines()[2:4] == ["stop", "claude-box-graph-t1-qa-1"]
    assert not post(base, "stop-box", "evil; rm -rf /", ctx.token)[1]["ok"]
    _, out = post(base, "cancel", "t1", ctx.token)
    assert out["ok"] and "claude-box-graph-t1-qa-1" in out["output"] or out["ok"]


def test_delete_refuses_running_then_deletes(ui_actions):
    base, state, tmp, ctx = ui_actions
    _, out = post(base, "delete-task", "t1", ctx.token)
    assert not out["ok"] and "still running" in out["output"]
    (state / "logs" / "t1" / "01-qa.meta.json").write_text(
        json.dumps({"role": "qa", "attempt": 1, "started": "x", "finished": "y", "exit_code": 0})
    )
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


def test_purge_keeps_running_tasks(ui_actions):
    base, state, _, ctx = ui_actions
    (state / "tasks" / "t3.json").write_text(
        json.dumps({"task_id": "t3", "status": "done", "steps": []})
    )
    _, out = post(base, "purge", "", ctx.token)
    assert out["ok"] and "purged 1 task(s); kept running: t1" == out["output"]
    assert not (state / "tasks" / "t3.json").exists() and (state / "tasks" / "t1.json").exists()
