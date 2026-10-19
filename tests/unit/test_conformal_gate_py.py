"""Unit tests for ``mini_ork.gates.conformal_gate``.

Hermetic: each test gets a temp SQLite seeded directly with the
``verifier_results`` row shape that ``abstain_gate``'s loaders query,
plus the ``oversight_inbox`` bootstrap path for the defer branch. No
lane, no network, no live run.

The groups mirror ``kickoffs/auto/vt10-conformal-gate.md`` §Tests:

  1. ``nonconformity`` scoring and clamping.
  2. ``calibrate`` split-conformal quantile (delegated arithmetic).
  3. ``decide`` boundary semantics.
  4. ``calibration_report`` finite-sample claim gating.
  5. The evaluator: off/uncalibrated pass-through, defer + one
     oversight row, accept, and the fail-is-never-lifted invariant.
  6. Env clamps for ``MO_CONFORMAL_ALPHA`` / ``MO_CONFORMAL_MIN_CALIB``.
  7. The existing abstain-gate regression runs alongside this file
     (see the kickoff's verification command).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Importing registers both evaluators at module load via the OCP seam.
from mini_ork.gates import gate_registry as gr  # noqa: E402
import mini_ork.gates.abstain_gate as abstain_gate  # noqa: E402
import mini_ork.gates.conformal_gate as conformal_gate  # noqa: E402
from mini_ork.gates.oversight_inbox import pending  # noqa: E402

_KNOBS = (
    "MO_CONFORMAL_ORACLE",
    "MO_CONFORMAL_ALPHA",
    "MO_CONFORMAL_MIN_CALIB",
    "MO_ABSTAIN_ALPHA",
    "MO_ABSTAIN_MIN_CALIB",
    "MO_ABSTAIN_MAX_ROWS",
)


@pytest.fixture(autouse=True)
def _documented_default_env(monkeypatch):
    """Every test starts from the documented defaults (gate OFF)."""
    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)


_VERIFIER_RESULTS_DDL = (
    "CREATE TABLE IF NOT EXISTS verifier_results ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " verifier_name TEXT NOT NULL,"
    " verdict TEXT NOT NULL,"
    " confidence REAL,"
    " is_false_positive INTEGER NOT NULL DEFAULT 0,"
    " is_false_negative INTEGER NOT NULL DEFAULT 0,"
    " created_at INTEGER NOT NULL)"
)


@pytest.fixture()
def db(tmp_path):
    """Temp DB with a ``verifier_results`` table (no rows yet)."""
    db_path = str(tmp_path / "state.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(_VERIFIER_RESULTS_DDL)
        con.commit()
    finally:
        con.close()
    return db_path


def _seed(db_path: str, verifier_name: str, rows: list[tuple[str, float, int]]) -> None:
    """Insert ``(verdict, confidence, is_false_positive)`` rows.

    ``created_at`` is a per-row increasing integer so ``ORDER BY
    created_at DESC`` is deterministic (newest row = last inserted).
    """
    con = sqlite3.connect(db_path)
    try:
        con.executemany(
            "INSERT INTO verifier_results (verifier_name, verdict, confidence,"
            " is_false_positive, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (verifier_name, verdict, confidence, is_fp, 1000 + idx)
                for idx, (verdict, confidence, is_fp) in enumerate(rows)
            ],
        )
        con.commit()
    finally:
        con.close()


def _seed_correct_passes(db_path: str, verifier: str, confidences: list[float]) -> None:
    _seed(db_path, verifier, [("pass", c, 0) for c in confidences])


def _ctx(
    verdict: str = "pass",
    confidence: float | None = 0.99,
    *,
    verifier: str = "test_verifier",
) -> str:
    return json.dumps(
        {
            "verifier_verdict": verdict,
            "verdict": verdict,
            "confidence": confidence,
            "verifier_name": verifier,
            "run_id": "run-test",
            "task_class": "framework-edit",
            "lane": "codex",
            "recipe": "framework-edit",
        }
    )


def _evaluate(condition: str, context_json: str, db_path: str | None) -> str:
    """Drive the gate through the public OCP seam, never a private handle."""
    evaluator = gr.GATE_EVALUATORS["conformal_gate"]
    return evaluator(condition, context_json, db_path, None)


# ─────────────────────────────────────────────────────────────────────────────
# (1) nonconformity
# ─────────────────────────────────────────────────────────────────────────────


def test_nonconformity_wrong_rows_are_one():
    assert conformal_gate.nonconformity(confidence=0.99, wrong=True) == 1.0
    assert conformal_gate.nonconformity(confidence=0.0, wrong=True) == 1.0
    assert conformal_gate.nonconformity(confidence=None, wrong=True) == 1.0


def test_nonconformity_confidence_mapping():
    assert conformal_gate.nonconformity(confidence=0.9) == pytest.approx(0.1)
    assert conformal_gate.nonconformity(confidence=1.0) == pytest.approx(0.0)
    assert conformal_gate.nonconformity(confidence=0.0) == pytest.approx(1.0)


def test_nonconformity_missing_invalid_and_nan_are_conservative():
    assert conformal_gate.nonconformity(confidence=None) == 1.0
    assert conformal_gate.nonconformity(confidence="not-a-number") == 1.0
    assert conformal_gate.nonconformity(confidence=float("nan")) == 1.0


def test_nonconformity_out_of_range_clamps_into_unit_interval():
    assert conformal_gate.nonconformity(confidence=1.5) == pytest.approx(0.0)
    assert conformal_gate.nonconformity(confidence=-0.25) == pytest.approx(1.0)
    for confidence in (1.5, -0.25, 0.0, 1.0, 0.37):
        score = conformal_gate.nonconformity(confidence=confidence)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# (2) calibrate
# ─────────────────────────────────────────────────────────────────────────────


def test_calibrate_matches_conformal_quantile_index():
    assert conformal_gate.calibrate([0.1, 0.2, 0.3, 0.4, 0.5], 0.5) == pytest.approx(0.3)
    scores = [0.01 * (i + 1) for i in range(10)]
    assert conformal_gate.calibrate(scores, 0.10) == pytest.approx(0.10)
    assert conformal_gate.calibrate(scores, 0.30) == pytest.approx(0.08)
    assert conformal_gate.calibrate(scores, 0.0) == pytest.approx(0.10)
    assert conformal_gate.calibrate(scores, 0.999) == pytest.approx(0.01)


def test_calibrate_empty_list_returns_one():
    assert conformal_gate.calibrate([], 0.10) == 1.0


def test_calibrate_sorts_non_monotonic_input():
    scrambled = [0.9, 0.1, 0.5, 0.2, 0.3]
    ordered = sorted(scrambled)
    for alpha in (0.0, 0.1, 0.25, 0.5, 0.75, 0.999):
        assert conformal_gate.calibrate(scrambled, alpha) == pytest.approx(
            conformal_gate.calibrate(ordered, alpha)
        )
    # n=5, alpha=0.25 -> ceil(6 * 0.75) = 5 -> 5th smallest = max.
    assert conformal_gate.calibrate(scrambled, 0.25) == pytest.approx(0.9)


def test_calibrate_delegates_to_abstain_gate_public_alias():
    cases = [
        ([], 0.1),
        ([0.3, 0.1, 0.2], 0.0),
        ([0.3, 0.1, 0.2], 0.999),
        ([0.1, 0.2, 0.3, 0.4, 0.5], 0.5),
        ([(i + 1) / 31.0 for i in range(30)], 0.10),
        ([0.05] * 30, 0.10),
    ]
    for scores, alpha in cases:
        assert conformal_gate.calibrate(scores, alpha) == pytest.approx(
            abstain_gate.conformal_threshold(scores, alpha)
        )


# ─────────────────────────────────────────────────────────────────────────────
# (3) decide
# ─────────────────────────────────────────────────────────────────────────────


def test_decide_accept_iff_score_le_threshold():
    assert conformal_gate.decide(0.3, 0.3) == "accept"  # boundary inclusive
    assert conformal_gate.decide(0.29, 0.3) == "accept"
    assert conformal_gate.decide(0.31, 0.3) == "abstain"


# ─────────────────────────────────────────────────────────────────────────────
# (4) calibration_report
# ─────────────────────────────────────────────────────────────────────────────


def test_calibration_report_shape():
    report = conformal_gate.calibration_report([0.1, 0.2], 0.10)
    for key in ("n", "alpha", "threshold", "coverage_lower", "min_calib", "calibrated"):
        assert key in report
    assert isinstance(report["calibrated"], bool)


def test_calibration_report_below_min_calib_claims_no_coverage():
    report = conformal_gate.calibration_report([0.05] * 29, 0.10)
    assert report["n"] == 29
    assert report["min_calib"] == 30
    assert report["calibrated"] is False
    assert report["coverage_lower"] is None


def test_calibration_report_at_min_calib_states_bound():
    scores = [0.05] * 30
    report = conformal_gate.calibration_report(scores, 0.10)
    assert report["n"] == 30
    assert report["calibrated"] is True
    assert report["coverage_lower"] == pytest.approx(1.0 - 0.10)
    assert report["alpha"] == pytest.approx(0.10)
    assert report["threshold"] == pytest.approx(conformal_gate.calibrate(scores, 0.10))


def test_min_calib_env_is_raise_only_and_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("MO_CONFORMAL_MIN_CALIB", "1")
    lowered = conformal_gate.calibration_report([0.05] * 29, 0.10)
    assert lowered["min_calib"] == 30
    assert lowered["calibrated"] is False
    assert lowered["coverage_lower"] is None

    monkeypatch.setenv("MO_CONFORMAL_MIN_CALIB", "50")
    raised = conformal_gate.calibration_report([0.05] * 30, 0.10)
    assert raised["min_calib"] == 50
    assert raised["calibrated"] is False
    assert raised["coverage_lower"] is None

    monkeypatch.setenv("MO_CONFORMAL_MIN_CALIB", "banana")
    assert conformal_gate.calibration_report([0.05] * 29, 0.10)["min_calib"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# (5) Evaluator
# ─────────────────────────────────────────────────────────────────────────────


def test_gate_registered_through_ocp_seam_on_import():
    assert "conformal_gate" in gr.GATE_EVALUATORS
    assert callable(gr.GATE_EVALUATORS["conformal_gate"])
    # A second gate, not a replacement: abstain_gate still coexists.
    assert "abstain_gate" in gr.GATE_EVALUATORS
    assert gr.GATE_EVALUATORS["abstain_gate"] is not gr.GATE_EVALUATORS["conformal_gate"]


def test_register_function_registers_gate_type():
    gr.GATE_EVALUATORS.pop("conformal_gate", None)
    try:
        assert "conformal_gate" not in gr.GATE_EVALUATORS
        assert conformal_gate.register() is None
        assert callable(gr.GATE_EVALUATORS["conformal_gate"])
    finally:
        conformal_gate.register()  # restore the import-time registration


def test_evaluator_off_passes_verdict_through_unchanged(db):
    """Knob unset/0: verdicts unchanged, nothing enqueued even though the
    calibration data would defer a 0.2-confidence pass."""
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    assert _evaluate("test_verifier", _ctx(confidence=0.2), db) == "pass"
    assert pending(db_path=db) == []
    assert _evaluate("test_verifier", _ctx("fail", 0.99), db) == "fail"
    assert pending(db_path=db) == []


def test_evaluator_uncalibrated_passes_through_without_claim(db):
    """Fewer than min_calib rows: no calibration -> no abstention claim."""
    _seed_correct_passes(db, "test_verifier", [0.95] * 5)
    assert _evaluate("test_verifier", _ctx(confidence=0.2), db) == "pass"
    # No DB at all -> zero rows -> still a pass-through.
    assert _evaluate("test_verifier", _ctx(confidence=0.2), None) == "pass"
    assert pending(db_path=db) == []


def test_evaluator_defer_via_ocp_seam_enqueues_exactly_one_oversight_row(db, monkeypatch):
    """A high non-conformity pass with enough calibration rows defers and
    writes exactly one oversight_inbox row through the public evaluator."""
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    # Foreign verifier + fail rows must not enter the calibration.
    _seed(db, "other_verifier", [("pass", 0.99, 0)] * 5)
    _seed(db, "test_verifier", [("fail", 0.9, 0)] * 2)

    assert _evaluate("test_verifier", _ctx(confidence=0.2), db) == "defer"

    inbox = pending(db_path=db)
    assert len(inbox) == 1
    row = inbox[0]
    assert row["gate_id"] == "conformal_gate"
    assert row["status"] == "pending"
    evidence = row["context"]
    assert evidence["reason"] == "conformal-threshold-exceeded"
    assert evidence["score"] == pytest.approx(0.8)
    assert evidence["threshold"] == pytest.approx(0.05)
    assert evidence["n"] == 30
    assert evidence["alpha"] == pytest.approx(0.10)
    assert evidence["verifier_name"] == "test_verifier"


def test_evaluator_defer_reachable_through_gate_evaluate(db, monkeypatch):
    """End-to-end OCP path: a registered ``conformal_gate`` row evaluated
    via ``gate_registry.gate_evaluate`` defers and enqueues one item."""
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    gid = gr.gate_register(db, "conformal_gate", condition="test_verifier")

    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.2), mini_ork_root=str(REPO)
    )

    assert verdict == "defer"
    inbox = pending(db_path=db)
    assert len(inbox) == 1
    assert inbox[0]["gate_id"] == "conformal_gate"
    assert inbox[0]["context"]["reason"] == "conformal-threshold-exceeded"


def test_evaluator_accept_returns_original_pass(db, monkeypatch):
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    # score = 1 - 0.99 = 0.01 <= threshold 0.05 -> accept.
    assert _evaluate("test_verifier", _ctx(confidence=0.99), db) == "pass"
    assert pending(db_path=db) == []


def test_evaluator_fail_is_never_lifted(db, monkeypatch):
    """Load-bearing invariant: ``fail`` short-circuits before the knob."""
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    for confidence in (0.99, 0.01, None):
        assert _evaluate("test_verifier", _ctx("fail", confidence), db) == "fail"
    assert pending(db_path=db) == []


def test_evaluator_fail_safe_defaults_mirror_abstain_gate(db, monkeypatch):
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    _seed_correct_passes(db, "test_verifier", [0.95] * 30)
    evaluator = gr.GATE_EVALUATORS["conformal_gate"]
    # Malformed JSON defers.
    assert evaluator("test_verifier", "{not json", db, None) == "defer"
    # Empty / unknown / non-pass verdicts defer without enqueuing.
    for payload in ({}, _ctx("bogus", 0.9), _ctx("defer", 0.2)):
        assert evaluator("test_verifier", json.dumps(payload), db, None) == "defer"
    assert pending(db_path=db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (6) Env clamps, observed through the evaluator
# ─────────────────────────────────────────────────────────────────────────────


def test_alpha_env_above_default_is_clamped_in_evaluator(db, monkeypatch):
    """MO_CONFORMAL_ALPHA=0.9 behaves as 0.10: threshold ~= 28/31, so a
    0.5-score pass is accepted. An unclamped 0.9 would defer it."""
    scores = [(i + 1) / 31.0 for i in range(30)]
    _seed(db, "test_verifier", [("pass", 1.0 - s, 0) for s in scores])
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    monkeypatch.setenv("MO_CONFORMAL_ALPHA", "0.9")
    assert _evaluate("test_verifier", _ctx(confidence=0.5), db) == "pass"
    assert pending(db_path=db) == []


def test_min_calib_env_below_default_is_raised_in_evaluator(db, monkeypatch):
    """MO_CONFORMAL_MIN_CALIB=1 is raised to 30: five rows pass through."""
    _seed_correct_passes(db, "test_verifier", [0.95] * 5)
    monkeypatch.setenv("MO_CONFORMAL_ORACLE", "1")
    monkeypatch.setenv("MO_CONFORMAL_MIN_CALIB", "1")
    assert _evaluate("test_verifier", _ctx(confidence=0.2), db) == "pass"
    assert pending(db_path=db) == []


def test_alpha_helper_clamps_and_defaults(monkeypatch):
    monkeypatch.setenv("MO_CONFORMAL_ALPHA", "0.9")
    assert conformal_gate.alpha() == pytest.approx(0.10)
    monkeypatch.setenv("MO_CONFORMAL_ALPHA", "0.05")
    assert conformal_gate.alpha() == pytest.approx(0.05)
    monkeypatch.setenv("MO_CONFORMAL_ALPHA", "banana")
    assert conformal_gate.alpha() == pytest.approx(0.10)


# ─────────────────────────────────────────────────────────────────────────────
# Review bar: finite-sample claim in the docstring
# ─────────────────────────────────────────────────────────────────────────────


def test_module_docstring_states_finite_sample_claim():
    doc = conformal_gate.__doc__ or ""
    assert "finite" in doc.lower()
    assert "coverage" in doc.lower()
