"""claude-graph: run one task through the graph, or look at one that ran."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START

from claude_graph.boxes import ClaudeBoxRunner
from claude_graph.config import DEFAULT_ENGINE, find_claude_box, load_settings, state_dir
from claude_graph.github import GhGitHub
from claude_graph.graph import build_graph
from claude_graph.state import (
    DEFAULT_MAX_ITERATIONS,
    MAX_TASK_ID,
    TASK_ID,
    TaskState,
    initial_state,
)
from claude_graph.ui.data import Store
from claude_graph.ui.server import UiContext, serve


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"claude-graph: {e}", file=sys.stderr)
        return 2


class CliError(Exception):
    pass


ENGINES = ("container", "docker", "fake", "auto")  # what the runner's CLAUDE_BOX_ENGINE accepts


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-graph")
    sub = p.add_subparsers(required=True)

    run = sub.add_parser("run", help="run a request through pm → architect → coder → qa → reviewer")
    run.add_argument(
        "words",
        nargs="*",
        metavar="[repo] request",
        help="the repo (a git checkout; default: the current directory) and what you want; "
        "omit with --resume",
    )
    run.add_argument(
        "--task-id", help="[A-Za-z0-9._-]{1,40}; default: a slug of the request, -2 on repeats"
    )
    run.add_argument("--resume", metavar="TASK_ID", help="continue a task from its last checkpoint")
    run.add_argument(
        "--max-iterations",
        type=_cap,
        help=f"coder runs before giving up, 1-20 (default {DEFAULT_MAX_ITERATIONS}); "
        "with --resume, a new, higher cap for an exhausted task",
    )
    run.add_argument("--engine", default=DEFAULT_ENGINE, choices=ENGINES, help="default: container")
    run.add_argument(
        "--runner",
        type=Path,
        help="path to the box runner (abox); default: abox or claude-box on PATH",
    )
    run.add_argument("--timeout", type=int, default=3600, help="seconds per box")
    pub = run.add_mutually_exclusive_group()
    pub.add_argument(
        "--no-publish",
        action="store_true",
        help="no push, no pull request, no CI: QA runs everything locally",
    )
    pub.add_argument(
        "--publish",
        action="store_true",
        help="with --resume: turn the pull-request flow on for a task started with --no-publish",
    )
    run.add_argument(
        "--ci-timeout", type=int, default=900, help="seconds to wait for the PR's checks"
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="final/last state of a task")
    status.add_argument("task_id")
    status.set_defaults(func=cmd_status)

    ui = sub.add_parser("ui", help="dashboard on localhost: tasks, steps, live box logs")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--engine", default=DEFAULT_ENGINE, choices=ENGINES)
    ui.add_argument("--runner", type=Path)
    ui.add_argument(
        "--actions",
        action="store_true",
        help="let the page start, resume, cancel and delete tasks (token minted per start)",
    )
    ui.set_defaults(func=cmd_ui)

    su = sub.add_parser("setup", help="check prerequisites, fix what a script can, list the rest")
    su.add_argument("--skip-build", action="store_true", help="do not build the image if missing")
    su.set_defaults(func=cmd_setup)

    logs = sub.add_parser("logs", help="list a task's box logs, or print one role's")
    logs.add_argument("task_id")
    logs.add_argument("role", nargs="?")
    logs.set_defaults(func=cmd_logs)
    return p


def _cap(text: str) -> int:
    """`--max-iterations`: 1 to 20, the same bounds the dashboard enforces."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 1 <= value <= 20:
        raise argparse.ArgumentTypeError("must be between 1 and 20")
    return value


