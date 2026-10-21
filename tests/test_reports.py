from claude_graph.reports import ArchitectReport, QaReport, ReviewReport, parse_report


def fenced(body: str, lang: str = "json") -> str:
    return f"```{lang}\n{body}\n```"


def test_last_fenced_block_wins():
    text = (
        "first\n"
        + fenced('{"passed": true, "report": "a"}')
        + "\nthen\n"
        + fenced('{"passed": false, "report": "b"}')
    )
    assert parse_report(text, QaReport) == QaReport(passed=False, report="b")


def test_no_block_is_none():
    assert parse_report("no json here", QaReport) is None


def test_none_text_is_none():
    assert parse_report(None, QaReport) is None


def test_string_bool_is_not_a_pass():
    assert parse_report(fenced('{"passed": "yes", "report": "x"}'), QaReport) is None


def test_broken_json_is_none():
    assert parse_report(fenced('{"passed": true,'), QaReport) is None


def test_non_object_json_is_none():
    assert parse_report(fenced("[1, 2]"), QaReport) is None


def test_extra_keys_are_ignored():
    r = parse_report(fenced('{"verdict": "approve", "notes": "ok", "extra": 1}'), ReviewReport)
    assert r == ReviewReport(verdict="approve", notes="ok")


def test_unknown_verdict_is_none():
    assert parse_report(fenced('{"verdict": "maybe", "notes": ""}'), ReviewReport) is None


def test_fence_language_is_case_insensitive():
    assert parse_report(fenced('{"plan": "p"}', "JSON"), ArchitectReport) == ArchitectReport(
        plan="p"
    )


def test_unfenced_json_is_not_a_report():
    assert parse_report('{"plan": "p"}', ArchitectReport) is None
