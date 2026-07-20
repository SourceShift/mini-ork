"""Standalone golden contracts for the native profile-answerer owner.

These tests intentionally do not source a Bash library. They preserve the
retired implementation's externally observable prompt, validation, parsing,
persistence, and Kimi-retry contracts while proving that the supported runtime
has one owner: :mod:`mini_ork.ported.profile_answerer`.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import profile_answerer as pa  # noqa: E402


def test_prompt_build_golden_contract(tmp_path: Path) -> None:
    """The fixed prompt and JSON escaping remain byte-stable."""
    kickoff_path = tmp_path / "kickoff.md"
    kickoff_path.write_text("# Tiny kickoff\n\n" + ("x" * 1024) + "\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "q1", "question": "What concrete outcome should the verifier enforce?"},
        {"id": "q2", "question": "Which lane should run first?"},
    ])

    assert pa.build_prompt(kickoff_path, questions_json) == (
        "You answer mini-ork run profile questions for autonomous child runs.\n\n"
        "Return ONLY a strict JSON object with this exact shape:\n"
        '{"answers":[{"question":"...","answer":"..."}],"auto_answered":true}\n\n'
        "Rules:\n"
        "- Answer every question using the kickoff content.\n"
        "- Keep answers concise and operational.\n"
        "- Do not include markdown, prose, code fences, or extra keys.\n\n"
        "Kickoff:\n# Tiny kickoff\n\n" + ("x" * 1024) + "\n\n\n"
        "Questions JSON:\n"
        '[{"id": "q1", "question": "What concrete outcome should the verifier enforce?"}, '
        '{"id": "q2", "question": "Which lane should run first?"}]\n'
    )

    big_kickoff = tmp_path / "big.md"
    big_kickoff.write_text("a" * 25_000, encoding="utf-8")
    big_prompt = pa.build_prompt(big_kickoff, "[]")
    assert "a" * 20_000 in big_prompt
    assert "a" * 20_001 not in big_prompt


def test_invalid_questions_json_uses_empty_list(tmp_path: Path) -> None:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\n", encoding="utf-8")
    assert pa.build_prompt(kickoff, "not-json").endswith("Questions JSON:\n[]\n")


def test_arg_validation_contract(tmp_path: Path) -> None:
    """Invalid arguments retain the public error messages."""
    valid_kickoff = tmp_path / "kickoff.md"
    valid_kickoff.write_text("# kickoff\n", encoding="utf-8")
    valid_questions = '[{"id":"q1","question":"q1"}]'
    valid_out = tmp_path / "answers.json"

    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions("", valid_questions, valid_out)
    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions(valid_kickoff, "", valid_out)
    with pytest.raises(ValueError, match=r"usage: mo_answer_profile_questions"):
        pa.answer_profile_questions(valid_kickoff, valid_questions, "")

    missing_kickoff = str(tmp_path / "does-not-exist.md")
    with pytest.raises(FileNotFoundError, match=re.escape(missing_kickoff)):
        pa.answer_profile_questions(missing_kickoff, valid_questions, valid_out)


def test_parse_fence_stripping_contract(tmp_path: Path) -> None:
    """Markdown-fenced provider JSON is normalized and persisted."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([{"id": "q1", "question": "q1"}])
    raw = (
        "```json\n"
        '{"answers":[{"question":"q1","answer":"a1"}],"auto_answered":true}\n'
        "```"
    )
    out = tmp_path / "answers.json"

    result = pa.answer_profile_questions(
        kickoff, questions_json, out, dispatch=lambda _: raw,
    )

    assert result == {
        "answers": [{"question": "q1", "answer": "a1"}],
        "auto_answered": True,
    }
    assert json.loads(out.read_text(encoding="utf-8")) == result


