import pytest

from claude_graph.roles import ROLE_NAMES, system_prompt, user_prompt
from claude_graph.state import initial_state


def state(**overrides):
    s = initial_state(task_id="t1", request="add a /health route", repo="/r", max_iterations=3)
    s.update(overrides)
    return s


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_system_prompt_is_persona_plus_shared_rules(role):
    text = system_prompt(role)
    assert "## Report" in text  # every persona ends with its json contract
    assert "## Shared rules" in text  # _rules.md
    assert "## Standards" in text  # _standards.md
    for rule in (
        "### Single responsibility",
        "### DRY",
        "### KISS",
        "### YAGNI",
        "### Modern practice (2026)",
        "### Security",
    ):
        assert rule in text
    assert text.index("## Report") < text.index("## Shared rules") < text.index("## Standards")


def test_unknown_role_is_an_error():
    with pytest.raises(KeyError):
        system_prompt("manager")
    with pytest.raises(KeyError):
        user_prompt("manager", state())


def test_every_prompt_names_task_branch_base_and_request():
    s = state(branch="claude-graph/t1", base_sha="abc123", iteration=2)
    for role in ROLE_NAMES:
        text = user_prompt(role, s)
        assert "# Task t1" in text
        assert "claude-graph/t1" in text
        assert "abc123" in text
        assert "add a /health route" in text
        assert "attempt 2 of 3" in text


def test_branch_is_predictable_before_the_first_box():
    assert "claude-graph/t1" in user_prompt("architect", state())


def test_first_coder_attempt_has_the_plan_and_no_history():
    text = user_prompt("coder", state(plan="PLAN-TEXT"))
    assert "PLAN-TEXT" in text
    assert "Previous QA report" not in text
    assert "Previous review notes" not in text


def test_coder_retry_sees_qa_and_review():
    text = user_prompt("coder", state(plan="P", qa_report="QA-FAIL", review_notes="REVIEW-NOTES"))
    assert "QA-FAIL" in text
    assert "REVIEW-NOTES" in text


def test_architect_redesign_sees_review_notes_only_on_redesign():
    assert "WHY" in user_prompt("architect", state(review_verdict="redesign", review_notes="WHY"))
    assert "WHY" not in user_prompt(
        "architect", state(review_verdict="changes", review_notes="WHY")
    )


def test_pm_sees_only_the_request_and_everyone_after_sees_the_spec():
    s = state(spec="SPEC-TEXT", acceptance_criteria=["first check", "second check"])
    assert "SPEC-TEXT" not in user_prompt("pm", s)
    for role in ("architect", "coder", "qa", "reviewer"):
        text = user_prompt(role, s)
        assert "SPEC-TEXT" in text
        assert "1. first check\n2. second check" in text


def test_qa_and_reviewer_see_the_coder_summary():
    s = state(plan="P", coder_summary="CODER-SAID", qa_report="QA-SAID")
    assert "CODER-SAID" in user_prompt("qa", s)
    assert "CODER-SAID" in user_prompt("reviewer", s)
    assert "QA-SAID" in user_prompt("reviewer", s)
    assert "QA-SAID" not in user_prompt("qa", s)
