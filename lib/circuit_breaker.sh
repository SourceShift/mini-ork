#!/usr/bin/env bash
# circuit_breaker.sh — Behavioral liveness gate. Halts a recipe run that is
# burning cost without producing progress.
#
# This is the **behavioral** complement to v0.2 Phase D's cost circuit
# breaker (MO_DAILY_BUDGET_USD). Cost-CB caps spend; this one catches the
# failure mode where spend is *under* the cap but the recipe is making
# zero forward progress — reviewer rejecting the same patch every cycle,
# implementer writing zero files, artifact_hash frozen, etc.
#
# Why this exists:
#   SAFETY.md forbids "silent self-mutation, hidden rollback, promotion
#   without measurable utility". A recipe that burns budget on a stuck
#   loop violates the third clause: it can't be improving anything if
#   nothing changes. The cost-CB fires only when the daily budget
#   exhausts, which is the wrong signal — by then the user has already
#   paid for the no-op cycle. This gate fires on the actual stagnation
#   signals BEFORE the budget runs out.
#
#   Mirrors Wave 1 Phase O ("Panel-failure detection") pattern:
#   three orthogonal detectors, each fail-open when it can't measure,
#   joined via a configurable decision policy (majority / OR / AND).
#   Sister gates: coalition_gate.sh (ρ + family-diversity),
#                 adaptive_stability.sh (round-over-round verdict drift).
#   This one: behavioral progress (artifact / verdict / writes).
#
#   The three signals are deliberately orthogonal to each other so a
#   single noisy signal cannot OPEN the breaker on its own under the
#   default majority policy.
#
# Public API:
#   mo_check_liveness_breaker <run_id>
#       → emits structured JSON on stdout:
#         { "run_id":              <str>,
#           "state":                "CLOSED" | "OPEN" | "HALF_OPEN",
#           "verdict":             "PROCEED" | "LIVENESS_TRIP" | "PROBE",
#           "signals": {
#             "artifact_invariant":   { "fired": <bool>, "rationale": <str>,
#                                       "consecutive": <int>,
#                                       "threshold":  <int> },
#             "verdict_stuck":        { "fired": <bool>, "rationale": <str>,
#                                       "consecutive": <int>,
#                                       "threshold":  <int>,
#                                       "stuck_verdict": <str|null> },
#             "cost_burn_no_write":   { "fired": <bool>, "rationale": <str>,
#                                       "cost_usd": <float>,
#                                       "unique_files_written": <int>,
#                                       "cost_threshold": <float> },
#           },
#           "policy":               "majority" | "or" | "and",
#           "fired_count":          <int>,
#           "signal_count":         <int>,
#           "cooldown_remaining_s": <int>,
#           "rationale":            "<one sentence>" }
#       rc=0 when state=CLOSED or HALF_OPEN (caller may proceed).
#       rc=1 when state=OPEN (LIVENESS_TRIP — caller MUST halt).
#
# Env knobs:
#   MO_CB_ARTIFACT_WINDOW   consecutive same-artifact_hash count that fires
#                           the invariant signal. Default 3.
#   MO_CB_VERDICT_WINDOW    consecutive identical non-APPROVE reviewer_verdict
#                           count that fires the stuck signal. Default 3.
#   MO_CB_COST_THRESHOLD    cost-burn ceiling in USD for the run when zero
#                           unique files have been written. Default 1.00.
#   MO_CB_POLICY            "majority" | "or" | "and". How signals combine.
#                           Default "majority" (2-of-3).
#   MO_CB_COOLDOWN_S        seconds to remain OPEN before allowing a single
#                           PROBE (HALF_OPEN) iteration. Default 1800 (30m).
#                           Matches ralph-claude-code's CB_COOLDOWN_MINUTES.
#   MO_CB_DISABLE           when set to 1, gate always emits PROCEED. Escape
#                           hatch — log-only mode.
#
# Requires: bash 4+, python3, sqlite3, jq.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_cb_ensure_state_table() {
  # Persists CB state across runs so HALF_OPEN cooldowns are honored after
  # process exits. One row per (task_class, recipe) pair.
  [ "${_MO_CB_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    CREATE TABLE IF NOT EXISTS circuit_breaker_state (
        scope_key     TEXT PRIMARY KEY,
        state         TEXT NOT NULL DEFAULT 'CLOSED',
        opened_at     INTEGER,
        last_run_id   TEXT,
        last_reason   TEXT,
        trip_count    INTEGER NOT NULL DEFAULT 0,
        updated_at    INTEGER NOT NULL
    )
