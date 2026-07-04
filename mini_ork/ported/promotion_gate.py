"""promotion_gate — Python port of ``lib/promotion_gate.sh``.

Faithful port of the three public bash functions in
``lib/promotion_gate.sh``:

  * ``promotion_evaluate``     — deterministic-oracle gate (code_fix /
                                  db_migration) that consults
                                  ``benchmark_results`` + ``version_registry``
                                  baseline and writes a row to
                                  ``promotion_records`` (migration 0011
                                  schema).
  * ``promotion_approve``      — resolves a ``pending_human_approval`` row
                                  by UPDATE-then-SELECT and returns a
                                  JSON payload with the post-update
                                  ``decided_at``.
  * ``mo_promote_synthesis_gate`` — selective-feedback conjunction gate for
                                  synthesis-class task classes
                                  (``research_synthesis``, ``refactor_audit``,
                                  ``blog-post``, ``ui-audit``,
                                  ``ops-runbook``). Three-condition
                                  conjunction (panel score, CW-POR, ≥1
                                  structural signal). Soft-dep on
                                  ``lib/cw_por.sh`` via subprocess
                                  shell-out so the gate never raises
                                  when W1-C is missing.

Co-existence model (strangler-fig): ``lib/promotion_gate.sh`` stays
byte-identical. This port gives Python callers an in-process target and
gives ``tests/unit/test_promotion_gate_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Public API mirrors the bash contract:

    promotion_evaluate(db_path, candidate_id, *,
                       require_human=None, mini_ork_root=None) -> dict
        Returns the decision JSON as a dict. Raises ``SystemExit`` when
        the candidate has no ``base_workflow_version_id`` row (rc=1 in
        bash). Floats are rounded to 6 decimals (round(_, 6)).

    promotion_approve(db_path, candidate_id, approver, rationale) -> dict
        Returns ``{candidate_id, decision, approver, approved_at}``.
        Raises ``SystemExit`` when no pending row was updated (rc=1 in
        bash).

    mo_promote_synthesis_gate(verdict_file, task_class, *,
                              mini_ork_root=None,
                              score_threshold=None,
                              cw_por_threshold=None,
                              min_citation_density=None,
                              min_finding_cardinality=None) -> (dict, int)
        Returns ``(json_dict, rc)`` where rc ∈ {0, 1, 2}. Deterministic
        classes (default ``code_fix db_migration``) early-return rc=0
        with reason ``deterministic_class``.

Env knobs (bash reads these at function entry; the Python port reads the
same env names with the same defaults at function entry — NO import-time
caching):

    MINI_ORK_REQUIRE_HUMAN_APPROVAL  → promotion_evaluate require_human (default "false")
    MO_PROMOTE_SCORE_THRESHOLD       → synthesis gate panel-score floor (default 80)
    MO_CW_POR_THRESHOLD              → CW-POR ceiling (default 0.3)
    MO_MIN_CITATION_DENSITY          → min citation density per lens (default 3)
    MO_MIN_FINDING_CARDINALITY       → min finding cardinality (default 5)
    MO_DETERMINISTIC_TASK_CLASSES    → space-separated bypass list
                                        (default "code_fix db_migration")
    MINI_ORK_ROOT                    → where to find ``lib/cw_por.sh`` for the
                                        soft subprocess dep
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from typing import Any

__all__ = [
    "promotion_evaluate",
    "promotion_approve",
    "mo_promote_synthesis_gate",
]


# ── DDL (mirrors lib/promotion_gate.sh::_promo_ensure_tables) ────────────────


def ensure_table(db_path: str) -> None:
    """Idempotent ``CREATE TABLE IF NOT EXISTS promotion_records``.

    Mirrors ``_promo_ensure_tables`` in lib/promotion_gate.sh. The
    CREATE-IF-NOT-EXISTS is a safe no-op against a DB seeded by
    migration 0011 (which uses the real schema); it only acts on a
    brand-new DB that hasn't yet been migrated. The bash function also
    uses a per-process flag (``_MO_PROMO_SCHEMA_INIT``) to skip the
    CREATE; ``CREATE IF NOT EXISTS`` is naturally idempotent so the
    Python port just runs the SQL every call.

    NB: the schema below is the LEGACY draft (record_id PK + evaluated_at
    + safety_violations) that bash's ``_promo_ensure_tables`` would have
    created on a pre-migration DB. The REAL schema is in migration 0011
    (promotion_id PK + decided_at TEXT + decided_by CHECK gate|human) and
    is what bash's INSERT into ``promotion_records`` actually targets.
    Per the kickoff's "no redefine the table" rule, the port runs this
    CREATE only as a fallback; the production flow relies on migration
    0011 having already created the real table.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS promotion_records (
                record_id               TEXT PRIMARY KEY,
                candidate_id            TEXT NOT NULL,
                kind                    TEXT NOT NULL DEFAULT 'workflow',
                decision                TEXT NOT NULL
                                            CHECK(decision IN (
                                                'promoted','quarantined','rejected',
                                                'pending_human_approval'
                                            )),
                rationale               TEXT,
                utility_before          REAL,
                utility_after           REAL,
                benchmark_run_id        TEXT,
                approver                TEXT,
                approval_rationale      TEXT,
                safety_violations       TEXT DEFAULT '[]',
                evaluated_at            INTEGER NOT NULL,
                approved_at             INTEGER
            );
        """)
        con.commit()
    finally:
        con.close()


# ── promotion_evaluate (deterministic-oracle gate) ───────────────────────────


def promotion_evaluate(
    db_path: str,
    candidate_id: str,
    *,
    require_human: str | None = None,
    mini_ork_root: str | None = None,  # noqa: ARG001 - parity with bash; unused
) -> dict[str, Any]:
    """Evaluate a candidate for promotion.

    Mirrors ``lib/promotion_gate.sh::promotion_evaluate`` exactly:

      * SUM(pass) / AVG(utility_score) / MIN(pass) / COUNT(*) over
        ``benchmark_results`` for the candidate_id
      * baseline ``utility_score`` from ``version_registry`` (default 0.0
        when missing) — guarded by a sqlite_master existence check
      * decision tree: ``require_human`` → pending_human_approval; not
        all_pass && total_tasks>0 → rejected; utility_delta≤0 → quarantined;
        else promoted
      * INSERTs into ``promotion_records`` (migration 0011 schema:
        ``promotion_id`` PK, ``from_version_id`` + ``to_version_id`` NOT
        NULL FKs to workflow_memory, ``decided_by``='gate')
      * exits rc=1 (raises ``SystemExit``) when ``workflow_candidates`` has
        no row for ``candidate_id`` (matching bash's silent exit-1 contract)

    Returns the decision JSON as a dict with floats rounded to 6
    decimals (``round(_, 6)``).

    ``mini_ork_root`` is accepted for parity with the bash surface but is
    unused — bash sources its sub-libraries lazily, the port does not
    need the root to reach the DB.
    """
    if not candidate_id:
        raise SystemExit("promotion_evaluate: candidate_id required")

    ensure_table(db_path)

    if require_human is None:
        require_human = os.environ.get("MINI_ORK_REQUIRE_HUMAN_APPROVAL", "false")

    # Connect — match bash: PRAGMA busy_timeout=5000 not strictly needed
    # for in-process reads, but keeps parity with the embedded heredocs
    # that do set it.
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # ── Fetch most recent benchmark run (mirrors bash lines 83-94) ──
        brun = con.execute("""
            SELECT candidate_id, passed, avg_utility_score, all_pass, total_tasks
            FROM (
                SELECT candidate_id,
                       SUM(pass) as passed,
                       AVG(utility_score) as avg_utility_score,
                       MIN(pass) as all_pass,
                       COUNT(*) as total_tasks
                FROM benchmark_results WHERE candidate_id=?
                GROUP BY candidate_id
            )
        """, (candidate_id,)).fetchone()

        # ── Baseline from version_registry (mirrors bash lines 97-105) ──
        vr_exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='version_registry'"
        ).fetchone()
        if vr_exists:
            baseline_row = con.execute("""
                SELECT utility_score FROM version_registry
                WHERE name=? AND status='stable'
                ORDER BY promoted_at DESC LIMIT 1
            """, (candidate_id,)).fetchone()
            utility_before = float(baseline_row[0]) if baseline_row else 0.0
        else:
            utility_before = 0.0

        utility_after = float(brun["avg_utility_score"]) if brun else 0.0
        all_pass = bool(brun["all_pass"]) if brun else False

        # ── safety_violations: bash swallows the try/except → []
        # (mirrors bash lines 109-119). DO NOT populate from gate_registry.
        safety_violations: list[Any] = []

        utility_delta = utility_after - utility_before

        # ── Decision logic (mirrors bash lines 123-140) ──
        if require_human.lower() == "true":
            decision = "pending_human_approval"
            rationale = "Human gate required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"
        elif (not all_pass) and brun and brun["total_tasks"] > 0:
            decision = "rejected"
            rationale = (
                f"Not all benchmark tasks passed "
                f"({brun['passed']}/{brun['total_tasks']})"
            )
        elif utility_delta <= 0 and brun:
            decision = "quarantined"
            rationale = (
                f"Utility did not improve: before={utility_before:.4f}, "
                f"after={utility_after:.4f}, delta={utility_delta:.4f}"
            )
        else:
            decision = "promoted"
            rationale = (
                f"Utility improved by {utility_delta:.4f} "
                f"({utility_before:.4f} → {utility_after:.4f}); "
                f"all benchmark tasks passed."
            )

        result = {
            "decision": decision,
            "rationale": rationale,
            "utility_before": round(utility_before, 6),
            "utility_after": round(utility_after, 6),
            "utility_delta": round(utility_delta, 6),
            "benchmark_run_id": candidate_id,
            "all_pass": all_pass,
            "safety_violations": safety_violations,
        }

        # ── INSERT into promotion_records (mirrors bash lines 152-183) ──
        record_id = f"pr-{uuid.uuid4().hex[:16]}"
        base_ver_row = con.execute(
            "SELECT base_workflow_version_id FROM workflow_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        base_ver = base_ver_row[0] if base_ver_row else None
        if not base_ver:
            print(
                f"promotion_evaluate: candidate {candidate_id} has no base_workflow_version_id",
                file=sys.stderr,
            )
            raise SystemExit(1)

        con.execute("""
            INSERT INTO promotion_records
                (promotion_id, candidate_id, from_version_id, to_version_id,
                 utility_before, utility_after, benchmark_run_id,
                 rationale, decision, decided_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            record_id, candidate_id, base_ver, base_ver,
            utility_before, utility_after, None,
            rationale, decision, "gate",
        ))
        con.commit()
    finally:
        con.close()

    return result


