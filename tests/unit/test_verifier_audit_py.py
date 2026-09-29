"""Hermetic tests for ``mini_ork.learning.verifier_audit`` + the
``promotion_gate`` integration it gates.

The four cases the kickoff specifies:

  (1) ``audit()`` with a hack-probe flag in seeded data → ``ok=False``,
      ``flags`` contains ``"hack_probe"``; a promotion through
      ``promotion_evaluate`` is downgraded to ``quarantined`` with a
      rationale prefixed ``verifier-audit:``.
  (2) ``audit()`` with clean histories → ``ok=True``, no flags.
  (3) ``audit()`` with no histories (default DB probes return ``[]``) →
      ``ok=True``, no flags (fail-open per detector).
  (4) ``MO_PROMOTION_VERIFIER_AUDIT=0`` restores the prior behaviour:
      promotion_evaluate returns ``promoted`` unchanged.

The kickoff tests "promotion quarantined with reason" via the audit's
``flags`` key plus the rationale prefix the gate emits; the flag is injected
through ``hack_history=`` so the test is hermetic (no detector-shaped DB
table needs to exist).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO))
from mini_ork.gates import promotion_gate as pg  # noqa: E402
from mini_ork.learning import verifier_audit as va  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Initialise a fresh SQLite file via init_db.

    Mirrors the ``db`` fixture in ``tests/unit/test_promotion_gate_py.py`` so
    the gate integration tests reuse the same canonical setup.
    """
    home = tmp_path
    db_path = home / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO)
    return db_path


