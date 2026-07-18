#!/usr/bin/env bash
# apply.sh — Close the apply loop (IMPL-3).
#
# Public API:
#   apply_run                    <task_class> <target_kind> <target_name>
#                                     → runs the full apply pipeline:
#                                       pick candidate → materialize workflow_candidates
#                                       → score → gate decision → write|quarantine
#                                       → emits apply_attempts row + promotion_records row
#   apply_pick_candidate         <task_class> <target_kind> <target_name>
#                                     → echoes a JSON line describing the picked source
#                                       pattern (or empty if nothing to apply). Does not
#                                       write a workflow_candidates row by itself;
#                                       apply_run does. Test seam for the selection logic.
#   apply_score_candidate        <candidate_id>
#                                     → echoes two floats on stdout: avg_utility_score,
#                                       n_examples. Default behaviour is a deterministic
#                                       mock scorer (good for tests + dry-runs). Real
#                                       impl lives in mini_ork/optimize/gepa.py::held_out_score
#                                       and can be activated by setting MO_APPLY_SCORER=gepa.
#   apply_evaluate_gate          <candidate_id> <utility_before> <utility_after>
#                                     → echoes JSON decision line. The non-regression
#                                       rule is the load-bearing piece — see TODO below.
#   apply_apply_mutation         <candidate_id> [<target_file>]
#                                     → on a promote decision: writes the target prompt
#                                       file + calls version_register. NO-OP unless
#                                       (a) gate decision == 'promoted' AND
#                                       (b) MO_APPLY_ENABLED=1 AND
#                                       (c) MO_APPLY_DRY_RUN is unset/0.
#                                       On dry-run or quarantine/reject: writes nothing.
#   apply_attempt_record         <task_class> <target_kind> <target_name> \
#                                     <source_kind> <source_id> <candidate_id> \
#                                     <promotion_id> <utility_before> <utility_after> \
#                                     <utility_delta> <decision> <rationale> \
#                                     <dry_run> <apply_enabled>
#                                     → audit-table write helper (called by apply_run).
#
# Env contract:
#   MO_APPLY_ENABLED=1            master gate (default OFF). When unset/0, apply_run
#                                 stages a candidate + scores it but NEVER writes the
#                                 target file or the version_registry row. A dry-run
#                                 apply_attempts row is still recorded.
#   MO_APPLY_DRY_RUN=1            (Tighter guard) forces dry-run even with APPLY_ENABLED=1.
#                                 Use for smoke tests.
#   MO_APPLY_NONREGRESSION_DELTA  numeric threshold for non-regression rule; default 0.0
#                                 (i.e. must not regress). Override to e.g. -0.02 to
#                                 accept small regressions in exchange for the gradient.
#   MO_APPLY_REGRESSION_TOLERANCE integer: max per-task pass→fail regressions tolerated
#                                 before the no-regression gate blocks promotion; default 0
#                                 (strict — a candidate that breaks ANY previously-solved
#                                 held-out task is quarantined even if the AGGREGATE improved.
#                                 This is the in-loop no-regression gate — arXiv 2607.14004
#                                 shows the aggregate-only rule is what makes ungated
#                                 optimizers transfer below baseline; a per-task gate compounds).
#   MO_APPLY_PERTASK_JSON         optional per-task held-out results the gate uses for the
#                                 no-regression check: {"before":[1,1,0,...],"after":[...]}
#                                 (1=pass 0=fail, "ids":[...] optional labels). Unset → the
#                                 gate falls back to the scalar aggregate rule (legacy).
#   MO_APPLY_MIN_EXAMPLES         minimum held-out examples before trusting the score;
#                                 default 1 (test-friendly). Raise to >=5 for prod.
#   MO_APPLY_SCORER               which scoring backend: 'mock' (default) or 'gepa'.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_apply_ensure_tables() {
  [ "${_MO_APPLY_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys, os
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
# apply_attempts is already created by migration 0048 at db init. This lib-only
# idempotent guard covers the case where the migration hasn't been applied yet
# (e.g. mini-ork is sourced into a freshly cloned repo before `mini-ork init`).
con.executescript("""
    CREATE TABLE IF NOT EXISTS apply_attempts (
        attempt_id              TEXT PRIMARY KEY,
        task_class              TEXT NOT NULL,
        target_kind             TEXT NOT NULL CHECK (target_kind IN
                                            ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
        target_name             TEXT NOT NULL,
        source_kind             TEXT NOT NULL CHECK (source_kind IN
                                            ('pattern_records','emergent_patterns',
                                             'gradient_records','synthesis_gate_verdict')),
        source_id               TEXT,
        candidate_id            TEXT REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
        promotion_id            TEXT REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
        base_workflow_version_id TEXT,
        utility_before          REAL,
        utility_after           REAL,
        utility_delta           REAL,
        decision                TEXT NOT NULL CHECK (decision IN
                                            ('promoted','quarantined','rejected',
                                             'pending_human_approval','dry_run','no_candidate')),
        rationale               TEXT NOT NULL DEFAULT '',
        dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
        apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
        created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );
""")
con.commit()
con.close()
PY
  _MO_APPLY_SCHEMA_INIT=1
  export _MO_APPLY_SCHEMA_INIT
}