# ── promotion_approve (resolves pending_human_approval) ──────────────────────


def promotion_approve(
    db_path: str,
    candidate_id: str,
    approver: str,
    rationale: str,
) -> dict[str, Any]:
    """Approve a candidate that's in ``pending_human_approval`` state.

    Mirrors ``lib/promotion_gate.sh::promotion_approve`` exactly:

      * ``UPDATE promotion_records`` set decision='promoted',
        decided_by='human', rationale=?, decided_at=strftime(...)
        WHERE candidate_id=? AND decision='pending_human_approval'
      * capture rowcount; if 0 → print stderr + exit 1 (matches bash)
      * capture the post-update ``decided_at`` via a SELECT and return it
      * response JSON keeps the caller-facing keys ``approver`` +
        ``approved_at`` (backed by the DB's ``decided_by`` + ``decided_at``)

    The ``approver`` arg is preserved in the response payload; the DB
    ``decided_by`` column stores the literal string ``'human'`` (a CHECK
    constraint, not a free-form value).
    """
    if not candidate_id:
        raise SystemExit("promotion_approve: candidate_id required")
    if not approver:
        raise SystemExit("promotion_approve: approver required")
    if not rationale:
        raise SystemExit("promotion_approve: rationale required")

    ensure_table(db_path)

    con = sqlite3.connect(db_path)
    try:
        updated = con.execute("""
            UPDATE promotion_records
            SET decision='promoted',
                decided_by='human',
                rationale=?,
                decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE candidate_id=? AND decision='pending_human_approval'
        """, (rationale, candidate_id)).rowcount

        decided_at = None
        if updated:
            row = con.execute(
                "SELECT decided_at FROM promotion_records "
                "WHERE candidate_id=? AND decision='promoted' "
                "ORDER BY decided_at DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            decided_at = row[0] if row else None
        con.commit()
    finally:
        con.close()

    if updated == 0:
        print(
            f"promotion_approve: no pending approval found for {candidate_id}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return {
        "candidate_id": candidate_id,
        "decision": "promoted",
        "approver": approver,
        "approved_at": decided_at,
    }


# ── mo_promote_synthesis_gate (synthesis-class conjunction gate) ─────────────


def _cw_por_subprocess(
    verdict_file: str,
    mini_ork_root: str | None = None,
) -> tuple[str, str]:
    """Run lib/cw_por.sh::mo_compute_cw_por via subprocess shell-out.

    Returns ``(cw_por_value, cw_por_status)`` matching bash semantics:
      * ``('null', 'default_passed')`` — when lib/cw_por.sh missing
      * ``('null', 'default_passed')`` — when mo_compute_cw_por missing
      * ``(float_str | 'null', status_str)`` — when the function exists

    The bash implementation calls the bash function in-process via
    `source lib/cw_por.sh`. The Python port shells out so the parity
    test (which also calls bash) sees the exact same byte-stream.
    """
    if mini_ork_root is None:
        mini_ork_root = os.environ.get("MINI_ORK_ROOT", "")
    cw_por_sh = os.path.join(mini_ork_root, "lib", "cw_por.sh") if mini_ork_root else ""
    if not cw_por_sh or not os.path.exists(cw_por_sh):
        return ("null", "default_passed")
    try:
        # Probe the function exists by sourcing it in a dry-run subshell.
        probe = subprocess.run(
            ["bash", "-c",
             f'source "{cw_por_sh}" 2>/dev/null; '
             f'declare -f mo_compute_cw_por >/dev/null 2>&1 && echo "Y" || echo "N"'],
            capture_output=True, text=True, timeout=10,
        )
        if probe.stdout.strip() != "Y":
            return ("null", "default_passed")
        r = subprocess.run(
            ["bash", "-c",
             f'source "{cw_por_sh}" 2>/dev/null; mo_compute_cw_por "$1"',
             "_", verdict_file],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return ("null", "indeterminate_default_passed")
        try:
            payload = json.loads(r.stdout.strip())
        except json.JSONDecodeError:
            return ("null", "indeterminate_default_passed")
        cw_value = payload.get("cw_por")
        verdict = payload.get("verdict", "indeterminate")
        cw_value_s = "null" if cw_value is None else str(cw_value)
        if verdict == "panel_healthy":
            status = "passed"
        elif verdict == "authority_capture_suspected":
            status = "failed"
        else:
            status = "indeterminate_default_passed"
        return (cw_value_s, status)
    except Exception:
        return ("null", "indeterminate_default_passed")


def mo_promote_synthesis_gate(
    verdict_file: str,
    task_class: str,
    *,
    mini_ork_root: str | None = None,
    score_threshold: float | None = None,
    cw_por_threshold: float | None = None,
    min_citation_density: float | None = None,
    min_finding_cardinality: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Selective-feedback conjunction gate for synthesis-class task classes.

    Mirrors ``lib/promotion_gate.sh::mo_promote_synthesis_gate`` exactly.

    Exit-code contract:
        rc=0  all conditions met (or deterministic-class bypass)
        rc=1  any of {low_panel_score, authority_capture, no_structural_signal}
        rc=2  malformed input (file missing, json parse fail, missing
              .panel_score)

    Deterministic-class bypass: when ``task_class`` is in
    ``$MO_DETERMINISTIC_TASK_CLASSES`` (default ``code_fix db_migration``)
    the gate early-returns rc=0 with reason ``deterministic_class``.

    Env knobs (read at function entry — NO import-time caching):
        MO_PROMOTE_SCORE_THRESHOLD     default 80
        MO_CW_POR_THRESHOLD            default 0.3
        MO_MIN_CITATION_DENSITY        default 3
        MO_MIN_FINDING_CARDINALITY     default 5
        MO_DETERMINISTIC_TASK_CLASSES  default "code_fix db_migration"
    """
    if not verdict_file:
        return ({"error": "verdict_file required"}, 2)
    if not task_class:
        return ({"error": "task_class required"}, 2)

    # ── File-missing check (mirrors bash lines 308-311) ──
    if not os.path.isfile(verdict_file):
        print(
            json.dumps({"error": f"verdict file not found: {verdict_file}"}),
            file=sys.stderr,
        )
        return ({"error": f"verdict file not found: {verdict_file}"}, 2)

    # ── Deterministic-class bypass (mirrors bash lines 313-319) ──
    dcs = os.environ.get(
        "MO_DETERMINISTIC_TASK_CLASSES", "code_fix db_migration")
    if f" {task_class} " in f" {dcs} ":
        return (
            {
                "decision": "approved",
                "reason": "deterministic_class",
                "task_class": task_class,
                "note": "routes through promotion_evaluate single-pass verifier path",
            },
            0,
        )

    # ── Env-driven defaults (mirrors bash lines 321-324) ──
    if score_threshold is None:
        score_threshold = float(os.environ.get("MO_PROMOTE_SCORE_THRESHOLD", "80"))
    if cw_por_threshold is None:
        cw_por_threshold = float(os.environ.get("MO_CW_POR_THRESHOLD", "0.3"))
    if min_citation_density is None:
        min_citation_density = float(os.environ.get("MO_MIN_CITATION_DENSITY", "3"))
    if min_finding_cardinality is None:
        min_finding_cardinality = int(os.environ.get("MO_MIN_FINDING_CARDINALITY", "5"))

    # ── CW-POR soft-dep shell-out (mirrors bash lines 326-346) ──
    cw_value_s, cw_status = _cw_por_subprocess(verdict_file, mini_ork_root)

    # ── JSON parse (mirrors bash lines 361-366) ──
    try:
        with open(verdict_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return ({"error": f"json parse failed: {e}"}, 2)

    panel_score = data.get("panel_score")
    if panel_score is None:
        return ({"error": "verdict file missing required .panel_score"}, 2)
    panel_score = float(panel_score)

    structural = data.get("structural", {}) or {}
    cit_density = float(structural.get("citation_density_per_lens", 0))
    file_cov = int(structural.get("file_coverage_delta", 0))
    finding_card = int(structural.get("finding_cardinality", 0))

    cw_val: float | None
    if cw_value_s == "null":
        cw_val = None
    else:
        try:
            cw_val = float(cw_value_s)
        except ValueError:
            cw_val = None

    # ── Condition 1: panel score gate (mirrors bash lines 379-395) ──
    if panel_score < score_threshold:
        return (
            {
                "decision": "rejected",
                "reason": "low_panel_score",
                "task_class": task_class,
                "signals": {
                    "panel_score": panel_score,
                    "panel_score_threshold": score_threshold,
                    "cw_por": cw_val,
                    "cw_por_status": cw_status,
                    "structural": structural,
                },
                "rationale": (
                    f"panel_score={panel_score:.2f} below threshold "
                    f"{score_threshold:.2f}"
                ),
            },
            1,
        )

    # ── Condition 2: CW-POR gate (mirrors bash lines 397-415) ──
    if cw_status == "failed":
        return (
            {
                "decision": "rejected",
                "reason": "authority_capture",
                "task_class": task_class,
                "signals": {
                    "panel_score": panel_score,
                    "cw_por": cw_val,
                    "cw_por_status": cw_status,
                    "cw_por_threshold": cw_por_threshold,
                    "structural": structural,
                },
                "rationale": (
                    f"CW-POR={cw_val} exceeds threshold "
                    f"{cw_por_threshold:.2f}; authority capture suspected"
                ),
            },
            1,
        )

    # ── Condition 3: ≥1 structural signal (mirrors bash lines 417-444) ──
    signal_hits: list[str] = []
    if cit_density > min_citation_density:
        signal_hits.append(
            f"citation_density={cit_density:.2f} > {min_citation_density:.2f}"
        )
    if file_cov > 0:
        signal_hits.append(f"file_coverage_delta={file_cov} > 0")
    if finding_card > min_finding_cardinality:
        signal_hits.append(
            f"finding_cardinality={finding_card} > {min_finding_cardinality}"
        )

    if not signal_hits:
        return (
            {
                "decision": "rejected",
                "reason": "no_structural_signal",
                "task_class": task_class,
                "signals": {
                    "panel_score": panel_score,
                    "cw_por": cw_val,
                    "cw_por_status": cw_status,
                    "structural": structural,
                    "min_citation_density": min_citation_density,
                    "min_finding_cardinality": min_finding_cardinality,
                },
                "rationale": (
                    "no independent structural quality signal: "
                    f"citation_density={cit_density:.2f}, "
                    f"file_coverage_delta={file_cov}, "
                    f"finding_cardinality={finding_card}"
                ),
            },
            1,
        )

    # ── All three conditions pass (mirrors bash lines 446-463) ──
    return (
        {
            "decision": "approved",
            "reason": "all_conditions_met",
            "task_class": task_class,
            "signals": {
                "panel_score": panel_score,
                "panel_score_threshold": score_threshold,
                "cw_por": cw_val,
                "cw_por_status": cw_status,
                "cw_por_threshold": cw_por_threshold,
                "structural": structural,
                "structural_signals_met": signal_hits,
            },
            "rationale": (
                f"panel_score={panel_score:.2f} >= {score_threshold:.2f}, "
                f"cw_por={cw_status}, structural signals met: "
                + "; ".join(signal_hits)
            ),
        },
        0,
    )