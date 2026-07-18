"""Standalone unit tests for ``mini_ork.ported.profile_gate``.

Replaces the bash-parity gate (against ``lib/profile_gate.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer runs ``lib/profile_gate.sh`` in a subprocess — it
asserts the port's behaviour directly. These pin the same contract exercised
by the bash unit test (``tests/unit/test_profile_gate_zero_questions.sh``):
normalizing the "needs_answers with ZERO human_questions" contradiction to
"ready", while leaving every other case untouched.

File I/O is scoped to pytest's ``tmp_path`` fixture (no production paths, no
env vars, no sqlite) so each test is fully isolated.
"""

from __future__ import annotations

import json
from pathlib import Path

from mini_ork.ported.profile_gate import normalize_zero_questions

_MARKER = "needs_answers->ready (0 questions: nothing to answer)"


def _write(path: Path, body: dict | str) -> Path:
    if isinstance(body, str):
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(json.dumps(body), encoding="utf-8")
    return path


class TestNeedsAnswersZeroQuestions:
    """Case 1 (dedicated bash test) — the core bug fix."""

    def test_normalizes_to_ready_and_rewrites_file(self, tmp_path):
        p = _write(tmp_path / "p1.json", {
            "profile_status": "needs_answers", "human_questions": [],
            "confidence": 0.9, "recipe": "code_fix",
        })
        out = normalize_zero_questions(str(p))

        assert out == "ready"
        j = json.loads(p.read_text(encoding="utf-8"))
        assert j["profile_status"] == "ready"
        assert j["human_questions"] == []
        assert j["profile_status_normalized"] == _MARKER
        assert j["confidence"] == 0.9  # gate stays independent of confidence floor

    def test_preserves_confidence_and_extra_keys(self, tmp_path):
        p = _write(tmp_path / "p6.json", {
            "profile_status": "needs_answers", "human_questions": [],
            "confidence": 0.42, "recipe": "code_fix", "task_class": "code_fix",
            "target_repo": "mo-wt-profile_gate", "success_criteria": ["parity"],
            "scope_allow": ["*.py"], "scope_deny": ["lib/profile_gate.sh"],
        })
        out = normalize_zero_questions(str(p))

        assert out == "ready"
        j = json.loads(p.read_text(encoding="utf-8"))
        assert j["confidence"] == 0.42
        assert j["recipe"] == "code_fix"
        assert j["task_class"] == "code_fix"
        assert j["target_repo"] == "mo-wt-profile_gate"
        assert j["success_criteria"] == ["parity"]
        assert j["scope_allow"] == ["*.py"]
        assert j["scope_deny"] == ["lib/profile_gate.sh"]


class TestNeedsAnswersRealQuestions:
    """Case 2 (dedicated bash test) — a legitimate block must stay blocked."""

    def test_unchanged_when_questions_present(self, tmp_path):
        body = {
            "profile_status": "needs_answers",
            "human_questions": ["What is the target repo?"],
            "confidence": 0.4,
        }
        p = _write(tmp_path / "p2.json", body)
        before = p.read_bytes()

        out = normalize_zero_questions(str(p))

        assert out == "needs_answers"
        assert p.read_bytes() == before  # file untouched


class TestAlreadyReady:
    """Case 3 (dedicated bash test) — idempotence on an already-ready profile."""

    def test_ready_with_no_questions_unchanged(self, tmp_path):
        body = {"profile_status": "ready", "human_questions": [], "confidence": 1.0}
        p = _write(tmp_path / "p3.json", body)
        before = p.read_bytes()

        out = normalize_zero_questions(str(p))

        assert out == "ready"
        assert p.read_bytes() == before

    def test_ready_with_nonempty_questions_passthrough(self, tmp_path):
        body = {
            "profile_status": "ready",
            "human_questions": ["documentation-only flag remains?"],
            "confidence": 0.7,
        }
        p = _write(tmp_path / "p7.json", body)
        before = p.read_bytes()

        out = normalize_zero_questions(str(p))

        assert out == "ready"
        assert p.read_bytes() == before


class TestMissingOrEmptyPath:
    """Case 4 (dedicated bash test) — missing path is a safe no-op."""

    def test_missing_path_returns_empty_and_creates_nothing(self, tmp_path):
        bogus = tmp_path / "does-not-exist.json"
        assert not bogus.exists()

        out = normalize_zero_questions(str(bogus))

        assert out == ""
        assert not bogus.exists()

    def test_empty_string_path_returns_empty(self):
        assert normalize_zero_questions("") == ""


class TestMalformedJson:
    def test_malformed_json_returns_empty_and_file_untouched(self, tmp_path):
        bad = "{not json"
        p = _write(tmp_path / "mal.json", bad)
        before = p.read_bytes()

        out = normalize_zero_questions(str(p))

        assert out == ""
        assert p.read_bytes() == before == bad.encode("utf-8")


class TestStatusCoercion:
    """profile_status is coerced via ``str(x or "")`` — missing/None-valued
    keys must not be misread as a truthy 'needs_answers' match."""

    def test_missing_profile_status_key_treated_as_empty(self, tmp_path):
        p = _write(tmp_path / "p8.json", {"human_questions": []})
        out = normalize_zero_questions(str(p))
        assert out == ""

    def test_null_profile_status_returns_empty(self, tmp_path):
        p = _write(tmp_path / "p9.json", {
            "profile_status": None, "human_questions": [],
        })
        out = normalize_zero_questions(str(p))
        assert out == ""

    def test_missing_human_questions_key_treated_as_empty_list(self, tmp_path):
        # No `human_questions` key at all -> `.get(...) or []` -> [] -> normalizes.
        p = _write(tmp_path / "p10.json", {
            "profile_status": "needs_answers", "confidence": 0.5,
        })
        out = normalize_zero_questions(str(p))
        assert out == "ready"
        j = json.loads(p.read_text(encoding="utf-8"))
        assert j["human_questions"] == []
        assert j["profile_status_normalized"] == _MARKER