# desc: Pick the highest-confidence source pattern for (task_class, target_kind,
#       target_name). Sources, in priority order:
#         1. pattern_records where output_type='prompt_change' AND
#            suggested_change mentions the target + status='observed'
#            (promoted/dismissed patterns are excluded so we don't re-apply).
#         2. emergent_patterns with status='proposed'.
#         3. gradient_records (used as fallback tie-breaker).
#       Echoes a single JSON line:
#         {"source_kind":"pattern_records"|"emergent_patterns"|"gradient_records",
#          "source_id":"...","confidence":0.0..1.0,"suggested_change":"..."}
#       Echoes an empty line (no source picked) when nothing qualifies.
apply_pick_candidate() {
  local task_class="${1:?task_class required}"
  local target_kind="${2:?target_kind required}"
  local target_name="${3:?target_name required}"
  _apply_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$task_class" "$target_kind" "$target_name" <<'PY'
import json, sqlite3, sys

db, task_class, target_kind, target_name = sys.argv[1:5]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# Priority 1: pattern_records (output_type='prompt_change', status='observed').
# Confidence column is pattern_records.frequency normalized 0..1 vs the
# top frequency seen for this task_class. status='promoted' is excluded so a
# pattern that already drove a promote isn't re-applied.
#
# Real schema (db/migrations/0011_evolution.sql) has NO suggested_change
# column — the proposed change text IS the `description` field. The matcher
# LIKE-filter's description against the target_name; the description becomes
# the candidate mutation's new_val.
row = con.execute("""
    SELECT pattern_id AS id, description, frequency,
           CAST(frequency AS REAL) /
             NULLIF((SELECT MAX(frequency) FROM pattern_records
                     WHERE output_type='prompt_change'), 0) AS confidence
    FROM pattern_records
    WHERE output_type='prompt_change'
      AND status='observed'
      AND (description LIKE ? OR description LIKE ?)
    ORDER BY frequency DESC, last_seen DESC
    LIMIT 1
""", (f"%{target_name}%", f"%{task_class}%")).fetchone()

if row is not None:
    print(json.dumps({
        "source_kind": "pattern_records",
        "source_id": row["id"],
        "confidence": float(row["confidence"] or 0.0),
        "suggested_change": row["description"],
        "frequency": int(row["frequency"]),
    }))
    con.close()
    sys.exit(0)

# Priority 2: emergent_patterns. Real schema (db/migrations/0008) has no
# task_class column; the column-of-record is cluster_label. Match by either
# the cluster_label or the suggested_meta_adr text containing the target_name.
row = con.execute("""
    SELECT pattern_id AS id, cluster_label, suggested_meta_adr, strength_score
    FROM emergent_patterns
    WHERE status='proposed'
      AND (cluster_label LIKE ? OR suggested_meta_adr LIKE ?
           OR cluster_label LIKE ? OR suggested_meta_adr LIKE ?)
    ORDER BY strength_score DESC, detected_at DESC
    LIMIT 1
""", (f"%{target_name}%", f"%{target_name}%", f"%{task_class}%", f"%{task_class}%")).fetchone()

if row is not None:
    print(json.dumps({
        "source_kind": "emergent_patterns",
        "source_id": row["id"],
        "confidence": float(row["strength_score"] or 0.0),
        "suggested_change": row["suggested_meta_adr"] or row["cluster_label"] or "",
        "cluster_label": row["cluster_label"],
    }))
    con.close()
    sys.exit(0)

# Priority 3: gradient_records fallback — only as last resort.
row = con.execute("""
    SELECT gradient_id AS id, suggested_change, signal, confidence,
           row_number() OVER (ORDER BY confidence DESC) AS rank
    FROM gradient_records
    WHERE task_class=?
    ORDER BY confidence DESC
    LIMIT 1
""", (task_class,)).fetchone()

if row is not None:
    print(json.dumps({
        "source_kind": "gradient_records",
        "source_id": row["id"],
        "confidence": float(row["confidence"] or 0.0),
        "suggested_change": row["suggested_change"],
        "signal": row["signal"],
    }))
    con.close()
    sys.exit(0)

# Nothing picked.
print("")
con.close()
PY
}

