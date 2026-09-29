"""Unit tests for ``mini_ork.gates.abstain_gate``.

Hermetic: a tmp SQLite per test, seeded via the canonical
``mini_ork.stores.migrate.init_db`` so the gate sees the same schema
shape it does in production. No lane, no network, no live run.

The five cases mirror the kickoff's verification spec verbatim
(``kickoffs/auto/rsi-i6-abstain-gate.md``, L39-43):

  (1) ≥ 30 calibration rows + low-confidence ``pass``  → ``defer``
        AND exactly one inbox row.
  (2) ≥ 30 calibration rows + high-confidence ``pass`` → ``pass``.
  (3) ``fail`` from the verifier stays ``fail`` regardless of
        confidence — the load-bearing invariant.
  (4) < 30 calibration rows → the verifier verdict passes through
        unchanged. Kickoff rule #3: "no calibration → no abstention
        claims".
  (5) Synthetic split-conformal property: the empirical miscoverage on
        a held-out set stays within ``alpha + tolerance``.

Hermeticity rules (lens ``code_impact_lens.md``):

  * Seed ``MINI_ORK_HOME`` / ``MINI_ORK_DB`` / ``MINI_ORK_ROOT`` in the
    fixture so ``oversight_inbox._resolve_db_path`` falls through to
    the right DB (``oversight_inbox.py:60-63``).
  * Use the ``mig.init_db`` fixture (not the lighter ``_init_db`` from
    ``test_calibration_py.py``) so the gate-bootstrap path sees the
    full schema and the gate handles a missing ``verifier_results``
    column gracefully.
  * Avoid absolute timestamps — seed with relative offsets (per the
    timebomb memory).
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Importing ``abstain_gate`` registers the ``abstain_gate`` evaluator
# at module load via the OCP seam (side effect).
from mini_ork.gates import gate_registry as gr  # noqa: E402
import mini_ork.gates.abstain_gate  # noqa: E402,F401
# Reference the module so static analysers that don't honour ``# noqa``
# see the import as used. The side effect (register_gate_evaluator) is
# what the test suite depends on.
_ = mini_ork.gates.abstain_gate  # noqa: F841
from mini_ork.gates.abstain_gate import (  # noqa: E402
    DEFAULT_ALPHA,
    DEFAULT_MAX_ROWS,
    DEFAULT_MIN_CALIB,
    _conformal_threshold,
    _ltt_min_confidence,
)
from mini_ork.gates.oversight_inbox import pending  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixture + helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh tmp SQLite initialised by ``mig.init_db``.

    Seeds the env vars ``oversight_inbox._resolve_db_path`` reads when
    the gate passes ``db_path=None`` down through it. We pass an
    explicit ``db_path`` everywhere in the tests, but the env stays
    consistent with the production call path.
    """
    db_path = str(tmp_path / "state.db")
    rc, out, err = mig.init_db(db=db_path, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    return db_path


def _register_abstain_gate(db_path: str, verifier_name: str = "test_verifier") -> str:
    """Register an ``abstain_gate`` row in ``gate_registry`` and return its id."""
    # Mirror the kickoff's condition convention — the gate can be
    # addressed by name without an external ``condition`` string.
    return gr.gate_register(
        db_path, "abstain_gate", condition=verifier_name, safety=False
    )


def _seed_calibration_rows(
    db_path: str,
    verifier_name: str,
    confidences: list[float],
    *,
    is_false_positive: bool = False,
    is_false_negative: bool = False,
) -> None:
    """Insert ``verifier_results`` rows directly via SQL.

    Bypasses ``mini-ork``'s normal verifier-dispatch flow because that
    flow runs the model — these tests are hermetic and never call a
    model. The DDL column shape mirrors migration 0025 verbatim.
    """
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        # Use a relative timestamp via strftime('now','-N seconds') so
        # the seed is stable regardless of the wall clock — same
        # discipline as ``test_oversight_calibration_py``.
        for idx, conf in enumerate(confidences):
            con.execute(
                "INSERT INTO verifier_results "
                "(result_id, run_id, verifier_name, verdict, "
                " confidence, is_false_positive, is_false_negative, "
                " created_at) "
                "VALUES (?, ?, ?, 'pass', ?, ?, ?, "
                "  strftime('%s', 'now', ?))",
                (
                    f"seed-{verifier_name}-{idx:04d}",
                    f"run-seed-{verifier_name}-{idx:04d}",
                    verifier_name,
                    float(conf),
                    1 if is_false_positive else 0,
                    1 if is_false_negative else 0,
                    f"-{idx} seconds",
                ),
            )
        con.commit()
    finally:
        con.close()


def _seed_mixed_calibration(
    db_path: str,
    verifier_name: str,
    rows: list[tuple[float, bool]],
) -> None:
    """Seed labelled pass rows where each tuple is ``(confidence, is_wrong)``.

    The LTT test scenarios need a mix of correct + wrong rows with
    distinct confidences; ``_seed_calibration_rows`` only flips one
    flag for an entire batch. This helper inserts each row directly
    so per-row ``is_false_positive`` matches the tuple — and
    ``result_id`` collisions (which happen if the batch helper is
    called twice with the same verifier_name) are avoided by
    indexing into a single shared ``idx`` counter.
    """
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        for idx, (conf, is_wrong) in enumerate(rows):
            con.execute(
                "INSERT INTO verifier_results "
                "(result_id, run_id, verifier_name, verdict, "
                " confidence, is_false_positive, is_false_negative, "
                " created_at) "
                "VALUES (?, ?, ?, 'pass', ?, ?, ?, "
                "  strftime('%s', 'now', ?))",
                (
                    f"seed-{verifier_name}-{idx:04d}",
                    f"run-seed-{verifier_name}-{idx:04d}",
                    verifier_name,
                    float(conf),
                    1 if is_wrong else 0,
                    0,
                    f"-{idx} seconds",
                ),
            )
        con.commit()
    finally:
        con.close()


def _ctx(
    *,
    verifier_verdict: str = "pass",
    confidence: float | None = None,
    verifier_name: str = "test_verifier",
    run_id: str = "run-test",
    task_class: str = "framework-edit",
    lane: str = "codex",
    recipe: str = "framework-edit",
) -> str:
    """Serialise a context dict the way the executor would."""
    import json
    return json.dumps(
        {
            "verifier_verdict": verifier_verdict,
            "verdict": verifier_verdict,
            "confidence": confidence,
            "verifier_name": verifier_name,
            "run_id": run_id,
            "task_class": task_class,
            "lane": lane,
            "recipe": recipe,
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) ≥ 30 rows + low-confidence pass → defer + one inbox row
# ─────────────────────────────────────────────────────────────────────────────


def test_1_low_confidence_pass_defers_and_enqueues(db):
    """30+ correct, high-confidence historical rows raise the
    conformal bar high enough that a low-confidence new pass defers."""
    # All 30 historical passes had confidence 0.95 and were correct.
    # Scores = 1 - confidence = 0.05 (uniformly). Threshold at the
    # 95th percentile of 30 identical values is 0.05, so the
    # min_confidence bar is 1 - 0.05 = 0.95. A new pass with
    # confidence 0.5 falls below the bar → defer.
    _seed_calibration_rows(db, "test_verifier", [0.95] * 35)
    gid = _register_abstain_gate(db)

    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.5), mini_ork_root=str(REPO)
    )

    assert verdict == "defer", (
        f"expected defer for low-confidence pass with ≥30 calib rows, "
        f"got {verdict!r}"
    )
    inbox = pending(db_path=db)
    assert len(inbox) == pre_count + 1, (
        f"expected exactly one new inbox row, got {len(inbox) - pre_count}"
    )
    row = inbox[-1]
    assert row["gate_id"] == "abstain_gate"
    assert row["status"] == "pending"
    # The evidence must carry enough to be actionable downstream.
    ctx = row["context"]
    assert ctx["verifier_verdict"] == "pass"
    assert ctx["confidence"] == 0.5
    assert ctx["n_calib"] >= DEFAULT_MIN_CALIB
    assert 0.0 <= ctx["min_confidence"] <= 1.0
    assert ctx["alpha"] == DEFAULT_ALPHA
    assert ctx["verifier_name"] == "test_verifier"


