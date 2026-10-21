"""Role prompts: the persona for each node, and the per-run brief built from state."""

from __future__ import annotations

from pathlib import Path

from claude_graph.state import TaskState

ROLES_DIR = Path(__file__).parent / "roles"
ROLE_NAMES = ("pm", "architect", "coder", "qa", "reviewer")


def system_prompt(role: str) -> str:
    """The role's persona, then the shared rules, then the code standards every role is held to."""
    _check(role)
    persona = (ROLES_DIR / f"{role}.md").read_text()
    rules = (ROLES_DIR / "_rules.md").read_text()
    standards = (ROLES_DIR / "_standards.md").read_text()
    return f"{persona}\n\n---\n\n{rules}\n\n---\n\n{standards}"


def user_prompt(role: str, state: TaskState) -> str:
    """What this role is asked to do now: the request plus what earlier roles produced."""
    _check(role)
    parts = [
        f"# Task {state['task_id']}",
        f"Branch: {state.get('branch') or 'claude-graph/' + state['task_id']}",
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
    if role in ("qa", "reviewer"):
        _section(parts, "What the coder says it did", state.get("coder_summary"))
    if role == "reviewer":
        _section(parts, "QA report", state.get("qa_report"))
    return "\n".join(parts)


def _numbered(items: list[str] | None) -> str | None:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)) if items else None


def _section(parts: list[str], title: str, body: str | None) -> None:
    if body:
        parts += ["", f"## {title}", body]


def _check(role: str) -> None:
    if role not in ROLE_NAMES:
        raise KeyError(role)
