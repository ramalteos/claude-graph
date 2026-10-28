import pytest

from claude_graph.state import TASK_ID


@pytest.mark.parametrize(
    "bad", ["abc\n", "a..b", ".x", "x.", "x.lock", "", ".", "..", "a/b", "a b", "-x", "--help"]
)
def test_task_ids_git_would_refuse_as_branch_names_are_rejected(bad):
    assert TASK_ID.match(bad) is None


@pytest.mark.parametrize("good", ["t20261022-092756", "health-route", "v1.2", "A_b-C.d"])
def test_ordinary_task_ids_pass(good):
    assert TASK_ID.match(good)


def test_task_id_fits_the_longest_box_name():
    from claude_graph.boxes import BOX_PREFIX, box_name
    from claude_graph.state import MAX_TASK_ID

    longest = BOX_PREFIX + box_name("x" * MAX_TASK_ID, "architect", 20)
    assert len(longest) <= 63  # Apple `container` rejects a longer container id
    assert TASK_ID.match("x" * MAX_TASK_ID)
    assert TASK_ID.match("x" * (MAX_TASK_ID + 1)) is None


def test_default_cap_is_ten_coder_runs():
    from claude_graph.state import DEFAULT_MAX_ITERATIONS, initial_state

    assert DEFAULT_MAX_ITERATIONS == 10
    assert initial_state(task_id="t", request="r", repo="/r")["max_iterations"] == 10