# ─────────────────────────────────────────────────────────────────────────────
# (2) ≥ 30 rows + high-confidence pass → pass
# ─────────────────────────────────────────────────────────────────────────────


def test_2_high_confidence_pass_passes_through(db):
    """With the same calibration, a high-confidence pass clears the
    bar and the gate returns ``pass`` without enqueuing."""
    _seed_calibration_rows(db, "test_verifier", [0.95] * 35)
    gid = _register_abstain_gate(db)

    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.99), mini_ork_root=str(REPO)
    )

    assert verdict == "pass", (
        f"expected pass for high-confidence pass with ≥30 calib rows, "
        f"got {verdict!r}"
    )
    # High-confidence passes must NOT enqueue — otherwise the gate
    # would create rule-by-fatigue on the oversight channel.
    assert len(pending(db_path=db)) == pre_count


# ─────────────────────────────────────────────────────────────────────────────
# (3) fail stays fail — load-bearing invariant
# ─────────────────────────────────────────────────────────────────────────────


def test_3_fail_verdict_stays_fail_regardless_of_confidence(db):
    """A ``fail`` from the verifier is NEVER lifted to ``pass``.

    Even with calibration data that would normally defer a low-confidence
    pass, a ``fail`` verdict must short-circuit to ``fail``. Tested with
    every confidence value the kickoff could plausibly ask about:
    absent (calibration fallback path) and 0.99 (high-confidence fail
    must still not be lifted)."""
    _seed_calibration_rows(db, "test_verifier", [0.95] * 35)
    gid = _register_abstain_gate(db)

    # 3a: explicit high-confidence fail → fail, no inbox row.
    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(verifier_verdict="fail", confidence=0.99),
        mini_ork_root=str(REPO),
    )
    assert verdict == "fail", (
        f"expected fail to short-circuit, got {verdict!r}"
    )
    assert len(pending(db_path=db)) == pre_count, (
        "fail path must NOT enqueue — that would inflate the "
        "oversight channel with verdict-flips, not abstentions"
    )

    # 3b: low-confidence fail → still fail, still no inbox row.
    verdict = gr.gate_evaluate(
        db, gid, _ctx(verifier_verdict="fail", confidence=0.01),
        mini_ork_root=str(REPO),
    )
    assert verdict == "fail"
    assert len(pending(db_path=db)) == pre_count


