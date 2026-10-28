from langgraph.checkpoint.sqlite import SqliteSaver

from claude_graph.graph import Settings, build_graph
from claude_graph.state import initial_state
from fake_runner import FakeRunner, report, result

SPEC = result(report({"spec": "THE-SPEC", "acceptance_criteria": ["AC-ONE", "AC-TWO"]}))
PLAN = result(report({"plan": "THE-PLAN"}))
CODED = result(report({"summary": "CODED-IT"}))
QA_PASS = result(report({"passed": True, "report": "all green"}))
QA_FAIL = result(report({"passed": False, "report": "QA-FAILED-X"}))
APPROVE = result(report({"verdict": "approve", "notes": "lgtm"}))
CHANGES = result(report({"verdict": "changes", "notes": "FIX-THIS"}))
REDESIGN = result(report({"verdict": "redesign", "notes": "WRONG-PLACE"}))


def run(scripts, max_iterations=3, settings=None, github=None, publish=False):
    runner = FakeRunner({"pm": [SPEC], **{k: list(v) for k, v in scripts.items()}})
    graph = build_graph(runner, settings or Settings(), github=github)
    final = graph.invoke(
        initial_state(
            task_id="t1", request="REQ", repo="/r", max_iterations=max_iterations, publish=publish
        )
    )
    return final, runner


def prompt_for(runner, role, nth=0):
    return [c["prompt"] for c in runner.calls if c["role"] == role][nth]


def test_happy_path():
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]}
    )
    assert runner.roles() == ["pm", "architect", "coder", "qa", "reviewer"]
    assert final["status"] == "done"
    assert final["plan"] == "THE-PLAN"
    assert final["coder_summary"] == "CODED-IT"
    assert final["qa_passed"] is True
    assert final["review_verdict"] == "approve"
    assert final["iteration"] == 1
    assert (final["branch"], final["base_sha"], final["worktree"]) == (
        "claude-graph/t1",
        "abc123",
        "/tmp/wt/t1",
    )
    assert [s["role"] for s in final["steps"]] == ["pm", "architect", "coder", "qa", "reviewer"]
    assert all(s["parsed_ok"] for s in final["steps"])
    assert [c["attempt"] for c in runner.calls] == [1, 1, 1, 1, 1]


def test_models_and_efforts_come_from_settings():
    settings = Settings(models={"coder": "sonnet"}, efforts={"qa": "low"})
    _, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]},
        settings=settings,
    )
    by_role = {c["role"]: c for c in runner.calls}
    assert (by_role["coder"]["model"], by_role["coder"]["effort"]) == ("sonnet", "xhigh")
    assert (by_role["qa"]["model"], by_role["qa"]["effort"]) == ("opus", "low")


def test_default_models_per_role():
    _, runner = run({"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]})
    assert {c["role"]: c["model"] for c in runner.calls} == {
        "pm": "opus",
        "architect": "opus",
        "coder": "opus",
        "qa": "opus",
        "reviewer": "opus",
    }
    assert {c["effort"] for c in runner.calls} == {"xhigh"}


def test_qa_failure_loops_to_coder_with_the_report():
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [QA_FAIL, QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    assert runner.roles() == ["pm", "architect", "coder", "qa", "coder", "qa", "reviewer"]
    assert final["status"] == "done"
    assert final["iteration"] == 2
    assert "QA-FAILED-X" in runner.calls[4]["prompt"]
    assert runner.calls[4]["attempt"] == 2


def test_changes_requested_loops_to_coder_with_the_notes():
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [QA_PASS, QA_PASS],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    assert runner.roles() == [
        "pm",
        "architect",
        "coder",
        "qa",
        "reviewer",
        "coder",
        "qa",
        "reviewer",
    ]
    assert "FIX-THIS" in runner.calls[5]["prompt"]
    assert final["status"] == "done"


def test_redesign_goes_back_to_the_architect():
    final, runner = run(
        {
            "architect": [PLAN, result(report({"plan": "PLAN-2"}))],
            "coder": [CODED, CODED],
            "qa": [QA_PASS, QA_PASS],
            "reviewer": [REDESIGN, APPROVE],
        }
    )
    assert runner.roles() == [
        "pm",
        "architect",
        "coder",
        "qa",
        "reviewer",
        "architect",
        "coder",
        "qa",
        "reviewer",
    ]
    assert "WRONG-PLACE" in runner.calls[5]["prompt"]
    assert final["plan"] == "PLAN-2"
    assert final["status"] == "done"


def test_iteration_cap_ends_exhausted():
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED, CODED], "qa": [QA_FAIL, QA_FAIL]}, max_iterations=2
    )
    assert runner.roles() == ["pm", "architect", "coder", "qa", "coder", "qa"]
    assert final["status"] == "exhausted"
    assert final["qa_passed"] is False


