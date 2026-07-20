"""circuit_breaker — Python port of lib/circuit_breaker.sh.

Faithful port of ``mo_check_liveness_breaker``: the behavioral liveness gate
that halts a recipe run burning cost without producing progress. Mirrors the
embedded Python heredoc in lib/circuit_breaker.sh exactly — same SQL, same
signal logic, same state-transition rules, same JSON shape.

Co-existence model (strangler-fig): ``lib/circuit_breaker.sh`` stays
byte-identical. This port gives Python callers an in-process target and
gives ``tests/unit/test_circuit_breaker_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Env knobs (bash reads these at function entry; the Python port takes them as
explicit kwargs so callers/tests can pin them — the parity test passes the
same values it exports to the bash subprocess):
  MO_CB_ARTIFACT_WINDOW   → artifact_window   (default 3)
  MO_CB_VERDICT_WINDOW    → verdict_window    (default 3)
  MO_CB_COST_THRESHOLD    → cost_threshold    (default 1.00)
  MO_CB_POLICY            → policy            (default "majority"; or/and)
  MO_CB_COOLDOWN_S        → cooldown_s        (default 1800)
  MO_CB_DISABLE           → disable           (default False; "1" enables)

``MO_CB_DISABLE`` is honoured at function entry BEFORE any DB work — matches
the bash lines 109-112 escape-hatch contract. The kwarg and the env combine
as OR (either triggers the bypass).

Two JSON shapes:
  - Known run: full diagnostic dict with signals/policy/fired_count/etc.
  - Unknown run: simpler fail-open shape (run_id, state, verdict, rationale,
    reason="run_unknown_default_proceed") — bash printf line 110 is the
    reference. Set-equality is the only safe parity check.

MINI_ORK_DB resolution: bash uses ``${MINI_ORK_DB:?}`` (errors if unset).
The port raises ValueError when ``db`` is None and MINI_ORK_DB is unset —
it never silently reads a cwd-relative default.

Public surface:
    check_liveness_breaker(run_id, db=None,
                           artifact_window=3, verdict_window=3,
                           cost_threshold=1.00, policy="majority",
                           cooldown_s=1800, disable=False) -> tuple[dict, int]

    Returns (json_dict, rc). rc=1 only when state=OPEN (LIVENESS_TRIP);
    rc=0 for CLOSED/PROBE/unknown-run/disabled paths.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any


def _resolve_db(db: str | None) -> str:
    resolved = db or os.environ.get("MINI_ORK_DB")
    if not resolved:
        raise ValueError("MINI_ORK_DB unset")
    return resolved


def _ensure_state_table(db: str) -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS — matches bash _cb_ensure_state_table."""
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            scope_key     TEXT PRIMARY KEY,
            state         TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at     INTEGER,
            last_run_id   TEXT,
            last_reason   TEXT,
            trip_count    INTEGER NOT NULL DEFAULT 0,
            updated_at    INTEGER NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def _eval_artifact_signal(con: sqlite3.Connection, tr: sqlite3.Row,
                          artifact_window: int) -> tuple[bool, int, str]:
    """Signal 1: artifact_hash invariance across last N runs in same scope."""
    recent = con.execute(
        """
        SELECT id, artifact_hash FROM task_runs
        WHERE task_class=? AND COALESCE(recipe,'none')=COALESCE(?, 'none')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (tr["task_class"], tr["recipe"], artifact_window),
    ).fetchall()

    if len(recent) < artifact_window:
        return False, 0, "insufficient history to evaluate"

    hashes = [r["artifact_hash"] for r in recent]
    if len(set(hashes)) == 1:
        sample = hashes[0] if hashes[0] is not None else "<null>"
        rationale = (
            f"artifact_hash unchanged across last {artifact_window} runs "
            f"in scope (hash={sample[:12] if sample != '<null>' else sample})"
        )
        return True, artifact_window, rationale

    rationale = (
        f"artifact_hash varied across last {artifact_window} runs — "
        f"forward progress detected"
    )
    return False, 1, rationale


def _eval_verdict_signal(con: sqlite3.Connection, run_id: str,
                         verdict_window: int) -> tuple[bool, int, str, Any]:
    """Signal 2: reviewer verdict stuck (last M identical non-APPROVE)."""
    traces = con.execute(
        """
        SELECT trace_id, reviewer_verdict, files_written, cost_usd
        FROM execution_traces
        WHERE trace_id LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (f"%{run_id}%", verdict_window),
    ).fetchall()

    if len(traces) < verdict_window:
        return False, 0, "insufficient trace history", None

    verdicts = [t["reviewer_verdict"] for t in traces if t["reviewer_verdict"]]
    if len(verdicts) < verdict_window:
        return False, 0, "insufficient trace history", None

    v0 = verdicts[0]
    if v0 and v0 != "APPROVE" and all(v == v0 for v in verdicts[:verdict_window]):
        rationale = (
            f"last {verdict_window} reviewer verdicts all '{v0}' — "
            f"reviewer rejecting the same patch repeatedly"
        )
        return True, verdict_window, rationale, v0

    rationale = (
        f"reviewer verdicts vary or APPROVE present in last "
        f"{verdict_window} traces — not stuck"
    )
    return False, 1, rationale, None


