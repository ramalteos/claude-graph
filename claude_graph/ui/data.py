"""Everything the dashboard reads: tasks, boxes, spend, attention, timeline, diffs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from claude_graph.config import DEFAULT_EFFORT, DEFAULT_MODELS
from claude_graph.login import login_verdict
from claude_graph.roles import ROLE_NAMES

API_VERSION = 1
STALL_AFTER_S = 1200  # a box silent this long is worth a look


class Loaded(NamedTuple):
    """One task's files, read once per request: snapshot, box records, live orchestrator pid."""

    snap: dict | None
    metas: list[dict]
    pid: int | None


def _is_orchestrator(pid: int) -> bool:
    """A pid file outlives a SIGKILLed orchestrator and pids get reused: only a process whose
    command line names this package counts. Unanswerable (no /proc, no ps) → trusted.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:  # Linux
            argv = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        try:  # macOS
            argv = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return True
    return "claude_graph" in argv or "claude-graph" in argv


@dataclass
class Store:
    """Reads under the claude-graph state dir. Pure reads except the git/engine probes."""

    state_dir: Path
    claude_box: Path | None
    engine: str
    _cache: dict[str, tuple[float, object]] = field(default_factory=dict, repr=False)

    @property
    def runner_name(self) -> str:
        return self.claude_box.name if self.claude_box else "abox"

    # --- raw files -----------------------------------------------------------------
    def snapshot(self, task_id: str) -> dict | None:
        path = self.state_dir / "tasks" / f"{task_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def metas(self, task_id: str) -> list[dict]:
        out = []
        for p in sorted((self.state_dir / "logs" / task_id).glob("*.meta.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def task_ids(self) -> list[str]:
        ids = {
            p.stem for p in (self.state_dir / "tasks").glob("*.json") if not p.name.startswith(".")
        }
        ids |= {p.name for p in (self.state_dir / "logs").glob("*") if p.is_dir()}
        return sorted(ids)

    def pid(self, task_id: str) -> int | None:
        path = self.state_dir / "tasks" / f"{task_id}.pid"
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return pid if _is_orchestrator(pid) else None

    def load(self) -> dict[str, Loaded]:
        """Every task's files, read once; the views below take this so a poll reads each once."""
        return {t: Loaded(self.snapshot(t), self.metas(t), self.pid(t)) for t in self.task_ids()}

    # --- assembled views -------------------------------------------------------------
    def tasks(self, loaded: dict[str, Loaded] | None = None) -> list[dict]:
        out = []
        for task_id, (snap, metas, pid) in _loaded(self, loaded).items():
            snap = snap or {"task_id": task_id, "status": "starting", "steps": []}
            running = next((m for m in metas if "finished" not in m), None)
            status = snap.get("status", "starting")
            if status in ("running", "starting") and not pid:
                status = "stopped"  # the orchestrator is gone; --resume continues the task
                running = None  # an open box record without an orchestrator is stale
            steps = snap.get("steps", [])
            out.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "request": snap.get("request", ""),
                    "repo": snap.get("repo"),
                    "branch": snap.get("branch"),
                    "base_sha": snap.get("base_sha"),
                    "worktree": snap.get("worktree"),
                    "worktree_present": bool(snap.get("worktree"))
                    and Path(snap["worktree"]).is_dir(),
                    "iteration": snap.get("iteration", 0),
                    "max_iterations": snap.get("max_iterations"),
                    "steps": steps,
                    "cost": round(sum(s.get("cost_usd") or 0 for s in steps), 4),
                    "started_at": metas[0].get("started") if metas else None,
                    "updated_at": snap.get("updated_at"),
                    "running": _box_view(task_id, running) if running else None,
                    "pid": pid,
                    "has": {
                        k: bool(snap.get(k)) for k in ("spec", "plan", "qa_report", "review_notes")
                    },
                    "review_verdict": snap.get("review_verdict"),
                    "qa_passed": snap.get("qa_passed"),
                    "progress": self.progress(snap),
                    "next_action": _next_action(status, snap, running),
                }
            )
        return sorted(
            out, key=lambda t: t.get("updated_at") or t.get("started_at") or "", reverse=True
        )

    def boxes(self, loaded: dict[str, Loaded] | None = None) -> list[dict]:
        """Boxes in flight: an open record whose orchestrator is still alive."""
        out = []
        for task_id, (_, metas, pid) in _loaded(self, loaded).items():
            if not pid:
                continue
            for m in metas:
                if "finished" not in m:
                    out.append(_box_view(task_id, m))
        return sorted(out, key=lambda b: b.get("started") or "")

    def engine_boxes(self) -> list[str]:
        return self._cached("ps", 3, self._ps)

    def timeline(self, limit: int = 60, loaded: dict[str, Loaded] | None = None) -> list[dict]:
        events = []
        for task_id, (_, metas, _pid) in _loaded(self, loaded).items():
            for m in metas:
                events.append({**_box_view(task_id, m), "finished": m.get("finished")})
        events.sort(key=lambda e: e.get("finished") or e.get("started") or "", reverse=True)
        return events[:limit]

    def role_stats(self, loaded: dict[str, Loaded] | None = None) -> list[dict]:
        acc: dict[str, dict] = {
            r: {"role": r, "runs": 0, "ok": 0, "secs": 0.0, "cost": 0.0} for r in ROLE_NAMES
        }
        for snap, _, _pid in _loaded(self, loaded).values():
            for s in (snap or {}).get("steps", []):
                a = acc.setdefault(
                    s["role"], {"role": s["role"], "runs": 0, "ok": 0, "secs": 0.0, "cost": 0.0}
                )
                a["runs"] += 1
                a["ok"] += 1 if s.get("parsed_ok") else 0
                a["secs"] += s.get("duration_s") or 0
                a["cost"] += s.get("cost_usd") or 0
        return [
            {
                **a,
                "avg_s": round(a["secs"] / a["runs"]) if a["runs"] else 0,
                "cost": round(a["cost"], 4),
            }
            for a in acc.values()
        ]

    def spend(self, tasks: list[dict]) -> dict:
        today = datetime.now(UTC).date().isoformat()
        total = sum(t["cost"] for t in tasks)
        today_cost = sum(t["cost"] for t in tasks if (t.get("updated_at") or "")[:10] == today)
        return {"total": round(total, 2), "today": round(today_cost, 2)}

    def logs(self) -> list[dict]:
        out = []
        for task_id in self.task_ids():
            for p in sorted((self.state_dir / "logs" / task_id).glob("*.log")):
                st = p.stat()
                out.append(
                    {"task_id": task_id, "name": p.name, "size": st.st_size, "mtime": st.st_mtime}
                )
        return sorted(out, key=lambda x: x["mtime"], reverse=True)

    def attention(self, tasks: list[dict]) -> list[dict]:
        items = []
        verdict = login_verdict()
        if verdict in ("missing", "expired", "refresh-expired", "unreadable"):
            items.append(
                {
                    "kind": "login",
                    "text": f"sandbox login is {verdict}: "
                    f"run `{self.runner_name} <repo>` and /login",
                }
            )
        for t in tasks:
            if t["status"] in ("failed", "exhausted", "stopped"):
                items.append({"kind": "task", "task_id": t["task_id"], "text": t["next_action"]})
            box = t.get("running")
            if box and box.get("elapsed_s", 0) > STALL_AFTER_S:
                items.append(
                    {
                        "kind": "box",
                        "task_id": t["task_id"],
                        "box": box["box"],
                        "text": f"{box['role']} box has run {box['elapsed_s'] // 60} min",
                    }
                )
        return items

    def progress(self, snap: dict) -> dict | None:
        repo, branch, base = snap.get("repo"), snap.get("branch"), snap.get("base_sha")
        if not (repo and branch and base):
            return None
        return self._cached(
            f"progress:{snap['task_id']}", 10, lambda: _git_progress(Path(repo), base, branch)
        )

    def diff(self, task_id: str) -> str:
        snap = self.snapshot(task_id)
        if not snap or not snap.get("branch"):
            return "no branch yet"
        repo = Path(snap["repo"])
        base, branch = snap["base_sha"], snap["branch"]
        log = _git(repo, ["log", "--oneline", f"{base}..{branch}"], 200_000)
        stat = _git(repo, ["diff", "--stat", f"{base}...{branch}"], 200_000)
        diff = _git(repo, ["diff", f"{base}...{branch}"], 400_000)
        return f"commits:\n{log or '  (none)'}\n\n{stat}\n{diff}"

    def document(self, task_id: str) -> dict | None:
        snap = self.snapshot(task_id)
        if not snap:
            return None
        keys = (
            "task_id",
            "status",
            "request",
            "repo",
            "branch",
            "base_sha",
            "worktree",
            "iteration",
            "max_iterations",
            "spec",
            "acceptance_criteria",
            "plan",
            "coder_summary",
            "qa_passed",
            "qa_report",
            "review_verdict",
            "review_notes",
            "steps",
            "updated_at",
        )
        return {k: snap.get(k) for k in keys}

    def state(self) -> dict:
        loaded = self.load()
        tasks = self.tasks(loaded)
        return {
            "api_version": API_VERSION,
            "now": datetime.now(UTC).isoformat(timespec="seconds"),
            "engine": self.engine,
            "engine_boxes": self.engine_boxes(),
            "login": login_verdict(),
            "state_dir": str(self.state_dir),
            "tasks": tasks,
            "boxes": self.boxes(loaded),
            "attention": self.attention(tasks),
            "timeline": self.timeline(loaded=loaded),
            "role_stats": self.role_stats(loaded),
            "spend": self.spend(tasks),
            "logs": self.logs(),
            "config": {
                "models": DEFAULT_MODELS,
                "effort": DEFAULT_EFFORT,
                "roles": list(ROLE_NAMES),
            },
        }

    # --- helpers ---------------------------------------------------------------------
    def _cached(self, key: str, ttl: float, compute):
        stamp, value = self._cache.get(key, (0.0, None))
        if time.monotonic() - stamp > ttl:
            value = compute()
            self._cache[key] = (time.monotonic(), value)
        return value

    def _ps(self) -> list[str]:
        if not self.claude_box:
            return []
        try:
            proc = subprocess.run(
                [str(self.claude_box), "ps"],
                capture_output=True,
                text=True,
                timeout=20,
                env={**os.environ, "CLAUDE_BOX_ENGINE": self.engine},
            )
            return [ln for ln in proc.stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"{self.runner_name} ps failed: {e}"]


