"""`claude-graph setup`: check every prerequisite, fix what a script can, say what is left."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_graph import config
from claude_graph.login import login_verdict

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
        runner = config.find_claude_box()
        _runner_check(r, runner, home, env, run)
        _link_self(r, home, env)
        _login_check(r, runner)
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
    runner = config.find_claude_box()
    if not _runner_check(r, runner, home, env, run):
        _link_self(r, home, env)
        _login_check(r)
        return r

    # 4. the image
    images = run(["container", "image", "list"], capture_output=True, text=True, env=env)
    if _image_present(images.stdout or ""):
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
    _link(r, link, exe)
    if str(link.parent) not in env.get("PATH", "").split(os.pathsep):
        r.left("~/.local/bin on PATH", 'add to ~/.zshrc:  export PATH="$HOME/.local/bin:$PATH"')


def _link(r: Report, link: Path, target: Path) -> None:
    """`link -> target`; re-point a dangling link, leave a live one or a real file alone."""
    if link.is_symlink():
        old = os.readlink(link)
        old_path = Path(old) if Path(old).is_absolute() else link.parent / old  # relative links
        if old_path.resolve() == target.resolve():
            r.ok(f"{link} present")
            return
        if old_path.exists():  # another live clone owns it; two clones must not flip it
            r.left(
                f"{link} points at {old}, not at this clone",
                f"ln -sfn {target} {link}   (only if this clone should win)",
            )
            return
        link.unlink()
        link.symlink_to(target)
        r.fixed(f"{link} -> {target}   (was {old}, dangling)")
        return
    if link.exists():
        r.left(
            f"{link} is a regular file, not a link",
            f"move {link} out of the way (or delete it), then re-run claude-graph setup",
        )
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    r.fixed(f"{link} -> {target}")


def _image_present(listing: str) -> bool:
    """`container image list` rows are `NAME TAG DIGEST`; match the name, not a substring."""
    for line in listing.splitlines():
        parts = line.split()
        if parts[:2] == [IMAGE, "latest"] or (parts and parts[0] == f"{IMAGE}:latest"):
            return True
    return False


def _runner_check(
    r: Report, runner: Path | None, home: Path, env: dict[str, str], run: Runner
) -> bool:
    if not runner:
        r.left(
            "sandbox runner (abox)",
            "git submodule update --init   (or clone the sandbox repo next to this one)",
        )
        return False
    repo = runner.resolve().parent  # a `~/.local/bin/claude-box` link points into the repo
    # probe under the fake engine: needs no `container` CLI; an old runner has no `headless`
    try:
        probe = run(
            [str(runner), "help"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**env, "CLAUDE_BOX_ENGINE": "fake"},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        r.left(
            f"sandbox runner {runner} cannot be run ({getattr(e, 'strerror', None) or e})",
            f"chmod +x {runner}   (or pass --runner <path to a working abox>)",
        )
        return False
    if "headless" not in (probe.stdout or "") + (probe.stderr or ""):
        r.left(
            f"sandbox runner {runner} is too old (no `headless` command)",
            f"git -C {repo} pull   (the runner needs the headless seam), "
            "or pass --runner <path to a current abox>",
        )
        return False
    r.ok(f"sandbox runner {runner}")
    abox = repo / "abox"
    if not abox.is_file():
        r.left("abox wrapper", f"the sandbox repo at {repo} has no `abox`; pull it")
        return True
    _link(r, home / ".local" / "bin" / "abox", abox)
    return True


def _login_check(r: Report, runner: Path | None = None) -> None:
    verdict = login_verdict()
    if verdict in ("ok", "api-key", "no-expiry"):
        r.ok(f"sandbox login ({verdict})")
        return
    abox = (
        "abox" if runner is None or runner.name == "abox" else str(runner.resolve().parent / "abox")
    )
    cmd = f"{abox} {config._PACKAGE_ROOT}"  # any repo works; this clone is always there
    r.left(f"sandbox login ({verdict})", f"{cmd}   then type /login, finish in the browser, /exit")
