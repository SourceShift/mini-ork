"""Cross-detector trustworthiness audit (G03-T08, I2).

Three shipped measurement modules each detect a way the grading signal lies:

  * ``mini_ork.learning.hack_probe``    — H2 reward-hacking monitor
                                          (arXiv 2609.04665)
  * ``mini_ork.learning.metric_anchor`` — H5 anchor-discipline audit
                                          (arXiv 2607.12790)
  * ``mini_ork.gates.gate_fuzzer``      — G3 blind-spot / over-block fuzzing

This module is the *promotion precondition* that acts on them. It runs each
detector in-process (reusing their public functions; never reimplementing
the math) and folds the per-detector reports into a single verdict::

    {"ok": bool, "flags": [...], "evidence": {...}}

A detector with insufficient data contributes no flag — "fail-open per
detector" — and the reason is recorded in ``evidence``. The aggregator never
*promotes* on a missing check; it only downgrades a would-be promote to
``quarantined`` when at least one detector has measured a real defect. The
gate in ``mini_ork.gates.promotion_gate`` is the only consumer and enforces
that contract on its caller side.

History assembly is the open design question this module answers. None of
the three detectors touches storage; each accepts a caller-assembled
``Sequence[Mapping]``. The audit honours the same discipline: when a caller
passes a ``hack_history=`` / ``metric_history=`` argument, that history is
used verbatim; otherwise the audit probes the DB best-effort for a table
whose columns project onto the detector shape, and logs every absence. No
synthetic histories are ever fabricated — a missing row is missing, and a
detector that reads ``[]`` returns ``hacking=False`` / ``intact=None``
which contributes zero flags.

No DB writes, no lane, no network, no model. The module is hermetic by
default: a fresh DB with no detector-shaped tables yields ``{"ok": True,
"flags": [], "evidence": {...}}`` and the promotion gate behaves exactly as
if the audit had been disabled.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

__all__ = ["audit", "AUDIT_VERSION"]

AUDIT_VERSION = "1"

_log = logging.getLogger(__name__)


_HACK_CORE_N = 10
"""Denominator for projecting ``collapse_history.anchor`` (a fraction) onto
the detector's ``core_pass`` integer. Picked to match the magnitude of the
existing hack_probe test fixtures (``tests/unit/test_verifier_audit_py.py:127``)
so a fresh reader against ``collapse_history`` produces the same detector
verdicts as the historical kwargs-injection tests. ``round(anchor * 10)`` is
monotonic non-decreasing in ``anchor`` (the writer stores anchor ∈ [0, 1]),
so a falling anchor in ``collapse_history`` produces a falling ``core_rate``
in the projected history — exactly the signal ``stagnation`` and ``level_gap``
exist to detect.
"""


def _read_collapse_history(
    db_path: str, task_class: str | None,
) -> list[sqlite3.Row]:
    """Read raw ``collapse_history`` rows (migration 0060) ordered by ``step``.

    Filters by ``task_class`` when one is given; returns the global history
    otherwise (kickoff rule #3: fall back when the candidate has no class).
    Mirrors the production read at ``mini_ork/recovery/circuit_breaker.py:308``
    verbatim — same exception discipline (``sqlite3.OperationalError`` only,
    so a real bug isn't swallowed), same ``ORDER BY step``, same ``LIMIT 64``
    cap (preserves the existing reader contract). The LIMIT bounds the
    detector's working set; older rows are correctly ignored.

    ``LIMIT 64`` was chosen by the prior reader at
    ``verifier_audit.py:84,126`` and is retained for behavioural parity with
    the kwargs-injection tests at ``tests/unit/test_verifier_audit_py.py``.
    """
    if not db_path:
        return []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        _log.warning("verifier_audit: collapse_history DB open failed: %s", exc)
        return []
    try:
        if task_class is not None:
            rows = con.execute(
                "SELECT step, score, anchor "
                "FROM collapse_history WHERE task_class=? "
                "ORDER BY step LIMIT 64",
                (task_class,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT step, score, anchor "
                "FROM collapse_history ORDER BY step LIMIT 64",
            ).fetchall()
        return rows
    except sqlite3.OperationalError as exc:
        # Missing table on a pre-0060 DB. The migration marks this table
        # as a coherent absence, not an error (apply.py:769-771). Fail
        # open: an empty list makes every detector return its no-flag
        # branch and the audit stays ``ok=True``.
        _log.warning("verifier_audit: collapse_history table missing: %s", exc)
        return []
    except sqlite3.Error as exc:
        _log.warning("verifier_audit: collapse_history read failed: %s", exc)
        return []
    finally:
        con.close()


def _project_hack_row(row: sqlite3.Row) -> dict:
    """Project a ``collapse_history`` row onto the hack_probe row shape.

    Detector contract (hack_probe.py:49-62, 189-211):

      * ``visible``   → float, the optimized score
      * ``core_pass`` → int, the count the detector divides by ``core_n``
      * ``core_n``    → int ≥ 1 (otherwise ``core_rate`` returns ``None``)
      * ``confident`` → bool, gates the ``confidently_wrong`` test
      * ``gen``       → int, used as ``change_point`` index label

    Mapping (kickoff rule #2: monotonic, documented, no fabrication):

      * ``gen`` = ``step`` — collapse_history steps are 0-indexed per
        task_class; the detector uses ``gen`` as a series label, not a
        monotonic anchor, so direct mapping is safe.
      * ``visible`` = ``score`` — the writer's optimized probe solve
        fraction.
      * ``core_pass`` = ``round(anchor * _HACK_CORE_N)`` — the writer
        stores ``anchor`` as a fraction; the detector needs an integer
        count, so we round. The mapping is monotonic, which is the
        property hack_probe's stagnation/level_gap tests rely on.
      * ``confident`` = ``True`` — unconditional. ``_write_collapse_history_row``
        only writes a row after a fully-scored apply decision, so every
        row in this table represents a completed probe (apply.py:729-795).
        Treating collapse_history rows as confident is therefore semantically
        correct, not a fabrication — and it's required for the
        ``confidently_wrong`` test to fire on genuine reward-hacking signals.
    """
    return {
        "gen": row["step"],
        "visible": row["score"],
        "core_pass": int(round(float(row["anchor"]) * _HACK_CORE_N)),
        "core_n": _HACK_CORE_N,
        "confident": True,
    }


def _project_metric_row(row: sqlite3.Row) -> dict:
    """Project a ``collapse_history`` row onto the metric_anchor row shape.

    Structural fail-open contract (metric_anchor.py:19-25): "an unaudited
    metric must never read as an intact one". The detector needs per-probe
    ``scored`` / ``held_out`` / ``agreements`` lists; ``collapse_history``
    stores per-decision fractions and does NOT carry those lists. We pass
    the row through anyway so the detector's own fall-through path runs
    cleanly, but the absent lists mean:

      * ``n_with_anchor = 0`` (no row has both ``scored`` and ``held_out``)
      * ``intact = None`` (the ``anchor["n"] < MIN_GENERATIONS`` branch)
      * ``undecided = ["no_anchor_measured"]``

    → the metric_anchor arm contributes NO flag. This is the same
    fail-open the audit honours for empty history; the detector's
    anti-fabrication contract demands it. Synthesizing ``held_out`` from
    ``anchor`` would be a fabrication (per metric_anchor.py:19-25) and
    is explicitly forbidden here.
    """
    return {
        "gen": row["step"],
        "scored": None,
        "held_out": None,
        "agreements": [],
    }


def _read_hack_history(
    db_path: str, task_class: str | None = None,
) -> list[dict]:
    """Read hack-probe-shaped rows from ``collapse_history`` (migration 0060).

    Filters by ``task_class`` when one is given; unfiltered otherwise. The
    ``collapse_history`` table is the single source of truth for the apply
    loop's scored decisions (``apply.py:_write_collapse_history_row``); the
    prior pass's multi-table probe (benchmark_results / self_improve_runs)
    saw zero detector-shaped rows in production and rendered the audit
    inert. See kickoff ``auto/rsi-i2-verifier-audit-repair.md``.
    """
    return [
        _project_hack_row(r)
        for r in _read_collapse_history(db_path, task_class)
    ]


def _read_metric_history(
    db_path: str, task_class: str | None = None,
) -> list[dict]:
    """Read metric-anchor-shaped rows from ``collapse_history`` (migration 0060).

    Per the structural fail-open contract documented on ``_project_metric_row``,
    every row from ``collapse_history`` is projected to a row whose
    ``scored`` / ``held_out`` / ``agreements`` are absent, so the metric_anchor
    detector reports ``intact=None`` and contributes no flag. The arm is
    wired end-to-end so future schema additions to ``collapse_history``
    (e.g. per-probe ``held_out`` columns) can land without changing the
    audit's public surface.
    """
    return [
        _project_metric_row(r)
        for r in _read_collapse_history(db_path, task_class)
    ]


def _run_hack_probe(history: list[dict]) -> dict[str, Any]:
    """Run hack_probe.monitor, returning its report unchanged.

    The detector is the only authority on its own verdict — the audit
    reuses its public function rather than reimplementing the math.
    """
    from mini_ork.learning import hack_probe
    return hack_probe.monitor(history)


def _run_metric_anchor(history: list[dict]) -> dict[str, Any]:
    """Run metric_anchor.audit, returning its report unchanged."""
    from mini_ork.learning import metric_anchor
    return metric_anchor.audit(history)


def _run_gate_fuzzer(corpus_path: str, workdir: str) -> dict[str, Any]:
    """Run gate_fuzzer against the artifact_contract corpus on a tempdir.

    The shipped adapter (``artifact_contract_evaluator``) calls the real
    shipped ``artifact_contract`` gate, so the fuzzer measures the gate as
    it ships — never a copy. ``workdir`` is the tempdir the caller passed
    in (the audit never auto-creates one to keep the call hermetic).
    """
    from mini_ork.gates import gate_fuzzer
    cases = gate_fuzzer.load_corpus(corpus_path)
    evaluator = gate_fuzzer.artifact_contract_evaluator(workdir)
    return gate_fuzzer.fuzz_gate(evaluator, cases)


def audit(
    task_class: str | None,
    db_path: str,
    *,
    hack_history: list[dict] | None = None,
    metric_history: list[dict] | None = None,
    gate_corpus_path: str | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Run the three detectors and fold them into a verdict.

    Parameters
    ----------
    task_class:
        When supplied, the DB-probe readers restrict the
        ``collapse_history`` SELECT to rows whose ``task_class`` matches
        (kickoff ``auto/rsi-i2-verifier-audit-repair.md`` rule #3). When
        ``None``, the readers return the global history (fail-open). The
        audit's public surface has always taken this positional arg; the
        prior pass ignored it, so a per-class audit read as the global
        history and the detector saw rows for other classes mixed in.
    db_path:
        SQLite path whose tables are probed best-effort for detector-shaped
        history rows. When no such rows are found, the corresponding
        detector runs on an empty history and contributes no flag.
    hack_history, metric_history, gate_corpus_path, workdir:
        Explicit overrides. When supplied, the audit skips the DB probe
        and uses the value verbatim. ``gate_corpus_path`` requires
        ``workdir`` (the artifact_contract evaluator writes files there);
        passing one without the other contributes no gate_fuzzer flag.

    Returns
    -------
    dict
        ``{"ok": bool, "flags": list[str], "evidence": dict}``. ``ok`` is
        ``True`` iff ``flags`` is empty. ``evidence`` always carries a key
        for each detector that ran (or whose run failed) so the rationale
        of a quarantined promotion is diagnosable.

    Each detector runs independently: an exception in one is logged to its
    evidence key as ``{"error": str(exc)}`` and contributes no flag. The
    audit never propagates a detector exception to its caller; that would
    let a misconfigured gate-fuzzer arm break a promotion path.
    """
    flags: list[str] = []
    evidence: dict[str, Any] = {"task_class": task_class}

    # ── H2: hack_probe ────────────────────────────────────────────────
    try:
        history = (hack_history
                   if hack_history is not None
                   else _read_hack_history(db_path, task_class))
        report = _run_hack_probe(history)
        evidence["hack_probe"] = report
        if bool(report.get("hacking")):
            flags.append("hack_probe")
    except Exception as exc:  # noqa: BLE001 — fail-open per detector
        _log.warning("verifier_audit: hack_probe arm failed: %s", exc)
        evidence["hack_probe"] = {"error": str(exc)}

    # ── H5: metric_anchor ─────────────────────────────────────────────
    try:
        history = (metric_history
                   if metric_history is not None
                   else _read_metric_history(db_path, task_class))
        report = _run_metric_anchor(history)
        evidence["metric_anchor"] = report
        if report.get("intact") is False:
            flags.append("metric_anchor")
    except Exception as exc:  # noqa: BLE001 — fail-open per detector
        _log.warning("verifier_audit: metric_anchor arm failed: %s", exc)
        evidence["metric_anchor"] = {"error": str(exc)}

    # ── G3: gate_fuzzer ───────────────────────────────────────────────
    # Path B from the code-impact lens: the arm only fires when the caller
    # supplies BOTH a corpus and a workdir, so the audit stays hermetic by
    # default and lights up as soon as a promotion-gate corpus lands.
    if gate_corpus_path is not None and workdir is not None:
        try:
            fuzz = _run_gate_fuzzer(gate_corpus_path, workdir)
            evidence["gate_fuzzer"] = fuzz
            if int(fuzz.get("blind_spots", 0) or 0) > 0:
                flags.append("gate_fuzzer")
        except Exception as exc:  # noqa: BLE001 — fail-open per detector
            _log.warning("verifier_audit: gate_fuzzer arm failed: %s", exc)
            evidence["gate_fuzzer"] = {"error": str(exc)}
    else:
        evidence["gate_fuzzer"] = {"skipped": "no corpus/workdir supplied"}

    return {
        "ok": len(flags) == 0,
        "flags": flags,
        "evidence": evidence,
        "audit_version": AUDIT_VERSION,
    }