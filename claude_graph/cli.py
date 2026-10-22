"""claude-graph: run one task through the graph, or look at one that ran."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from claude_graph.boxes import ClaudeBoxRunner
from claude_graph.config import DEFAULT_ENGINE, find_claude_box, load_settings, state_dir
from claude_graph.graph import build_graph
from claude_graph.state import DEFAULT_MAX_ITERATIONS, TASK_ID, TaskState, initial_state
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


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-graph")
    sub = p.add_subparsers(required=True)

    run = sub.add_parser("run", help="run a request through architect → coder → qa → reviewer")
    run.add_argument(
        "words",
        nargs="*",
        metavar="[repo] request",
        help="the repo (default: the current directory) and what you want; omit with --resume",
    )
    run.add_argument("--task-id", help="[A-Za-z0-9._-]{1,40}; default: a timestamp")
    run.add_argument("--resume", metavar="TASK_ID", help="continue a task from its last checkpoint")
    run.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    run.add_argument("--engine", default=DEFAULT_ENGINE, help="container (default), docker, fake")
    run.add_argument(
        "--runner",
        type=Path,
        help="path to the box runner (abox); default: abox or claude-box on PATH",
    )
    run.add_argument("--timeout", type=int, default=3600, help="seconds per box")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="final/last state of a task")
    status.add_argument("task_id")
    status.set_defaults(func=cmd_status)

    ui = sub.add_parser("ui", help="dashboard on localhost: tasks, steps, live box logs")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--engine", default=DEFAULT_ENGINE)
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


def _split_target(words: list[str]) -> tuple[Path, str]:
    """`run <repo> <request…>` or `run <request…>` from inside the repo, like the sandbox CLI."""
    if words and Path(words[0]).is_dir():
        return Path(words[0]).resolve(), " ".join(words[1:])
    return Path.cwd().resolve(), " ".join(words)


def cmd_run(args: argparse.Namespace) -> int:
    repo, request = _split_target(args.words)
    if not (repo / ".git").exists():
        raise CliError(f"not a git repo: {repo}")
    claude_box = args.runner or find_claude_box()
    if claude_box is None or not claude_box.is_file():
        raise CliError("runner not found; put abox on PATH or pass --runner <path>")
    task_id = args.resume or args.task_id or time.strftime("t%Y%m%d-%H%M%S")
    if not TASK_ID.match(task_id):
        raise CliError(f"task id must match {TASK_ID.pattern}: {task_id!r}")
    if not args.resume and not request:
        raise CliError("a request is required (or --resume <task-id>)")

    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    runner = ClaudeBoxRunner(
        claude_box=claude_box.resolve(),
        engine=args.engine,
        log_dir=root / "logs",
        timeout_s=args.timeout,
    )
    config = {"configurable": {"thread_id": task_id}}
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        graph = build_graph(runner, load_settings(repo), checkpointer=saver)
        if args.resume:
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise CliError(f"no such task: {task_id}")
            if not snapshot.next:
                final = snapshot.values
                print(summary(final))
                return 0 if final["status"] == "done" else 1
            print(f"resuming {task_id} at {', '.join(snapshot.next)}", file=sys.stderr)
            try:
                final = _drive(graph, None, config, root)
            except KeyboardInterrupt:
                print(
                    f"\ninterrupted; continue later with: claude-graph run --resume {task_id}",
                    file=sys.stderr,
                )
                return 130
        else:
            if graph.get_state(config).values:
                raise CliError(f"task {task_id} already exists; --resume it or pick another id")
            state = initial_state(
                task_id=task_id,
                request=request,
                repo=str(repo),
                max_iterations=args.max_iterations,
            )
            print(f"task {task_id} on {repo} (engine {args.engine})", file=sys.stderr)
            try:
                final = _drive(graph, state, config, root)
            except KeyboardInterrupt:
                print(
                    f"\ninterrupted; continue later with: claude-graph run --resume {task_id}",
                    file=sys.stderr,
                )
                return 130
    print(summary(final))
    return 0 if final["status"] == "done" else 1


def _drive(graph: Any, state: TaskState | None, config: dict, root: Path) -> TaskState:
    """Run the graph, writing the task snapshot after every node so the UI can follow."""
    final: TaskState = state or {}
    task_id = (state or graph.get_state(config).values)["task_id"]
    pid_file = root / "tasks" / f"{task_id}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))  # the dashboard's "is the orchestrator alive" check
    try:
        if state:
            _write_snapshot(root, state)
        for values in graph.stream(state, config, stream_mode="values"):
            final = values
            _write_snapshot(root, values)
    finally:
        pid_file.unlink(missing_ok=True)
    return final


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
    print(summary(json.loads(path.read_text())))
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
        sys.stdout.write(logs[-1].read_text(errors="replace"))
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
    tmp.write_text(json.dumps(body, indent=2, default=str))
    tmp.replace(tasks / f"{state['task_id']}.json")  # atomic: the UI never reads a half file
