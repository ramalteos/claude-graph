"""The graph: pm → architect → coder → publish → qa → reviewer, with bounded loops back."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from claude_graph.boxes import BRANCH_PREFIX, BoxResult, BoxRunner
from claude_graph.config import Settings
from claude_graph.github import GitHub, PublishError
from claude_graph.reports import (
    ArchitectReport,
    CoderReport,
    PmReport,
    QaReport,
    ReviewReport,
    parse_report,
)
from claude_graph.roles import system_prompt, user_prompt
from claude_graph.state import StepRecord, TaskState


def build_graph(
    runner: BoxRunner,
    settings: Settings | None = None,
    checkpointer: Any = None,
    github: GitHub | None = None,
    ci_timeout_s: int = 900,
) -> CompiledStateGraph:
    nodes = _Nodes(runner, settings or Settings(), github, ci_timeout_s)
    g = StateGraph(TaskState)
    g.add_node("pm", nodes.pm)
    g.add_node("architect", nodes.architect)
    g.add_node("coder", nodes.coder)
    g.add_node("qa", nodes.qa)
    g.add_node("reviewer", nodes.reviewer)
    g.add_edge(START, "pm")
    g.add_conditional_edges("pm", after_pm, {"architect": "architect", END: END})
    g.add_conditional_edges("architect", after_architect, {"coder": "coder", END: END})
    if github is not None:
        g.add_node("publish", nodes.publish)
        g.add_conditional_edges(
            "coder", after_coder_with_publish, {"publish": "publish", "qa": "qa", END: END}
        )
        g.add_conditional_edges("publish", after_publish, {"qa": "qa", END: END})
    else:
        g.add_conditional_edges("coder", after_coder, {"qa": "qa", END: END})
    g.add_conditional_edges("qa", after_qa, {"reviewer": "reviewer", "coder": "coder", END: END})
    g.add_conditional_edges(
        "reviewer", after_reviewer, {"coder": "coder", "architect": "architect", END: END}
    )
    return g.compile(checkpointer=checkpointer)


# --- routing: pure functions of state; the nodes decide, these only read ----------------


def after_pm(state: TaskState) -> str:
    return END if state["status"] == "failed" else "architect"


def after_architect(state: TaskState) -> str:
    return END if state["status"] == "failed" else "coder"


def after_coder(state: TaskState) -> str:
    return END if state["status"] == "failed" else "qa"


def after_coder_with_publish(state: TaskState) -> str:
    if state["status"] == "failed":
        return END
    return "publish" if state.get("publish") else "qa"


def after_publish(state: TaskState) -> str:
    return END if state["status"] == "failed" else "qa"


def after_qa(state: TaskState) -> str:
    if state["status"] == "exhausted":
        return END
    return "reviewer" if state["qa_passed"] else "coder"


def after_reviewer(state: TaskState) -> str:
    if state["status"] in ("done", "exhausted"):
        return END
    return "architect" if state["review_verdict"] == "redesign" else "coder"


# --- nodes -------------------------------------------------------------------------------


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


def _branch_for(slug: str) -> str | None:
    """`claude-graph/<slug>` when the pm's slug is a sane ref component, else None."""
    return BRANCH_PREFIX + slug if _SLUG.match(slug) and ".." not in slug else None


def _attempts(state: TaskState, role: str) -> int:
    return sum(1 for s in state.get("steps", []) if s["role"] == role)


def _box_failed(res: BoxResult) -> bool:
    return res.exit_code != 0 or res.is_error or res.result_text is None


def _tail(text: str | None, limit: int = 2000) -> str:
    return (text or "")[-limit:]


