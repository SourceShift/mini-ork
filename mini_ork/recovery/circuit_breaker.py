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
  MO_CB_COLLAPSE          → collapse_history injection + opt-out
                              (default ON; "0" disables the collapse signal)

``MO_CB_DISABLE`` is honoured at function entry BEFORE any DB work — matches
the bash lines 109-112 escape-hatch contract. The kwarg and the env combine
as OR (either triggers the bypass).

``MO_CB_COLLAPSE`` follows the same fail-open discipline: a broken
``collapse_detector.detect`` (or insufficient history) MUST NOT trip the
breaker — liveness gates cannot become kill switches because the detector
itself is buggy. Default is ON; the opt-out is the literal string ``"0"``.
Read inside the port so callers (notably ``gates/native_gates.py``) do not
need to forward a new kwarg.

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
                           cooldown_s=1800, disable=False,
                           collapse_history=None) -> tuple[dict, int]

    Returns (json_dict, rc). rc=1 only when state=OPEN (LIVENESS_TRIP);
    rc=0 for CLOSED/PROBE/unknown-run/disabled paths.

    ``collapse_history`` is a TEST SEAM only. When ``None`` (production),
    the breaker reads rows from the ``collapse_history`` table for the
    run's ``task_class`` (kickoff auto/rsi-i4-collapse-halt-repair.md).
    Tests inject a synthetic ``[{"step", "score", "anchor", "directives"},
    ...]`` list to exercise the detector's trip / watch / no-fire paths
    without seeding the table.

    Collapse is an INDEPENDENT trip, not a vote: ``signals_fired`` is the
    original three stagnation signals (``artifact_invariant``,
    ``verdict_stuck``, ``cost_burn_no_write``) so ``fired_count`` /
    ``signal_count`` and all policies are unchanged. When the collapse
    signal fires the breaker trips with verdict LIVENESS_TRIP and rc=1
    regardless of the configured policy; the rationale and ``last_reason``
    surface ``collapse_halt`` so audit rows stay honest. The collapse
    decision is exposed via a top-level ``out["collapse"]`` sub-object
    (NOT nested under ``out["signals"]`` — a future reader folding it back
    into the vote is exactly the regression this fix prevents).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any, Sequence

# Module-level dedupe so a broken detector cannot spam stderr on every
# breaker evaluation. Reset on process restart.
_LAST_COLLAPSE_LOG: str | None = None


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


def _log_collapse_once(reason: str) -> None:
    """Dedupe-warn to stderr for a broken/empty collapse signal — once per process.

    A broken ``collapse_detector`` must not spam stderr on every breaker
    evaluation; dedupe on the reason string so a single broken detector
    state produces one log line per process lifetime. ``None``-reasons
    are filtered upstream (no log when the signal is disabled cleanly).
    """
    global _LAST_COLLAPSE_LOG
    if _LAST_COLLAPSE_LOG == reason:
        return
    _LAST_COLLAPSE_LOG = reason
    sys.stderr.write(f"[circuit_breaker] collapse signal: {reason}\n")