def _seed_workflow(db_path: Path) -> None:
    """Seed the workflow rows that promotion_evaluate requires."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("""
            INSERT OR IGNORE INTO workflow_memory
                (workflow_version_id, workflow_name, yaml_hash, yaml_blob)
            VALUES ('test-wf-v1', 'test-wf', 'deadbeef', '# test')
        """)
        for cid in ("cand-clean", "cand-flag", "cand-flag-injection"):
            con.execute("""
                INSERT OR IGNORE INTO workflow_candidates
                    (candidate_id, base_workflow_version_id, created_by)
                VALUES (?, 'test-wf-v1', 'human')
            """, (cid,))
        con.commit()
    finally:
        con.close()


def _seed_bench_all_pass(db_path: Path, candidate_id: str) -> None:
    """Seed 4 passing benchmark_results rows across 3 INDEPENDENT runs.

    Same discipline as ``test_promotion_gate_py._seed_bench_all_pass``: N
    samples from one run are one observation, so the seed spreads over
    three distinct runs.
    """
    con = sqlite3.connect(str(db_path))
    try:
        for bid in ("bench-task-1", "bench-task-2",
                    "bench-task-3", "bench-task-4"):
            con.execute("""
                INSERT OR IGNORE INTO benchmark_tasks
                    (benchmark_id, task_class)
                VALUES (?, 'code_fix')
            """, (bid,))
        for rid in (1, 2, 3):
            con.execute("""
                INSERT OR IGNORE INTO runs (id, started_at)
                VALUES (?, strftime('%s','now'))
            """, (rid,))
        rows = [
            ("bench-task-1", 1, 1),
            ("bench-task-2", 2, 1),
            ("bench-task-3", 3, 1),
            ("bench-task-4", 1, 1),
        ]
        for i, (bid, rid, passed) in enumerate(rows, start=1):
            con.execute("""
                INSERT OR IGNORE INTO benchmark_results
                    (result_id, benchmark_id, candidate_id, run_id,
                     pass, utility_score)
                VALUES (?, ?, ?, ?, ?, 0.92)
            """, (f"res-{candidate_id}-{i}", bid, candidate_id, rid, passed))
        con.commit()
    finally:
        con.close()


def _hacked_history() -> list[dict]:
    """A history where the visible score rises while the frozen core falls.

    Same shape as ``tests/unit/test_hack_probe_py.test_monitor_hacked_history``.
    Eight rows: visible climbs 0.55→0.9, core_pass falls 4→0. The detector's
    family-wise p is below 0.05 → ``hacking=True``.
    """
    visible = [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]
    core_pass = [4, 3, 2, 1, 0, 0, 0, 0]
    return [
        {"gen": i, "visible": v, "core_pass": cp, "core_n": 10,
         "confident": True}
        for i, (v, cp) in enumerate(zip(visible, core_pass))
    ]


def _clean_hack_history() -> list[dict]:
    """A history where the visible score and core agree (no hacking)."""
    return [
        {"gen": i, "visible": 0.7, "core_pass": 7, "core_n": 10,
         "confident": True}
        for i in range(8)
    ]


def _clean_metric_history() -> list[dict]:
    """An anchor-disciplined metric: clean anchor, discriminating, full size."""
    held_out = list("abcdefghij")  # exactly REFERENCE_SIZE = 10
    rows: list[dict] = []
    for i in range(6):
        rows.append({
            "gen": i,
            "scored": [f"q{i}-a", f"q{i}-b"],
            "held_out": held_out,
            "agreements": [True, False, True, True, False, True],
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# (1) Hack-probe flag in seeded data → promotion quarantined with reason.
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_flags_hack_probe_in_seeded_history(db):
    """A history that the detector reports as ``hacking=True`` produces
    ``ok=False`` with ``flags=["hack_probe"]``. The detector itself is the
    only authority on its verdict; the aggregator reuses ``monitor``."""
    _seed_workflow(db)
    result = va.audit(None, str(db), hack_history=_hacked_history())

    assert result["ok"] is False
    assert "hack_probe" in result["flags"]
    # The detector's verdict is propagated verbatim in evidence.
    assert result["evidence"]["hack_probe"]["hacking"] is True


def test_promotion_quarantined_when_audit_flags(db, monkeypatch):
    """End-to-end: ``promotion_evaluate`` downgrades a would-be promote to
    ``quarantined`` when the audit flags the detector, and the rationale
    carries the ``verifier-audit:`` prefix naming the flag."""
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-flag")

    # Inject a flag via the gate's audit seam without mutating the audit
    # module's public surface. The audit's DB-probe path is preserved
    # (empty in tests); the gate integration test stubs the call.
    from mini_ork.learning import verifier_audit as va_mod

    def _fake_audit(task_class, db_path, **_kwargs):
        return {
            "ok": False,
            "flags": ["hack_probe"],
            "evidence": {"hack_probe": {"hacking": True}},
            "audit_version": va_mod.AUDIT_VERSION,
        }

    monkeypatch.setattr(va_mod, "audit", _fake_audit)

    pobj = pg.promotion_evaluate(str(db), "cand-flag")

    assert pobj["decision"] == "quarantined"
    assert "verifier-audit:hack_probe" in pobj["rationale"]
    # All benchmark tasks still passed; the gate downgraded only because
    # the audit flagged the grader.
    assert pobj["all_pass"] is True
    # Audit evidence travels in the payload so a downstream consumer can
    # inspect which detector fired.
    assert pobj["verifier_audit"]["ok"] is False
    assert "hack_probe" in pobj["verifier_audit"]["flags"]


# ─────────────────────────────────────────────────────────────────────────────
# (2) Clean data → promotion decision unchanged (audit ok=True).
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_clean_data_ok(db):
    """A history that the detector reports as ``hacking=False`` contributes
    no flag. ``audit()`` returns ``ok=True`` and ``flags==[]``."""
    _seed_workflow(db)
    result = va.audit(None, str(db),
                      hack_history=_clean_hack_history(),
                      metric_history=_clean_metric_history())

    assert result["ok"] is True
    assert result["flags"] == []
    # Both detectors' reports are present in evidence.
    assert result["evidence"]["hack_probe"]["hacking"] is False
    assert result["evidence"]["metric_anchor"]["intact"] is True


def test_promotion_promoted_when_audit_clean(db):
    """End-to-end: with the audit reporting ``ok=True``, the prior
    ``promoted`` decision is preserved unchanged."""
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-clean")

    pobj = pg.promotion_evaluate(str(db), "cand-clean")

    assert pobj["decision"] == "promoted"
    assert "verifier-audit" not in pobj["rationale"]
    assert pobj["verifier_audit"]["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# (3) Detectors with no data → no flags (fail-open per detector).
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_no_history_no_flags(db):
    """A fresh DB has no detector-shaped rows; the audit probes return
    ``[]``, every detector runs on empty history, and ``ok=True``."""
    _seed_workflow(db)
    result = va.audit(None, str(db))

    assert result["ok"] is True
    assert result["flags"] == []
    # The hack_probe detector reports ``hacking=False`` and ``p_family=None``
    # on empty history — never a fabricated finding.
    hp = result["evidence"]["hack_probe"]
    assert hp["n_generations"] == 0
    assert hp["hacking"] is False
    # metric_anchor reports ``intact=None`` on empty history — an unaudited
    # metric must never read as an intact one, but the audit treats ``None``
    # as "no flag" (the undecided verdict is a fact about the metric, not a
    # defect of the candidate's grader as a whole).
    ma = result["evidence"]["metric_anchor"]
    assert ma["n_generations"] == 0
    assert ma["intact"] is None
    # The gate-fuzzer arm is skipped when no corpus/workdir is supplied —
    # Path B from the code-impact lens. A skipped arm is not a defect.
    assert "skipped" in result["evidence"]["gate_fuzzer"]


def test_promotion_promoted_when_no_audit_data(db):
    """End-to-end: a fresh DB yields ``ok=True`` (fail-open) and the
    promotion proceeds as if the audit were empty."""
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-clean")

    pobj = pg.promotion_evaluate(str(db), "cand-clean")

    assert pobj["decision"] == "promoted"
    assert "verifier-audit" not in pobj["rationale"]
    # The audit ran (default ON) and reported ok=True.
    assert pobj["verifier_audit"]["ok"] is True
    assert pobj["verifier_audit"]["flags"] == []


# ─────────────────────────────────────────────────────────────────────────────
# (4) MO_PROMOTION_VERIFIER_AUDIT=0 restores prior behaviour.
# ─────────────────────────────────────────────────────────────────────────────


def test_opt_out_restores_promoted(db, monkeypatch):
    """``MO_PROMOTION_VERIFIER_AUDIT=0`` skips the audit entirely. Even
    when a flag WOULD have fired, the decision stays ``promoted`` and the
    rationale carries no ``verifier-audit:`` prefix."""
    _seed_workflow(db)
    _seed_bench_all_pass(db, "cand-clean")

    # Sanity: with the env unset (default ON), the audit runs.
    monkeypatch.delenv("MO_PROMOTION_VERIFIER_AUDIT", raising=False)
    before = pg.promotion_evaluate(str(db), "cand-clean")
    assert before["decision"] == "promoted"
    # On a fresh DB the audit is ok=True; payload still carries the
    # evidence so a downstream consumer can verify it ran.
    assert before["verifier_audit"]["ok"] is True

    # Now opt out. Even if the audit would have flagged, the gate must
    # return ``promoted`` unchanged. We assert this by stubbing the audit
    # to return a flag and confirming the gate ignores it.
    from mini_ork.learning import verifier_audit as va_mod

    def _fake_flagged(*_args, **_kwargs):
        return {
            "ok": False,
            "flags": ["hack_probe"],
            "evidence": {"hack_probe": {"hacking": True}},
            "audit_version": va_mod.AUDIT_VERSION,
        }

    monkeypatch.setattr(va_mod, "audit", _fake_flagged)
    monkeypatch.setenv("MO_PROMOTION_VERIFIER_AUDIT", "0")

    after = pg.promotion_evaluate(str(db), "cand-clean")
    assert after["decision"] == "promoted"
    assert "verifier-audit" not in after["rationale"]
    # The audit payload is None when the audit was opted out.
    assert after["verifier_audit"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Defensive contract — the audit must NEVER turn a reject into a promote.
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_cannot_upgrade_rejected_to_promoted(db, monkeypatch):
    """``promotion_evaluate`` rejects a candidate with zero benchmark rows
    before the audit runs. A flag in the audit must not change that. The
    audit only acts on the promote branch; the reject branch is invariant
    under audit input."""
    _seed_workflow(db)

    from mini_ork.learning import verifier_audit as va_mod

    def _fake_flagged(*_args, **_kwargs):
        return {
            "ok": False,
            "flags": ["hack_probe", "metric_anchor"],
            "evidence": {
                "hack_probe": {"hacking": True},
                "metric_anchor": {"intact": False},
            },
            "audit_version": va_mod.AUDIT_VERSION,
        }

    monkeypatch.setattr(va_mod, "audit", _fake_flagged)

    pobj = pg.promotion_evaluate(str(db), "cand-flag")

    assert pobj["decision"] == "rejected"
    assert "no benchmark measurement" in pobj["rationale"]
    # The audit never runs on the reject branch — there is nothing to act on,
    # so a rejected decision is invariant under audit input. The audit
    # only fires for would-be promotes (kickoff rule).
    assert pobj["verifier_audit"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (5) Production DB-probe path: collapse_history seeds drive the audit.
#     kickoff ``auto/rsi-i2-verifier-audit-repair.md`` §"Tests (add)" — these
#     tests deliberately avoid the hack_history / metric_history kwargs so the
#     DB-probe path is exercised end-to-end.
# ─────────────────────────────────────────────────────────────────────────────


def _seed_collapse_history(
    db_path: Path,
    task_class: str,
    rows: list[tuple[float, float]],
    run_id: str = "run-test",
) -> None:
    """Seed ``collapse_history`` with (score, anchor) pairs at sequential steps.

    Mirrors the apply-loop writer at ``mini_ork/cli/apply.py:729-795``:
    ``task_class``, ``step``, ``score``, ``anchor``, ``directives=0``,
    ``run_id``, ``created_at=now``. The audit's new reader
    (``_read_collapse_history``) consumes the same columns.
    """
    import time as _time
    con = sqlite3.connect(str(db_path))
    try:
        for step, (score, anchor) in enumerate(rows):
            con.execute(
                "INSERT INTO collapse_history "
                "(task_class, step, score, anchor, directives, "
                " run_id, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (task_class, step, score, anchor, run_id,
                 int(_time.time())),
            )
        con.commit()
    finally:
        con.close()


def test_audit_flags_hack_probe_via_collapse_history(db):
    """Hack-shape rows in ``collapse_history`` drive the audit end-to-end.

    The detector's ``MIN_GENERATIONS = 4`` (``hack_probe.py:35``) is the
    minimum that yields a ``p_family``, so seed 8 rows with visible score
    rising while anchor falls. Without the kwargs injection the audit must
    still find these rows — that's the whole point of the kickoff.

    Seed shape mirrors the canonical hack fixture at
    ``tests/unit/test_verifier_audit_py.py:117-130`` after projection:
    visible 0.55→0.9, core_rate 0.4→0.0. The detector's
    ``level_gap`` and ``confidently_wrong`` tests both produce a sign-test
    p ≈ 0.008 → family p ≈ 0.016 < 0.05 → ``hacking=True``.
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "code_fix",
        # (score, anchor) — score climbs 0.55→0.9, anchor falls 0.4→0.0.
        # The detector requires ``visible > core_rate`` consistently for
        # ``level_gap`` to fire (all-positive diffs → tiny sign-test p);
        # the existing kwargs fixture at lines 117-130 uses the same
        # core_pass=[4,3,2,1,0,0,0,0] magnitude.
        [(0.55, 0.4), (0.60, 0.3), (0.65, 0.2), (0.70, 0.1),
         (0.75, 0.0), (0.80, 0.0), (0.85, 0.0), (0.90, 0.0)],
    )

    # No kwargs — exercises the production DB-probe path.
    result = va.audit("code_fix", str(db))

    assert result["ok"] is False
    assert "hack_probe" in result["flags"]
    assert result["evidence"]["hack_probe"]["hacking"] is True

    # End-to-end: the same audit finding downgrades the promotion to
    # quarantined with the ``verifier-audit:`` rationale prefix.
    _seed_bench_all_pass(db, "cand-flag")
    pobj = pg.promotion_evaluate(str(db), "cand-flag")

    assert pobj["decision"] == "quarantined"
    assert "verifier-audit:hack_probe" in pobj["rationale"]
    assert pobj["verifier_audit"]["ok"] is False


