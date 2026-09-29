"""A failed lane records the harness's real API error, not a stderr banner."""
import json

from mini_ork.dispatch.core import _ERROR_CAP, _failure_detail

BANNER = ("⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or "
          "another auth source is set")


def _envelope(**over):
    doc = {"type": "result", "subtype": "success", "is_error": True,
           "terminal_reason": "api_error", "api_error_status": 403,
           "result": "Failed to authenticate. API Error: 403 Key limit exceeded (total limit)."}
    doc.update(over)
    return json.dumps(doc)


def test_envelope_error_leads_over_stderr_banner():
    # The shape observed from the Claude CLI on a capped OpenRouter key: rc=1,
    # the 403 only in the stdout envelope, stderr only the connectors banner.
    out = _failure_detail(_envelope(), BANNER)
    assert out.startswith("[api_error 403] Failed to authenticate. API Error: 403 Key limit exceeded")
    assert out.endswith(BANNER)


def test_no_envelope_keeps_stderr_tail():
    assert _failure_detail("", "boom\n") == "boom"
    assert _failure_detail("not json at all", "boom") == "boom"
    assert _failure_detail(json.dumps({"is_error": False, "result": "fine"}), "boom") == "boom"
    assert _failure_detail("", "x" * 5000) == "x" * _ERROR_CAP


def test_envelope_survives_long_stderr():
    out = _failure_detail(_envelope(), "y" * 5000)
    assert out.startswith("[api_error 403] Failed to authenticate")
    assert len(out) == _ERROR_CAP
    assert out.endswith("y")


def test_envelope_without_stderr():
    out = _failure_detail(_envelope(terminal_reason=None, api_error_status=None), "")
    assert out == "Failed to authenticate. API Error: 403 Key limit exceeded (total limit)."
