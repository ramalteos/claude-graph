"""Launch one headless role box through the sandbox runner and read back its result.

The runner owns worktree, mounts and engine; this module owns the subprocess and the log.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

TIMEOUT_EXIT = 124  # what `timeout(1)` reports; a stopped box is reported the same way
START_FAILED_EXIT = 127  # what a shell reports for a command it could not run
INTERRUPT_EXIT = 130  # the shell convention for SIGINT
BOX_PREFIX = "claude-box-"  # the runner prepends this to every --name
BRANCH_PREFIX = ""  # the task branch is the task id until the pm names the change


def box_name(task_id: str, role: str, attempt: int) -> str:
    return f"graph-{task_id}-{role}-{attempt}"


@dataclass(frozen=True)
class BoxResult:
    exit_code: int
    result_text: str | None  # `result` of the final stream-json event, when there was one
    is_error: bool
    duration_s: float
    cost_usd: float | None
    log_path: Path
    box: str | None
    worktree: Path | None
    branch: str | None
    base_sha: str | None


class BoxRunner(Protocol):
    def run(
        self,
        *,
        role: str,
        task_id: str,
        attempt: int,
        repo: Path,
        system_prompt: str,
        prompt: str,
        model: str,
        effort: str,
    ) -> BoxResult: ...

    def remove_worktree(self, *, task_id: str, repo: Path) -> None: ...


@dataclass(frozen=True)
class ClaudeBoxRunner:
    claude_box: Path
    engine: str
    log_dir: Path
    timeout_s: int = 3600

    def run(
        self,
        *,
        role: str,
        task_id: str,
        attempt: int,
        repo: Path,
        system_prompt: str,
        prompt: str,
        model: str,
        effort: str,
    ) -> BoxResult:
        name = box_name(task_id, role, attempt)
        stem = self.log_dir / task_id / f"{attempt:02d}-{role}"
        stem.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = stem.with_suffix(".prompt.md")
        system_file = stem.with_suffix(".system.md")
        log = stem.with_suffix(".log")
        prompt_file.write_text(prompt, encoding="utf-8")
        system_file.write_text(system_prompt, encoding="utf-8")
        argv = [
            str(self.claude_box),
            "headless",
            str(repo),
            "--name",
            name,
            "--worktree",
            task_id,
            "--branch",
            BRANCH_PREFIX + task_id,
            "--prompt-file",
            str(prompt_file),
            "--system-prompt-file",
            str(system_file),
            "--model",
            model,
            "--effort",
            effort,
        ]
        meta = stem.with_suffix(".meta.json")
        meta_body = {
            "role": role,
            "attempt": attempt,
            "box": BOX_PREFIX + name,
            "log": log.name,
            "model": model,
            "effort": effort,
            "started": _now(),
        }
        meta.write_text(json.dumps(meta_body), encoding="utf-8")
        started = time.monotonic()
        with log.open("wb") as out:
            try:
                # stdin closed: `claude -p` reads a non-TTY stdin to EOF
                exit_code = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    env=self._env(),
                    timeout=self.timeout_s,
                    check=False,
                ).returncode
            except subprocess.TimeoutExpired:
                # Killing the client does not stop the container on every engine.
                self._stop(BOX_PREFIX + name)
                exit_code = TIMEOUT_EXIT
            except OSError as e:
                # runner could not start: a failed step with the reason in its log
                out.write(
                    f"claude-graph: cannot start the runner {self.claude_box}: {e}\n".encode()
                )
                exit_code = START_FAILED_EXIT
            except KeyboardInterrupt:
                # Apple `container` cannot forward SIGINT: stop the box and close its record
                self._stop(BOX_PREFIX + name)
                meta_body.update(finished=_now(), exit_code=INTERRUPT_EXIT, interrupted=True)
                meta.write_text(json.dumps(meta_body), encoding="utf-8")
                raise
        result = read_log(log, exit_code=exit_code, duration_s=time.monotonic() - started)
        meta_body.update(
            finished=_now(),
            exit_code=exit_code,
            duration_s=result.duration_s,
            cost_usd=result.cost_usd,
            is_error=result.is_error,
        )
        meta.write_text(json.dumps(meta_body), encoding="utf-8")
        return result

    def remove_worktree(self, *, task_id: str, repo: Path) -> None:
        proc = subprocess.run(
            [str(self.claude_box), "headless", str(repo), "--worktree", task_id, "--remove"],
            env=self._env(),
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"worktree removal failed: {(proc.stderr or proc.stdout).strip()[-500:]}"
            )

    def _env(self) -> dict[str, str]:
        return {**os.environ, "CLAUDE_BOX_ENGINE": self.engine}

    def _stop(self, box: str) -> None:
        """Best effort: runs inside the timeout and Ctrl-C paths, which must not raise."""
        try:
            subprocess.run(
                [str(self.claude_box), "stop", box],
                env=self._env(),
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def split_lines(text: str) -> list[str]:
    """Split on "\n" only: `str.splitlines` also breaks on U+2028/U+2029, which JSON leaves
    raw inside strings.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def read_log(log: Path, *, exit_code: int, duration_s: float) -> BoxResult:
    """The meta lines claude-box prints plus the final `result` event of the claude stream."""
    meta: dict[str, str] = {}
    result_text: str | None = None
    is_error = False
    cost: float | None = None
    for line in split_lines(log.read_text(encoding="utf-8", errors="replace")):
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result_text = event.get("result")
                is_error = bool(event.get("is_error"))
                cost = event.get("total_cost_usd")
            continue
        key, sep, value = line.partition("=")
        if sep and key in ("worktree", "branch", "base", "box"):
            meta[key] = value
    return BoxResult(
        exit_code=exit_code,
        result_text=result_text,
        is_error=is_error,
        duration_s=duration_s,
        cost_usd=cost,
        log_path=log,
        box=meta.get("box"),
        worktree=Path(meta["worktree"]) if "worktree" in meta else None,
        branch=meta.get("branch"),
        base_sha=meta.get("base"),
    )
