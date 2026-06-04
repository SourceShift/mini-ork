#!/usr/bin/env bash
# adaptive_stability.sh — Adaptive Stability Detection for multi-round panel debate.
#
# Implements the early-termination signal from:
#   Hu et al 2025 — "Multi-Agent Debate with Adaptive Stability Detection"
#   — arxiv:2510.12697
#
# Why this exists:
#   Multi-round panel debate (each round emits a fresh batch of
#   reviewer_verdict rows in execution_traces) gets diminishing returns
#   after the panel stabilizes — additional rounds burn compute without
#   moving the verdict distribution. Static round caps (e.g. "always 3
#   rounds") either over-spend on already-stable panels or under-spend
#   on genuinely-contested ones. Adaptive stability detection uses the
#   round-over-round verdict drift to drive an early-stop signal:
#   recommend HALT when drift falls below threshold, CONTINUE while
#   the panel is still moving.
#
#   This is the panel-cost dual of the W1-B coalition gate: the latter
#   refuses to synthesize a coalition; this one refuses to KEEP
#   DEBATING a stabilized panel.
#
# Public API:
#   mo_check_panel_stability <panel_run_id> <current_round>
#       → emits structured JSON on stdout:
#         { "panel_run_id": <str>,
#           "current_round":   <int>,
#           "rounds_seen":     <int>,
#           "score_variance":  <float>,
#           "verdict_drift":   <float 0..1>,
#           "stability_threshold": <float>,
#           "recommendation": "HALT" | "CONTINUE",
#           "stable": <bool>,
#           "rationale": "<one sentence>" }
#       rc=0 always (this is a decision aid, not a gate — the caller
#         chooses whether to honor the HALT recommendation).
#
# Input contract — round numbers must be encoded in trace_id as
# `tr-<role>-r<N>-<panel_run_id>` so the function can bucket
# verdicts by round. If round encoding is absent the function returns
# recommendation=CONTINUE with rationale='round_unencoded_default_continue'
# (fail-open — never block debate on schema we don't understand).
#
# Env knobs:
#   MO_PANEL_STABILITY_THRESHOLD   verdict_drift below this → HALT.
#                                  Default 0.10 (10% of voters changed
#                                  position last round).
#   MO_PANEL_MIN_ROUNDS            never recommend HALT before this
#                                  round number. Default 2.
#   MO_PANEL_MAX_ROUNDS            recommend HALT at this round number
#                                  unconditionally. Default 5.
#
# Requires: bash 4+, python3 + sqlite3, jq.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

mo_check_panel_stability() {
  local panel_run_id="${1:?panel_run_id required}"
  local current_round="${2:?current_round required}"
  local threshold="${MO_PANEL_STABILITY_THRESHOLD:-0.10}"
  local min_rounds="${MO_PANEL_MIN_ROUNDS:-2}"
  local max_rounds="${MO_PANEL_MAX_ROUNDS:-5}"

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
      "$panel_run_id" "$current_round" "$threshold" "$min_rounds" "$max_rounds" <<'PY'
import json, re, sqlite3, statistics, sys
from collections import defaultdict

(db, panel_run_id, cur_r_s, thr_s, min_r_s, max_r_s) = sys.argv[1:7]
current_round = int(cur_r_s)
threshold     = float(thr_s)
min_rounds    = int(min_r_s)
max_rounds    = int(max_r_s)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT trace_id, agent_version_id, reviewer_verdict, verifier_output
    FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchall()
con.close()

# Bucket traces by round. Round is encoded as `r<N>` somewhere in the
# trace_id (e.g. tr-glm-r1-run-abc-123 or tr-r2-...).
round_buckets = defaultdict(list)
unrouted = 0
for r in rows:
    m = re.search(r"-r(\d+)-", r["trace_id"] or "")
    if not m:
        unrouted += 1
        continue
    rn = int(m.group(1))
    verdict = (r["reviewer_verdict"] or r["verifier_output"] or "").strip().lower()
    round_buckets[rn].append({
        "agent": r["agent_version_id"] or "unknown",
        "verdict": verdict,
    })

rounds_seen = sorted(round_buckets.keys())
n_rounds = len(rounds_seen)

# If no rounds encoded — fail-open, recommend CONTINUE.
if n_rounds == 0:
    if unrouted > 0:
        rationale = (f"trace_ids do not encode round number "
                     f"(-r<N>- segment); cannot compute drift")
        reason = "round_unencoded_default_continue"
    else:
        rationale = "no execution_traces found for panel_run_id"
        reason = "no_traces_default_continue"
    print(json.dumps({
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
    }))
    sys.exit(0)

# Compute round-over-round verdict drift. For each agent that appears
# in BOTH round N-1 and round N, count whether its verdict changed.
# Drift = (changed_agents / total_agents_in_both_rounds).
drift_per_round = []
if len(rounds_seen) >= 2:
    for i in range(1, len(rounds_seen)):
        prev_r, cur_r = rounds_seen[i-1], rounds_seen[i]
        prev_map = {t["agent"]: t["verdict"] for t in round_buckets[prev_r]}
        cur_map  = {t["agent"]: t["verdict"] for t in round_buckets[cur_r]}
        common = set(prev_map) & set(cur_map)
        if not common:
            drift_per_round.append(1.0)  # disjoint panels → max drift
            continue
        changed = sum(1 for a in common
                      if (prev_map[a][:50] != cur_map[a][:50]))
        drift_per_round.append(changed / len(common))

# Last-round drift is the signal. Multi-round drift average gives the
# variance proxy.
last_drift = drift_per_round[-1] if drift_per_round else 1.0
score_variance = (statistics.pvariance(drift_per_round)
                  if len(drift_per_round) > 1 else 0.0)

# Decision logic:
#   - Honor MAX_ROUNDS unconditionally (compute ceiling).
#   - Refuse HALT before MIN_ROUNDS (statistical floor).
#   - Otherwise HALT when last_drift < threshold.
if current_round >= max_rounds:
    rec, stable = "HALT", True
    reason = "max_rounds_reached"
    rationale = (f"current_round={current_round} >= max_rounds="
                 f"{max_rounds} — unconditional halt")
elif current_round < min_rounds:
    rec, stable = "CONTINUE", False
    reason = "below_min_rounds"
    rationale = (f"current_round={current_round} < min_rounds="
                 f"{min_rounds} — never halt before statistical floor")
elif last_drift < threshold:
    rec, stable = "HALT", True
    reason = "drift_below_threshold"
    rationale = (f"verdict_drift={last_drift:.3f} < threshold="
                 f"{threshold:.3f} — panel has stabilized")
else:
    rec, stable = "CONTINUE", False
    reason = "drift_above_threshold"
    rationale = (f"verdict_drift={last_drift:.3f} >= threshold="
                 f"{threshold:.3f} — panel still moving")

print(json.dumps({
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
}))
PY
}

