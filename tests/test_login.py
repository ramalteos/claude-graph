import pytest

from claude_graph.login import login_verdict


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe not utf-8",
        b"[1, 2]",
        b'{"claudeAiOauth": 7}',
        b'{"claudeAiOauth": {"expiresAt": "soon"}}',
        b'{"claudeAiOauth": {"expiresAt": 1, "refreshTokenExpiresAt": [1]}}',
    ],
)
def test_garbage_credentials_are_unreadable_not_a_crash(tmp_path, body, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / ".credentials.json"
    path.write_bytes(body)
    assert login_verdict(path) == "unreadable"


def test_sane_files_keep_their_verdicts(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / ".credentials.json"
    path.write_text('{"fake": true}')
    assert login_verdict(path) == "ok"
    path.write_text('{"claudeAiOauth": {"expiresAt": 9999999999999}}')
    assert login_verdict(path) == "ok"
    path.write_text('{"claudeAiOauth": {"expiresAt": 1}}')
    assert login_verdict(path) == "expired"
    path.write_text('{"claudeAiOauth": {"expiresAt": 9999999999999, "refreshTokenExpiresAt": 1}}')
    assert login_verdict(path) == "refresh-expired"
    path.write_text('{"claudeAiOauth": {"accessToken": "x"}}')
    assert login_verdict(path) == "no-expiry"