# ─────────────────────────────────────────────────────────────────────────────
# (4) < 30 rows → pass-through unchanged
# ─────────────────────────────────────────────────────────────────────────────


def test_4_below_min_calib_passes_verdict_through(db):
    """With fewer than ``MO_ABSTAIN_MIN_CALIB`` rows the gate must
    return the verifier verdict untouched — no calibration → no
    abstention claim (kickoff rule #3).

    The test seeds only 5 rows (well below the default 30) and checks
    that a low-confidence ``pass`` is NOT converted to ``defer``."""
    _seed_calibration_rows(db, "test_verifier", [0.95] * 5)
    gid = _register_abstain_gate(db)

    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.1), mini_ork_root=str(REPO)
    )

    assert verdict == "pass", (
        f"expected pass-through below MIN_CALIB, got {verdict!r} — "
        f"the gate must not claim calibration it doesn't have"
    )
    assert len(pending(db_path=db)) == pre_count, (
        "below-threshold verdict must NOT enqueue — that is the "
        "anti-abstention-claim invariant"
    )


def test_4b_below_min_calib_fail_still_fails(db):
    """Pair to (4): the pass-through branch must not lift a ``fail``
    either. Belt-and-braces, since (3) already covered the
    fail-short-circuit but (4)'s pass-through code path is a different
    branch in the implementation."""
    _seed_calibration_rows(db, "test_verifier", [0.95] * 5)
    gid = _register_abstain_gate(db)

    verdict = gr.gate_evaluate(
        db, gid, _ctx(verifier_verdict="fail", confidence=0.1),
        mini_ork_root=str(REPO),
    )
    assert verdict == "fail"


# ─────────────────────────────────────────────────────────────────────────────
# (5) Synthetic split-conformal property
# ─────────────────────────────────────────────────────────────────────────────