def test_audit_clean_collapse_history_no_flags(db):
    """Healthy rows in ``collapse_history`` produce no flags.

    Visible and anchor rise together, with one small dip that prevents the
    ``confidently_wrong`` sign test from firing on a perfectly-balanced
    zero-failure history (n=6 visible-pass, k=0 failures → p ≈ 0.03 < ALPHA,
    a detector quirk: the sign test treats "all one direction" as extreme
    regardless of which direction). The dip at step 2 (anchor 0.4 while
    visible 0.6) injects one failure → k=1, n=6 → p ≈ 0.22 — not significant.

    ``level_gap`` sees a mixed-pos/neg diff sequence → p ≈ 0.13, also not
    significant. Family p ≈ 0.32 — no flag.
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "code_fix",
        # (score, anchor) — both rise, with one dip in anchor at step 2
        # to keep the confidently_wrong sign test from going all-positive.
        [(0.40, 0.40), (0.50, 0.50), (0.60, 0.40), (0.70, 0.70),
         (0.75, 0.75), (0.80, 0.80), (0.85, 0.85), (0.90, 0.90)],
    )

    result = va.audit("code_fix", str(db))

    assert result["ok"] is True
    assert result["flags"] == []
    assert result["evidence"]["hack_probe"]["hacking"] is False
    # metric_anchor: the projection populates ``scored`` / ``held_out`` /
    # ``agreements`` per row from the row's own anchor (kickoff
    # ``auto/rsi-i2b-audit-sensitivity.md`` rule #1), so ``n_with_anchor``
    # counts every row. The balanced ``[True, False]`` agreement keeps
    # ``discrimination = 0.5`` → above the vacuity floor → ``intact is
    # True`` on a clean trajectory. The collapse signal lives in the
    # ``collapse`` arm, not here.
    assert result["evidence"]["metric_anchor"]["n_with_anchor"] == 8
    assert result["evidence"]["metric_anchor"]["intact"] is True
    # The new ``collapse`` arm reads the same rows; healthy rows produce
    # ``recommendation="none"`` and contribute no flag.
    assert result["evidence"]["collapse"]["recommendation"] == "none"


def test_audit_filters_collapse_history_by_task_class(db):
    """Rows for one task_class do not contaminate an audit over another.

    Two groups under distinct classes. The "hacked" group has the same
    shape that triggers hack_probe; the "clean" group is healthy. When
    the audit is scoped to "clean-class" the hacked rows must be filtered
    out by the ``WHERE task_class=?`` clause.
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "hacked-class",
        [(0.55, 0.4), (0.60, 0.3), (0.65, 0.2), (0.70, 0.1),
         (0.75, 0.0), (0.80, 0.0), (0.85, 0.0), (0.90, 0.0)],
    )
    _seed_collapse_history(
        db, "clean-class",
        [(0.40, 0.40), (0.50, 0.50), (0.60, 0.40), (0.70, 0.70),
         (0.75, 0.75), (0.80, 0.80), (0.85, 0.85), (0.90, 0.90)],
    )

    hacked = va.audit("hacked-class", str(db))
    assert hacked["ok"] is False
    assert "hack_probe" in hacked["flags"]

    clean = va.audit("clean-class", str(db))
    assert clean["ok"] is True
    assert clean["flags"] == []
    assert clean["evidence"]["hack_probe"]["hacking"] is False

    # The two class-scoped audits above prove isolation — the hacked
    # rows are invisible to the "clean-class" audit, and the clean
    # rows don't suppress the "hacked-class" audit. We deliberately do
    # NOT assert anything about the unfiltered (task_class=None) audit:
    # mixing classes can dilute or amplify the detector's family-wise
    # p in ways the per-class read avoids by construction.