def _eval_cost_signal(con: sqlite3.Connection, run_id: str,
                      cost_threshold: float) -> tuple[bool, float, int, str]:
    """Signal 3: cost_burn_no_write (STRICT > threshold AND zero unique files)."""
    all_traces = con.execute(
        """
        SELECT cost_usd, files_written FROM execution_traces
        WHERE trace_id LIKE ?
        """,
        (f"%{run_id}%",),
    ).fetchall()

    total_cost = sum(float(t["cost_usd"] or 0) for t in all_traces)
    unique_files: set[str] = set()
    for t in all_traces:
        try:
            fw = json.loads(t["files_written"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for entry in fw:
            if isinstance(entry, dict):
                p = entry.get("path")
                if p:
                    unique_files.add(p)
            elif isinstance(entry, str):
                unique_files.add(entry)

    fired = (total_cost > cost_threshold and len(unique_files) == 0)
    if fired:
        rationale = (
            f"cost_usd=${total_cost:.4f} > threshold=${cost_threshold:.2f} with zero "
            f"unique files written — burning spend without producing artifacts"
        )
    else:
        rationale = (
            f"cost_usd=${total_cost:.4f}, unique_files_written={len(unique_files)} — "
            f"productive spend"
        )
    return fired, total_cost, len(unique_files), rationale


def check_liveness_breaker(
    run_id: str,
    db: str | None = None,
    artifact_window: int = 3,
    verdict_window: int = 3,
    cost_threshold: float = 1.00,
    policy: str = "majority",
    cooldown_s: int = 1800,
    disable: bool = False,
) -> tuple[dict, int]:
    """Port of ``mo_check_liveness_breaker``. Returns (json_dict, rc).

    rc=1 only when state transitions to OPEN (LIVENESS_TRIP). All other
    outcomes (CLOSED, HALF_OPEN-PROBE, unknown-run, disabled) return rc=0
    — matches the bash function's exit-code contract."""
    # Honour escape hatch BEFORE touching DB — matches bash lines 109-112.
    if disable or os.environ.get("MO_CB_DISABLE") == "1":
        return {
            "run_id": run_id,
            "state": "CLOSED",
            "verdict": "PROCEED",
            "rationale": "MO_CB_DISABLE=1 — gate bypassed",
        }, 0

    db = _resolve_db(db)
    artifact_window = int(artifact_window)
    verdict_window = int(verdict_window)
    cost_threshold = float(cost_threshold)
    policy = str(policy).lower()
    cooldown_s = int(cooldown_s)

    _ensure_state_table(db)

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    tr = con.execute(
        "SELECT id, task_class, recipe, artifact_hash, cost_usd "
        "FROM task_runs WHERE id=?",
        (run_id,),
    ).fetchone()

    if tr is None:
        # Unknown run — fail-open with the SIMPLER JSON shape (no signals/
        # policy/fired_count/cooldown_remaining_s/rationale/remediation).
        # Matches bash printf line 110 exactly.
        con.close()
        return {
            "run_id": run_id,
            "state": "CLOSED",
            "verdict": "PROCEED",
            "rationale": "run_id not found in task_runs — fail-open (caller may not have written task_runs row yet)",
            "reason": "run_unknown_default_proceed",
        }, 0

    scope_key = f"{tr['task_class']}::{tr['recipe'] or 'none'}"

    st = con.execute(
        "SELECT * FROM circuit_breaker_state WHERE scope_key=?",
        (scope_key,),
    ).fetchone()
    now = int(time.time())
    prev_state = st["state"] if st else "CLOSED"
    opened_at = st["opened_at"] if st else None
    trip_count = st["trip_count"] if st else 0

    cooldown_remaining = 0
    if prev_state == "OPEN" and opened_at is not None:
        elapsed = now - opened_at
        if elapsed >= cooldown_s:
            prev_state = "HALF_OPEN"  # allow one probe
        else:
            cooldown_remaining = cooldown_s - elapsed

    art_fired, art_consecutive, art_rationale = _eval_artifact_signal(
        con, tr, artifact_window
    )
    vd_fired, vd_consecutive, vd_rationale, vd_stuck = _eval_verdict_signal(
        con, run_id, verdict_window
    )
    cost_fired, total_cost, n_unique, cost_rationale = _eval_cost_signal(
        con, run_id, cost_threshold
    )

    signals_fired = [art_fired, vd_fired, cost_fired]
    fired_count = sum(signals_fired)
    signal_count = len(signals_fired)

    if policy == "or":
        trip = fired_count >= 1
    elif policy == "and":
        trip = fired_count == signal_count
    else:  # majority (default)
        trip = fired_count > signal_count // 2

    # State transition.
    if prev_state == "HALF_OPEN":
        new_state = "OPEN" if trip else "CLOSED"
        if new_state == "OPEN":
            opened_at = now
            trip_count += 1
    elif trip:
        new_state = "OPEN"
        if prev_state != "OPEN":
            opened_at = now
            trip_count += 1
    else:
        new_state = "CLOSED"
        opened_at = None

    # Verdict mapping.
    if new_state == "OPEN":
        verdict = "LIVENESS_TRIP"
        rc = 1
    elif new_state == "HALF_OPEN":
        verdict = "PROBE"
        rc = 0
    else:
        verdict = "PROCEED"
        rc = 0

    fired_names = [
        n for n, f in zip(
            ["artifact_invariant", "verdict_stuck", "cost_burn_no_write"],
            signals_fired,
        ) if f
    ]

    if verdict == "LIVENESS_TRIP":
        top_rationale = (
            f"{fired_count}/{signal_count} stagnation signals fired under "
            f"policy={policy}: {', '.join(fired_names)} — halting"
        )
    elif verdict == "PROBE":
        top_rationale = (
            f"cooldown elapsed (>= {cooldown_s}s) — allowing one probe "
            f"iteration before deciding final state"
        )
    else:
        top_rationale = (
            f"{fired_count}/{signal_count} signals fired under policy={policy} "
            f"— below trip threshold, proceeding"
        )

    con.execute(
        """
        INSERT INTO circuit_breaker_state
            (scope_key, state, opened_at, last_run_id, last_reason, trip_count, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(scope_key) DO UPDATE SET
            state=excluded.state,
            opened_at=excluded.opened_at,
            last_run_id=excluded.last_run_id,
            last_reason=excluded.last_reason,
            trip_count=excluded.trip_count,
            updated_at=excluded.updated_at
        """,
        (
            scope_key, new_state, opened_at, run_id,
            ",".join(fired_names) if fired_names else None,
            trip_count, now,
        ),
    )
    con.commit()
    con.close()

    out = {
        "run_id": run_id,
        "scope_key": scope_key,
        "state": new_state,
        "previous_state": prev_state,
        "verdict": verdict,
        "trip_count": trip_count,
        "signals": {
            "artifact_invariant": {
                "fired": art_fired,
                "rationale": art_rationale,
                "consecutive": art_consecutive,
                "threshold": artifact_window,
            },
            "verdict_stuck": {
                "fired": vd_fired,
                "rationale": vd_rationale,
                "consecutive": vd_consecutive,
                "threshold": verdict_window,
                "stuck_verdict": vd_stuck,
            },
            "cost_burn_no_write": {
                "fired": cost_fired,
                "rationale": cost_rationale,
                "cost_usd": round(total_cost, 4),
                "unique_files_written": n_unique,
                "cost_threshold": cost_threshold,
            },
        },
        "policy": policy,
        "fired_count": fired_count,
        "signal_count": signal_count,
        "cooldown_remaining_s": cooldown_remaining,
        "rationale": top_rationale,
        "remediation": (
            "1) inspect last N task_runs in scope to confirm stagnation, "
            "2) set MO_CB_DISABLE=1 to bypass for one cycle, OR "
            "3) widen thresholds (MO_CB_ARTIFACT_WINDOW / MO_CB_VERDICT_WINDOW / "
            "MO_CB_COST_THRESHOLD), OR 4) wait for cooldown "
            f"({cooldown_s}s) to elapse for a PROBE retry"
        ) if verdict == "LIVENESS_TRIP" else None,
    }
    return out, rc