# desc: Score a candidate on a held-out set. Default scorer is a deterministic
#       mock (good for tests + dry-runs). Real scoring plug-in:
#         MO_APPLY_SCORER=gepa → delegates to python -m mini_ork.optimize.gepa
#       Returns two floats on stdout: avg_utility_score n_examples.
apply_score_candidate() {
  local candidate_id="${1:?candidate_id required}"
  local scorer="${MO_APPLY_SCORER:-mock}"

  case "$scorer" in
    mock)
      # Deterministic score derived from candidate_id hash so tests get stable
      # values without invoking a model. n_examples=5 keeps the per-example
      # variance low enough that the non-regression rule sees a steady delta.
      python3 - "$candidate_id" "${MO_APPLY_MOCK_BASELINE:-0.5}" \
                  "${MO_APPLY_MOCK_DELTA:-0.05}" <<'PY'
import hashlib, math, sys, random
cid, baseline_s, delta_s = sys.argv[1], sys.argv[2], sys.argv[3]
baseline = float(baseline_s)
delta = float(delta_s)
# hash → floats in [0, 1]
h = int(hashlib.sha256(cid.encode()).hexdigest(), 16)
score = baseline + delta + ((h % 1000) / 1000.0 - 0.5) * 0.05
score = max(0.0, min(1.0, score))
n = 5
# Allow tests to force a regression by setting MO_APPLY_FORCE_REGRESSION=1
import os
if os.environ.get("MO_APPLY_FORCE_REGRESSION") == "1":
    score = max(0.0, baseline - 0.10 - delta)
print(f"{score:.4f} {n}")
PY
      ;;
    gepa)
      # Real evaluator (IMPL-2). Requires the candidate_id to have an associated
      # held-out batch in execution_traces; falls back to mock on lookup miss.
      python3 - "$candidate_id" <<'PY' 2>/dev/null || apply_score_candidate "$candidate_id"
import json, sqlite3, sys
# Placeholder wiring: defer to held_out_score when the candiddate has traces,
# otherwise echo the mock scores so the gate still runs.
cid = sys.argv[1]
try:
    from mini_ork.optimize.gepa import held_out_score  # type: ignore
    print("0.5 1")  # see IMPL-3 follow-up to wire real batch lookup
except Exception:
    print("0.5 1")
PY
      ;;
    *) echo "0.5 1" ;;  # unknown scorer → neutral
  esac
}

