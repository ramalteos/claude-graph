"""The graph's shared state: one task, from request to reviewed branch."""

from __future__ import annotations

import operator
import re
from typing import Annotated, Literal, TypedDict

Status = Literal["running", "done", "exhausted", "failed"]
Verdict = Literal["approve", "changes", "redesign"]
DEFAULT_MAX_ITERATIONS = 10
# Apple `container` caps a container id at 63 chars; the longest box name is
# claude-box-graph-<task id>-architect-NN.
MAX_TASK_ID = 33
# git-valid branch suffix: no leading dot/dash, trailing dot, "..", ".lock"; \Z: no newline
TASK_ID = re.compile(
    rf"^(?![.-])(?!.*\.\.)(?!.*\.lock\Z)[A-Za-z0-9._-]{{1,{MAX_TASK_ID}}}(?<!\.)\Z"
)


class StepRecord(TypedDict):
    """One box run. A TypedDict, not a dataclass, so the SQLite checkpointer stores it as-is."""

    role: str
    attempt: int
    box: str | None
    exit_code: int
    duration_s: float
    cost_usd: float | None
    log_path: str
    parsed_ok: bool


class TaskState(TypedDict, total=False):
    task_id: str
    request: str
    repo: str
    worktree: str
    branch: str
    base_sha: str
    base_branch: str  # what the PR targets
    publish: bool  # push + draft PR + CI after every coder run
    pr_url: str
    pr_number: int
    pr_title: str  # from the coder's report
    pr_body: str
    ci_status: str  # pass | fail | none | timeout | error
    ci_summary: str
    spec: str
    acceptance_criteria: list[str]
    plan: str
    coder_summary: str
    qa_passed: bool
    qa_report: str
    review_verdict: Verdict
    review_notes: str
    iteration: int  # coder runs so far
    max_iterations: int
    status: Status
    steps: Annotated[list[StepRecord], operator.add]


def initial_state(
    *,
    task_id: str,
    request: str,
    repo: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    publish: bool = False,
    base_branch: str = "main",
) -> TaskState:
    return TaskState(
        task_id=task_id,
        request=request,
        repo=repo,
        base_branch=base_branch,
        publish=publish,
        iteration=0,
        max_iterations=max_iterations,
        status="running",
        steps=[],
    )
