"""Role prompts: the persona for each node, and the per-run brief built from state."""

from __future__ import annotations

from pathlib import Path

from claude_graph.boxes import BRANCH_PREFIX
from claude_graph.state import TaskState

ROLES_DIR = Path(__file__).parent / "roles"
ROLE_NAMES = ("pm", "architect", "coder", "qa", "reviewer")


def system_prompt(role: str) -> str:
    """The role's persona, then the shared rules, then the code standards every role is held to."""
    _check(role)
    persona = (ROLES_DIR / f"{role}.md").read_text(encoding="utf-8")
    rules = (ROLES_DIR / "_rules.md").read_text(encoding="utf-8")
    standards = (ROLES_DIR / "_standards.md").read_text(encoding="utf-8")
    return f"{persona}\n\n---\n\n{rules}\n\n---\n\n{standards}"


def user_prompt(role: str, state: TaskState, slack_channel: str = "") -> str:
    """What this role is asked to do now: the request plus what earlier roles produced."""
    _check(role)
    parts = [
        f"# Task {state['task_id']}",
        f"Branch: {state.get('branch') or BRANCH_PREFIX + state['task_id']}",
        f"Base commit: {state.get('base_sha') or '(the branch point; see git log)'}",
        f"Coder attempt {state.get('iteration', 0)} of {state['max_iterations']}",
        "",
        "## Request",
        state["request"],
    ]
    if role == "pm":
        return "\n".join(parts)
    _section(parts, "Spec (from the product manager)", state.get("spec"))
    _section(parts, "Acceptance criteria", _numbered(state.get("acceptance_criteria")))
    if role == "architect":
        if state.get("review_verdict") == "redesign":
            _section(
                parts, "Why the reviewer rejected the previous design", state.get("review_notes")
            )
        return "\n".join(parts)
    _section(parts, "Plan (from the architect)", state.get("plan"))
    if role == "coder":
        _section(parts, "Previous QA report", state.get("qa_report"))
        _section(parts, "Previous review notes", state.get("review_notes"))
        if state.get("publish"):
            _section(parts, "Pull request", _coder_pr(state))
    if role in ("qa", "reviewer"):
        _section(parts, "What the coder says it did", state.get("coder_summary"))
    if role == "qa":
        _section(parts, "Pull request and CI", _qa_ci(state))
    if role == "reviewer":
        _section(parts, "QA report", state.get("qa_report"))
        _section(parts, "Pull request", _reviewer_pr(state))
        _section(parts, "Slack", _reviewer_slack(state, slack_channel))
    return "\n".join(parts)


def _coder_pr(state: TaskState) -> str:
    where = f"PR {state['pr_url']}" if state.get("pr_url") else "a draft pull request"
    text = (
        f"After each of your runs the orchestrator pushes this branch, keeps {where} current "
        "and waits for its CI. Do not push."
    )
    if state.get("pr_number"):
        n = state["pr_number"]
        text += (
            f"\n\nStart from the review comments already on the PR: `gh pr view {n} --comments` "
            f"and, for inline ones, `gh api repos/{{owner}}/{{repo}}/pulls/{n}/comments` "
            "(CodeRabbit and human reviewers alike). Fix each valid finding, reply on its "
            "thread with what you did, and list the ones you declined, with the reason, in "
            "your summary."
        )
    return text


def _qa_ci(state: TaskState) -> str:
    if not state.get("pr_url"):
        return "No pull request for this task: run the full test suite and the linter locally."
    return (
        f"PR {state['pr_url']} (#{state['pr_number']}). CI result: {state.get('ci_status')}.\n"
        f"{state.get('ci_summary') or ''}\n\n"
        "Run the unit tests and the linter locally. Integration, end-to-end and build checks "
        "are verified by CI: judge them from the results above, and open a failing check's "
        "log with `gh run view <run-id> --log-failed` when its link names a run. A failed "
        "check is a fail, with the check named. When CI reported no checks, run the full "
        "test suite locally instead."
    )


def _reviewer_pr(state: TaskState) -> str | None:
    if not state.get("pr_url"):
        return None
    n = state["pr_number"]
    return (
        f"Review PR {state['pr_url']} on GitHub: `gh pr view {n}`, `gh pr diff {n}`. "
        f"CI result: {state.get('ci_status')}.\n{state.get('ci_summary') or ''}\n\n"
        f"Post your findings as a review on the pull request: write them to a file, then "
        f"`gh pr review {n} --comment --body-file <file>` (the token is the PR author's, so "
        "GitHub allows a comment review only). Your verdict goes in the JSON block.\n\n"
        f"Weigh the review comments already there (`gh pr view {n} --comments`, inline: "
        f"`gh api repos/{{owner}}/{{repo}}/pulls/{n}/comments`; CodeRabbit included): a "
        "valid finding the coder left unaddressed is a `changes` item in your notes; one "
        "that was fixed or rightly declined is not."
    )


def _reviewer_slack(state: TaskState, channel: str) -> str | None:
    if not channel or not state.get("pr_url"):
        return None
    title = state.get("pr_title") or state["request"].strip().splitlines()[0][:70]
    return (
        f"Only after an `approve` verdict, before your final message: post one message in "
        f"the Slack channel `#{channel}` with the Slack tool (`slack_send_message`, or the "
        f"claude.ai Slack connector). First line `<{state['pr_url']}|{title}>`, then the "
        "pull request body's `## What` bullets, nothing else: no greeting, no @mentions, "
        "no verdict text. Without a Slack tool, say so in your notes and continue."
    )


def _numbered(items: list[str] | None) -> str | None:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)) if items else None


def _section(parts: list[str], title: str, body: str | None) -> None:
    if body:
        parts += ["", f"## {title}", body]


def _check(role: str) -> None:
    if role not in ROLE_NAMES:
        raise KeyError(role)
