"""adaptive_stability — Python port of lib/adaptive_stability.sh.

Faithful port of ``mo_check_panel_stability`` — the early-termination
signal for multi-round panel debate (Hu et al 2025, arxiv:2510.12697).
The bash script is the authoritative source; this module gives Python
callers an in-process target and gives
``tests/unit/test_adaptive_stability_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): ``lib/adaptive_stability.sh`` stays
byte-identical. This port mirrors the embedded Python heredoc exactly —
same query, same round-bucketing regex, same fail-open reasons, same
two-site ``round(.., 4)`` rounding, same decision order and rationale
strings. Parity is enforced by the sibling test (>=6 live-subprocess
cases; floats within 1e-6, strings/bools equal).

Contract map (bash heredoc → Python):
  SELECT ... WHERE trace_id LIKE %pid% OR tr-%-pid%   → _fetch_rows
  re.search(r"-r(\\d+)-", trace_id)                    → round bucketing
  verdict = (reviewer_verdict or verifier_output or "").strip().lower()
                                                        → per-row verdict
  n_rounds == 0 → fail-open CONTINUE, drift=1.0        → two reason codes:
      unrouted>0 → round_unencoded_default_continue
      else       → no_traces_default_continue
      (this branch OMITS the drift_history key — matches bash)
  drift_per_round[i] = changed/len(common), disjoint→1.0
      changed compares verdict[:50] (truncated)
  last_drift = drift_per_round[-1] if any else 1.0
  score_variance = statistics.pvariance(dpr) if len>1 else 0.0
  decision order: max_rounds → min_rounds → threshold
  round(score_variance,4), round(last_drift,4),
  drift_history=[round(d,4) for d in drift_per_round]

Env knobs (bash reads these at function entry; the Python port takes
them as explicit parameters so callers/tests can pin them — the parity
test passes the same values it exports to the bash subprocess):
  MO_PANEL_STABILITY_THRESHOLD → threshold  (default 0.10)
  MO_PANEL_MIN_ROUNDS          → min_rounds (default 2)
  MO_PANEL_MAX_ROUNDS          → max_rounds (default 5)

MINI_ORK_DB resolution: bash uses ``${MINI_ORK_DB:?}`` (errors if unset).
The port raises ValueError when ``db`` is None and MINI_ORK_DB is unset
— it never silently reads a cwd-relative default.

Public surface:
    check_panel_stability(panel_run_id, current_round, db=None,
                          threshold=0.10, min_rounds=2, max_rounds=5) -> dict
"""
from __future__ import annotations

import os
import re
import sqlite3
import statistics
from collections import defaultdict

_ROUND_RE = re.compile(r"-r(\d+)-")


def _resolve_db(db: str | None) -> str:
    resolved = db or os.environ.get("MINI_ORK_DB")
    if not resolved:
        raise ValueError("MINI_ORK_DB unset")
    return resolved