def _split_target(words: list[str]) -> tuple[Path, str]:
    """`run <repo> <request…>` or `run <request…>` from inside the repo.

    The first word is the repo only if it is a git checkout or a path (`./x`, `/x`) to a dir.
    """
    if words:
        try:
            first = Path(words[0]).expanduser()
        except RuntimeError:  # `~nosuchuser…`: prose, not a path
            first = None
        spelled_as_path = "/" in words[0] or words[0].startswith((".", "~"))
        if first and ((first / ".git").exists() or (spelled_as_path and first.is_dir())):
            return first.resolve(), " ".join(words[1:])
    return Path.cwd().resolve(), " ".join(words)


def cmd_run(args: argparse.Namespace) -> int:
    repo, request = _split_target(args.words)
    if not (repo / ".git").exists():
        raise CliError(f"not a git repo: {repo}")
    claude_box = args.runner or find_claude_box()
    if claude_box is None or not claude_box.is_file():
        raise CliError("runner not found; put abox on PATH or pass --runner <path>")
    if not os.access(claude_box, os.X_OK):
        raise CliError(f"runner is not executable: {claude_box}   (chmod +x it)")
    if not args.resume and not request:
        raise CliError("a request is required (or --resume <task-id>)")
    task_id = args.resume or args.task_id or _slug(request)
    if not TASK_ID.match(task_id):
        raise CliError(f"task id must match {TASK_ID.pattern}: {task_id!r}")

    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    runner = ClaudeBoxRunner(
        claude_box=claude_box.resolve(),
        engine=args.engine,
        log_dir=root / "logs",
        timeout_s=args.timeout,
    )
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        probe = build_graph(runner, load_settings(repo), checkpointer=saver)
        if not args.resume and not args.task_id:
            task_id = _free_id(
                task_id,
                lambda t: bool(probe.get_state(_cfg(t)).values) or _branch_exists(repo, t),
            )
        config = _cfg(task_id)
        holder = Store(root, None, args.engine).pid(task_id)
        if holder:
            raise CliError(f"task {task_id} is already running (pid {holder}); cancel it first")
        if args.resume:
            snapshot = probe.get_state(config)
            if not snapshot.values:
                raise CliError(f"no such task: {task_id}")
            publish = _resume_publish(
                snapshot.values, force_on=args.publish, force_off=args.no_publish
            )
        else:
            publish = not args.no_publish
        graph = build_graph(
            runner,
            load_settings(repo),
            checkpointer=saver,
            github=GhGitHub() if publish else None,
            ci_timeout_s=args.ci_timeout,
        )
        if args.resume:
            snapshot = graph.get_state(config)
            values = snapshot.values
            if values["status"] == "done":
                print(summary(values))
                return 0
            # Turning publish on for a task that never had it: the branch needs its PR, so
            # resume at the publish step unless a coder run is the pending node anyway.
            turning_on = publish and not values.get("publish")
            extra: dict[str, Any] = {}
            if turning_on:
                extra = {
                    "publish": True,
                    "base_branch": values.get("base_branch") or _current_branch(repo),
                }
            if not snapshot.next:  # failed or exhausted: the graph ended, put it back on
                _reopen(graph, config, values, args.max_iterations, extra)
            elif args.max_iterations or turning_on:
                coded = any(s["role"] == "coder" for s in values.get("steps", []))
                at = "coder" if turning_on and coded and "coder" not in snapshot.next else None
                if args.max_iterations:
                    extra["max_iterations"] = args.max_iterations
                graph.update_state(config, extra, as_node=at or _last_node(values))
            print(
                f"resuming {task_id} at {', '.join(graph.get_state(config).next)}", file=sys.stderr
            )
            state = None
        else:
            if graph.get_state(config).values:
                raise CliError(f"task {task_id} already exists; --resume it or pick another id")
            state = initial_state(
                task_id=task_id,
                request=request,
                repo=str(repo),
                max_iterations=args.max_iterations or DEFAULT_MAX_ITERATIONS,
                publish=publish,
                base_branch=_current_branch(repo),
            )
            print(f"task {task_id} on {repo} (engine {args.engine})", file=sys.stderr)
        try:
            final = _drive(graph, state, config, root)
        except KeyboardInterrupt:
            _say(f"\ninterrupted; continue later with: claude-graph run --resume {task_id}")
            return 130
    print(summary(final))
    return 0 if final["status"] == "done" else 1