def _eval_collapse_signal(
    con: sqlite3.Connection,
    tr: sqlite3.Row | None,
    collapse_history: Sequence[dict] | None,
    enabled: bool = True,
) -> tuple[bool, int, str, dict]:
    """Signal 4: ``collapse_detector.detect`` recommends ``halt``.

    arXiv 2606.21090 documents the self-training collapse pattern: pass@1
    rises on the training objective while a frozen anchor set degrades.
    Watching the optimization score alone cannot see it; this signal wires
    the G7 ``collapse_detector`` into the breaker so the loop can stop
    itself before it burns another budget on a regression.

    Returns ``(fired, n, rationale, report)`` so the caller can surface the
    detector's own fields in the JSON output. Tuple shape mirrors the
    existing trio plus the report dict. ``fired`` maps
    ``recommendation == "halt"``; any other recommendation (incl.
    ``"watch"`` and ``"none"`` for the insufficient-history branch) yields
    ``fired=False``. Fail-open is load-bearing: a buggy detector must not
    halt healthy runs.

    Production read path: when ``collapse_history is None`` (no kwarg
    injected) and ``tr`` is a task_runs row, read rows from the
    ``collapse_history`` table filtered by ``task_class`` (kickoff
    auto/rsi-i4-collapse-halt-repair.md). A missing table is fail-open
    (logged once). An empty result set is a normal no-fire; we do NOT log
    on empty rows (only on the OperationalError that signals "table is
    absent"). The directives column stores an INTEGER count; coerce to
    ``[]`` because the detector's ``_directive_counts`` only reads the
    set-len.

    Test seam: when ``collapse_history`` is a Sequence, the DB is skipped
    entirely. This keeps the unit tests for ``_eval_collapse_signal`` free
    of DB seeding, while the integration tests (and production) cover the
    no-kwarg read path.

    ``enabled=False`` short-circuits the detector call: the audit
    rationale must surface ``MO_CB_COLLAPSE=0`` so an operator can tell
    the difference between "detector said none" and "detector was
    silenced".
    """
    if not enabled:
        n = len(collapse_history) if collapse_history else 0
        return False, n, (
            "collapse signal disabled via MO_CB_COLLAPSE=0 — not evaluated"
        ), {"recommendation": "none", "score_rise": None, "anchor_drop": None,
            "reason": "collapse signal disabled via MO_CB_COLLAPSE=0"}

    # Lazy import: collapse_detector has no DB / network / model deps, but
    # decoupling keeps circuit_breaker importable in slim test contexts
    # (parity harness mirrors the bash twin's minimal surface).
    try:
        from mini_ork.learning import collapse_detector
    except Exception as exc:  # ImportError / path surprises in slim harnesses
        _log_collapse_once(f"import failed: {type(exc).__name__}: {exc}")
        return False, 0, f"collapse_detector import unavailable: {exc}", {
            "recommendation": "none", "score_rise": None, "anchor_drop": None,
            "reason": f"import failed: {exc}",
        }

    if collapse_history is not None:
        history = list(collapse_history)
        collapse_table_missing = False
    elif tr is not None:
        # Production read path: SELECT rows for this task_class. Catch
        # sqlite3.OperationalError only (mirrors _reset_cb_state at
        # tests/unit/test_circuit_breaker_collapse.py:73-77) — a bare
        # sqlite3.Error would also swallow real bugs.
        try:
            rows = con.execute(
                "SELECT step, score, anchor, directives "
                "FROM collapse_history WHERE task_class=? ORDER BY step",
                (tr["task_class"],),
            ).fetchall()
            history = [
                {"step": r["step"], "score": r["score"],
                 "anchor": r["anchor"], "directives": []}
                for r in rows
            ]
        except sqlite3.OperationalError:
            _log_collapse_once("collapse_history table missing — fail-open")
            history = []
            collapse_table_missing = True
        else:
            collapse_table_missing = False
    else:
        # Unknown run AND no injected history: bail with empty list
        # (the detector's n<MIN_STEPS branch returns "none").
        history = []
        collapse_table_missing = False

    try:
        report = collapse_detector.detect(history)
    except (KeyError, TypeError, ValueError) as exc:
        _log_collapse_once(f"detect raised {type(exc).__name__}: {exc}")
        return False, len(history), (
            f"collapse_detector raised {type(exc).__name__}: {exc}"
        ), {"recommendation": "none", "score_rise": None, "anchor_drop": None,
            "reason": f"raised {type(exc).__name__}: {exc}"}

    n = int(report.get("n", len(history)))
    recommendation = report.get("recommendation", "none")
    fired = recommendation == "halt"
    reason = report.get("reason", "")
    if fired:
        rationale = (
            f"collapse_detector recommends halt: n={n} "
            f"score_rise={report.get('score_rise')!r} "
            f"anchor_drop={report.get('anchor_drop')!r} — {reason}"
        )
    else:
        rationale = (
            f"collapse_detector recommendation={recommendation!r}: "
            f"n={n} score_rise={report.get('score_rise')!r} "
            f"anchor_drop={report.get('anchor_drop')!r} — {reason}"
        )
    # Surface the "missing table" cause in the JSON rationale (the
    # stderr dedupe log fires once per process lifetime; the rationale
    # is per-call).
    if collapse_table_missing and not fired:
        rationale = f"{rationale} [collapse_history table missing — fail-open]"
    return fired, n, rationale, report


