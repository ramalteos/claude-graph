import os
import subprocess

import pytest

from claude_graph import setup as mod


class FakeRun:
    """Scripted subprocess.run: records argv, answers from a table keyed by the command."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(map(str, argv)))
        key = " ".join(map(str, argv[:3]))
        code, out, err = self.answers.get(key, (0, "", ""))
        return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "claude-box"
    repo.mkdir()
    for name in ("claude-box", "abox"):
        p = repo / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    monkeypatch.setenv("CLAUDE_GRAPH_RUNNER", str(repo / "abox"))
    monkeypatch.setenv("CLAUDE_BOX_CONFIG_HOME", str(tmp_path / "nocreds"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return repo


def test_everything_missing_says_what_to_do_first(tmp_path, sandbox):
    run = FakeRun({})
    r = mod.setup(which=lambda _: None, run=run, home=tmp_path, env={"PATH": ""})
    assert run.calls == []  # nothing is attempted without the engine
    assert "TODO   Apple `container` CLI" in r.text()
    assert r.todo[0].startswith("brew install --cask container")
    assert any("sandbox login (missing)" in ln for ln in r.lines)
    assert "/login" in r.text()


def test_starts_daemon_builds_image_links_abox(tmp_path, sandbox):
    run = FakeRun(
        {
            "container system status": (0, "status  stopped\n", ""),
            "container system start": (0, "", ""),
            "container image list": (0, "NAME TAG\n", ""),
        }
    )
    r = mod.setup(
        which=lambda n: "/opt/homebrew/bin/container",
        run=run,
        home=tmp_path,
        env={"PATH": str(tmp_path / ".local" / "bin")},
    )
    assert ["container", "system", "start"] in run.calls
    build = [c for c in run.calls if c[-1] == "build"]
    assert build == [[str(sandbox / "abox"), "build"]]
    link = tmp_path / ".local" / "bin" / "abox"
    assert link.is_symlink() and os.readlink(link) == str(sandbox / "abox")
    text = r.text()
    assert "fixed  container daemon started" in text
    assert "fixed  image claude-box:latest built" in text
    assert f"fixed  {link}" in text
    assert "TODO   sandbox login (missing)" in text
    assert "~/.local/bin on PATH" not in text


def test_all_good_is_quiet(tmp_path, sandbox, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / ".credentials.json").write_text(
        '{"claudeAiOauth": {"expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999}}'
    )
    monkeypatch.setenv("CLAUDE_BOX_CONFIG_HOME", str(creds))
    link = tmp_path / ".local" / "bin"
    link.mkdir(parents=True)
    (link / "abox").symlink_to(sandbox / "abox")
    run = FakeRun(
        {
            "container system status": (0, "status  running\n", ""),
            "container image list": (0, "claude-box  latest  abc\n", ""),
        }
    )
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(link)})
    assert r.todo == []
    assert "everything is in place" in r.text()
    assert not any(c[-1] == "build" for c in run.calls)


def test_skip_build_and_path_hint(tmp_path, sandbox):
    run = FakeRun(
        {
            "container system status": (0, "status  running\n", ""),
            "container image list": (0, "", ""),
        }
    )
    r = mod.setup(
        skip_build=True,
        which=lambda n: "/x/container",
        run=run,
        home=tmp_path,
        env={"PATH": "/usr/bin"},
    )
    assert not any(c[-1] == "build" for c in run.calls)
    assert any(t.endswith("build") for t in r.todo)
    assert any("export PATH" in t for t in r.todo)


def test_runner_lookup_prefers_submodule_then_sibling(tmp_path, monkeypatch):
    from claude_graph import config

    monkeypatch.delenv("CLAUDE_GRAPH_RUNNER", raising=False)
    monkeypatch.setenv("PATH", "")
    pkg_root = tmp_path / "claude-graph"
    (pkg_root / "claude_graph").mkdir(parents=True)
    monkeypatch.setattr(config, "_PACKAGE_ROOT", pkg_root)
    sibling = tmp_path / "claude-box"
    sibling.mkdir()
    (sibling / "claude-box").write_text("#!/bin/sh\n")
    assert config.find_claude_box() == sibling / "claude-box"
    (sibling / "abox").write_text("#!/bin/sh\n")
    assert config.find_claude_box() == sibling / "abox"
    sub = pkg_root / "sandbox"
    sub.mkdir()
    (sub / "abox").write_text("#!/bin/sh\n")
    assert config.find_claude_box() == sub / "abox"
