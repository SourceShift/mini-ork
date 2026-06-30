"""Tests for mini_ork.dispatch — the Phase-0 Python port of llm-dispatch.

Asserts the two bash failure classes are structurally impossible in the Python
layer (E2BIG over stdin, faithful rc) and that the codex telemetry parser is a
faithful port of cl_codex.sh.
"""

from __future__ import annotations

import sys

import pytest

from mini_ork.dispatch import (
    DispatchRequest,
    TokenUsage,
    codex_cost,
    dispatch_model,
    dispatch_with_command,
    parse_codex_usage,
    resolve_provider,
)

PY = sys.executable


def _req(prompt: str, **kw) -> DispatchRequest:
    return DispatchRequest(model="stub", prompt=prompt, **kw)


def test_no_e2big_with_multi_mb_prompt():
    # 1.5 MB prompt — well over macOS ARG_MAX (~1 MB). Passing this as an argv
    # element or env var (what the bash lanes did) aborts execve() with E2BIG.
    # Over stdin there is no limit: the stub reads it and reports its length.
    big = "x" * 1_500_000
    cmd = [PY, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.read())))"]
    res = dispatch_with_command(_req(big), cmd)
    assert res.ok is True
    assert res.rc == 0
    assert res.text.strip() == str(len(big))


def test_rc_is_propagated_faithfully_on_failure():
    # A non-zero exit must surface as ok=False with the real rc — the bash shim
    # `if cmd; then…; fi` (no else) returned 0 here (D-013/D-014 regression).
    cmd = [PY, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(7)"]
    res = dispatch_with_command(_req("hello"), cmd)
    assert res.ok is False
    assert res.rc == 7
    assert "boom" in res.error


def test_stdout_captured_on_success():
    cmd = [PY, "-c", "print('ANSWER-42')"]
    res = dispatch_with_command(_req("q"), cmd)
    assert res.ok is True
    assert "ANSWER-42" in res.text


def test_timeout_yields_rc_124():
    cmd = [PY, "-c", "import time; time.sleep(5)"]
    res = dispatch_with_command(_req("q", timeout_s=0.4), cmd)
    assert res.ok is False
    assert res.rc == 124
    assert "timeout" in res.error


def test_spawn_failure_yields_rc_127():
    res = dispatch_with_command(_req("q"), ["/nonexistent/binary-xyz"])
    assert res.ok is False
    assert res.rc == 127


def test_codex_usage_parser_sums_turns():
    stream = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t1"}',
            '{"type":"turn.completed","usage":{"input_tokens":1000,"output_tokens":500,"cached_input_tokens":200}}',
            "not-json-noise",
            '{"type":"turn.completed","usage":{"input_tokens":2000,"output_tokens":700,"cached_input_tokens":100}}',
        ]
    )
    u = parse_codex_usage(stream)
    assert u.input_tokens == 3000
    assert u.output_tokens == 1200
    assert u.cached_input_tokens == 300


def test_codex_cost_subtracts_cached_before_full_rate():
    u = TokenUsage(input_tokens=3000, output_tokens=1200, cached_input_tokens=300)
    # fresh_in=2700 -> (2700*1.25 + 300*0.125 + 1200*10.0)/1e6
    expected = (2700 * 1.25 + 300 * 0.125 + 1200 * 10.0) / 1e6
    assert codex_cost("", u) == pytest.approx(expected)


def test_dispatch_through_a_codex_style_stub():
    # End-to-end: a stub that emits codex JSONL on stdout, parsed via the codex
    # parsers — proves the parser wiring path returns typed telemetry.
    emit = (
        "import sys; "
        "sys.stdout.write('"
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5,"cached_input_tokens":2}}'
        "')"
    )
    res = dispatch_with_command(
        _req("q"), [PY, "-c", emit], parse_usage=parse_codex_usage, parse_cost=codex_cost
    )
    assert res.ok is True
    assert res.usage.input_tokens == 10
    assert res.usage.output_tokens == 5
    assert res.cost_usd == pytest.approx((8 * 1.25 + 2 * 0.125 + 5 * 10.0) / 1e6)


def test_unknown_lane_is_structured_not_raised():
    res = dispatch_model(DispatchRequest(model="not-a-real-lane", prompt="q"))
    assert res.ok is False
    assert res.rc == 2
    assert "unknown model lane" in res.error


def test_resolve_known_lane_points_at_wrapper():
    spec = resolve_provider("codex")
    assert spec.command[0].endswith("lib/providers/cl_codex.sh")
    assert spec.parse_usage is parse_codex_usage