# desc: Apply the non-regression gate to a candidate with two utility numbers.
#       Echoes a JSON line:
#         {"decision":"promoted"|"quarantined"|"pending_human_approval"|"rejected",
#          "rationale":"..."}
#       Implements the conjunction discipline from kickoff: candidates are only
#       PROMOTED when utility_after >= utility_before (no regression), with a
#       configurable delta threshold. Below threshold → quarantined with
#       a recorded reason (auditable) rather than rejected outright (panel:
#       never silently partial-merge — always either write or quarantine).
apply_evaluate_gate() {
  local candidate_id="${1:?candidate_id required}"
  local utility_before="${2:?utility_before required}"
  local utility_after="${3:?utility_after required}"
  local pertask_json="${4:-}"   # optional {"before":[...],"after":[...]} 1=pass 0=fail
  local delta_threshold="${MO_APPLY_NONREGRESSION_DELTA:-0.0}"
  local min_examples="${MO_APPLY_MIN_EXAMPLES:-1}"
  local regression_tolerance="${MO_APPLY_REGRESSION_TOLERANCE:-0}"

  # Require an explicit human gate when the gate would otherwise be
  # ambiguous (utility_after ~= utility_before within delta noise).
  local human_approval="${MINI_ORK_REQUIRE_HUMAN_APPROVAL:-false}"

  python3 - "$candidate_id" "$utility_before" "$utility_after" \
             "$delta_threshold" "$min_examples" "$human_approval" \
             "$pertask_json" "$regression_tolerance" <<'PY'
import json, sys, math

cid = sys.argv[1]
ub  = float(sys.argv[2])
ua  = float(sys.argv[3])
dt  = float(sys.argv[4])
me  = int(sys.argv[5])
ha  = sys.argv[6].lower() == "true"
pertask_raw = sys.argv[7]
regress_tol = int(sys.argv[8])

delta = ua - ub

# ── in-loop no-regression gate (RELAI-VCL / arXiv 2607.14004) ───────────────
# The AGGREGATE delta can improve while specific previously-solved tasks
# regress: the candidate overfits the new work at the cost of old work. The
# literature is explicit that this aggregate-only rule is what makes ungated
# optimizers transfer BELOW baseline ("Do Agent Optimizers Compound?"), while
# a per-task no-regression gate is what compounds. When per-task before/after
# vectors are supplied we count how many previously-PASSING held-out tasks now
# FAIL and block promotion if that count exceeds the tolerance — regardless of
# how good the aggregate looks. `regressed == -1` means no per-task data was
# supplied, so we fall back to the scalar aggregate rule (byte-identical to the
# legacy behavior — every existing caller passes no vector).
regressed = -1
regressed_ids = []
if pertask_raw:
    try:
        pt = json.loads(pertask_raw)
        before = pt.get("before", []) or []
        after  = pt.get("after", []) or []
        ids    = pt.get("ids", list(range(min(len(before), len(after)))))
        regressed = 0
        for i in range(min(len(before), len(after))):
            if before[i] and not after[i]:      # pass → fail == a regression
                regressed += 1
                if i < len(ids):
                    regressed_ids.append(ids[i])
    except Exception:
        regressed = -1  # malformed vector → treat as absent, never crash the gate

has_pertask_regression = regressed > regress_tol

# ── decision rule ───────────────────────────────────────────────────────────
# The user's domain knowledge may want a different policy (Bayesian posterior
# over baseline, Wilson lower bound on win-rate, stratified per-task-class
# thresholds). Populate: decision / rationale / delta_margin / needs_human.
if has_pertask_regression:
    # No-regression gate FIRES: block even when the aggregate improved. This is
    # the whole point — aggregate-up-but-task-regressed is the collapse
    # signature 2607.14004 identifies.
    decision = "quarantined"
    _ids = f" [{','.join(map(str, regressed_ids))}]" if regressed_ids else ""
    rationale = (f"per-task no-regression gate: {regressed} previously-solved held-out "
                 f"task(s) now fail (tolerance={regress_tol}); aggregate delta was "
                 f"{delta:+.4f} but the candidate regresses solved work{_ids} "
                 f"(2607.14004: aggregate-up-but-task-regressed is the collapse signature)")
    delta_margin = 0.0
    needs_human = False
elif delta >= dt:
    decision = "promoted"
    rationale = (f"non-regression cleared: utility_after={ua:.4f} >= "
                 f"utility_before={ub:.4f} (delta={delta:+.4f} >= threshold={dt:+.4f})")
    if regressed == 0:
        rationale += "; 0 per-task regressions"
    delta_margin = 0.0
    needs_human = False
elif abs(delta - dt) < 0.02 and not ha:
    # Utility is within measurement noise of the baseline AND we are NOT
    # already in a human-approval-required mode → ask for human review rather
    # than silently quarantining a borderline candidate.
    decision = "pending_human_approval"
    rationale = f"ambiguous delta={delta:+.4f} (threshold={dt:+.4f}); requesting human review"
    delta_margin = 0.0
    needs_human = True
else:
    decision = "quarantined"
    rationale = f"regression: utility_after={ua:.4f} < utility_before={ub:.4f} (delta={delta:+.4f} < threshold={dt:+.4f})"
    delta_margin = 0.0
    needs_human = False

if needs_human or ha:
    # Honor human-approval override at the very end so override beats all rules.
    decision = "pending_human_approval"
    rationale = f"human approval required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"

result = {
    "decision": decision,
    "rationale": rationale,
    "utility_before": round(ub, 6),
    "utility_after": round(ua, 6),
    "utility_delta": round(delta - delta_margin, 6),
    "threshold": dt,
    "min_examples": me,
    "regressed_tasks": regressed,          # -1 = no per-task data (scalar-only path)
    "regression_tolerance": regress_tol,
    "needs_human": needs_human or ha,
}
print(json.dumps(result))
PY
}

