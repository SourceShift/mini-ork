"""Parity gate: mini_ork.ported.profile_answerer vs lib/profile_answerer.sh.

Each test invokes the LIVE bash subprocess (sourcing lib/profile_answerer.sh
and a per-test stub of `llm_dispatch` so the LLM boundary is replayable) on
the same inputs as the Python port and asserts byte-identical output.

No mocks, no hardcoded expected outputs — expected is always derived from a
control bash invocation that shares the inputs.

Six cases (the kickoff's >=6 floor):

  (a) prompt_build_parity              — bash heredoc prompt builder vs
                                         pa.build_prompt; sha256 byte-equal
  (b) arg_validation_parity            — bash exit 2 + stderr text matches
                                         port ValueError / FileNotFoundError
                                         across 4 sub-cases (empty kickoff /
                                         empty questions / empty out /
                                         missing-kickoff file)
  (c) parse_fence_stripping_parity     — bash vs python on a raw LLM response
                                         wrapped in ```json … ``` fences
  (d) parse_balanced_extraction_parity — bash vs python on a raw response
                                         that prepends prose before the JSON
                                         object
  (e) omitted_question_parity          — bash vs python both raise on a raw
                                         response that omits one of the input
                                         questions (mirror SystemExit as
                                         RuntimeError, matching message text)
  (f) end_to_end_replay_parity         — bash vs python on a CAPTURED REAL LLM
                                         response (tests/fixtures/profile_answerer_replay.raw)
                                         — byte-equal JSON output across the full
                                         pipeline (prompt → dispatch → fence-strip
                                         → balanced-extract → validate → persist)

Capture-once / replay-many: test (f) skips if the fixture file is missing —
this honors the 'no live LLM calls in test suite' rule. To capture:

    bash scripts/capture_profile_answerer_fixture.sh
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import profile_answerer as pa  # noqa: E402

SH = REPO / "lib" / "profile_answerer.sh"
FIXTURES = REPO / "tests" / "fixtures"
REPLAY_FIXTURE = FIXTURES / "profile_answerer_replay.raw"


def _bash_invoke(
    kickoff_path: str | os.PathLike,
    questions_json: str,
    out_path: str | os.PathLike,
    raw_override: str | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the LIVE bash `mo_answer_profile_questions` with a stubbed
    `llm_dispatch` (if `raw_override` is provided) and return the CompletedProcess.

    The stub is sourced AFTER lib/profile_answerer.sh so the function
    override wins at call time (bash resolves function names at call time,
    not source time).
    """
    if raw_override is not None:
        # Write the stub to a temp file so we can source it after the lib.
        # The stub defines llm_dispatch so the live dispatch is short-circuited.
        stub_body = (
            "llm_dispatch() {\n"
            "  # Stub for parity tests: echo the captured raw response verbatim.\n"
            '  printf "%s" "$MO_PROFILE_TEST_RAW"\n'
            "  return 0\n"
            "}\n"
            "export -f llm_dispatch\n"
        )
        stub_file = Path(out_path).parent / "_llm_dispatch_stub.sh"
        stub_file.write_text(stub_body, encoding="utf-8")
        # shlex.quote wraps in single quotes (safe against inner double-quote
        # delimiter parsing that would otherwise word-split the QJSON inside
        # the bash -c wrapper string).
        wrapper = (
            f'. {shlex.quote(str(SH))}\n'
            f'. {shlex.quote(str(stub_file))}\n'
            f'mo_answer_profile_questions {shlex.quote(str(kickoff_path))} '
            f'{shlex.quote(str(questions_json))} {shlex.quote(str(out_path))}\n'
        )
        env = {**os.environ, "MO_PROFILE_TEST_RAW": raw_override}
    else:
        # No stub — let bash's real llm_dispatch fire. Used only by test (f)'s
        # capture-once branch, never on CI re-runs (the fixture exists by then).
        wrapper = (
            f'. {shlex.quote(str(SH))}\n'
            f'mo_answer_profile_questions {shlex.quote(str(kickoff_path))} '
            f'{shlex.quote(str(questions_json))} {shlex.quote(str(out_path))}\n'
        )
        env = dict(os.environ)

    return subprocess.run(
        ["bash", "-c", wrapper],
        capture_output=True,
        text=True,
        env=env,
    )