def test_5_synthetic_conformal_coverage_holds():
    """Empirical miscoverage on held-out data stays within alpha + tol.

    The synthetic distribution is uniform on [0, 1] (so the true
    ``P(score > t) = 1 - t`` for any threshold ``t``). The conformal
    threshold at risk ``alpha`` is the ``(1 - alpha)`` quantile, which
    is exactly ``1 - alpha`` for the uniform — empirical coverage on a
    large held-out set must therefore be ``alpha ± tolerance``.

    Tolerance here is the same order the project uses for empirical
    comparisons (``tests/unit/test_calibration_py.py:425`` style),
    widened to ``2e-2`` to absorb 5 000-trial Monte-Carlo noise for
    alpha = 0.10 (Bernstein: ``stddev(sigma) ≈ sqrt(alpha*(1-alpha)/N)``
    = ~4e-3 for N = 5 000, plus a 3-sigma headroom → ``~2e-2``).
    """
    rng = random.Random(42)
    alpha = 0.10
    n_calib = 200
    n_held = 5_000
    tolerance = 2e-2

    # Calibration: 200 uniform scores in [0, 1].
    calib = [rng.random() for _ in range(n_calib)]
    threshold = _conformal_threshold(calib, alpha)

    # Held-out: 5 000 fresh scores from the same distribution.
    held_out = [rng.random() for _ in range(n_held)]
    exceed = sum(1 for s in held_out if s > threshold) / n_held

    # The conformal guarantee: empirical P(score > threshold) ≤ alpha
    # in expectation. The implementation indexes at
    # ``ceil((n+1)*(1-alpha)) - 1`` which for n=200, alpha=0.10 is
    # 181, so threshold ≈ 90th percentile ≈ 0.90. P(uniform > 0.90)
    # ≈ 0.10 by definition.
    assert exceed <= alpha + tolerance, (
        f"empirical coverage {exceed:.4f} exceeds alpha + tolerance "
        f"({alpha} + {tolerance}); conformal threshold not bounding"
    )