# ─────────────────────────────────────────────────────────────────────────────
# (6) I2b sensitivity — the verifier audit must flag a textbook collapse.
#     kickoff ``auto/rsi-i2b-audit-sensitivity.md`` — the audit previously
#     missed the textbook signature (n_with_anchor=0, no collapse arm); this
#     section proves both fixes end-to-end against the live smoke fixture.
# ─────────────────────────────────────────────────────────────────────────────


def test_audit_flags_textbook_collapse_via_collapse_history(db):
    """The live-smoke fixture: 10 rows of ``score = 0.40 + 0.06*i``,
    ``anchor = 0.95 - 0.08*i`` (score rising, anchor falling).

    The kickoff's prior state returned ``ok=True, flags=[]`` because:
      (a) ``_project_metric_row`` emitted ``scored=None, held_out=None``
          → ``metric_anchor`` saw zero anchor measurements;
      (b) the audit never wired ``collapse_detector.detect``, so the
          breaker's ``recommendation="halt"`` had no echo in the audit.

    After the I2b fix the audit:
      * populates ``scored`` / ``held_out`` / ``agreements`` so
        ``metric_anchor.audit`` reports ``n_with_anchor == 10``;
      * runs ``collapse_detector.detect`` on the same rows and trips
        ``recommendation == "halt"`` (half-split means: first-half
        score mean = 0.52, second-half = 0.82 → score_rise = +0.30;
        first-half anchor mean = 0.79, second-half = 0.39 → anchor_drop
        = +0.40; divergence = ``score_rise > 0 and anchor_drop > 0``).
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "code_fix",
        [(round(0.40 + 0.06 * i, 6), round(0.95 - 0.08 * i, 6))
         for i in range(10)],
    )

    # No kwargs — exercises the production DB-probe path. The audit
    # must pick up the textbook collapse end-to-end.
    result = va.audit("code_fix", str(db))

    assert result["ok"] is False
    # Membership-only assertion per the recipe contract — the kickoff's
    # exact spec is ``"collapse" in flags``. ``metric_anchor`` may also
    # flag (its ``intact`` verdict is whatever the detector's own math
    # produces on the projected rows); the test deliberately does not
    # require either way.
    assert "collapse" in result["flags"]
    # The metric_anchor row projection populates every row's anchor
    # fields so ``n_with_anchor == 10`` (kickoff rule #1).
    assert result["evidence"]["metric_anchor"]["n_with_anchor"] == 10
    # The new collapse detector arm ran and reported the textbook halt.
    assert result["evidence"]["collapse"]["recommendation"] == "halt"
    assert result["evidence"]["collapse"]["divergence"] is True
    # The score rises and the anchor falls — the half-split means carry
    # the same signal the circuit breaker already trusts.
    assert result["evidence"]["collapse"]["score_rise"] > 0
    assert result["evidence"]["collapse"]["anchor_drop"] > 0


def test_audit_clean_10row_collapse_history_no_flags(db):
    """Healthy 10 rows: both score and anchor rise together.

    The collapse arm's half-split means give ``score_rise > 0`` but
    ``anchor_drop < 0`` (anchor rising → ``first_mean(anchor) -
    second_mean(anchor)`` is negative). ``divergence`` requires BOTH,
    so ``recommendation="none"`` and no flag.

    ``metric_anchor`` reads the constant ``[True, False]`` agreements
    → ``discrimination = 0.5`` for every row → above the vacuity
    floor → ``intact is True`` → no flag. ``hack_probe`` sees the
    visible score and the visible rise (anchor → core_pass) both
    increasing, so ``level_gap`` and ``stagnation`` see no divergence
    → ``hacking is False`` → no flag.
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "code_fix",
        [(round(0.40 + 0.06 * i, 6), round(0.40 + 0.06 * i, 6))
         for i in range(10)],
    )

    result = va.audit("code_fix", str(db))

    assert result["ok"] is True
    assert result["flags"] == []
    assert result["evidence"]["hack_probe"]["hacking"] is False
    # metric_anchor: full measurement set, clean anchor, non-vacuous
    # discrimination → intact is True (not None as before the fix).
    assert result["evidence"]["metric_anchor"]["n_with_anchor"] == 10
    assert result["evidence"]["metric_anchor"]["intact"] is True
    # collapse: anchor rising → anchor_drop < 0 → divergence False
    # → recommendation="none" (kickoff test #2).
    assert result["evidence"]["collapse"]["recommendation"] == "none"
    assert result["evidence"]["collapse"]["divergence"] is False


