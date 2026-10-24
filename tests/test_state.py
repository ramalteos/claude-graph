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
