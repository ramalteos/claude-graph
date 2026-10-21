import json

from claude_graph.render import render_event, render_log


def ev(**kw):
    return json.dumps(kw)


def test_system_init_line():
    assert render_event({"type": "system", "session_id": "s1", "model": "opus"}) == [
        "· session s1 · model opus"
    ]


def test_assistant_text_and_tool_use():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Looking.\nTwo lines."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}},
            ]
        },
    }
    assert render_event(event) == ["Looking.", "Two lines.", "▶ Bash pytest -q", "▶ Read /a/b.py"]


def test_tool_results():
    ok = {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "content": "first line\nsecond", "is_error": False}]
        },
    }
    bad = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "boom"}],
                    "is_error": True,
                }
            ]
        },
    }
    assert render_event(ok) == ["  ↳ first line"]
    assert render_event(bad) == ["✗ tool error: boom"]


def test_result_line_with_cost_and_text():
    event = {
        "type": "result",
        "subtype": "success",
        "num_turns": 7,
        "total_cost_usd": 0.5,
        "result": "done\n```json\n{}\n```",
    }
    assert render_event(event) == ["■ success · 7 turns · $0.5000", "done", "```json", "{}", "```"]


def test_log_passes_meta_lines_and_skips_broken_json():
    text = "worktree=/tmp/wt\n{not json\n" + ev(type="system", session_id="s", model="m") + "\n"
    assert render_log(text) == ["· worktree=/tmp/wt", "· session s · model m"]


def test_long_tool_input_is_cut():
    event = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}]
        },
    }
    line = render_event(event)[0]
    assert len(line) < 200 and line.endswith("…")