def _loaded(store: Store, loaded: dict[str, Loaded] | None) -> dict[str, Loaded]:
    return loaded if loaded is not None else store.load()  # {} means "no tasks", not "reload"


def _box_view(task_id: str, m: dict) -> dict:
    started = m.get("started")
    elapsed = None
    if started and "finished" not in m:
        try:
            elapsed = int(time.time() - datetime.fromisoformat(started).timestamp())
        except ValueError:
            elapsed = None
    return {
        "task_id": task_id,
        "role": m.get("role"),
        "attempt": m.get("attempt"),
        "box": m.get("box"),
        "model": m.get("model"),
        "effort": m.get("effort"),
        "log": m.get("log"),
        "started": started,
        "elapsed_s": elapsed,
        "exit_code": m.get("exit_code"),
        "duration_s": m.get("duration_s"),
        "cost_usd": m.get("cost_usd"),
    }


def _next_action(status: str, snap: dict, running: dict | None) -> str:
    if running:
        return f"{running.get('role')} is in a box (attempt {running.get('attempt')})"
    if status == "done":
        return f"review and merge {snap.get('branch')}"
    if status == "exhausted":
        return (
            f"gave up after {snap.get('iteration')} coder runs — read the QA/review notes, "
            "fix by hand or resume with a higher cap"
        )
    if status == "failed":
        last = (snap.get("steps") or [{}])[-1]
        return (
            f"{last.get('role', 'a')} box failed (exit {last.get('exit_code')}) — "
            "open its log, then resume"
        )
    if status == "stopped":
        return (
            "orchestrator is not running (Ctrl-C or crash) — "
            "resume to continue from the last finished step"
        )
    return "waiting for the next node"


def _git(repo: Path, args: list[str], limit: int) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git failed: {e}"
    out = proc.stdout if proc.returncode == 0 else proc.stderr
    return out[-limit:] if len(out) > limit else out


def _git_progress(repo: Path, base: str, branch: str) -> dict | None:
    if not repo.is_dir():
        return None
    try:
        check = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if check.returncode != 0:
        return {"commits": 0, "shortstat": "", "branch_exists": False}
    commits = _git(repo, ["rev-list", "--count", f"{base}..{branch}"], 100).strip()
    shortstat = _git(repo, ["diff", "--shortstat", f"{base}...{branch}"], 500).strip()
    return {
        "commits": int(commits) if commits.isdigit() else 0,
        "shortstat": shortstat,
        "branch_exists": True,
    }