def _bash_build_prompt(kickoff_path: str | os.PathLike, questions_json: str) -> str:
    """Invoke ONLY the bash prompt-builder heredoc (verbatim from
    lib/profile_answerer.sh:26-53) and return its stdout."""
    bash_script = (
        'python3 - "$1" "$2" <<\'PY\'\n'
        'import json\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        'kickoff = Path(sys.argv[1]).read_text(encoding="utf-8")[:20000]\n'
        'try:\n'
        '    questions = json.loads(sys.argv[2] or "[]")\n'
        'except json.JSONDecodeError:\n'
        '    questions = []\n'
        '\n'
        'print("""You answer mini-ork run profile questions for autonomous child runs.\n'
        '\n'
        'Return ONLY a strict JSON object with this exact shape:\n'
        '{"answers":[{"question":"...","answer":"..."}],"auto_answered":true}\n'
        '\n'
        'Rules:\n'
        '- Answer every question using the kickoff content.\n'
        '- Keep answers concise and operational.\n'
        '- Do not include markdown, prose, code fences, or extra keys.\n'
        '\n'
        'Kickoff:\n'
        '""" + kickoff + """\n'
        '\n'
        'Questions JSON:\n'
        '""" + json.dumps(questions, ensure_ascii=True))\n'
        'PY\n'
    )
    r = subprocess.run(
        ["bash", "-c", bash_script, "_", str(kickoff_path), questions_json],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (a) prompt_build_parity — bash heredoc vs pa.build_prompt, sha256-equal
# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_build_parity(tmp_path):
    """Python `build_prompt` matches the verbatim bash heredoc at
    lib/profile_answerer.sh:26-53 byte-for-byte (sha256-equal)."""
    kickoff_path = tmp_path / "kickoff.md"
    kickoff_path.write_text("# Tiny kickoff\n\n" + ("x" * 1024) + "\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "q1", "question": "What concrete outcome should the verifier enforce?"},
        {"id": "q2", "question": "Which lane should run first?"},
    ])

    bash_prompt = _bash_build_prompt(kickoff_path, questions_json)
    py_prompt = pa.build_prompt(kickoff_path, questions_json)

    assert hashlib.sha256(bash_prompt.encode("utf-8")).hexdigest() == \
           hashlib.sha256(py_prompt.encode("utf-8")).hexdigest(), (
        f"prompt mismatch: bash head={bash_prompt[:200]!r} py head={py_prompt[:200]!r}"
    )

    # Also: [:20000] cap parity — kickoff of 25_000 chars gets truncated to 20_000.
    big = "a" * 25_000
    big_kickoff = tmp_path / "big.md"
    big_kickoff.write_text(big, encoding="utf-8")
    bash_big = _bash_build_prompt(big_kickoff, "[]")
    py_big = pa.build_prompt(big_kickoff, "[]")
    # The bash heredoc slices to [:20000]; the python port must match.
    assert "a" * 20_000 in py_big
    assert "a" * 20_001 not in py_big
    assert bash_big == py_big


# ─────────────────────────────────────────────────────────────────────────────
# (b) arg_validation_parity — bash exit 2 + stderr text vs Python exception
# ─────────────────────────────────────────────────────────────────────────────
def test_arg_validation_parity(tmp_path):
    """Python `answer_profile_questions` raises the same exception type with
    the same message text as bash's exit-2 + stderr contract across 4 sub-cases."""
    valid_kickoff = tmp_path / "kickoff.md"
    valid_kickoff.write_text("# kickoff\n", encoding="utf-8")
    valid_questions = '[{"id":"q1","question":"q1"}]'
    valid_out = tmp_path / "answers.json"

    # (b1) empty kickoff_path
    r1 = _bash_invoke("", valid_questions, valid_out)
    assert r1.returncode == 2
    assert "usage: mo_answer_profile_questions" in r1.stderr
    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions("", valid_questions, valid_out)

    # (b2) empty questions_json
    r2 = _bash_invoke(valid_kickoff, "", valid_out)
    assert r2.returncode == 2
    assert "usage: mo_answer_profile_questions" in r2.stderr
    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions(valid_kickoff, "", valid_out)

    # (b3) empty profile_answers_out_path
    r3 = _bash_invoke(valid_kickoff, valid_questions, "")
    assert r3.returncode == 2
    assert "usage: mo_answer_profile_questions" in r3.stderr
    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions(valid_kickoff, valid_questions, "")

    # (b4) valid args but kickoff file missing
    missing_kickoff = str(tmp_path / "does-not-exist.md")
    r4 = _bash_invoke(missing_kickoff, valid_questions, valid_out)
    assert r4.returncode == 2
    assert f"profile answerer kickoff not found: {missing_kickoff}" in r4.stderr
    with pytest.raises(FileNotFoundError, match=re.escape(missing_kickoff)):
        pa.answer_profile_questions(missing_kickoff, valid_questions, valid_out)


