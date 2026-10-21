"""The actions the page may trigger. Each one is the CLI or claude-box, never a second
implementation; every argument is validated here before anything is spent."""

from __future__ import annotations

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

BOX_NAME = re.compile(r"^claude-box-graph-[A-Za-z0-9._-]{1,80}$")
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
        argv = ["run", str(repo.resolve()), request]
        if task_id:
            if not TASK_ID.match(task_id):
                return _no(f"task id must match {TASK_ID.pattern}")
            if self.store.snapshot(task_id):
                return _no(f"task {task_id} already exists")
            argv += ["--task-id", task_id]
        max_iter = spec.get("max_iterations")
        if max_iter is not None:
            if not (isinstance(max_iter, int) and 1 <= max_iter <= 20):
                return _no("max_iterations must be an integer from 1 to 20")
            argv += ["--max-iterations", str(max_iter)]
        return self._spawn(argv, task_id or "(timestamp id)")

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
        return self._spawn(["run", snap["repo"], "--resume", task_id], task_id)

    def _spawn(self, argv: list[str], label: str) -> dict:
        base = self.runner_argv or [sys.executable, "-m", "claude_graph.cli"]
        full = [*base, *argv, "--engine", self.engine]
        if self.claude_box:
            full += ["--runner", str(self.claude_box)]
        out_dir = self.store.state_dir / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = (out_dir / f"{label.replace('(', '').replace(')', '').replace(' ', '-')}.out").open(
            "ab"
        )
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
        notes = []
        if pid:
            os.kill(pid, signal.SIGTERM)
            notes.append(f"sent SIGTERM to orchestrator pid {pid}")
        for m in self.store.metas(task_id):
            if "finished" not in m and m.get("box") and BOX_NAME.match(m["box"]):
                notes.append(
                    self._claude_box(["stop", m["box"]])["output"].strip() or f"stopped {m['box']}"
                )
        return {"ok": True, "output": "\n".join(notes) or "nothing was running"}

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
        if self.store.pid(task_id) or any("finished" not in m for m in self.store.metas(task_id)):
            return _no(f"task {task_id} is still running — cancel it first")
        removed = self._forget(task_id)
        return {
            "ok": True,
            "output": f"deleted {task_id}: {removed}. The branch and worktree are untouched.",
        }

    def _purge(self, _: str) -> dict:
        kept, removed = [], []
        for task_id in self.store.task_ids():
            if self.store.pid(task_id) or any(
                "finished" not in m for m in self.store.metas(task_id)
            ):
                kept.append(task_id)
            else:
                self._forget(task_id)
                removed.append(task_id)
        return {
            "ok": True,
            "output": f"purged {len(removed)} task(s)"
            + (f"; kept running: {', '.join(kept)}" if kept else ""),
        }

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
            with sqlite3.connect(db) as conn:
                for table in ("checkpoints", "writes"):
                    try:
                        conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (task_id,))  # noqa: S608
                    except sqlite3.OperationalError:
                        continue
            bits.append("checkpoints")
        return ", ".join(bits) or "nothing on disk"

    def _claude_box(self, args: list[str]) -> dict:
        if not self.claude_box:
            return _no("claude-box not configured")
        proc = subprocess.run(
            [str(self.claude_box), *args],
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "CLAUDE_BOX_ENGINE": self.engine},
        )
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-4000:]}


def _no(msg: str) -> dict:
    return {"ok": False, "output": msg}