def test_redesign_on_the_last_iteration_is_exhausted_not_another_plan():
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [REDESIGN]},
        max_iterations=1,
    )
    assert runner.roles() == ["pm", "architect", "coder", "qa", "reviewer"]
    assert final["status"] == "exhausted"
    assert final["review_verdict"] == "redesign"


def test_architect_box_failure_ends_failed():
    final, runner = run({"architect": [result(None, exit_code=1)]})
    assert runner.roles() == ["pm", "architect"]
    assert final["status"] == "failed"


def test_architect_without_a_report_keeps_its_text_as_the_plan():
    final, _ = run(
        {
            "architect": [result("just prose, no block")],
            "coder": [CODED],
            "qa": [QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    assert final["plan"] == "just prose, no block"
    assert final["status"] == "done"
    assert final["steps"][1]["parsed_ok"] is False


def test_qa_without_a_verdict_is_a_fail():
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [result("ran stuff, forgot the block"), QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    assert runner.roles()[:5] == ["pm", "architect", "coder", "qa", "coder"]
    assert "no verdict" in runner.calls[4]["prompt"]
    assert final["status"] == "done"


def test_reviewer_box_crash_is_changes_requested():
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [QA_PASS, QA_PASS],
            "reviewer": [result(None, exit_code=1), APPROVE],
        }
    )
    assert runner.roles() == [
        "pm",
        "architect",
        "coder",
        "qa",
        "reviewer",
        "coder",
        "qa",
        "reviewer",
    ]
    assert final["status"] == "done"


def test_checkpointed_run_resumes_as_a_no_op_when_finished():
    runner = FakeRunner(
        {
            "pm": [SPEC],
            "architect": [PLAN],
            "coder": [CODED],
            "qa": [QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    with SqliteSaver.from_conn_string(":memory:") as saver:
        graph = build_graph(runner, Settings(), checkpointer=saver)
        config = {"configurable": {"thread_id": "t1"}}
        graph.invoke(initial_state(task_id="t1", request="REQ", repo="/r"), config)
        assert len(runner.calls) == 5
        assert graph.get_state(config).next == ()
        assert graph.get_state(config).values["status"] == "done"


def test_pm_spec_and_criteria_reach_every_later_role():
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]}
    )
    assert final["spec"] == "THE-SPEC"
    assert final["acceptance_criteria"] == ["AC-ONE", "AC-TWO"]
    for call in runner.calls[1:]:
        assert "THE-SPEC" in call["prompt"]
        assert "2. AC-TWO" in call["prompt"]
    assert "THE-SPEC" not in runner.calls[0]["prompt"]


def test_pm_box_failure_ends_failed_before_the_architect():
    runner = FakeRunner({"pm": [result(None, exit_code=1)]})
    final = build_graph(runner, Settings()).invoke(
        initial_state(task_id="t1", request="REQ", repo="/r")
    )
    assert runner.roles() == ["pm"]
    assert final["status"] == "failed"


def test_pm_without_a_report_keeps_its_text_as_the_spec():
    runner = FakeRunner(
        {
            "pm": [result("prose spec")],
            "architect": [PLAN],
            "coder": [CODED],
            "qa": [QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    final = build_graph(runner, Settings()).invoke(
        initial_state(task_id="t1", request="REQ", repo="/r")
    )
    assert final["spec"] == "prose spec"
    assert final["acceptance_criteria"] == []
    assert final["status"] == "done"


def test_a_failed_qa_box_cannot_pass_even_with_a_valid_block():
    broken_pass = result(report({"passed": True, "report": "green"}), exit_code=124)
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [broken_pass, QA_PASS],
            "reviewer": [APPROVE],
        }
    )
    assert final["status"] == "done"
    assert runner.roles() == ["pm", "architect", "coder", "qa", "coder", "qa", "reviewer"]


def test_a_failed_reviewer_box_asks_for_changes_even_with_an_approve_block():
    broken_approve = result(report({"verdict": "approve", "notes": "lgtm"}), exit_code=1)
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [QA_PASS, QA_PASS],
            "reviewer": [broken_approve, APPROVE],
        }
    )
    assert final["status"] == "done"
    assert runner.roles().count("reviewer") == 2


def test_a_failed_coder_box_fails_the_task_instead_of_running_qa():
    crashed = result(None, exit_code=127)
    final, runner = run({"architect": [PLAN], "coder": [crashed]})
    assert final["status"] == "failed"
    assert runner.roles() == ["pm", "architect", "coder"]
    assert "exit 127" in final["coder_summary"]


def test_publish_pushes_opens_a_draft_pr_and_hands_ci_to_qa():
    from fake_github import FakeGitHub

    gh = FakeGitHub(checks=("pass", "build: pass https://ci/1"))
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]},
        github=gh,
        publish=True,
    )
    assert final["status"] == "done"
    assert gh.calls == ["push", "ensure_pr", "wait_checks", "mark_ready"]
    assert final["pr_url"].endswith("/pull/7") and final["pr_number"] == 7
    assert final["ci_status"] == "pass" and "build: pass" in final["ci_summary"]
    assert "https://ci/1" in prompt_for(runner, "qa") and "pull/7" in prompt_for(runner, "qa")
    assert "gh pr diff 7" in prompt_for(runner, "reviewer")