def test_parse_balanced_extraction_contract(tmp_path: Path) -> None:
    """A prose preamble is removed by the balanced-brace scanner."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([{"id": "q1", "question": "q1"}])
    raw = (
        "Sure! Here you go:\n\n"
        '{"answers":[{"question":"q1","answer":"a {nested} value"}],'
        '"auto_answered":true}\nTrailing prose.'
    )
    out = tmp_path / "answers.json"

    result = pa.answer_profile_questions(
        kickoff, questions_json, out, dispatch=lambda _: raw,
    )

    assert result["answers"] == [{"question": "q1", "answer": "a {nested} value"}]


def test_omitted_question_contract(tmp_path: Path) -> None:
    """A provider response that omits an input question is rejected."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "q1", "question": "first question"},
        {"id": "q2", "question": "second question"},
    ])
    raw = (
        "```json\n"
        '{"answers":[{"question":"first question","answer":"a1"}],'
        '"auto_answered":true}\n'
        "```"
    )

    with pytest.raises(RuntimeError, match=r"profile answerer omitted question: second question"):
        pa.answer_profile_questions(
            kickoff, questions_json, tmp_path / "answers.json", dispatch=lambda _: raw,
        )


def test_non_json_response_contract(tmp_path: Path) -> None:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# k\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"profile answerer returned non-json output"):
        pa.answer_profile_questions(
            kickoff,
            json.dumps([{"question": "q1"}]),
            tmp_path / "answers.json",
            dispatch=lambda _: "not JSON",
        )


def test_end_to_end_replay_golden_contract(tmp_path: Path) -> None:
    """A representative provider payload produces exact persisted bytes."""
    raw = (
        "Provider acknowledgement before payload.\n"
        '{"answers":['
        '{"question":"Summarize the goal in one sentence.","answer":"Deliver x."},'
        '{"question":"What is explicitly out of scope?","answer":"y."}'
        '],"auto_answered":true}\nTrailing text ignored.'
    )
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# Tiny kickoff\n\nGoal: x. Out of scope: y.\n", encoding="utf-8")
    questions_json = json.dumps([
        {"id": "goal", "question": "Summarize the goal in one sentence."},
        {"id": "scope", "question": "What is explicitly out of scope?"},
    ])
    out = tmp_path / "answers.json"

    pa.answer_profile_questions(kickoff, questions_json, out, dispatch=lambda _: raw)

    assert out.read_text(encoding="utf-8") == (
        "{\n"
        '  "answers": [\n'
        "    {\n"
        '      "question": "Summarize the goal in one sentence.",\n'
        '      "answer": "Deliver x."\n'
        "    },\n"
        "    {\n"
        '      "question": "What is explicitly out of scope?",\n'
        '      "answer": "y."\n'
        "    }\n"
        "  ],\n"
        '  "auto_answered": true\n'
        "}\n"
    )


def test_default_dispatch_uses_native_kimi_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone default path reaches native Kimi, never Bash."""
    from mini_ork.ported import llm_dispatch as native_dispatch

    calls = []

    def fake(argv, *, root, dispatch_fn):
        calls.append((argv, root, dispatch_fn))
        print('{"answers":[]}', end="")
        return 0

    marker = lambda *args: 0
    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    raw = pa._default_dispatch(
        "profile prompt", repo_root="/engine", dispatch_fn=marker,
    )

    assert raw == '{"answers":[]}'
    assert calls == [(
        [
            "--task-class", "profile_answerer",
            "--node-type", "profile_answerer",
            "--model", "kimi",
            "--prompt-text", "profile prompt",
        ],
        "/engine",
        marker,
    )]


def test_default_dispatch_retries_kimi_on_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace success is unusable and retains the historical Kimi retry."""
    from mini_ork.ported import llm_dispatch as native_dispatch

    calls = 0
    models = []

    def fake(argv, *, root, dispatch_fn):
        nonlocal calls
        calls += 1
        models.append(argv[argv.index("--model") + 1])
        print("   " if calls == 1 else '{"answers":[]}', end="")
        return 0

    monkeypatch.setattr(native_dispatch, "llm_dispatch", fake)
    raw = pa._default_dispatch("profile prompt", repo_root="/engine")

    assert raw == '{"answers":[]}'
    assert models == ["kimi", "kimi"]


def test_retired_bash_owner_is_absent() -> None:
    assert not (REPO / "lib" / "profile_answerer.sh").exists()