class _Nodes:
    def __init__(
        self,
        runner: BoxRunner,
        settings: Settings,
        github: GitHub | None = None,
        ci_timeout_s: int = 900,
    ) -> None:
        self.runner = runner
        self.settings = settings
        self.github = github
        self.ci_timeout_s = ci_timeout_s

    def _run(self, role: str, state: TaskState) -> tuple[BoxResult, int]:
        attempt = _attempts(state, role) + 1
        res = self.runner.run(
            role=role,
            task_id=state["task_id"],
            attempt=attempt,
            repo=Path(state["repo"]),
            system_prompt=system_prompt(role),
            prompt=user_prompt(role, state),
            model=self.settings.model(role),
            effort=self.settings.effort(role),
        )
        return res, attempt

    @staticmethod
    def _update(
        state: TaskState, role: str, attempt: int, res: BoxResult, parsed_ok: bool
    ) -> TaskState:
        step = StepRecord(
            role=role,
            attempt=attempt,
            box=res.box,
            exit_code=res.exit_code,
            duration_s=res.duration_s,
            cost_usd=res.cost_usd,
            log_path=str(res.log_path),
            parsed_ok=parsed_ok,
        )
        update = TaskState(steps=[step])
        # The first box to report the worktree pins it; later ones agree by construction.
        if res.branch and not state.get("branch"):
            update.update(
                branch=res.branch, base_sha=res.base_sha or "", worktree=str(res.worktree or "")
            )
        return update

    def pm(self, state: TaskState) -> TaskState:
        res, attempt = self._run("pm", state)
        report = parse_report(res.result_text, PmReport)
        update = self._update(state, "pm", attempt, res, report is not None)
        if _box_failed(res):
            update["status"] = "failed"
            return update
        update["spec"] = report.spec if report else res.result_text or ""
        update["acceptance_criteria"] = report.acceptance_criteria if report else []
        if report and self.github is not None and res.branch:
            named = _branch_for(report.slug)
            if named and named != res.branch:
                if self.github.rename_branch(Path(state["repo"]), res.branch, named):
                    update["branch"] = named
        return update

    def architect(self, state: TaskState) -> TaskState:
        res, attempt = self._run("architect", state)
        report = parse_report(res.result_text, ArchitectReport)
        update = self._update(state, "architect", attempt, res, report is not None)
        if _box_failed(res):
            update["status"] = "failed"
            return update
        update["plan"] = report.plan if report else res.result_text or ""
        return update

    def coder(self, state: TaskState) -> TaskState:
        state = {**state, "iteration": state["iteration"] + 1}
        res, attempt = self._run("coder", state)
        report = parse_report(res.result_text, CoderReport)
        update = self._update(state, "coder", attempt, res, report is not None)
        update["iteration"] = state["iteration"]
        if _box_failed(res):
            # QA against a tree the coder never finished burns an iteration for nothing.
            update["status"] = "failed"
            update["coder_summary"] = (
                f"coder box failed (exit {res.exit_code}); see its log. " + _tail(res.result_text)
            )
        elif report:
            update["coder_summary"] = report.summary
            if report.pr_title:
                update["pr_title"] = report.pr_title
            if report.pr_body:
                update["pr_body"] = report.pr_body
        else:
            update["coder_summary"] = _tail(res.result_text)
        return update

    def publish(self, state: TaskState) -> TaskState:
        """Host side, no box: push the branch, keep one draft PR, wait for its checks."""
        assert self.github is not None
        repo, branch = Path(state["repo"]), state["branch"]
        title = state.get("pr_title") or state["request"].strip().splitlines()[0][:70]
        body = (state.get("pr_body") or state.get("spec", "")[:3000]).rstrip() + (
            f"\n\nTask `{state['task_id']}`, opened by claude-graph; reviewed on this pull "
            "request, never merged by it."
        )
        try:
            self.github.push(repo, branch)
            pr = self.github.ensure_pr(
                repo, branch, state.get("base_branch") or "main", title, body
            )
            if state.get("pr_number"):  # a later coder run: title and body may have improved
                self.github.update_pr(repo, pr.number, title, body)
            checks = self.github.wait_checks(repo, pr.number, self.ci_timeout_s)
        except PublishError as e:
            return TaskState(status="failed", ci_status="error", ci_summary=str(e))
        return TaskState(
            pr_url=pr.url, pr_number=pr.number, ci_status=checks.status, ci_summary=checks.summary
        )

    def qa(self, state: TaskState) -> TaskState:
        res, attempt = self._run("qa", state)
        # A verdict from a box that failed (stopped, timed out) is no verdict.
        report = None if _box_failed(res) else parse_report(res.result_text, QaReport)
        update = self._update(state, "qa", attempt, res, report is not None)
        if report:
            update["qa_passed"], update["qa_report"] = report.passed, report.report
        else:
            update["qa_passed"] = False
            update["qa_report"] = (
                f"qa gave no verdict (box exit {res.exit_code}). Output tail:\n"
                + _tail(res.result_text)
            )
        if not update["qa_passed"] and state["iteration"] >= state["max_iterations"]:
            update["status"] = "exhausted"
        return update

    def reviewer(self, state: TaskState) -> TaskState:
        res, attempt = self._run("reviewer", state)
        report = None if _box_failed(res) else parse_report(res.result_text, ReviewReport)
        update = self._update(state, "reviewer", attempt, res, report is not None)
        if report:
            update["review_verdict"], update["review_notes"] = report.verdict, report.notes
        else:
            update["review_verdict"] = "changes"
            update["review_notes"] = (
                f"reviewer gave no verdict (box exit {res.exit_code}). Output tail:\n"
                + _tail(res.result_text)
            )
        if update["review_verdict"] == "approve":
            update["status"] = "done"
            if self.github is not None and state.get("pr_number"):
                try:
                    self.github.mark_ready(Path(state["repo"]), state["pr_number"])
                except PublishError:
                    pass  # the verdict stands; the PR just stays a draft
        elif state["iteration"] >= state["max_iterations"]:
            update["status"] = "exhausted"
        return update
