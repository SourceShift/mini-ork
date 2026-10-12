#!/usr/bin/env python3
# verifiers/engagement-check.py — pure-python engagement proof for the
# orch-smoke-default-on recipe.
#
# Asserts:
#   1. MO_RUN_ORCHESTRATOR is ABSENT from os.environ. A present value
#      (especially =1) does not prove the default — runner_orchestrator.py
#      `enabled()` is ON for unset/empty/unrecognized, OFF only for
#      explicit falsy. Engagement is meaningful only with the variable
#      UNSET.
#   2. Exactly one row exists in run_events for (run_id,
#      event_type LIKE 'orchestrator.%', node_id = 'fail_probe') —
#      i.e. the per-run orchestrator fired at the fail_probe wave
#      boundary and wrote exactly one engagement row. Zero rows means
#      the orchestrator never fired; >=2 rows means the boundary was
#      crossed twice (a real defect, not a flake).
#   3. The sidecar JSON carries the row's event_type so the
#      engagement row's <kind> is auditable from the contract alone.
#
# Failures are fail-closed: missing MINI_ORK_DB, missing MINI_ORK_RUN_ID,
# absent row, or count != 1 ⇒ exit 1 with reasons[] in the sidecar.
#
# Pure-python, rc-deterministic, no network, no wall-clock dependency
# beyond a single SELECT COUNT(*) — bash-removal world (2026-07).

import json
import os
import sqlite3
import sys

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", ".")
DB = os.environ.get("MINI_ORK_DB", "")
RUN_ID = (os.environ.get("MINI_ORK_RUN_ID")
          or os.environ.get("MINI_ORK_TASK_RUN_ID") or "")
TARGET_NODE = "fail_probe"

reasons = []
checks = {
    "mo_run_orchestrator_unset": False,
    "db_in_scope": False,
    "run_id_in_scope": False,
    "engagement_row_count": None,
    "engagement_row_count_is_one": False,
    "engagement_event_type": None,
}

# --- 1. MO_RUN_ORCHESTRATOR absent ----------------------------------------
if "MO_RUN_ORCHESTRATOR" in os.environ:
    reasons.append(
        f"MO_RUN_ORCHESTRATOR is set to {os.environ['MO_RUN_ORCHESTRATOR']!r} — "
        "engagement is meaningful only with the variable UNSET. The default-on "
        "claim is void when the value is explicit (even =1)."
    )
else:
    checks["mo_run_orchestrator_unset"] = True

# --- 2. DB / run id in scope ----------------------------------------------
if not DB or not os.path.isfile(DB):
    reasons.append(f"MINI_ORK_DB unset or not a file (db={DB!r}) — "
                   "engagement check requires a state.db to query")
elif not RUN_ID:
    reasons.append("MINI_ORK_RUN_ID / MINI_ORK_TASK_RUN_ID unset — "
                   "engagement check requires a run id to scope the query")
else:
    checks["db_in_scope"] = True
    checks["run_id_in_scope"] = True

# --- 3. Engagement row count ---------------------------------------------
# Use SELECT COUNT(*) so a double-fired boundary (>=2 rows) is detected
# rather than collapsed to 1 by fetchone(). 0 rows = orchestrator never
# fired; >=2 rows = boundary crossed twice (a real defect, not a flake).
row_count = 0
event_type = None
if checks["db_in_scope"] and checks["run_id_in_scope"]:
    try:
        con = sqlite3.connect(DB, timeout=2.0)
        con.execute("PRAGMA busy_timeout = 2000")
        # Read-only SELECT — does not mutate state.db.
        count_row = con.execute(
            "SELECT COUNT(*) FROM run_events "
            "WHERE run_id = ? "
            "  AND event_type LIKE 'orchestrator.%' "
            "  AND json_extract(payload_json, '$.node_id') = ?",
            (RUN_ID, TARGET_NODE),
        ).fetchone()
        if count_row is not None:
            row_count = int(count_row[0])
        # Capture the event_type from the first matching row so the
        # engagement row's <kind> is auditable from the contract alone.
        if row_count > 0:
            et_row = con.execute(
                "SELECT event_type FROM run_events "
                "WHERE run_id = ? "
                "  AND event_type LIKE 'orchestrator.%' "
                "  AND json_extract(payload_json, '$.node_id') = ? "
                "ORDER BY created_at ASC, event_id ASC LIMIT 1",
                (RUN_ID, TARGET_NODE),
            ).fetchone()
            if et_row is not None:
                event_type = et_row[0]
        con.close()
        checks["engagement_event_type"] = event_type
    except sqlite3.Error as e:
        reasons.append(f"run_events query failed: {e}")
        checks["engagement_row_count"] = -1
    else:
        checks["engagement_row_count"] = row_count
        if row_count != 1:
            reasons.append(
                f"engagement row count for node {TARGET_NODE!r} is {row_count} — "
                "expected exactly 1 (one orchestrator.<kind> row at the "
                "fail_probe wave boundary). 0 means the orchestrator did not "
                "fire; >=2 means the boundary was crossed twice."
            )
        else:
            checks["engagement_row_count_is_one"] = True

# --- Compose verdict ------------------------------------------------------
passed = not reasons
result = json.dumps({
    "verifier": "engagement-check",
    "pass": passed,
    "reasons": reasons,
    "checks": checks,
    "target_node": TARGET_NODE,
    "run_id": RUN_ID,
    "mo_run_orchestrator": os.environ.get("MO_RUN_ORCHESTRATOR", "<unset>"),
    "event_type": event_type,
    "row_count": row_count,
})
print(result)

# Persist the sidecar so the obs UI's Why? panel can show the
# engagement verdict alongside the fail_probe sidecar.
SIDECAR = os.path.join(RUN_DIR, "verifier-result-engagement-check.json")
try:
    with open(SIDECAR, "w") as f:
        f.write(result + "\n")
except OSError as e:
    # Sidecar write failure is non-fatal for the engagement verdict
    # (the JSON is on stdout) but the reason is reported on stderr.
    print(f"warning: sidecar write failed: {e}", file=sys.stderr)

# Exit reflects the verdict so the executor's verifier gate sees
# the failure; rc=1 ⇒ engagement not proven, the run is rightly failed.
sys.exit(0 if passed else 1)
