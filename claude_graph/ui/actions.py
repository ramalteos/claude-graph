"""Page actions: each is the CLI or the runner, validated here before anything is spent."""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_graph.state import TASK_ID
from claude_graph.ui.data import Store

BOX_NAME = re.compile(r"^claude-box-graph-[A-Za-z0-9._-]{1,80}\Z")
SUPPORTED = ("run", "resume", "stop-box", "cancel", "remove-worktree", "delete-task", "purge")


@dataclass
class Actions:
    store: Store
    claude_box: Path | None
    engine: str
    runner_argv: list[str] | None = None  # how to start the orchestrator; default: this venv

    def do(self, name: str, arg: str) -> dict:
        if name not in SUPPORTED:
            return _no(f"unsupported action {name!r}; supported: {', '.join(SUPPORTED)}")
        try:
            return getattr(self, "_" + name.replace("-", "_"))(arg)
        except Exception as e:  # noqa: BLE001 — report, never drop the connection
            return _no(f"{type(e).__name__}: {e}")

    # --- starting work -----------------------------------------------------------------
    def _run(self, arg: str) -> dict:
        try:
            spec = json.loads(arg or "{}")
        except json.JSONDecodeError:
            return _no("run needs a JSON object: repo, request, task_id?, max_iterations?")
        if not isinstance(spec, dict):
            return _no("run needs a JSON object")
        repo = Path(str(spec.get("repo", ""))).expanduser()
        request = str(spec.get("request", "")).strip()
        task_id = str(spec.get("task_id") or "").strip()
        if not repo.is_dir() or not (repo / ".git").exists():
            return _no(f"not a git repo: {repo}")
        if not request:
            return _no("request is empty")
        options: list[str] = []
        if task_id:
            if not TASK_ID.match(task_id):
                return _no(f"task id must match {TASK_ID.pattern}")
            if self.store.snapshot(task_id):
                return _no(f"task {task_id} already exists")
            options += ["--task-id", task_id]
        max_iter = spec.get("max_iterations")
        if max_iter is not None:
            if not (isinstance(max_iter, int) and 1 <= max_iter <= 20):
                return _no("max_iterations must be an integer from 1 to 20")
            options += ["--max-iterations", str(max_iter)]
        return self._spawn(options, [str(repo.resolve()), request], task_id or "(timestamp id)")

    def _resume(self, task_id: str) -> dict:
        if not TASK_ID.match(task_id):
            return _no("bad task id")
        snap = self.store.snapshot(task_id)
        if not snap:
            return _no(f"no task {task_id}")
        if self.store.pid(task_id):
            return _no(f"task {task_id} is already running")
        if snap.get("status") == "done":
            return _no(f"task {task_id} is done; nothing to resume")
        if snap.get("status") == "exhausted":
            cap = snap.get("max_iterations")
            return _no(
                f"task {task_id} exhausted its {cap} coder runs; resume from a terminal with "
                f"--max-iterations > {cap}"
            )
        return self._spawn(["--resume", task_id], [snap["repo"]], task_id)

    def _spawn(self, options: list[str], positionals: list[str], label: str) -> dict:
        """`claude-graph run <options> -- <positionals>`; `--` protects a leading-dash request."""
        base = self.runner_argv or [sys.executable, "-m", "claude_graph.cli"]
        full = [*base, "run", *options, "--engine", self.engine]
        if self.claude_box:
            full += ["--runner", str(self.claude_box)]
        full += ["--", *positionals]
        out_dir = self.store.state_dir / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{label.replace('(', '').replace(')', '').replace(' ', '-')}.out"
        with out_path.open("ab") as out:  # the child inherits the fd; ours closes here
            proc = subprocess.Popen(
                full,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
            )
        return {"ok": True, "output": f"started {label} (pid {proc.pid})\n{' '.join(full)}"}

    # --- stopping work -----------------------------------------------------------------
    def _stop_box(self, box: str) -> dict:
        if not BOX_NAME.match(box):
            return _no("bad box name")
        return self._claude_box(["stop", box])

    def _cancel(self, task_id: str) -> dict:
        if not TASK_ID.match(task_id):
            return _no("bad task id")
        pid = self.store.pid(task_id)
        notes, ok = [], True
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                notes.append(f"sent SIGTERM to orchestrator pid {pid}")
            except ProcessLookupError:  # it exited between the check and the signal
                notes.append(f"orchestrator pid {pid} already gone")
        for box in self._open_boxes(task_id):
            note, stopped = self._stop_box_note(box)
            notes.append(note)
            ok = ok and stopped
        return {"ok": ok, "output": "\n".join(notes) or "nothing was running"}

    # --- cleaning up -------------------------------------------------------------------
    def _remove_worktree(self, task_id: str) -> dict:
        if not TASK_ID.match(task_id):
            return _no("bad task id")
        snap = self.store.snapshot(task_id)
        if not snap or not snap.get("repo"):
            return _no(f"no task {task_id}")
        if self.store.pid(task_id):
            return _no(f"task {task_id} is still running")
        return self._claude_box(["headless", snap["repo"], "--worktree", task_id, "--remove"])

    def _delete_task(self, task_id: str) -> dict:
        if not TASK_ID.match(task_id):
            return _no("bad task id")
        if self.store.pid(task_id):
            return _no(f"task {task_id} is still running — cancel it first")
        notes = self._stop_stale_boxes(task_id)
        if self.store.pid(task_id):  # resumed while the stops ran
            return _no(f"task {task_id} is still running — cancel it first")
        removed = self._forget(task_id)
        return {
            "ok": True,
            "output": "\n".join(
                [*notes, f"deleted {task_id}: {removed}. The branch and worktree are untouched."]
            ),
        }

    def _purge(self, _: str) -> dict:
        kept, removed, notes = [], [], []
        for task_id in self.store.task_ids():
            if self.store.pid(task_id):
                kept.append(task_id)
            else:
                notes += self._stop_stale_boxes(task_id)
                self._forget(task_id)
                removed.append(task_id)
        head = f"purged {len(removed)} task(s)" + (
            f"; kept running: {', '.join(kept)}" if kept else ""
        )
        return {"ok": True, "output": "\n".join([head, *notes])}

    def _open_boxes(self, task_id: str) -> list[str]:
        """Boxes whose record never got `finished`."""
        return [
            m["box"]
            for m in self.store.metas(task_id)
            if "finished" not in m and m.get("box") and BOX_NAME.match(m["box"])
        ]

    def _stop_stale_boxes(self, task_id: str) -> list[str]:
        """Stop boxes left open by a dead orchestrator (SIGKILL, crash), best effort."""
        return [self._stop_box_note(box, "stale box")[0] for box in self._open_boxes(task_id)]

    def _stop_box_note(self, box: str, kind: str = "box") -> tuple[str, bool]:
        """`abox stop` plus a one-line note of what happened."""
        res = self._claude_box(["stop", box])
        if res["ok"]:
            return f"stopped {kind} {box}", True
        tail = res["output"].strip()[-200:]
        return f"could not stop {kind} {box} ({tail}); check the engine panel", False

    def _forget(self, task_id: str) -> str:
        root = self.store.state_dir
        bits = []
        for p in (
            root / "tasks" / f"{task_id}.json",
            root / "tasks" / f"{task_id}.pid",
            root / "runs" / f"{task_id}.out",
        ):
            if p.exists():
                p.unlink()
                bits.append(p.name)
        log_dir = root / "logs" / task_id
        if log_dir.is_dir():
            for p in log_dir.iterdir():
                p.unlink()
            log_dir.rmdir()
            bits.append("logs/")
        db = root / "checkpoints.sqlite"
        if db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(db, timeout=1)) as conn, conn:
                    for table in ("checkpoints", "writes"):
                        conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (task_id,))  # noqa: S608
                bits.append("checkpoints")
            except sqlite3.OperationalError as e:  # locked by a live run, or no such table yet
                bits.append(f"checkpoints: {e}, left in place")
        return ", ".join(bits) or "nothing on disk"

    def _claude_box(self, args: list[str]) -> dict:
        if not self.claude_box:
            return _no("runner not configured")
        try:
            proc = subprocess.run(
                [str(self.claude_box), *args],
                capture_output=True,
                text=True,
                timeout=600,
                env={**os.environ, "CLAUDE_BOX_ENGINE": self.engine},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return _no(f"{self.claude_box} {' '.join(args)}: {e}")
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-4000:]}


def _no(msg: str) -> dict:
    return {"ok": False, "output": msg}
