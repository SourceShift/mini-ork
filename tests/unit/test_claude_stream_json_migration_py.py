"""The claude stream-json migration must be value-identical, not just live.

Switching a lane's ``--output-format`` from ``json`` to ``stream-json`` changes
the shape of what the four claude parsers read (``parse_usage``, ``claude_cost``,
``claude_result_text``, ``claude_session_id``). Getting that wrong is silent:
``session_id`` feeds E4 turn-resume and the steering FIFO, and usage/cost feed
llm_calls telemetry, so a botched parse produces a run that looks fine and
resumes nothing while reporting zero cost.

The fixtures below are the real shapes, captured from a live lane on 2026-09-18.
The load-bearing test is ``test_both_formats_parse_to_identical_values``: one
prompt, two formats, four parsers, same answer.
"""

from __future__ import annotations

import json

from mini_ork.dispatch.providers import (
    _build_anthropic_compat,
    _build_anthropic_native,
    _claude_envelope,
    _wants_stream_json,
    claude_cost,
    claude_result_text,
    claude_session_id,
    parse_claude_usage,
)

_SESSION = "4cb4f6fd-d293-4eda-b9d9-1cd02ba9f85f"
_BODY = "Count from 1 to 3.\n1\n2\n3"

_RESULT_FIELDS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": _BODY,
    "session_id": _SESSION,
    "total_cost_usd": 0.317925,
    "usage": {
        "input_tokens": 58775,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 962,
    },
}

# `--output-format json`: one object, at exit.
JSON_MODE = json.dumps(_RESULT_FIELDS)

# `--output-format stream-json --verbose --include-partial-messages`: one object
# per line, the same fields in the final `result` event.
STREAM_MODE = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "system", "subtype": "init", "session_id": _SESSION, "tools": []},
        {"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"},
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "1"}},
        },
        {
            "type": "assistant",
            "session_id": _SESSION,
            "message": {"content": [{"type": "text", "text": _BODY}]},
        },
        _RESULT_FIELDS,
    )
)


def test_both_formats_parse_to_identical_values():
    """THE assertion: the format switch is invisible to every consumer."""
    for parse in (parse_claude_usage, claude_result_text, claude_session_id):
        assert parse(JSON_MODE) == parse(STREAM_MODE), parse.__name__
    # claude_cost takes (stdout, usage); compare against its own parsed usage.
    assert claude_cost(JSON_MODE, parse_claude_usage(JSON_MODE)) == claude_cost(
        STREAM_MODE, parse_claude_usage(STREAM_MODE)
    )


def test_parsed_values_are_actually_the_measured_ones():
    """Identical-but-wrong would pass the parity test, so pin the values too."""
    usage = parse_claude_usage(STREAM_MODE)
    assert usage.input_tokens == 58775
    assert usage.output_tokens == 962
    assert claude_cost(STREAM_MODE, usage) == 0.317925
    assert claude_session_id(STREAM_MODE) == _SESSION
    assert claude_result_text(STREAM_MODE) == _BODY


def test_the_last_result_event_wins():
    """A resumed or multi-turn run emits more than one result event; the last
    is the one whose usage was billed, so an early one must not shadow it."""
    first = dict(_RESULT_FIELDS, total_cost_usd=0.01, result="stale")
    stdout = "\n".join([json.dumps(first), json.dumps(_RESULT_FIELDS)])
    assert claude_cost(stdout, parse_claude_usage(stdout)) == 0.317925
    assert claude_result_text(stdout) == _BODY