""")
con.commit()
con.close()
PY
  _MO_CB_SCHEMA_INIT=1
  export _MO_CB_SCHEMA_INIT
}

mo_check_liveness_breaker() {
  local run_id="${1:?run_id required}"

  # Honor escape hatch.
  if [ "${MO_CB_DISABLE:-0}" = "1" ]; then
    printf '{"run_id":"%s","state":"CLOSED","verdict":"PROCEED","rationale":"MO_CB_DISABLE=1 — gate bypassed"}\n' "$run_id"
    return 0
  fi

  local artifact_window="${MO_CB_ARTIFACT_WINDOW:-3}"
  local verdict_window="${MO_CB_VERDICT_WINDOW:-3}"
  local cost_threshold="${MO_CB_COST_THRESHOLD:-1.00}"
  local policy="${MO_CB_POLICY:-majority}"
  local cooldown_s="${MO_CB_COOLDOWN_S:-1800}"

  _cb_ensure_state_table

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
      "$run_id" "$artifact_window" "$verdict_window" \
      "$cost_threshold" "$policy" "$cooldown_s" <<'PY'
import json, sqlite3, sys, time

(db, run_id, art_win_s, vd_win_s, cost_thr_s, policy, cooldown_s_s) = sys.argv[1:8]
artifact_window = int(art_win_s)
verdict_window  = int(vd_win_s)
cost_threshold  = float(cost_thr_s)
policy          = policy.lower()
cooldown_s      = int(cooldown_s_s)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# ── resolve scope_key (task_class + recipe) from task_runs ────────────────
tr = con.execute(
    "SELECT id, task_class, recipe, artifact_hash, cost_usd "
    "FROM task_runs WHERE id=?",
    (run_id,)
).fetchone()
if tr is None:
    # Unknown run — fail-open. Caller proceeds; no state recorded.
    print(json.dumps({
        "run_id": run_id,
        "state": "CLOSED",
        "verdict": "PROCEED",
        "rationale": "run_id not found in task_runs — fail-open (caller may not have written task_runs row yet)",
        "reason": "run_unknown_default_proceed",
    }))
    sys.exit(0)

scope_key = f"{tr['task_class']}::{tr['recipe'] or 'none'}"

# ── load persisted CB state for this scope ─────────────────────────────────
st = con.execute(
    "SELECT * FROM circuit_breaker_state WHERE scope_key=?",
    (scope_key,)
).fetchone()
now = int(time.time())
prev_state    = st["state"]     if st else "CLOSED"
opened_at     = st["opened_at"] if st else None
trip_count    = st["trip_count"] if st else 0

# Cooldown: if previously OPEN and cooldown elapsed → transition to HALF_OPEN.
cooldown_remaining = 0
if prev_state == "OPEN" and opened_at is not None:
    elapsed = now - opened_at
    if elapsed >= cooldown_s:
        prev_state = "HALF_OPEN"   # allow one probe
    else:
        cooldown_remaining = cooldown_s - elapsed

# ── signal 1: artifact_hash invariance ────────────────────────────────────
# Count the last N task_runs in the same scope and check whether artifact_hash
# was unchanged across them. NULL hashes count as "no artifact written" which
# is also a stagnation signal.
recent = con.execute("""
    SELECT id, artifact_hash
    FROM task_runs
    WHERE task_class=? AND COALESCE(recipe,'none')=COALESCE(?,'none')
    ORDER BY created_at DESC
    LIMIT ?
""", (tr["task_class"], tr["recipe"], artifact_window)).fetchall()

art_fired = False
art_consecutive = 0
art_rationale = "insufficient history to evaluate"
if len(recent) >= artifact_window:
    hashes = [r["artifact_hash"] for r in recent]
    # All-same (including all-None) → invariant. Fired iff len(set) == 1.
    if len(set(hashes)) == 1:
        art_fired = True
        art_consecutive = artifact_window
        sample = hashes[0] if hashes[0] is not None else "<null>"
        art_rationale = (
            f"artifact_hash unchanged across last {artifact_window} runs "
            f"in scope (hash={sample[:12] if sample != '<null>' else sample})"
        )
    else:
        art_consecutive = 1
        art_rationale = (
            f"artifact_hash varied across last {artifact_window} runs — "
            f"forward progress detected"
        )

# ── signal 2: verdict-stuck ────────────────────────────────────────────────
# Last M execution_traces.reviewer_verdict values for THIS run all identical
# and non-APPROVE. APPROVE means the reviewer is satisfied → not a stuck
# signal even if it repeats.
traces = con.execute("""
    SELECT trace_id, reviewer_verdict, files_written, cost_usd
    FROM execution_traces
    WHERE trace_id LIKE ?
    ORDER BY created_at DESC
    LIMIT ?
