import os
import stat
from pathlib import Path

from claude_graph.config import find_claude_box


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_GRAPH_RUNNER", str(tmp_path / "mine"))
    assert find_claude_box() == tmp_path / "mine"


def test_abox_beats_claude_box_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_GRAPH_RUNNER", raising=False)
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    plain = executable(first / "claude-box")
    apple = executable(second / "abox")
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    assert find_claude_box() == apple
    apple.unlink()
    assert find_claude_box() == plain


def test_every_role_defaults_to_opus():
    from claude_graph.config import Settings
    from claude_graph.roles import ROLE_NAMES

    assert {Settings().model(r) for r in ROLE_NAMES} == {"opus"}