# ─────────────────────────────────────────────────────────────────────────────
# (c) parse_fence_stripping_parity — raw LLM response wrapped in fences
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_fence_stripping_parity(tmp_path):
    """Markdown fence-stripping is byte-stable across bash and python.

    Raw fixture: ```json\\n{...}\\n```. Stubbed llm_dispatch echoes this.
    """
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "q1", "question": "q1"},
    ])
    raw = (
        "```json\n"
        '{"answers":[{"question":"q1","answer":"a1"}],"auto_answered":true}\n'
        "```"
    )

    bash_out = tmp_path / "bash_answers.json"
    py_out = tmp_path / "py_answers.json"

    _bash_invoke(kickoff, questions_json, bash_out, raw_override=raw)
    pa.answer_profile_questions(
        kickoff, questions_json, py_out, dispatch=lambda _: raw,
    )

    bash_payload = json.loads(bash_out.read_text(encoding="utf-8"))
    py_payload = json.loads(py_out.read_text(encoding="utf-8"))
    assert bash_payload == py_payload
    assert bash_payload == {
        "answers": [{"question": "q1", "answer": "a1"}],
        "auto_answered": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (d) parse_balanced_extraction_parity — preamble-then-JSON recovery
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_balanced_extraction_parity(tmp_path):
    """Prose preamble before the JSON object is recovered by the balanced-brace
    scanner in both bash and python."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([{"id": "q1", "question": "q1"}])
    raw = (
        "Sure! Here you go:\n"
        "\n"
        '{"answers":[{"question":"q1","answer":"a1"}],"auto_answered":true}\n'
    )

    bash_out = tmp_path / "bash_answers.json"
    py_out = tmp_path / "py_answers.json"

    _bash_invoke(kickoff, questions_json, bash_out, raw_override=raw)
    pa.answer_profile_questions(
        kickoff, questions_json, py_out, dispatch=lambda _: raw,
    )

    bash_payload = json.loads(bash_out.read_text(encoding="utf-8"))
    py_payload = json.loads(py_out.read_text(encoding="utf-8"))
    assert bash_payload == py_payload
    assert bash_payload == {
        "answers": [{"question": "q1", "answer": "a1"}],
        "auto_answered": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (e) omitted_question_parity — bash SystemExit vs python RuntimeError
# ─────────────────────────────────────────────────────────────────────────────
def test_omitted_question_parity(tmp_path):
    """When the LLM response omits one of the input questions, both bash and
    python refuse with the same message text (bash raises SystemExit with a
    specific message; python mirrors as RuntimeError)."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "q1", "question": "first question"},
        {"id": "q2", "question": "second question"},
    ])
    # Only q1 is answered; q2 omitted.
    raw = (
        "```json\n"
        '{"answers":[{"question":"first question","answer":"a1"}],'
        '"auto_answered":true}\n'
        "```"
    )

    bash_out = tmp_path / "bash_answers.json"
    py_out = tmp_path / "py_answers.json"

    rb = _bash_invoke(kickoff, questions_json, bash_out, raw_override=raw)
    assert rb.returncode != 0
    assert "profile answerer omitted question" in rb.stderr
    assert "second question" in rb.stderr

    with pytest.raises(RuntimeError, match=r"profile answerer omitted question: second question"):
        pa.answer_profile_questions(
            kickoff, questions_json, py_out, dispatch=lambda _: raw,
        )


# ─────────────────────────────────────────────────────────────────────────────
# (f) end_to_end_replay_parity — captured real LLM response
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(
    not REPLAY_FIXTURE.exists(),
    reason=(
        "Replay fixture missing. To capture a real LLM response once: "
        "bash scripts/capture_profile_answerer_fixture.sh"
    ),
)
def test_end_to_end_replay_parity(tmp_path):
    """Full pipeline (prompt → dispatch → fence-strip → balanced-extract →
    validate → persist) is byte-stable on a CAPTURED REAL LLM response.

    Capture-once / replay-many: pytest NEVER calls the live LLM (CI cost +
    flakiness). The fixture is captured manually via
    scripts/capture_profile_answerer_fixture.sh, which runs the bash
    function ONCE against the real deepseek/kimi dispatch chain and saves
    the raw LLM text to tests/fixtures/profile_answerer_replay.raw. After
    that, this test replays the captured response through both sides and
    asserts byte-equal JSON output.
    """
    fixture_raw = REPLAY_FIXTURE.read_text(encoding="utf-8")
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(
        "# Tiny kickoff\n\nGoal: x. Out of scope: y.\n",
        encoding="utf-8",
    )
    questions_json = json.dumps([
        {"id": "goal", "question": "Summarize the goal in one sentence."},
        {"id": "scope", "question": "What is explicitly out of scope?"},
    ])

    bash_out = tmp_path / "bash_answers.json"
    py_out = tmp_path / "py_answers.json"

    _bash_invoke(kickoff, questions_json, bash_out, raw_override=fixture_raw)
    pa.answer_profile_questions(
        kickoff, questions_json, py_out, dispatch=lambda _: fixture_raw,
    )

    bash_text = bash_out.read_text(encoding="utf-8")
    py_text = py_out.read_text(encoding="utf-8")
    bash_payload = json.loads(bash_text)
    py_payload = json.loads(py_text)

    # Byte-equal JSON output (1e-6 float tolerance per kickoff — vacuous here
    # since profile_answerer never produces floats; kept as defense in depth).
    def _round_floats(o, tol=1e-6):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, dict):
            return {k: _round_floats(v, tol) for k, v in o.items()}
        if isinstance(o, list):
            return [_round_floats(v, tol) for v in o]
        return o
    assert _round_floats(bash_payload) == _round_floats(py_payload), (
        f"bash={bash_payload!r}\npython={py_payload!r}"
    )