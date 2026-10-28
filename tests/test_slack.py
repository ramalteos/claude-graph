import pytest
from fake_slack import webhook_server

from claude_graph.slack import SlackError, WebhookSlack, announcement

PR = {
    "pr_url": "https://github.com/o/r/pull/7",
    "pr_title": "feat(x): add **the** thing [DPT-0]",
    "request": "REQ",
    "coder_summary": "Added the thing.\nmore\n\nsecond paragraph",
}
HEAD = "<https://github.com/o/r/pull/7|feat(x): add *the* thing [DPT-0]>"


def test_announcement_is_the_link_and_the_what_bullets():
    body = (
        "## What\n- Migration adds `UNIQUE (entity_id)`\n"
        "- `insertEntityAddress` maps 23505 -> 409\n  wrapped line\n"
        "## Why\nbecause\n## Checklist\n- [x] tests"
    )
    assert announcement({**PR, "pr_body": body}).splitlines() == [
        HEAD,
        "• Migration adds `UNIQUE (entity_id)`",
        "• `insertEntityAddress` maps 23505 -&gt; 409 wrapped line",
    ]


def test_announcement_falls_back_to_the_coder_summary():
    assert announcement({**PR, "pr_body": "## Why\nreasons"}) == f"{HEAD} — Added the thing. more"
    assert announcement({**PR, "pr_body": "", "coder_summary": ""}) == HEAD


def test_announcement_without_a_coder_title_uses_the_request_and_escapes():
    text = announcement({"pr_url": "u", "request": "fix <a> & b\nmore", "coder_summary": ""})
    assert text == "<u|fix &lt;a&gt; &amp; b>"


def test_announcement_caps_the_bullets_and_the_length():
    body = "## What\n" + "\n".join(f"- item {i}" for i in range(20))
    assert announcement({**PR, "pr_body": body}).count("•") == 8
    assert len(announcement({**PR, "pr_body": "## What\n- " + "x" * 5000})) <= 2000


def test_webhook_posts_json_text():
    with webhook_server() as (url, received):
        WebhookSlack(url).post("hello <x>")
    assert received == [{"text": "hello <x>"}]


def test_webhook_errors_are_slack_errors():
    with webhook_server(status=500) as (url, _):
        with pytest.raises(SlackError, match="HTTP 500"):
            WebhookSlack(url).post("x")
    with pytest.raises(SlackError, match="slack webhook"):
        WebhookSlack("http://127.0.0.1:1/hook", timeout_s=2).post("x")
