import os
import subprocess
from pathlib import Path

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


def help_answer(sandbox):
    """What `abox help` prints on a runner new enough to have the headless seam."""
    return {f"{sandbox / 'abox'} help": (0, "usage: claude-box run|ps|stop|headless …\n", "")}


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
    run = FakeRun(help_answer(sandbox))
    r = mod.setup(which=lambda _: None, run=run, home=tmp_path, env={"PATH": ""})
    assert not any(c[0] == "container" for c in run.calls)  # no engine call without the engine
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
            **help_answer(sandbox),
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
            **help_answer(sandbox),
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
            **help_answer(sandbox),
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


def test_links_claude_graph_itself(tmp_path, sandbox, monkeypatch):
    import sys as _sys

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "claude-graph").write_text("#!/bin/sh\n")
    monkeypatch.setattr(_sys, "executable", str(venv_bin / "python"))
    run = FakeRun(
        {
            "container system status": (0, "status  running\n", ""),
            "container image list": (0, "claude-box latest\n", ""),
            **help_answer(sandbox),
        }
    )
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": "/usr/bin"})
    link = tmp_path / ".local" / "bin" / "claude-graph"
    assert link.is_symlink() and os.readlink(link) == str(venv_bin / "claude-graph")
    assert sum("export PATH" in t for t in r.todo) == 1


def test_login_hint_names_this_clone_not_a_fixed_path(tmp_path, sandbox, monkeypatch):
    from claude_graph import config

    clone = tmp_path / "elsewhere" / "claude-graph"
    monkeypatch.setattr(config, "_PACKAGE_ROOT", clone)
    r = mod.setup(which=lambda _: None, run=FakeRun(help_answer(sandbox)), home=tmp_path, env={})
    login = next(t for t in r.todo if "/login" in t)
    assert str(clone) in login
    assert "Documents/GitHub" not in login


def test_image_check_matches_the_name_not_a_substring(tmp_path, sandbox):
    run = FakeRun(
        {
            "container system status": (0, "status  running\n", ""),
            "container image list": (0, "NAME  TAG  DIGEST\nmy-claude-box-fork  latest  x\n", ""),
            **help_answer(sandbox),
        }
    )
    r = mod.setup(
        skip_build=True, which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""}
    )
    assert any(t.endswith("build") for t in r.todo)


def test_runner_without_the_headless_seam_is_flagged(tmp_path, sandbox):
    run = FakeRun(
        {
            "container system status": (0, "status  running\n", ""),
            "container image list": (0, "claude-box  latest  abc\n", ""),
            f"{sandbox / 'abox'} help": (0, "usage: claude-box run|ps|stop\n", ""),
        }
    )
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""})
    assert any("too old" in ln for ln in r.lines), r.text()
    assert any("pull" in t for t in r.todo)
    assert not any(c[-1] == "build" for c in run.calls)  # an old runner is not asked to build


ENGINE_OK = {
    "container system status": (0, "status  running\n", ""),
    "container image list": (0, "claude-box  latest  abc\n", ""),
}


def test_a_symlinked_runner_finds_abox_in_its_real_repo(tmp_path, sandbox, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "claude-box"
    link.symlink_to(sandbox / "claude-box")
    monkeypatch.setenv("CLAUDE_GRAPH_RUNNER", str(link))
    run = FakeRun({**ENGINE_OK, f"{link} help": (0, "usage: … headless …\n", "")})
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""})
    assert not any("has no `abox`" in ln for ln in r.lines), r.text()
    assert os.readlink(tmp_path / ".local" / "bin" / "abox") == str(sandbox / "abox")


def test_stale_links_are_repointed_at_this_clone(tmp_path, sandbox, monkeypatch):
    import sys as _sys

    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "abox").symlink_to(tmp_path / "old-clone" / "abox")  # dangling
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "claude-graph").write_text("#!/bin/sh\n")
    monkeypatch.setattr(_sys, "executable", str(venv_bin / "python"))
    (bin_dir / "claude-graph").symlink_to(tmp_path / "old-venv" / "bin" / "claude-graph")
    run = FakeRun({**ENGINE_OK, **help_answer(sandbox)})
    r = mod.setup(
        which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(bin_dir)}
    )
    assert os.readlink(bin_dir / "abox") == str(sandbox / "abox")
    assert os.readlink(bin_dir / "claude-graph") == str(venv_bin / "claude-graph")
    assert sum("fixed" in ln and "->" in ln for ln in r.lines) == 2, r.text()
    assert all("/login" in t for t in r.todo), r.todo  # nothing left but the human login