# Self-test entry point.
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT
  export MINI_ORK_DB="$_td/state.db"

  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id           TEXT PRIMARY KEY,
  agent_version_id   TEXT,
  reviewer_verdict   TEXT,
  verifier_output    TEXT
);""")
con.commit()
con.close()
PY

  echo "── fixture A (3 rounds, panel stabilized — round 2 → 3 zero drift) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM execution_traces")
prun = "run-stable"
rows = []
# Round 1 — panel disagrees.
rows += [("tr-glm-r1-"+prun,  "glm",  "approve"),
         ("tr-kimi-r1-"+prun, "kimi", "reject"),
         ("tr-cdx-r1-"+prun,  "codex","reject")]
# Round 2 — kimi converges to approve.
rows += [("tr-glm-r2-"+prun,  "glm",  "approve"),
         ("tr-kimi-r2-"+prun, "kimi", "approve"),
         ("tr-cdx-r2-"+prun,  "codex","reject")]
# Round 3 — all agree (zero drift from round 2 → 3 for nobody who changed).
rows += [("tr-glm-r3-"+prun,  "glm",  "approve"),
         ("tr-kimi-r3-"+prun, "kimi", "approve"),
         ("tr-cdx-r3-"+prun,  "codex","reject")]
con.executemany("INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES (?,?,?)", rows)
con.commit()
con.close()
PY
  out_a=$(mo_check_panel_stability "run-stable" 3)
  echo "$out_a" | jq -c .
  [ "$(echo "$out_a" | jq -r .recommendation)" = "HALT" ] \
    || { echo "FIXTURE A FAILED (expected HALT)" >&2; exit 1; }

  echo "── fixture B (round 1 — below min_rounds → CONTINUE) ──"
  out_b=$(mo_check_panel_stability "run-stable" 1)
  echo "$out_b" | jq -c .
  [ "$(echo "$out_b" | jq -r .recommendation)" = "CONTINUE" ] \
    && [ "$(echo "$out_b" | jq -r .reason)" = "below_min_rounds" ] \
    || { echo "FIXTURE B FAILED" >&2; exit 1; }

  echo "── fixture C (max_rounds reached → unconditional HALT) ──"
  out_c=$(MO_PANEL_MAX_ROUNDS=3 mo_check_panel_stability "run-stable" 3)
  echo "$out_c" | jq -c .
  [ "$(echo "$out_c" | jq -r .recommendation)" = "HALT" ] \
    || { echo "FIXTURE C FAILED" >&2; exit 1; }

  echo "── fixture D (rounds not encoded → fail-open CONTINUE) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM execution_traces")
con.execute("INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES (?,?,?)",
            ("tr-glm-run-noround-123", "glm", "approve"))
con.commit()
con.close()
PY
  out_d=$(mo_check_panel_stability "run-noround" 3)
  echo "$out_d" | jq -c .
  [ "$(echo "$out_d" | jq -r .recommendation)" = "CONTINUE" ] \
    && [ "$(echo "$out_d" | jq -r .reason)" = "round_unencoded_default_continue" ] \
    || { echo "FIXTURE D FAILED" >&2; exit 1; }

  echo "all four self-test fixtures passed."
fi
