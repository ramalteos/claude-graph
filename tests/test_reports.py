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


def test_raw_newlines_inside_json_strings_are_accepted():
    from claude_graph.reports import QaReport, parse_report

    text = 'Done.\n```json\n{"passed": false, "report": "line one\nline two"}\n```'
    r = parse_report(text, QaReport)
    assert r is not None and r.passed is False and r.report == "line one\nline two"


def test_a_one_line_fence_is_still_a_block():
    from claude_graph.reports import CoderReport, parse_report

    r = parse_report('ok ```json {"summary": "s"} ```', CoderReport)
    assert r is not None and r.summary == "s"


def test_a_fenced_snippet_inside_the_report_string_does_not_end_the_block():
    from claude_graph.reports import ArchitectReport, parse_report

    plan = "Run:\\n```bash\\nls\\n```\\nthen stop"  # escaped newlines, literal fences, valid JSON
    text = 'Plan below.\n```json\n{"plan": "' + plan + '"}\n```'
    r = parse_report(text, ArchitectReport)
    assert r is not None and "```bash" in r.plan


def test_a_raw_fence_inside_a_lenient_string_still_parses():
    from claude_graph.reports import QaReport, parse_report

    text = '```json\n{"passed": true, "report": "see\n```\nlog\n```\ndone"}\n```'
    r = parse_report(text, QaReport)
    assert r is not None and r.passed is True and "log" in r.report


def test_the_last_json_block_wins_even_with_a_trailing_plain_fence():
    from claude_graph.reports import CoderReport, parse_report

    text = '```json\n{"summary": "first"}\n```\n```json\n{"summary": "last"}\n```\n```\nnotes\n```'
    r = parse_report(text, CoderReport)
    assert r is not None and r.summary == "last"


def test_coder_report_carries_an_optional_pr_title_and_body():
    from claude_graph.reports import CoderReport, parse_report

    old = parse_report('```json\n{"summary": "s"}\n```', CoderReport)
    assert old is not None and old.pr_title == "" and old.pr_body == ""
    new = parse_report(
        '```json\n{"summary": "s", "pr_title": "feat(x): add y", "pr_body": "## What\\n- y"}\n```',
        CoderReport,
    )
    assert (
        new is not None and new.pr_title == "feat(x): add y" and new.pr_body.startswith("## What")
    )


def test_pm_report_carries_an_optional_branch_slug():
    from claude_graph.reports import PmReport, parse_report

    old = parse_report('```json\n{"spec": "s", "acceptance_criteria": []}\n```', PmReport)
    assert old is not None and old.slug == ""
    new = parse_report(
        '```json\n{"spec": "s", "acceptance_criteria": [], "slug": "optimize-product-read"}\n```',
        PmReport,
    )
    assert new is not None and new.slug == "optimize-product-read"