""", (f"%{run_id}%", verdict_window)).fetchall()

vd_fired = False
vd_consecutive = 0
vd_stuck = None
vd_rationale = "insufficient trace history"
if len(traces) >= verdict_window:
    verdicts = [t["reviewer_verdict"] for t in traces if t["reviewer_verdict"]]
    if len(verdicts) >= verdict_window:
        v0 = verdicts[0]
        if v0 and v0 != "APPROVE" and all(v == v0 for v in verdicts[:verdict_window]):
            vd_fired = True
            vd_consecutive = verdict_window
            vd_stuck = v0
            vd_rationale = (
                f"last {verdict_window} reviewer verdicts all '{v0}' — "
                f"reviewer rejecting the same patch repeatedly"
            )
        else:
            vd_consecutive = 1
            vd_rationale = (
                f"reviewer verdicts vary or APPROVE present in last "
                f"{verdict_window} traces — not stuck"
            )

# ── signal 3: cost-burn without write ──────────────────────────────────────
# Sum cost_usd across all traces for this run, count unique files_written.
# Fires when cost > threshold AND zero unique writes.
all_traces = con.execute("""
    SELECT cost_usd, files_written FROM execution_traces
    WHERE trace_id LIKE ?
""", (f"%{run_id}%",)).fetchall()

total_cost = sum(float(t["cost_usd"] or 0) for t in all_traces)
unique_files = set()
for t in all_traces:
    try:
        fw = json.loads(t["files_written"] or "[]")
        for entry in fw:
            if isinstance(entry, dict):
                p = entry.get("path")
                if p: unique_files.add(p)
            elif isinstance(entry, str):
                unique_files.add(entry)
    except (json.JSONDecodeError, TypeError):
        pass

cost_fired = (total_cost > cost_threshold and len(unique_files) == 0)
cost_rationale = (
    f"cost_usd=${total_cost:.4f} > threshold=${cost_threshold:.2f} with zero "
    f"unique files written — burning spend without producing artifacts"
    if cost_fired else
    f"cost_usd=${total_cost:.4f}, unique_files_written={len(unique_files)} — "
    f"productive spend"
)

# ── decision policy ────────────────────────────────────────────────────────
signals_fired = [art_fired, vd_fired, cost_fired]
fired_count   = sum(signals_fired)
signal_count  = len(signals_fired)

if policy == "or":
    trip = fired_count >= 1
elif policy == "and":
    trip = fired_count == signal_count
else:  # majority (default)
    trip = fired_count > signal_count // 2

# ── state transition ─────────────────────────────────────────────────────
if prev_state == "HALF_OPEN":
    # Probe iteration. If trip again → straight back to OPEN with bumped
    # trip_count. If clean → CLOSED.
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

# ── verdict mapping ────────────────────────────────────────────────────────
if new_state == "OPEN":
    verdict = "LIVENESS_TRIP"
    rc = 1
elif new_state == "HALF_OPEN":
    verdict = "PROBE"
    rc = 0
else:
    verdict = "PROCEED"
    rc = 0

# ── compose top-level rationale ────────────────────────────────────────────
fired_names = [n for n, f in zip(
    ["artifact_invariant", "verdict_stuck", "cost_burn_no_write"],
    signals_fired) if f]
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

# ── persist state ─────────────────────────────────────────────────────────
con.execute("""
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
""", (scope_key, new_state, opened_at, run_id,
      ",".join(fired_names) if fired_names else None,
      trip_count, now))
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
            "unique_files_written": len(unique_files),
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
print(json.dumps(out))
sys.exit(rc)
PY
}

# ──────────────────────────────────────────────────────────────────────────
# Self-test entry point. Mirrors Wave 1 primitive pattern: 4 inline fixtures
# that exercise the trip / no-trip / cooldown / unknown-run paths against a
# minimal stand-in state.db.
# ──────────────────────────────────────────────────────────────────────────
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT
  export MINI_ORK_DB="$_td/state.db"
  export MINI_ORK_ROOT

  # Minimal schema — only the columns the gate reads.
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,
  artifact_hash TEXT, cost_usd REAL, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id TEXT PRIMARY KEY, reviewer_verdict TEXT,
  files_written TEXT, cost_usd REAL,
  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
""")
con.commit()
con.close()
PY

  echo "── fixture A (all 3 signals fire — LIVENESS_TRIP under majority) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