def _fetch_rows(db: str, panel_run_id: str) -> list[sqlite3.Row]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT trace_id, agent_version_id, reviewer_verdict, verifier_output
        FROM execution_traces
        WHERE trace_id LIKE ? OR trace_id LIKE ?
        """,
        (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%"),
    ).fetchall()
    con.close()
    return rows


def check_panel_stability(
    panel_run_id: str,
    current_round: int,
    db: str | None = None,
    threshold: float = 0.10,
    min_rounds: int = 2,
    max_rounds: int = 5,
) -> dict:
    """Port of ``mo_check_panel_stability``. Returns the same JSON dict
    the bash heredoc prints (decision aid — no non-zero rc concept)."""
    db = _resolve_db(db)
    current_round = int(current_round)
    threshold = float(threshold)
    min_rounds = int(min_rounds)
    max_rounds = int(max_rounds)

    rows = _fetch_rows(db, panel_run_id)

    # Bucket traces by round. Round is encoded as `r<N>` somewhere in the
    # trace_id (e.g. tr-glm-r1-run-abc-123 or tr-r2-...).
    round_buckets: dict[int, list[dict]] = defaultdict(list)
    unrouted = 0
    for r in rows:
        m = _ROUND_RE.search(r["trace_id"] or "")
        if not m:
            unrouted += 1
            continue
        rn = int(m.group(1))
        verdict = (r["reviewer_verdict"] or r["verifier_output"] or "").strip().lower()
        round_buckets[rn].append(
            {"agent": r["agent_version_id"] or "unknown", "verdict": verdict}
        )

    rounds_seen = sorted(round_buckets.keys())
    n_rounds = len(rounds_seen)

    # If no rounds encoded — fail-open, recommend CONTINUE.
    if n_rounds == 0:
        if unrouted > 0:
            rationale = (
                "trace_ids do not encode round number "
                "(-r<N>- segment); cannot compute drift"
            )
            reason = "round_unencoded_default_continue"
        else:
            rationale = "no execution_traces found for panel_run_id"
            reason = "no_traces_default_continue"
        return {
            "panel_run_id": panel_run_id,
            "current_round": current_round,
            "rounds_seen": 0,
            "score_variance": 0.0,
            "verdict_drift": 1.0,
            "stability_threshold": threshold,
            "min_rounds": min_rounds,
            "max_rounds": max_rounds,
            "recommendation": "CONTINUE",
            "stable": False,
            "reason": reason,
            "rationale": rationale,
        }

    # Compute round-over-round verdict drift. For each agent that appears
    # in BOTH round N-1 and round N, count whether its verdict changed.
    # Drift = (changed_agents / total_agents_in_both_rounds).
    drift_per_round: list[float] = []
    if len(rounds_seen) >= 2:
        for i in range(1, len(rounds_seen)):
            prev_r, cur_r = rounds_seen[i - 1], rounds_seen[i]
            prev_map = {t["agent"]: t["verdict"] for t in round_buckets[prev_r]}
            cur_map = {t["agent"]: t["verdict"] for t in round_buckets[cur_r]}
            common = set(prev_map) & set(cur_map)
            if not common:
                drift_per_round.append(1.0)  # disjoint panels → max drift
                continue
            changed = sum(
                1 for a in common if (prev_map[a][:50] != cur_map[a][:50])
            )
            drift_per_round.append(changed / len(common))

    # Last-round drift is the signal. Multi-round drift average gives the
    # variance proxy.
    last_drift = drift_per_round[-1] if drift_per_round else 1.0
    score_variance = (
        statistics.pvariance(drift_per_round) if len(drift_per_round) > 1 else 0.0
    )

    # Decision logic:
    #   - Honor MAX_ROUNDS unconditionally (compute ceiling).
    #   - Refuse HALT before MIN_ROUNDS (statistical floor).
    #   - Otherwise HALT when last_drift < threshold.
    if current_round >= max_rounds:
        rec, stable = "HALT", True
        reason = "max_rounds_reached"
        rationale = (
            f"current_round={current_round} >= max_rounds="
            f"{max_rounds} — unconditional halt"
        )
    elif current_round < min_rounds:
        rec, stable = "CONTINUE", False
        reason = "below_min_rounds"
        rationale = (
            f"current_round={current_round} < min_rounds="
            f"{min_rounds} — never halt before statistical floor"
        )
    elif last_drift < threshold:
        rec, stable = "HALT", True
        reason = "drift_below_threshold"
        rationale = (
            f"verdict_drift={last_drift:.3f} < threshold="
            f"{threshold:.3f} — panel has stabilized"
        )
    else:
        rec, stable = "CONTINUE", False
        reason = "drift_above_threshold"
        rationale = (
            f"verdict_drift={last_drift:.3f} >= threshold="
            f"{threshold:.3f} — panel still moving"
        )

    return {
        "panel_run_id": panel_run_id,
        "current_round": current_round,
        "rounds_seen": n_rounds,
        "score_variance": round(score_variance, 4),
        "verdict_drift": round(last_drift, 4),
        "stability_threshold": threshold,
        "min_rounds": min_rounds,
        "max_rounds": max_rounds,
        "recommendation": rec,
        "stable": stable,
        "reason": reason,
        "rationale": rationale,
        "drift_history": [round(d, 4) for d in drift_per_round],
    }