def test_a_coder_loop_pushes_again_but_keeps_one_pr():
    from fake_github import FakeGitHub

    gh = FakeGitHub()
    final, runner = run(
        {
            "architect": [PLAN],
            "coder": [CODED, CODED],
            "qa": [QA_FAIL, QA_PASS],
            "reviewer": [APPROVE],
        },
        github=gh,
        publish=True,
    )
    assert final["status"] == "done" and final["pr_number"] == 7
    assert gh.calls.count("push") == 2 and gh.calls.count("mark_ready") == 1
    assert runner.roles() == ["pm", "architect", "coder", "qa", "coder", "qa", "reviewer"]


def test_a_failed_ci_reaches_qa_and_a_changes_verdict_does_not_mark_ready():
    from fake_github import FakeGitHub

    gh = FakeGitHub(checks=("fail", "e2e: fail https://ci/9"))
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_FAIL], "reviewer": [CHANGES]},
        max_iterations=1,
        github=gh,
        publish=True,
    )
    assert final["status"] == "exhausted"
    assert "e2e: fail" in prompt_for(runner, "qa") and "mark_ready" not in gh.calls


def test_a_push_failure_fails_the_task_before_qa():
    from fake_github import FakeGitHub

    gh = FakeGitHub(fail_push=True)
    final, runner = run({"architect": [PLAN], "coder": [CODED]}, github=gh, publish=True)
    assert final["status"] == "failed" and "push rejected" in final["ci_summary"]
    assert runner.roles() == ["pm", "architect", "coder"]


def test_without_publish_the_flow_stays_local():
    from fake_github import FakeGitHub

    gh = FakeGitHub()
    final, runner = run(
        {"architect": [PLAN], "coder": [CODED], "qa": [QA_PASS], "reviewer": [APPROVE]}, github=gh
    )
    assert final["status"] == "done" and gh.calls == [] and "pr_url" not in final
    assert "full test suite" in prompt_for(runner, "qa").lower()