def test_a_run_killed_before_it_finished_still_yields_its_session_id():
    """This is where stream-json is strictly better than json mode: the
    session_id is on the init line, so a run killed before it could emit a
    result — the exact runs E4 exists to resume — still hands back an id. In
    json mode stdout would be a truncated object and yield nothing."""
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": _SESSION}),
            json.dumps({"type": "assistant", "session_id": _SESSION, "message": {"content": []}}),
        ]
    )
    # No result event -> the *billed* parsers degrade to zero rather than raising.
    assert _claude_envelope(stdout) == {}
    assert parse_claude_usage(stdout).input_tokens == 0
    assert claude_cost(stdout, parse_claude_usage(stdout)) == 0.0
    # ...but the id survives, which is what makes the run resumable.
    assert claude_session_id(stdout) == _SESSION
    # An interrupted stream that DID emit a result keeps that one.
    assert claude_session_id(stdout + "\n" + json.dumps(dict(_RESULT_FIELDS, is_error=True))) == _SESSION


def test_z_insight_stripping_still_applies_in_stream_mode():
    """The planner shape gate slices the widest {..} span; a trailing protocol
    block would land its closing brace inside the block and reject a valid plan."""
    body = '{"steps": []}\n<z-insight>\n{"work_unit": {}}\n</z-insight>'
    stdout = json.dumps(dict(_RESULT_FIELDS, result=body))
    assert claude_result_text(stdout) == '{"steps": []}'


def test_malformed_and_empty_stdout_degrade_to_zero():
    for bad in ("", "not json at all", '{"type": "result"}', "{oops\n"):
        assert parse_claude_usage(bad).input_tokens == 0
        assert claude_cost(bad, parse_claude_usage(bad)) == 0.0
        assert claude_session_id(bad) == ""


def test_streaming_is_the_default_and_gateway_true_opts_out():
    """Every lane streams unless it declares it cannot. An unset, missing or
    misspelled value must NOT silently disable streaming — only the literal
    boolean ``true`` does."""
    assert _wants_stream_json({}) is True
    assert _wants_stream_json({"gateway": False}) is True
    for value in ("false", "False", 0, None, "no", "", "true", 1):
        assert _wants_stream_json({"gateway": value}) is True, value
    assert _wants_stream_json({"gateway": True}) is False


def _spec_for(entry: dict) -> object:
    return _build_anthropic_compat(
        "probe", entry, None, {}, entry.get("model")
    )


def test_streaming_lane_argv_carries_the_three_required_flags():
    """--include-partial-messages is what makes it token-level rather than
    whole-message; --verbose is mandatory whenever --print meets stream-json."""
    spec = _spec_for(
        {
            "base_url": "https://example.invalid/anthropic",
            "api_key_env": "NOPE_KEY",
            "gateway": False,
        }
    )
    cmd = spec.command  # type: ignore[attr-defined]
    assert "stream-json" in cmd
    assert "--include-partial-messages" in cmd
    assert "--verbose" in cmd
    assert "json" not in cmd


def test_default_lane_argv_streams():
    """A lane with no `gateway` key — the shape of every entry written before
    the knob existed — now streams, so its output lands on the live sidecar
    while the node runs instead of as one object at exit."""
    spec = _spec_for(
        {"base_url": "https://example.invalid/anthropic", "api_key_env": "NOPE_KEY"}
    )
    assert spec.command == (  # type: ignore[attr-defined]
        "claude",
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    )


def test_gateway_true_lane_falls_back_to_json():
    """The opt-out is the only way to get the single-object form back."""
    spec = _spec_for(
        {
            "base_url": "https://example.invalid/anthropic",
            "api_key_env": "NOPE_KEY",
            "gateway": True,
        }
    )
    assert spec.command[-1] == "json"  # type: ignore[attr-defined]
    assert "stream-json" not in spec.command  # type: ignore[attr-defined]


def test_anthropic_native_streams_by_default():
    """opus/sonnet run the same CLI against real Anthropic, which streams; the
    default follows the compat lanes, and `gateway: true` is the escape hatch."""
    on = _build_anthropic_native("opus", {}, None, {}, None)
    off = _build_anthropic_native("opus", {"gateway": True}, None, {}, None)
    assert "stream-json" in on.command  # type: ignore[attr-defined]
    assert off.command[-1] == "json"  # type: ignore[attr-defined]
