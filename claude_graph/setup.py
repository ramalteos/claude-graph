"""`claude-graph setup`: check every prerequisite, fix what a script can, say what is left."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_graph.config import find_claude_box
from claude_graph.ui.data import login_verdict

IMAGE = "claude-box"
Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)

    def ok(self, what: str) -> None:
        self.lines.append(f"  ok     {what}")

    def fixed(self, what: str) -> None:
        self.lines.append(f"  fixed  {what}")

    def left(self, what: str, how: str) -> None:
        self.lines.append(f"  TODO   {what}")
        self.todo.append(how)

    def text(self) -> str:
        out = ["claude-graph setup", *self.lines]
        if self.todo:
            out += ["", "still to do, in this order:"] + [
                f"  {i}. {t}" for i, t in enumerate(self.todo, 1)
            ]
        else:
            out += ["", 'everything is in place — run: claude-graph run <repo> "<request>"']
        return "\n".join(out)


def setup(
    *,
    skip_build: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = subprocess.run,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> Report:
    r = Report()
    home = home or Path.home()
    env = env if env is not None else dict(os.environ)

    # 1. the engine
    if not which("container"):
        r.left(
            "Apple `container` CLI",
            "brew install --cask container   (asks for your password), "
            "then re-run claude-graph setup",
        )
        runner = find_claude_box()
        _runner_check(r, runner, home, env)
        _link_self(r, home, env)
        _login_check(r)
        return r
    r.ok("Apple `container` CLI")

    # 2. its daemon
    status = run(["container", "system", "status"], capture_output=True, text=True, env=env)
    if status.returncode == 0 and "running" in status.stdout:
        r.ok("container daemon running")
    else:
        started = run(["container", "system", "start"], capture_output=True, text=True, env=env)
        if started.returncode == 0:
            r.fixed("container daemon started")
        else:
            r.left(
                "container daemon",
                f"container system start   (failed: {started.stderr.strip()[-200:]})",
            )

    # 3. the runner
    runner = find_claude_box()
    if not _runner_check(r, runner, home, env):
        _link_self(r, home, env)
        _login_check(r)
        return r

    # 4. the image
    images = run(["container", "image", "list"], capture_output=True, text=True, env=env)
    if IMAGE in images.stdout:
        r.ok(f"image {IMAGE}:latest present")
    elif skip_build:
        r.left(f"image {IMAGE}:latest", f"{runner} build")
    else:
        build = run([str(runner), "build"], env={**env, "CLAUDE_BOX_ENGINE": "container"})
        if build.returncode == 0:
            r.fixed(f"image {IMAGE}:latest built")
        else:
            r.left(
                f"image {IMAGE}:latest",
                f"{runner} build   (failed with exit {build.returncode}; see the output above)",
            )

    # 5. this command on PATH, then the login
    _link_self(r, home, env)
    _login_check(r, runner)
    return r


def _link_self(r: Report, home: Path, env: dict[str, str]) -> None:
    """`claude-graph` on PATH, so it is typed from anywhere and never from inside the venv."""
    exe = Path(sys.executable).parent / "claude-graph"
    if not exe.is_file():
        return
    link = home / ".local" / "bin" / "claude-graph"
    if link.exists() or link.is_symlink():
        r.ok(f"{link} present")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(exe)
        r.fixed(f"{link} -> {exe}")
    if str(link.parent) not in env.get("PATH", "").split(os.pathsep):
        r.left("~/.local/bin on PATH", 'add to ~/.zshrc:  export PATH="$HOME/.local/bin:$PATH"')


def _runner_check(r: Report, runner: Path | None, home: Path, env: dict[str, str]) -> bool:
    if not runner:
        r.left(
            "sandbox runner (abox)",
            "git submodule update --init   (or clone the sandbox repo next to this one)",
        )
        return False
    r.ok(f"sandbox runner {runner}")
    abox = runner.parent / "abox"
    if not abox.is_file():
        r.left("abox wrapper", f"the sandbox repo at {runner.parent} has no `abox`; pull it")
        return True
    link = home / ".local" / "bin" / "abox"
    if link.exists() or link.is_symlink():
        r.ok(f"{link} present")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(abox)
        r.fixed(f"{link} -> {abox}")
    return True


def _login_check(r: Report, runner: Path | None = None) -> None:
    verdict = login_verdict()
    if verdict in ("ok", "api-key", "no-expiry"):
        r.ok(f"sandbox login ({verdict})")
        return
    abox = "abox" if runner is None or runner.name == "abox" else str(runner.parent / "abox")
    cmd = f"{abox} ~/Documents/GitHub/claude-graph"
    r.left(f"sandbox login ({verdict})", f"{cmd}   then type /login, finish in the browser, /exit")