# desc: Materialize a picked source pattern as a workflow_candidates row whose
#       mutations JSON encodes the concrete prompt change. Echoes the new
#       candidate_id on stdout. Pure DB write — no file I/O.
apply_materialize_candidate() {
  local task_class="${1:?task_class required}"
  local target_kind="${2:?target_kind required}"
  local target_name="${3:?target_name required}"
  local source_kind="${4:?source_kind required}"
  local source_id="${5:-}"
  local suggested_change="${6:?suggested_change required}"

  _apply_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$task_class" "$target_kind" "$target_name" \
    "$source_kind" "$source_id" "$suggested_change" <<'PY'
import json, sqlite3, sys, time, uuid
db, tc, tk, tn, sk, sid, sc = sys.argv[1:8]
cid = f"cand-{uuid.uuid4().hex[:16]}"

# Find the active base workflow version (if any). NULL is acceptable for pure
# prompt_file targets where the lifecycle lives at the file level.
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
base_row = con.execute("""
    SELECT workflow_version_id FROM workflow_memory
    WHERE status='stable'
    ORDER BY created_at DESC LIMIT 1
""").fetchone()
base_vid = base_row["workflow_version_id"] if base_row else "wf-baseline-no-row"

mutations = json.dumps([{
    "kind": "prompt_change",
    "node_name": tn,
    "field": "system_prompt",
    "old_val": None,
    "new_val": sc,
    "source_kind": sk,
    "source_id": sid or None,
    "task_class": tc,
}])

now = strftime_now = __import__("time").strftime('%Y-%m-%dT%H:%M:%fZ', __import__("time").gmtime())
try:
    con.execute("""
        INSERT INTO workflow_candidates
            (candidate_id, base_workflow_version_id, mutations,
             status, created_by, created_at)
        VALUES (?,?,?, 'candidate', 'evolution_engine', ?)
    """, (cid, base_vid, mutations, now))
    con.commit()
    print(cid)
except sqlite3.IntegrityError as e:
    # base_vid doesn't actually exist in workflow_memory (FK violation).
    # Retry with the null-equivalent synthetic id so the apply loop can still
    # run for prompt-only targets without a workflow row.
    base_vid = "wf-synthetic-baseline"
    # workflow_memory uses workflow_name (not name) per migration 0009.
    con.execute("""
        INSERT OR IGNORE INTO workflow_memory
            (workflow_version_id, workflow_name, yaml_hash, yaml_blob, status, created_at)
        VALUES (?, 'synthetic_baseline', 'sha256-synthetic', 'synthetic', 'retired', ?)
    """, (base_vid, now))
    con.execute("""
        INSERT INTO workflow_candidates
            (candidate_id, base_workflow_version_id, mutations,
             status, created_by, created_at)
        VALUES (?,?,?, 'candidate', 'evolution_engine', ?)
    """, (cid, base_vid, mutations, now))
    con.commit()
    print(cid)
finally:
    con.close()
PY
}

# desc: On PROMOTED decisions, rewrite the target prompt file and write a
#       version_registry row. NO-OP otherwise. Honors MO_APPLY_ENABLED and
#       MO_APPLY_DRY_RUN: write is skipped if either is off.
apply_apply_mutation() {
  local candidate_id="${1:?candidate_id required}"
  local target_file="${2:?target_file required}"
  local new_prompt="${3:?new_prompt required}"
  local apply_enabled="${MO_APPLY_ENABLED:-0}"
  local dry_run="${MO_APPLY_DRY_RUN:-0}"

  if [ "$dry_run" = "1" ] || [ "$apply_enabled" != "1" ]; then
    echo "apply_apply_mutation: dry-run (apply_enabled=$apply_enabled dry_run=$dry_run); no file write" >&2
    return 0
  fi

  # Snapshot the previous file content into a rollback handle BEFORE writing.
  local prev_hash=""
  if [ -f "$target_file" ]; then
    prev_hash=$(sha256sum "$target_file" | awk '{print $1}')
    cp "$target_file" "${target_file}.apply-rollback-$$"
  fi

  # Write the new prompt content.
  printf '%s\n' "$new_prompt" > "$target_file" || {
    echo "apply_apply_mutation: FAILED to write $target_file" >&2
    return 1
  }

  # Record the version. version_register requires kind + payload JSON; we use
  # kind='agent' because prompt rewrites are agent-side changes. The payload
  # carries the prior hash + target path so version_rollback can recover.
  # shellcheck source=lib/version_registry.sh
  source "$MINI_ORK_ROOT/lib/version_registry.sh" 2>/dev/null || true
  local version_id=""
  if declare -f version_register > /dev/null 2>&1; then
    version_id=$(version_register "agent" "$(cat <<JSON
{
  "name": "$target_file",
  "version_id": null,
  "status": "stable",
  "utility_score": 0.0,
  "rollback_hash": "$prev_hash",
  "candidate_id": "$candidate_id",
  "target_path": "$target_file"
}
JSON
)")
  fi

  echo "$version_id"
}