now = int(time.time())
# 3 prior runs, all same artifact_hash + reviewer rejecting + cost > $1 + no writes.
for i, rid in enumerate(["run-old-1", "run-old-2", "run-stuck"]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", "deadbeef" * 4, 0.5, now - (3 - i)))
# 3 traces for the active run, all REQUEST_CHANGES, no files_written, $1.50 total.
for j in range(3):
    con.execute("INSERT INTO execution_traces (trace_id, reviewer_verdict, files_written, cost_usd) VALUES (?,?,?,?)",
                (f"tr-{j}-run-stuck", "REQUEST_CHANGES", "[]", 0.5))
con.commit()
con.close()
PY
  out_a=$(mo_check_liveness_breaker "run-stuck" || true)
  echo "$out_a" | jq -c '{state,verdict,fired_count,signals:(.signals|map_values(.fired))}'
  [ "$(echo "$out_a" | jq -r .verdict)" = "LIVENESS_TRIP" ] \
    && [ "$(echo "$out_a" | jq -r .state)" = "OPEN" ] \
    && [ "$(echo "$out_a" | jq -r .fired_count)" = "3" ] \
    || { echo "FIXTURE A FAILED (expected LIVENESS_TRIP / OPEN / 3 fired)" >&2; exit 1; }

  echo "── fixture B (artifact varies, productive run — PROCEED) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM task_runs")
con.execute("DELETE FROM execution_traces")
now = int(time.time())
for i, (rid, h) in enumerate([("run-old-1","aaaa"), ("run-old-2","bbbb"), ("run-prod","cccc")]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", h*4, 0.3, now - (3 - i)))
# Implementer wrote files, reviewer approved.
for j in range(3):
    con.execute("INSERT INTO execution_traces (trace_id, reviewer_verdict, files_written, cost_usd) VALUES (?,?,?,?)",
                (f"tr-{j}-run-prod", "APPROVE",
                 '[{"path":"src/foo.py","hash":"x"}]', 0.1))
con.commit()
con.close()
PY
  # Fresh scope to avoid sticky OPEN from fixture A.
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM circuit_breaker_state")
con.commit(); con.close()
PY
  out_b=$(mo_check_liveness_breaker "run-prod")
  echo "$out_b" | jq -c '{state,verdict,fired_count}'
  [ "$(echo "$out_b" | jq -r .verdict)" = "PROCEED" ] \
    && [ "$(echo "$out_b" | jq -r .state)" = "CLOSED" ] \
    && [ "$(echo "$out_b" | jq -r .fired_count)" = "0" ] \
    || { echo "FIXTURE B FAILED (expected PROCEED / CLOSED / 0 fired)" >&2; exit 1; }

  echo "── fixture C (cooldown elapsed → HALF_OPEN PROBE) ──"
  # Seed state as OPEN with opened_at well in the past, then re-check
  # against a productive run to confirm HALF_OPEN → CLOSED.
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM circuit_breaker_state")
con.execute("DELETE FROM task_runs")
con.execute("DELETE FROM execution_traces")
now = int(time.time())
# Insert OPEN state opened 2 hours ago — cooldown of 1800s long elapsed.
con.execute("INSERT INTO circuit_breaker_state (scope_key, state, opened_at, last_run_id, last_reason, trip_count, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("code_fix::code-fix", "OPEN", now - 7200, "run-old", "all_signals", 1, now - 7200))
# Active run with healthy signals (artifact varies, no traces yet).
for i, (rid, h) in enumerate([("run-x","1111"), ("run-y","2222"), ("run-probe","3333")]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", h*4, 0.2, now - (3-i)))
con.commit()
con.close()
PY
  out_c=$(mo_check_liveness_breaker "run-probe")
  echo "$out_c" | jq -c '{state,verdict,previous_state}'
  [ "$(echo "$out_c" | jq -r .verdict)" = "PROCEED" ] \
    && [ "$(echo "$out_c" | jq -r .previous_state)" = "HALF_OPEN" ] \
    || { echo "FIXTURE C FAILED (expected PROCEED after HALF_OPEN probe)" >&2; exit 1; }

  echo "── fixture D (unknown run_id → fail-open PROCEED) ──"
  out_d=$(mo_check_liveness_breaker "run-does-not-exist")
  echo "$out_d" | jq -c '{state,verdict,reason}'
  [ "$(echo "$out_d" | jq -r .verdict)" = "PROCEED" ] \
    && [ "$(echo "$out_d" | jq -r .reason)" = "run_unknown_default_proceed" ] \
    || { echo "FIXTURE D FAILED (expected fail-open PROCEED)" >&2; exit 1; }

  echo "all four self-test fixtures passed."
fi