_STOP_WORDS = frozenset(
    "a an the and or of to for in on at by with is are be can which that this it me my i we "
    "you please into from as".split()
)


def _slug(text: str) -> str:
    """A task id from the request: ASCII, lowercase, up to five non-filler words that fit
    MAX_TASK_ID; a URL counts as its last path segment (a ticket key, a PR number)."""
    text = re.sub(r"https?://\S+", lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], text)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = [w for w in re.split(r"[^a-z0-9]+", ascii_text.lower()) if w and w not in _STOP_WORDS]
    if not words:
        return "task"
    slug = words[0][:MAX_TASK_ID]
    for word in words[1:5]:
        if len(slug) + 1 + len(word) > MAX_TASK_ID:
            break
        slug += f"-{word}"
    return slug


def _free_id(base: str, taken: Callable[[str], bool]) -> str:
    """`base`, or `base-2`, `base-3`, … for a request that ran before."""
    candidate, n = base, 2
    while taken(candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _branch_exists(repo: Path, name: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{name}"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _cfg(task_id: str) -> dict:
    return {"configurable": {"thread_id": task_id}}


def _current_branch(repo: Path) -> str:
    """The checked-out branch, which the pull request targets; `main` when detached."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "main"


def _last_node(values: TaskState) -> str:
    """Node whose edge leads to what runs next: the last finished step's role, or START."""
    steps = values.get("steps", [])
    return steps[-1]["role"] if steps else START


def _resume_publish(values: TaskState, *, force_on: bool, force_off: bool) -> bool:
    """`--publish` / `--no-publish` win; a task from before the publish step existed (no
    `publish` key) gets it; otherwise the task keeps what it was started with."""
    if force_off:
        return False
    if force_on or "publish" not in values:
        return True
    return bool(values["publish"])


def _reopen(
    graph: Any,
    config: dict,
    values: TaskState,
    new_cap: int | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Put a failed/exhausted task back on the graph: failed replays from the node before the
    failed one; exhausted takes a higher cap and re-routes from the exhausting node."""
    steps = values.get("steps", [])
    update: dict[str, Any] = {"status": "running", **(extra or {})}
    if values["status"] == "exhausted":
        cap = values["max_iterations"]
        if not new_cap or new_cap <= cap:
            raise CliError(
                f"task exhausted its {cap} coder runs; resume with --max-iterations > {cap}"
            )
        update["max_iterations"] = new_cap
        as_node = _last_node(values)
    else:
        as_node = steps[-2]["role"] if len(steps) >= 2 else START
        if new_cap:
            update["max_iterations"] = new_cap
    graph.update_state(config, update, as_node=as_node)


def _drive(graph: Any, state: TaskState | None, config: dict, root: Path) -> TaskState:
    """Run the graph, writing the task snapshot after every node so the UI can follow."""
    final: TaskState = state or {}
    task_id = (state or graph.get_state(config).values)["task_id"]
    pid_file = root / "tasks" / f"{task_id}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")  # the dashboard's liveness check
    # an interrupt outside the box wait is safe: snapshots atomic, checkpoints transactional
    previous = _interrupt_on_signals()
    try:
        # first snapshot: the task shows as running at once
        _write_snapshot(root, state or graph.get_state(config).values)
        for values in graph.stream(state, config, stream_mode="values"):
            final = values
            _write_snapshot(root, values)
    finally:
        _release_pid(pid_file)
        _restore_signals(previous)
    return final


def _release_pid(pid_file: Path) -> None:
    """Drop the pid file only if it is still ours; a run that died on a name collision must
    not erase the orchestrator that owns the task."""
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass


def _interrupt_on_signals() -> dict[int, Any]:
    """SIGTERM and SIGHUP take the Ctrl-C path, so the box is stopped and its record closed.
    A SIGHUP already ignored (nohup) stays ignored. Returns what `_restore_signals` needs.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(signum) is signal.SIG_IGN:
            continue
        previous[signum] = signal.signal(signum, interrupt)
    return previous


def _restore_signals(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        if handler is not None:  # None: installed outside Python; nothing to put back
            signal.signal(signum, handler)


def _say(text: str) -> None:
    try:
        print(text, file=sys.stderr)
    except OSError:  # the terminal that sent SIGHUP is gone
        pass


def cmd_ui(args: argparse.Namespace) -> int:
    claude_box = args.runner or find_claude_box()
    ctx = UiContext(
        state_dir=state_dir(), claude_box=claude_box, engine=args.engine, actions=args.actions
    )
    server = serve(ctx, port=args.port)
    print(f"claude-graph ui: http://127.0.0.1:{server.server_port}/  (Ctrl-C to stop)")
    if args.actions:
        print(
            "claude-graph ui: ACTIONS ENABLED — the page can start, cancel and delete tasks",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from claude_graph.setup import setup

    report = setup(skip_build=args.skip_build)
    print(report.text())
    return 0 if not report.todo else 1


def cmd_status(args: argparse.Namespace) -> int:
    path = state_dir() / "tasks" / f"{args.task_id}.json"
    if not path.is_file():
        raise CliError(f"no finished task {args.task_id} (still running? see `logs`)")
    print(summary(json.loads(path.read_text(encoding="utf-8"))))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    task_dir = state_dir() / "logs" / args.task_id
    if not task_dir.is_dir():
        raise CliError(f"no logs for task {args.task_id}")
    logs = sorted(task_dir.glob("*.log"))
    if args.role:
        logs = [p for p in logs if p.stem.endswith(f"-{args.role}")]
        if not logs:
            raise CliError(f"no {args.role} log for task {args.task_id}")
        sys.stdout.write(logs[-1].read_text(encoding="utf-8", errors="replace"))
        return 0
    for p in logs:
        print(p)
    return 0


def summary(state: TaskState) -> str:
    steps = state.get("steps", [])
    cost = sum(s["cost_usd"] or 0 for s in steps)
    lines = [
        f"task {state['task_id']}: {state['status']}",
        f"  branch:     {state.get('branch', '-')}",
        f"  base:       {state.get('base_sha', '-')}",
        *([f"  pr:         {state['pr_url']}"] if state.get("pr_url") else []),
        *([f"  ci:         {state['ci_status']}"] if state.get("ci_status") else []),
        f"  iterations: {state.get('iteration', 0)}/{state.get('max_iterations', '?')}",
        f"  boxes:      {len(steps)}  (${cost:.2f})",
    ]
    for s in steps:
        mark = "" if s["parsed_ok"] else "  (no report)"
        lines.append(
            f"    {s['attempt']}. {s['role']:<9} exit {s['exit_code']} {s['duration_s']:.0f}s{mark}"
        )
    if state.get("review_notes"):
        lines += ["  review:", _indent(state["review_notes"])]
    elif state.get("qa_report") and not state.get("qa_passed"):
        lines += ["  qa:", _indent(state["qa_report"])]
    return "\n".join(lines)


def _indent(text: str, limit: int = 1500) -> str:
    return "\n".join("    " + line for line in text[:limit].splitlines())


def _write_snapshot(root: Path, state: TaskState) -> None:
    tasks = root / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    body = {**state, "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    tmp = tasks / f".{state['task_id']}.json.tmp"
    tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    tmp.replace(tasks / f"{state['task_id']}.json")  # atomic: the UI never reads a half file


if __name__ == "__main__":  # `python -m claude_graph.cli`: how the dashboard spawns a run
    sys.exit(main())