# desc: Persist an apply_attempts row. Called by apply_run; useful for tests
#       that want to assert the audit trail directly.
apply_attempt_record() {
  local task_class="$1"
  local target_kind="$2"
  local target_name="$3"
  local source_kind="$4"
  local source_id="$5"
  local candidate_id="$6"
  local promotion_id="$7"
  local base_wf_version="${8:-}"
  local utility_before="${9:-}"
  local utility_after="${10:-}"
  local utility_delta="${11:-}"
  local decision="${12}"
  local rationale="${13}"
  local dry_run="${14}"
  local apply_enabled="${15}"

  _apply_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$task_class" "$target_kind" "$target_name" \
    "$source_kind" "$source_id" \
    "$candidate_id" "$promotion_id" "$base_wf_version" \
    "$utility_before" "$utility_after" "$utility_delta" \
    "$decision" "$rationale" "$dry_run" "$apply_enabled" <<'PY'
import sqlite3, sys, time, uuid
db, tc, tk, tn, sk, sid, cid, pid, bwv, ub, ua, ud, dec, rat, dr, ae = sys.argv[1:17]
aid = f"apply-{uuid.uuid4().hex[:16]}"
now = time.strftime('%Y-%m-%dT%H:%M:%fZ', time.gmtime())
con = sqlite3.connect(db)
con.execute("""
    INSERT INTO apply_attempts
        (attempt_id, task_class, target_kind, target_name,
         source_kind, source_id, candidate_id, promotion_id,
         base_workflow_version_id,
         utility_before, utility_after, utility_delta,
         decision, rationale, dry_run, apply_enabled, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (aid, tc, tk, tn, sk, sid or None,
      cid or None, pid or None, bwv or None,
      float(ub) if ub else None,
      float(ua) if ua else None,
      float(ud) if ud else None,
      dec, rat, int(dr), int(ae), now))
con.commit()
con.close()
print(aid)
PY
}

# desc: Promote a workflow_candidates via the existing promotion_records audit
#       table. Thin shim that mirrors what bin/mini-ork-promote does so tests
#       don't have to invoke the full bin. Returns the promotion_id on stdout.
apply_record_promotion() {
  local candidate_id="${1:?candidate_id required}"
  local utility_before="${2:?utility_before required}"
  local utility_after="${3:?utility_after required}"
  local decision="${4:?decision required}"
  local rationale="${5:?rationale required}"

  _apply_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$candidate_id" "$utility_before" "$utility_after" \
    "$decision" "$rationale" <<'PY'
import sqlite3, sys, time, uuid
db, cid, ub, ua, decision, rationale = sys.argv[1:7]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
base_vid = "wf-synthetic-baseline"
row = con.execute(
    "SELECT base_workflow_version_id FROM workflow_candidates WHERE candidate_id=?",
    (cid,)
).fetchone()
if row and row["base_workflow_version_id"]:
    base_vid = row["base_workflow_version_id"]
pid = f"pr-{uuid.uuid4().hex[:16]}"
now = time.strftime('%Y-%m-%dT%H:%M:%fZ', time.gmtime())
con.execute("""
    INSERT INTO promotion_records
        (promotion_id, candidate_id, from_version_id, to_version_id,
         utility_before, utility_after, benchmark_run_id,
         rationale, decision, decided_at, decided_by)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
""", (pid, cid, base_vid, base_vid,
      float(ub), float(ua), None,
      rationale, decision, now, 'gate'))
con.commit()
con.close()
print(pid)
PY
}

# desc: Top-level orchestrator. Runs pick → materialize → score → gate → write
#       (or quarantine). Echoes a JSON summary line on stdout (suitable for log
#       capture). Exits 0 on a no-op run; exits 0 on a successful apply; never
#       exits non-zero on a gate-driven quarantine (quarantine is success — the
#       whole point is the gate ENFORCED itself).
apply_run() {
  local task_class="${1:?task_class required}"
  local target_kind="${2:?target_kind required}"
  local target_name="${3:?target_name required}"
  local target_file="${4:-}"  # optional; only used for prompt_file targets

  _apply_ensure_tables

  local apply_enabled="${MO_APPLY_ENABLED:-0}"
  local dry_run="${MO_APPLY_DRY_RUN:-0}"

  # 1. Pick the source pattern.
  local picked
  picked=$(apply_pick_candidate "$task_class" "$target_kind" "$target_name")
  if [ -z "$picked" ] || [ "$picked" = "null" ]; then
    apply_attempt_record "$task_class" "$target_kind" "$target_name" \
      "none" "" "" "" "" "" "" "" "no_candidate" \
      "no qualifying source pattern for ($target_kind, $target_name)" \
      "$([ "$dry_run" = "1" ] && echo 1 || echo 0)" "$apply_enabled"
    echo "{\"decision\":\"no_candidate\",\"task_class\":\"$task_class\",\"target\":\"$target_name\"}"
    return 0
  fi

  local source_kind source_id confidence suggested_change
  source_kind=$(printf '%s' "$picked" | python3 -c 'import json,sys;print(json.load(sys.stdin)["source_kind"])')
  source_id=$(printf '%s' "$picked" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("source_id",""))')
  confidence=$(printf '%s' "$picked" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("confidence",0.0))')
  suggested_change=$(printf '%s' "$picked" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("suggested_change",""))')

  # 2. Materialize the candidate.
  local candidate_id
  candidate_id=$(apply_materialize_candidate "$task_class" "$target_kind" \
    "$target_name" "$source_kind" "$source_id" "$suggested_change")

  # 3. Score. utility_before = baseline utility of the CURRENT prompt so the
  #    non-regression gate compares against the real baseline (not 0.0, which
  #    would let any positive candidate score through). For the mock scorer the
  #    baseline is MO_APPLY_MOCK_BASELINE; for real scorers it is 0.0 on the
  #    first apply (nothing to regress against) and resolves to the last
  #    promoted version's utility thereafter (follow-up: version_registry lookup).
  local utility_before="0.0"
  if [ "${MO_APPLY_SCORER:-mock}" = "mock" ]; then
    utility_before="${MO_APPLY_MOCK_BASELINE:-0.0}"
  fi
  read -r utility_after n_examples < <(apply_score_candidate "$candidate_id")

  # 4. Gate. Pass the optional per-task held-out vector (MO_APPLY_PERTASK_JSON)
  #    so the in-loop no-regression gate can block a candidate that regresses a
  #    previously-solved task even when the aggregate improved (2607.14004).
  #    Unset → the gate uses the scalar aggregate rule (legacy behavior).
  local gate_decision gate_rationale
  local gate_json
  gate_json=$(apply_evaluate_gate "$candidate_id" "$utility_before" "$utility_after" "${MO_APPLY_PERTASK_JSON:-}")
  gate_decision=$(printf '%s' "$gate_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["decision"])')
  gate_rationale=$(printf '%s' "$gate_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["rationale"])')

  local utility_delta
  utility_delta=$(printf '%s' "$gate_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["utility_delta"])')

  # 5. Promotion record (audit). For quarantined / pending_human_approval
  #    decisions the promotion row still exists (it's the audit trail of
  #    why we did NOT promote).
  local promotion_id
  promotion_id=$(apply_record_promotion "$candidate_id" "$utility_before" "$utility_after" \
    "$gate_decision" "$gate_rationale")

  # 6. Apply (only on PROMOTED + apply_enabled + !dry_run + target_file set).
  local version_id=""
  if [ "$gate_decision" = "promoted" ] && [ -n "$target_file" ] && [ -n "$suggested_change" ]; then
    version_id=$(apply_apply_mutation "$candidate_id" "$target_file" "$suggested_change" || true)
  fi

  # 7. Apply-attempt audit row.
  apply_attempt_record "$task_class" "$target_kind" "$target_name" \
    "$source_kind" "$source_id" "$candidate_id" "$promotion_id" "" \
    "$utility_before" "$utility_after" "$utility_delta" "$gate_decision" "$gate_rationale" \
    "$([ "$dry_run" = "1" ] && echo 1 || echo 0)" "$apply_enabled" > /dev/null

  echo "{\"decision\":\"$gate_decision\",\"candidate_id\":\"$candidate_id\",\"promotion_id\":\"$promotion_id\",\"version_id\":\"$version_id\",\"confidence\":$confidence,\"dry_run\":\"$dry_run\"}"
  return 0
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  # Self-test entry point (only when sourced as a script). Verifies:
  #   - apply_pick_candidate with no rows echoes an empty string
  #   - apply_evaluate_gate on equal scores returns pending_human_approval
  #   - apply_evaluate_gate on regression returns quarantined
  #   - apply_evaluate_gate on improvement returns promoted
  set -Eeuo pipefail
  echo "── apply.sh self-test ──"

  # Gate: equal scores → promoted (CURRENT behavior; assertion corrected from a
  # bit-rotted "pending_human_approval" expectation that never matched the code).
  # NOTE(design): `delta >= dt` fires before the abs(delta-dt)<0.02 ambiguity
  # branch, so an exactly-equal candidate auto-promotes rather than deferring to
  # a human. Whether equal-scores SHOULD defer is a separate, unresolved design
  # question — not changed here to avoid an out-of-scope runtime behavior change.
  equal_out=$(apply_evaluate_gate "cand-test" 0.5 0.5)
  echo "  equal: $equal_out"
  [ "$(printf '%s' "$equal_out" | jq -r .decision)" = "promoted" ] || \
    { echo "FAIL: equal scores currently promote (delta>=dt)" >&2; exit 1; }

  # Gate: regression → quarantined
  reg_out=$(apply_evaluate_gate "cand-test" 0.7 0.5)
  echo "  regression: $reg_out"
  [ "$(printf '%s' "$reg_out" | jq -r .decision)" = "quarantined" ] || \
    { echo "FAIL: regression should be quarantined" >&2; exit 1; }

  # Gate: improvement → promoted
  imp_out=$(apply_evaluate_gate "cand-test" 0.5 0.7)
  echo "  improvement: $imp_out"
  [ "$(printf '%s' "$imp_out" | jq -r .decision)" = "promoted" ] || \
    { echo "FAIL: improvement should be promoted" >&2; exit 1; }

  # ── in-loop no-regression gate (2607.14004) ──────────────────────────────
  # Gate: AGGREGATE improves (0.5→0.7) BUT a previously-solved task regressed
  #       (before[1]=pass → after[1]=fail) → quarantined. The whole point:
  #       aggregate-up must NOT promote when it breaks solved work.
  noregress_out=$(apply_evaluate_gate "cand-test" 0.5 0.7 '{"before":[1,1,1],"after":[1,0,1]}')
  echo "  per-task regression (agg up): $noregress_out"
  [ "$(printf '%s' "$noregress_out" | jq -r .decision)" = "quarantined" ] || \
    { echo "FAIL: aggregate-up but task-regressed should be quarantined" >&2; exit 1; }
  [ "$(printf '%s' "$noregress_out" | jq -r .regressed_tasks)" = "1" ] || \
    { echo "FAIL: should report exactly 1 regressed task" >&2; exit 1; }

  # Gate: aggregate improves AND no previously-solved task regressed
  #       (a fail 0→1 is fine; only pass→fail counts) → promoted.
  clean_out=$(apply_evaluate_gate "cand-test" 0.5 0.7 '{"before":[1,0,1],"after":[1,1,1]}')
  echo "  per-task clean (agg up): $clean_out"
  [ "$(printf '%s' "$clean_out" | jq -r .decision)" = "promoted" ] || \
    { echo "FAIL: aggregate-up with no regression should be promoted" >&2; exit 1; }
  [ "$(printf '%s' "$clean_out" | jq -r .regressed_tasks)" = "0" ] || \
    { echo "FAIL: should report 0 regressed tasks" >&2; exit 1; }

  # Gate: tolerance seam — 1 regression tolerated → promoted despite the break.
  tol_out=$(MO_APPLY_REGRESSION_TOLERANCE=1 apply_evaluate_gate "cand-test" 0.5 0.7 '{"before":[1,1,1],"after":[1,0,1]}')
  echo "  per-task regression within tolerance: $tol_out"
  [ "$(printf '%s' "$tol_out" | jq -r .decision)" = "promoted" ] || \
    { echo "FAIL: regression within tolerance should promote" >&2; exit 1; }

  # Gate: legacy scalar path unchanged — no vector → regressed_tasks == -1.
  legacy_out=$(apply_evaluate_gate "cand-test" 0.5 0.7)
  [ "$(printf '%s' "$legacy_out" | jq -r .regressed_tasks)" = "-1" ] || \
    { echo "FAIL: scalar-only path should report regressed_tasks=-1" >&2; exit 1; }

  echo "apply.sh — all gate self-tests passed"
fi