def test_a_real_file_in_the_way_of_a_link_is_not_clobbered(tmp_path, sandbox):
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "abox").write_text("#!/bin/sh\necho mine\n")
    run = FakeRun({**ENGINE_OK, **help_answer(sandbox)})
    r = mod.setup(
        which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(bin_dir)}
    )
    assert (bin_dir / "abox").read_text() == "#!/bin/sh\necho mine\n"
    assert any("abox" in t and "move" in t for t in r.todo), r.text()


def test_a_runner_that_cannot_be_probed_is_a_todo_not_a_traceback(tmp_path, sandbox):
    class Refusing(FakeRun):
        def __call__(self, argv, **kw):
            if argv[-1] == "help":
                raise PermissionError(13, "Permission denied", str(argv[0]))
            return super().__call__(argv, **kw)

    run = Refusing(ENGINE_OK)
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""})
    assert any("cannot be run" in ln for ln in r.lines), r.text()
    assert any("chmod +x" in t for t in r.todo)
    assert not any(c[-1] == "build" for c in run.calls)
    assert not any(ln.startswith("  ok") and "sandbox runner" in ln for ln in r.lines), r.text()


def test_hints_for_a_symlinked_runner_name_its_real_repo(tmp_path, sandbox, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "claude-box"
    link.symlink_to(sandbox / "claude-box")
    monkeypatch.setenv("CLAUDE_GRAPH_RUNNER", str(link))
    # Too old: the pull hint must point at the repo the link resolves to, not at bin/.
    run = FakeRun({**ENGINE_OK, f"{link} help": (0, "usage: claude-box run|ps|stop\n", "")})
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""})
    pull = next(t for t in r.todo if "pull" in t)
    assert f"git -C {sandbox} pull" in pull, pull
    # Current: the login hint names the real abox, not bin/abox.
    run = FakeRun({**ENGINE_OK, f"{link} help": (0, "usage: … headless …\n", "")})
    r = mod.setup(which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": ""})
    login = next(t for t in r.todo if "/login" in t)
    assert login.startswith(str(sandbox / "abox")), login


def test_a_link_to_another_live_clone_is_left_alone(tmp_path, sandbox):
    other = tmp_path / "other-clone"
    other.mkdir()
    (other / "abox").write_text("#!/bin/sh\n")
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "abox").symlink_to(other / "abox")
    run = FakeRun({**ENGINE_OK, **help_answer(sandbox)})
    r = mod.setup(
        which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(bin_dir)}
    )
    assert os.readlink(bin_dir / "abox") == str(other / "abox")
    assert any("ln -sfn" in t and str(sandbox / "abox") in t for t in r.todo), r.text()


def test_without_the_engine_the_login_hint_still_names_the_real_abox(
    tmp_path, sandbox, monkeypatch
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "claude-box"
    link.symlink_to(sandbox / "claude-box")
    monkeypatch.setenv("CLAUDE_GRAPH_RUNNER", str(link))
    run = FakeRun({f"{link} help": (0, "usage: … headless …\n", "")})
    r = mod.setup(which=lambda _: None, run=run, home=tmp_path, env={"PATH": ""})
    login = next(t for t in r.todo if "/login" in t)
    assert login.startswith(str(sandbox / "abox")), login


def test_a_relative_link_to_another_live_clone_is_left_alone(tmp_path, sandbox):
    other = tmp_path / "other-clone"
    other.mkdir()
    (other / "abox").write_text("#!/bin/sh\n")
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "abox").symlink_to(Path("..") / ".." / "other-clone" / "abox")  # relative, valid
    run = FakeRun({**ENGINE_OK, **help_answer(sandbox)})
    r = mod.setup(
        which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(bin_dir)}
    )
    assert os.readlink(bin_dir / "abox") == "../../other-clone/abox"
    assert any("ln -sfn" in t for t in r.todo), r.text()


def test_a_relative_link_to_this_clone_counts_as_present(tmp_path, sandbox):
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "abox").symlink_to(
        Path("..") / ".." / "claude-box" / "abox"
    )  # same target, spelled relative
    run = FakeRun({**ENGINE_OK, **help_answer(sandbox)})
    r = mod.setup(
        which=lambda n: "/x/container", run=run, home=tmp_path, env={"PATH": str(bin_dir)}
    )
    assert os.readlink(bin_dir / "abox") == "../../claude-box/abox"
    assert any(f"{bin_dir / 'abox'} present" in ln for ln in r.lines), r.text()
    assert not any("ln -sfn" in t for t in r.todo)