def test_audit_fewer_than_min_steps_no_flags(db):
    """Fewer than ``MIN_STEPS = 4`` rows → every detector stays silent.

    ``collapse_detector.detect`` returns ``recommendation="none"`` on
    ``n < 4`` (``collapse_detector.py:71-84``) without computing any
    statistics — the arm is fail-open per detector by construction.

    ``metric_anchor`` returns ``intact is None`` on
    ``anchor["n"] < MIN_GENERATIONS = 4`` (``metric_anchor.py:207``) —
    an unaudited metric must never read as intact, but the audit treats
    ``None`` as no flag.

    ``hack_probe`` returns ``hacking is False`` on
    ``n < MIN_GENERATIONS = 4`` — no family-wise p, no decision.
    """
    _seed_workflow(db)
    _seed_collapse_history(
        db, "code_fix",
        # 3 rows of the kickoff fixture's exact shape — score rising,
        # anchor falling. With fewer than MIN_STEPS the detectors all
        # abstain, so no flag fires despite the rising/falling signal.
        [(0.40, 0.95), (0.46, 0.87), (0.52, 0.79)],
    )

    result = va.audit("code_fix", str(db))

    assert result["ok"] is True
    assert result["flags"] == []
    assert result["evidence"]["hack_probe"]["hacking"] is False
    assert result["evidence"]["metric_anchor"]["intact"] is None
    # Collapse detector: insufficient history, no statistics computed.
    assert result["evidence"]["collapse"]["recommendation"] == "none"
    assert result["evidence"]["collapse"]["reason"].startswith("insufficient")