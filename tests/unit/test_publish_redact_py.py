"""Redaction tests for ``mini_ork.publish.redact``.

The point of these is the contract, not the regexes: after ``redact_value``
runs over a payload, ``scan_for_secrets_obj`` must report *nothing*, and the
things a reader needs (file paths as ``~/…``, check names) must survive.
"""
from __future__ import annotations

import json

from mini_ork.publish.redact import (
    RedactionReport,
    redact_text,
    redact_value,
    scan_for_secrets,
    scan_for_secrets_obj,
)


def test_provider_and_github_tokens_are_replaced():
    text = (
        "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 and "
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    )
    cleaned = redact_text(text)
    assert "sk-ant-api03" not in cleaned
    assert "ghp_" not in cleaned
    assert scan_for_secrets(cleaned) == []


def test_aws_google_slack_and_jwt_are_replaced():
    text = (
        "AKIAIOSFODNN7EXAMPLE AIzaSyA1234567890abcdefghijklmnopqrstuv "
        "xoxb-123456789012-abcdefghijkl eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    )
    cleaned = redact_text(text)
    for marker in ("AKIAIOSFODNN7EXAMPLE", "AIzaSy", "xoxb-", "eyJhbGci"):
        assert marker not in cleaned
    assert scan_for_secrets(cleaned) == []


def test_private_key_block_is_redacted_whole():
    text = (
        "before\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n"
        "-----END OPENSSH PRIVATE KEY-----\nafter"
    )
    cleaned = redact_text(text)
    assert "BEGIN OPENSSH" not in cleaned
    assert "before" in cleaned and "after" in cleaned
    assert scan_for_secrets(cleaned) == []


def test_auth_header_assignment_and_url_credentials():
    text = (
        'Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n'
        "ANTHROPIC_API_KEY=sk-ant-api03-verylongsecretvalue123\n"
        "postgres://user:hunter2pass@db.internal:5432/app"
    )
    cleaned = redact_text(text)
    assert "abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "verylongsecretvalue" not in cleaned
    assert "hunter2pass" not in cleaned
    assert scan_for_secrets(cleaned) == []


def test_home_and_volume_paths_keep_the_tail():
    text = "/Users/alice/ps/mini-ork/run.log and /Volumes/ext/ps/x.py"
    cleaned = redact_text(text)
    assert "/Users/alice" not in cleaned
    assert "/Volumes/ext" not in cleaned
    assert "ps/mini-ork/run.log" in cleaned
    assert "ps/x.py" in cleaned


def test_decorator_is_not_an_email_but_a_real_address_is():
    text = "@pytest.fixture and amir@example.com"
    cleaned = redact_text(text)
    assert "@pytest.fixture" in cleaned
    assert "amir@example.com" not in cleaned
    assert "<email>" in cleaned


def test_sensitive_keys_lose_their_value_and_are_reported():
    report = RedactionReport()
    value = {"api_key": "short", "nested": {"password": "hunter2"}, "ok": "value"}
    cleaned = redact_value(value, report)
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["nested"]["password"] == "[redacted]"
    assert cleaned["ok"] == "value"
    assert "api_key" in report.to_dict()["dropped_keys"]


def test_scan_is_clean_after_a_redaction_pass():
    payload = {
        "objective": "use sk-ant-api03-abcdefghijklmnopqrstuvwxyz and /Users/bob/x",
        "transcript": [
            {"role": "user", "text": "token=ghp_abcdefghijklmnopqrstuvwxyz012345"},
            {"role": "assistant", "text": "mail me at dev@example.org"},
        ],
    }
    report = RedactionReport()
    cleaned = redact_value(payload, report)
    assert scan_for_secrets_obj(cleaned) == []
    assert report.counts  # something was actually replaced


def test_an_emptied_assignment_does_not_swallow_the_next_line():
    """``TOKEN=[redacted]`` must not make the *next* line look like a secret.

    Regression: with ``\\s*`` around the separator the residual scan bridged
    the newline and reported the following line as an ``env_secret`` forever.
    """
    text = "ANTHROPIC_API_KEY=sk-ant-api03-verylongsecretvalue123\nnext line here"
    cleaned = redact_text(text)
    assert cleaned.splitlines()[0] == "ANTHROPIC_API_KEY=[redacted]"
    assert cleaned.splitlines()[1] == "next line here"
    assert scan_for_secrets(cleaned) == []


def test_ssh_remote_keeps_host_and_path():
    text = "origin git@github.com:SourceShift/mini-ork.git"
    cleaned = redact_text(text)
    assert cleaned == "origin ssh://github.com/SourceShift/mini-ork.git"
    assert scan_for_secrets(cleaned) == []


def test_non_http_dsn_password_is_redacted_not_mistaken_for_an_email():
    text = "DATABASE_URL=postgres://user:hunter2pass@db.internal:5432/app"
    cleaned = redact_text(text)
    assert "hunter2pass" not in cleaned
    assert "<email>" not in cleaned
    assert scan_for_secrets(cleaned) == []


def test_redaction_is_idempotent():
    text = "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz amir@example.com"
    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice


def test_json_escaped_newlines_do_not_create_false_auth_headers():
    """A header redacted to a marker must not be re-flagged after JSON dump."""
    payload = {"text": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\nnext line"}
    cleaned = redact_value(payload, RedactionReport())
    blob = json.dumps(cleaned)
    assert "abcdefghijklmnopqrstuvwxyz" not in blob
    assert scan_for_secrets_obj(cleaned) == []