def test_5b_empty_scores_returns_permissive_threshold():
    """Edge case: with no historical rows, the threshold helper
    returns 1.0 (most permissive) — the caller short-circuits before
    this matters via the ``len(scores) < min_calib`` guard, but the
    helper itself must not raise."""
    assert _conformal_threshold([], DEFAULT_ALPHA) == 1.0
    assert _conformal_threshold([0.5] * 5, DEFAULT_ALPHA) >= 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (6) 0 calibration rows + pass + confidence=None → pass (no calibration,
#     no abstention claim — kickoff rule #3 + repair-ordering invariant)
# ─────────────────────────────────────────────────────────────────────────────


def test_6_no_calibration_no_confidence_passes_verdict_through(db):
    """With no labelled history AND no per-call confidence, the gate
    must return ``pass`` — not ``defer``. The repair reordered the
    branches in ``_eval_abstain`` so the calibration-load
    pass-through fires BEFORE the confidence read.

    Without this ordering, the old code would ``return "defer"`` on
    ``confidence is None`` (the original L273-274 short-circuit) even
    though we have zero calibration rows to back an abstention claim.
    The live DB on the prior branch was in exactly this state: zero
    ``verifier_results`` rows, every opt-in defer.
    """
    # No calibration rows seeded — empty ``verifier_results`` table.
    gid = _register_abstain_gate(db)

    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=None), mini_ork_root=str(REPO)
    )

    assert verdict == "pass", (
        f"expected pass-through with 0 calib rows + no confidence, "
        f"got {verdict!r} — the gate must not abstain without data"
    )
    assert len(pending(db_path=db)) == pre_count, (
        "pass-through must NOT enqueue — there is nothing to triage"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (7) ≥ 30 rows + pass + confidence=None → defer + one inbox row
#     (calibration exists, confidence is missing → the calibrated-error
#      fallback fires; with no margin in the context it stays None and
#      we defer)
# ─────────────────────────────────────────────────────────────────────────────


def test_7_calibration_exists_no_confidence_defers(db):
    """With ≥30 labelled rows but no per-call confidence AND no
    route_margin fallback, the gate must ``defer``. This is the
    mirror of (1) but exercises the ``confidence is None → defer``
    branch that now sits AFTER the calibration pass-through.

    No inbox-row assertion: per kickoff §Mechanism item 2, the
    missing-confidence branch keeps its "current behaviour" — an
    early return without enqueue. The conformal-comparison path
    (which does enqueue) is only reachable with a real confidence
    value and is covered by test_1.
    """
    _seed_calibration_rows(db, "test_verifier", [0.95] * 35)
    gid = _register_abstain_gate(db)

    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=None), mini_ork_root=str(REPO)
    )

    assert verdict == "defer", (
        f"expected defer for pass with ≥30 calib rows + no "
        f"confidence, got {verdict!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: OCP seam fires for a brand-new gate_id
# ─────────────────────────────────────────────────────────────────────────────


def test_ocp_seam_registers_abstain_gate():
    """Smoke test on the OCP seam itself: ``abstain_gate`` shows up
    in ``GATE_EVALUATORS`` after this module is imported (the module
    side-effect that bootstrap relies on). Mirrors
    ``test_gate_evaluator_registry_py.py:19-28`` for the gate in
    question."""
    assert "abstain_gate" in gr.GATE_EVALUATORS
    # The default knobs match the kickoff's defaults — guards against
    # a future refactor silently changing the calibration contract.
    assert DEFAULT_ALPHA == 0.05
    assert DEFAULT_MIN_CALIB == 30
    assert DEFAULT_MAX_ROWS >= DEFAULT_MIN_CALIB


# ─────────────────────────────────────────────────────────────────────────────
# (8) LTT — 40 rows, 1 wrong at low confidence → low defers, high passes
# ─────────────────────────────────────────────────────────────────────────────


def test_8_ltt_low_wrong_low_defers_high_passes(db):
    """LTT scenario A: 40 labelled pass rows, 1 wrong at LOW confidence
    (the verifier got it wrong but wasn't confident).

    The (k+1)/(n+1) bound is ≤ alpha when the low-confidence wrong row
    is included alongside 39 correct rows. A new low-confidence pass
    (below the wrong row's confidence) defers; a new high-confidence
    pass passes.
    """
    rows: list[tuple[float, bool]] = (
        [(0.95, False)] * 39 + [(0.05, True)]
    )
    _seed_mixed_calibration(db, "test_verifier", rows)
    gid = _register_abstain_gate(db)

    # Low-confidence pass: below the wrong row's confidence → defer.
    pre_count = len(pending(db_path=db))
    verdict_low = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.01), mini_ork_root=str(REPO)
    )
    assert verdict_low == "defer", (
        f"expected defer for low-confidence pass under LTT, "
        f"got {verdict_low!r}"
    )
    inbox = pending(db_path=db)
    assert len(inbox) == pre_count + 1
    row = inbox[-1]
    ctx = row["context"]
    assert ctx["verifier_verdict"] == "pass"
    assert ctx["confidence"] == 0.01
    # Evidence carries the LTT internals (no "reason" key on this
    # branch — only the uncertifiable branch sets it).
    assert ctx["alpha_bound_met"] is True
    assert ctx["accepted_n"] >= DEFAULT_MIN_CALIB
    assert ctx["accepted_wrong"] == 1
    assert 0.0 <= ctx["bound"] <= DEFAULT_ALPHA

    # High-confidence pass: above the bar → pass, no inbox row.
    pre_count = len(pending(db_path=db))
    verdict_high = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.99), mini_ork_root=str(REPO)
    )
    assert verdict_high == "pass", (
        f"expected pass for high-confidence pass under LTT, "
        f"got {verdict_high!r}"
    )
    assert len(pending(db_path=db)) == pre_count, (
        "high-confidence pass must NOT enqueue"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (9) LTT — 40 rows, 4 wrong spread across confidences (10% > alpha) → all
#     pass verdicts defer with reason ``verifier-uncertifiable-at-alpha``
# ─────────────────────────────────────────────────────────────────────────────


def test_9_ltt_uncertifiable_at_alpha(db):
    """LTT scenario B: 40 labelled pass rows, 4 wrong (10% > alpha=0.05)
    spread across confidences.

    Because wrong rows are present at every confidence bar the
    ``(k+1)/(n+1)`` bound exceeds alpha at every candidate threshold
    — the verifier is uncertifiable. Even a 0.99-confidence pass
    defers, and the inbox row carries ``reason:
    verifier-uncertifiable-at-alpha``. This is the regression that
    the kickoff calls out — the old conformal-quantile rule would
    have let a 0.05-confidence pass through under the same data.
    """
    rows: list[tuple[float, bool]] = (
        [(0.9, False)] * 36
        + [(0.5, True), (0.7, True), (0.85, True), (0.95, True)]
    )
    _seed_mixed_calibration(db, "test_verifier", rows)
    gid = _register_abstain_gate(db)

    # Even a 0.99-confidence pass defers.
    pre_count = len(pending(db_path=db))
    verdict = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.99), mini_ork_root=str(REPO)
    )
    assert verdict == "defer", (
        f"verifier uncertifiable at alpha; expected defer for "
        f"0.99-confidence pass, got {verdict!r}"
    )

    inbox = pending(db_path=db)
    assert len(inbox) == pre_count + 1
    row = inbox[-1]
    ctx = row["context"]
    assert ctx["reason"] == "verifier-uncertifiable-at-alpha", (
        f"expected verifier-uncertifiable-at-alpha reason, "
        f"got {ctx.get('reason')!r}"
    )
    assert ctx["alpha_bound_met"] is False
    assert ctx["accepted_n"] == 40
    assert ctx["accepted_wrong"] == 4
    # Bound at the full set = (4+1)/(40+1) ≈ 0.122 > alpha 0.05.
    assert ctx["bound"] > DEFAULT_ALPHA, (
        f"uncertifiable branch must report bound > alpha, "
        f"got {ctx['bound']}"
    )

    # And a low-confidence pass also defers — same reason.
    pre_count = len(pending(db_path=db))
    verdict_low = gr.gate_evaluate(
        db, gid, _ctx(confidence=0.1), mini_ork_root=str(REPO)
    )
    assert verdict_low == "defer"
    row = pending(db_path=db)[-1]
    assert row["context"]["reason"] == "verifier-uncertifiable-at-alpha"
    assert len(pending(db_path=db)) == pre_count + 1