def check_liveness_breaker(
    run_id: str,
    db: str | None = None,
    artifact_window: int = 3,
    verdict_window: int = 3,
    cost_threshold: float = 1.00,
    policy: str = "majority",
    cooldown_s: int = 1800,
    disable: bool = False,
    collapse_history: Sequence[dict] | None = None,
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

    # Collapse-signal opt-out: same fail-open-for-liveness discipline as
    # the disable hatch above. A disabled collapse signal still touches
    # the DB (we need the cost / verdict / artifact evaluations to run),
    # but reports fired=False with a stable rationale so audit rows stay
    # honest about why the signal did not participate.
    collapse_enabled = os.environ.get("MO_CB_COLLAPSE", "1") != "0"

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
    collapse_fired, collapse_n, collapse_rationale, collapse_report = (
        _eval_collapse_signal(
            con, tr, collapse_history,
            enabled=collapse_enabled,
        )
    )

    # Collapse is an INDEPENDENT trip, not a vote. The original three
    # stagnation signals drive `signals_fired` (so fired_count /
    # signal_count and every policy semantics are unchanged on
    # origin/main). The collapse decision short-circuits AFTER the
    # policy gate so a trip-via-collapse cannot be hidden by a
    # permissive policy, AND a trip-via-policy is not masked by a
    # silent collapse. See kickoff auto/rsi-i4-collapse-halt-repair.md
    # rule #3.
    signals_fired = [art_fired, vd_fired, cost_fired]
    fired_count = sum(signals_fired)
    signal_count = len(signals_fired)

    if policy == "or":
        trip = fired_count >= 1
    elif policy == "and":
        trip = fired_count == signal_count
    else:  # majority (default)
        trip = fired_count > signal_count // 2

    independent_collapse_trip = False
    if collapse_fired and not trip:
        trip = True
        independent_collapse_trip = True

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
        if independent_collapse_trip:
            # Collapse-driven trips rewrite fired_names to the single
            # token so the audit row reads cleanly. fired_count from
            # the vote is 0 in this branch (the vote did not trip).
            top_rationale = (
                f"collapse_halt fired under policy={policy} "
                f"({fired_count}/{signal_count} stagnation signals fired) — halting"
            )
            last_reason_tokens = ["collapse_halt"]
        else:
            top_rationale = (
                f"{fired_count}/{signal_count} stagnation signals fired under "
                f"policy={policy}: {', '.join(fired_names)} — halting"
            )
            last_reason_tokens = list(fired_names)
    elif verdict == "PROBE":
        top_rationale = (
            f"cooldown elapsed (>= {cooldown_s}s) — allowing one probe "
            f"iteration before deciding final state"
        )
        last_reason_tokens = list(fired_names) if fired_names else []
    else:
        top_rationale = (
            f"{fired_count}/{signal_count} signals fired under policy={policy} "
            f"— below trip threshold, proceeding"
        )
        last_reason_tokens = list(fired_names) if fired_names else []

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
            ",".join(last_reason_tokens) if last_reason_tokens else None,
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
        # Collapse is NOT under `signals` — it is structurally
        # independent of the vote. Putting it here invites a future
        # reader to fold it back into fired_count, which is exactly the
        # regression this fix prevents (kickoff rule #3).
        "collapse": {
            "fired": collapse_fired,
            "independent_trip": independent_collapse_trip,
            "recommendation": collapse_report.get("recommendation", "none"),
            "reason": collapse_report.get("reason", ""),
            "score_rise": collapse_report.get("score_rise"),
            "anchor_drop": collapse_report.get("anchor_drop"),
            "history_rows": collapse_n,
            "enabled": collapse_enabled,
            "rationale": collapse_rationale,
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