# ─────────────────────────────────────────────────────────────────────────────
# (10) LTT — synthetic property: empirical FPR among accepted passes ≤ alpha
# ─────────────────────────────────────────────────────────────────────────────


def test_10_ltt_empirical_fpr_within_alpha():
    """LTT scenario C: on a synthetic held-out set drawn from the
    same distribution as the calibration, the empirical FPR among
    accepted rows is ≤ alpha + tolerance.

    The (k+1)/(n+1) bound is a high-confidence upper bound on the
    true FPR — with enough held-out trials the empirical FPR should
    stay within alpha of the true FPR. We seed calibration as 95%
    correct at high confidence + 5% wrong at low confidence; the LTT
    algorithm picks the smallest confidence bar such that
    ``(k+1)/(n+1) ≤ alpha`` over accepted rows, which gives an
    accepted set whose empirical FPR is bounded by alpha.
    """
    rng = random.Random(42)
    alpha_val = 0.10
    tolerance = 4e-2  # generous for held-out noise (sqrt(0.05*0.95/10000)≈2e-3)

    # Calibration: 190 correct at conf uniform in [0.6, 1.0], 10 wrong
    # at conf uniform in [0.05, 0.25]. Wrong rows are well below the
    # bulk of correct rows, so the LTT threshold sits near the wrong
    # rows' confidence — wrong rows are accepted at the smallest t
    # that still satisfies the bound.
    correct = [(rng.uniform(0.6, 1.0), False) for _ in range(190)]
    wrong = [(rng.uniform(0.05, 0.25), True) for _ in range(10)]
    lab = correct + wrong

    min_conf, uncert, accepted_n, accepted_wrong, bound = _ltt_min_confidence(
        lab, alpha_val
    )

    assert not uncert, "calibration should be certifiable at alpha=0.10"
    assert bound <= alpha_val, (
        f"LTT bound {bound} > alpha {alpha_val} (algorithm violation)"
    )
    # Accepted set covers every row at the smallest t (since including
    # wrong rows keeps the bound at (k+1)/(n+1) ≤ alpha).
    assert accepted_n == len(lab), (
        f"smallest t should accept the full set; got accepted_n="
        f"{accepted_n} of {len(lab)}"
    )
    assert accepted_wrong == len(wrong), (
        f"smallest t should accept all wrong rows when bound ≤ alpha; "
        f"got accepted_wrong={accepted_wrong}"
    )

    # Held-out: same distribution. With 10 000 trials the empirical
    # FPR is well within a few percent of the true FPR (≈ 5%).
    rng_h = random.Random(43)
    held_correct = [(rng_h.uniform(0.6, 1.0), False) for _ in range(9500)]
    held_wrong = [(rng_h.uniform(0.05, 0.25), True) for _ in range(500)]
    held = held_correct + held_wrong

    accepted = [(c, w) for c, w in held if c >= min_conf]
    assert accepted, "held-out set should produce accepted rows"
    empirical_fpr = sum(1 for _, w in accepted if w) / len(accepted)

    assert empirical_fpr <= alpha_val + tolerance, (
        f"empirical FPR {empirical_fpr:.4f} exceeds alpha + tolerance "
        f"({alpha_val} + {tolerance}); LTT threshold did not bound"
    )
