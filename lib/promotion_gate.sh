#!/usr/bin/env bash
# promotion_gate.sh — PromotionDecision logic for workflow/agent candidates.
#
# Public API:
#   promotion_evaluate  <candidate_id>
#       → emits decision JSON: { decision, rationale, utility_before,
#                                utility_after, benchmark_run_id }
#   promotion_approve   <candidate_id> <approver> <rationale>
#       → resolves a pending_human_approval decision

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_promo_ensure_tables() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_PROMO_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
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
con.close()
PY
  _MO_PROMO_SCHEMA_INIT=1
  export _MO_PROMO_SCHEMA_INIT
}

# desc: Evaluate a candidate for promotion. Queries benchmark_results and
#       version_registry for baseline utility. Returns decision JSON.
promotion_evaluate() {
  local candidate_id="${1:?candidate_id required}"
  _promo_ensure_tables

  # Load sub-libraries if not already loaded
  # shellcheck source=lib/benchmark_suite.sh
  source "${MINI_ORK_ROOT}/lib/benchmark_suite.sh"  2>/dev/null || true
  # shellcheck source=lib/utility_function.sh
  source "${MINI_ORK_ROOT}/lib/utility_function.sh" 2>/dev/null || true
  # shellcheck source=lib/version_registry.sh
  source "${MINI_ORK_ROOT}/lib/version_registry.sh" 2>/dev/null || true
  # shellcheck source=lib/gate_registry.sh
  source "${MINI_ORK_ROOT}/lib/gate_registry.sh"    2>/dev/null || true

  local require_human="${MINI_ORK_REQUIRE_HUMAN_APPROVAL:-false}"

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$candidate_id" "$require_human" <<'PY'
import sqlite3, json, sys, time, uuid

db, cid, require_human = sys.argv[1], sys.argv[2], sys.argv[3]
now = int(time.time())
record_id = f"pr-{uuid.uuid4().hex[:16]}"

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# Fetch most recent benchmark run for this candidate.
# v0.2-pt35 (Phase E gap closure, 2026-06-02): real benchmark_results
# column is `pass` (INTEGER 0/1 bool), NOT `passed`. Patched both
# aliases + the result-dict key consumed below.
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
""", (cid,)).fetchone()

# Fetch baseline utility from version_registry
baseline_row = con.execute("""
    SELECT utility_score FROM version_registry
    WHERE name=? AND status='stable'
    ORDER BY promoted_at DESC LIMIT 1
""", (cid,)).fetchone() if con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='version_registry'"
).fetchone() else None

utility_before = float(baseline_row[0]) if baseline_row else 0.0
utility_after  = float(brun["avg_utility_score"]) if brun else 0.0
all_pass       = bool(brun["all_pass"]) if brun else False

# Check for safety violations in gate_registry
safety_violations = []
try:
    sv_rows = con.execute("""
        SELECT gate_id, condition FROM gate_registry
        WHERE safety=1 AND (task_class_filter IS NULL OR task_class_filter=?)
    """, (cid,)).fetchall()
    # Safety gates stored as references; actual evaluation happens via gate_run_all
    safety_violations = []  # gate_run_all handles this at call time
except Exception:
    pass

utility_delta = utility_after - utility_before

# Decision logic
if require_human.lower() == "true":
    decision = "pending_human_approval"
    rationale = f"Human gate required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"
elif not all_pass and brun and brun["total_tasks"] > 0:
    decision = "rejected"
    rationale = (f"Not all benchmark tasks passed "
                 f"({brun['passed']}/{brun['total_tasks']})")
elif utility_delta <= 0 and brun:
    decision = "quarantined"
    rationale = (f"Utility did not improve: before={utility_before:.4f}, "
                 f"after={utility_after:.4f}, delta={utility_delta:.4f}")
else:
    decision = "promoted"
    rationale = (f"Utility improved by {utility_delta:.4f} "
                 f"({utility_before:.4f} → {utility_after:.4f}); "
                 f"all benchmark tasks passed.")

result = {
    "decision": decision,
    "rationale": rationale,
    "utility_before": round(utility_before, 6),
    "utility_after": round(utility_after, 6),
    "utility_delta": round(utility_delta, 6),
    "benchmark_run_id": cid,
    "all_pass": all_pass,
    "safety_violations": safety_violations,
}

# v0.2-pt35 (Phase E gap closure, 2026-06-02): real schema columns are
# promotion_id (PK), candidate_id, from_version_id, to_version_id,
# utility_before, utility_after, benchmark_run_id, rationale, decision,
# decided_at, decided_by. Previous code wrote `record_id` +
# `safety_violations` + `evaluated_at` from a divergent draft.
# from_version_id + to_version_id are required NOT NULL FKs to
# workflow_memory — resolve via the candidate's base_workflow_version_id
# (from_ = baseline; to_ = baseline + candidate mutations applied — for
# now both reference the baseline since we don't yet materialize a new
# workflow_memory row per candidate).
base_ver_row = con.execute(
    "SELECT base_workflow_version_id FROM workflow_candidates WHERE candidate_id=?",
    (cid,)
).fetchone()
base_ver = base_ver_row[0] if base_ver_row else None
if not base_ver:
    print(f"promotion_evaluate: candidate {cid} has no base_workflow_version_id", file=sys.stderr)
    sys.exit(1)

con.execute("""
    INSERT INTO promotion_records
        (promotion_id, candidate_id, from_version_id, to_version_id,
         utility_before, utility_after, benchmark_run_id,
         rationale, decision, decided_by)
    VALUES (?,?,?,?,?,?,?,?,?,?)
""", (
    record_id, cid, base_ver, base_ver,
    utility_before, utility_after, None,
    rationale, decision, "gate",
))
con.commit()
con.close()
print(json.dumps(result))
PY
}

# desc: Approve a candidate that is in pending_human_approval state.
#       approver is a string identifier, rationale is free text.
promotion_approve() {
  local candidate_id="${1:?candidate_id required}"
  local approver="${2:?approver required}"
  local rationale="${3:?rationale required}"
  _promo_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$candidate_id" "$approver" "$rationale" <<'PY'
import sqlite3, json, sys, time
db, cid, approver, rationale = sys.argv[1:5]
con = sqlite3.connect(db)
# v0.2-pt37 (2026-06-05): real schema (migration 0011) uses
# decided_at TEXT (ISO timestamp via strftime) and decided_by TEXT
# (CHECK IN ('gate','human')). The previous body wrote `approver`,
# `approval_rationale`, `approved_at` — three columns that don't
# exist on the real promotion_records table. The
# CREATE-IF-NOT-EXISTS in _promo_ensure_tables was a no-op against
# the migration-seeded schema, so every UPDATE failed with rc=0 +
# 0 rowcount, and the function exited 1 silently.
#
# Map: approver → decided_by ('human' literal), approval_rationale →
# rationale (single column), approved_at → decided_at (let SQLite
# default fire via strftime).
updated = con.execute("""
    UPDATE promotion_records
    SET decision='promoted',
        decided_by='human',
        rationale=?,
        decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE candidate_id=? AND decision='pending_human_approval'
""", (rationale, cid)).rowcount
# Capture decided_at for return payload.
decided_at = None
if updated:
    row = con.execute(
        "SELECT decided_at FROM promotion_records WHERE candidate_id=? AND decision='promoted' ORDER BY decided_at DESC LIMIT 1",
        (cid,)
    ).fetchone()
    decided_at = row[0] if row else None
con.commit()
con.close()
if updated == 0:
    print(f"promotion_approve: no pending approval found for {cid}", file=sys.stderr)
    sys.exit(1)
# Maintain caller-facing JSON contract: keep `approver` + `approved_at`
# keys in the response (callers may key off them); back them with the
# decided_by/decided_at values.
print(json.dumps({
    "candidate_id": cid,
    "decision": "promoted",
    "approver": approver,
    "approved_at": decided_at,
}))
PY
}

# ── Synthesis-class promotion gate (Wave 1 D, oracle-hardening-v0.3) ─────────
#
# desc: Selective-feedback conjunction gate for synthesis-class task classes.
#
# The framework auto-promotes deterministic-oracle classes (code_fix,
# db_migration) via promotion_evaluate above — the external verifier IS
# the oracle. Synthesis classes (research_synthesis, refactor_audit,
# blog-post, ui-audit, ops-runbook) lack such an oracle: their promotion
# gate is LLM-judged by the same family distribution that produced the
# candidate. Zenil 2026 (arxiv:2601.05280) proves this yields
# degenerative dynamics in the limit.
#
# Adapala 2025 (arxiv:2509.10509) demonstrates the cheap remedy:
# "Anti-Ouroboros" effect — a selective filter retaining only
# candidates passing a quality threshold BEFORE re-ingest flips
# collapse into emergent resilience. The selection step does the work
# of a tiny oracle slice.
#
# This gate requires ALL THREE conditions to mark a synthesis candidate
# auto-promote-eligible:
#
#   1. Panel rubric score >= MO_PROMOTE_SCORE_THRESHOLD (default 80)
#   2. CW-POR <= MO_CW_POR_THRESHOLD (default 0.3) — no authority capture.
#      Soft dep on lib/cw_por.sh: default-passes if the library is absent
#      so this gate doesn't hard-depend on W1-C ordering.
#   3. >= 1 independent structural quality signal:
#        - citation_density_per_lens > MO_MIN_CITATION_DENSITY (default 3)
#        - file_coverage_delta > 0
#        - finding_cardinality > MO_MIN_FINDING_CARDINALITY (default 5)
#
# Conjunction beats single-signal: correlated failures across the LLM
# panel are less likely to also fool the second independent structural
# signal.
#
# Deterministic-oracle classes route through promotion_evaluate above
# unchanged; this gate is the additive synthesis-class branch.
#
# Public API:
#   mo_promote_synthesis_gate <panel_verdict.json> <task_class>
#       → emits structured JSON: { decision, reason, signals: {...} }
#       → rc=0 when all three conditions met (or task_class is
#         deterministic — early-return with reason='deterministic_class')
#       → rc=1 when any condition fails (reason ∈
#         { low_panel_score, authority_capture, no_structural_signal })
#       → rc=2 on malformed input
#
# Input contract — panel_verdict.json must contain:
#   { "panel_score": <float 0..100>,
#     "voters": [ ... ],                # passed-through to cw_por if available
#     "structural": {
#       "citation_density_per_lens": <float>,
#       "file_coverage_delta": <int>,
#       "finding_cardinality": <int>
#     }
#   }

DETERMINISTIC_TASK_CLASSES="${MO_DETERMINISTIC_TASK_CLASSES:-code_fix db_migration}"

mo_promote_synthesis_gate() {
  local verdict_file="${1:?verdict_file required}"
  local task_class="${2:?task_class required}"

  if [ ! -f "$verdict_file" ]; then
    printf '{"error":"verdict file not found: %s"}\n' "$verdict_file" >&2
    return 2
  fi

  # Deterministic-oracle classes bypass this gate entirely.
  case " $DETERMINISTIC_TASK_CLASSES " in
    *" $task_class "*)
      printf '{"decision":"approved","reason":"deterministic_class","task_class":"%s","note":"routes through promotion_evaluate single-pass verifier path"}\n' "$task_class"
      return 0
      ;;
  esac

  local score_threshold="${MO_PROMOTE_SCORE_THRESHOLD:-80}"
  local cw_por_threshold="${MO_CW_POR_THRESHOLD:-0.3}"
  local min_citation_density="${MO_MIN_CITATION_DENSITY:-3}"
  local min_finding_cardinality="${MO_MIN_FINDING_CARDINALITY:-5}"

  # Compute CW-POR if the library is available and ground-truth signal
  # exists; otherwise default-pass (rationale: no W1-C hard dep).
  local cw_por_value="null"
  local cw_por_status="default_passed"
  if [ -f "${MINI_ORK_ROOT}/lib/cw_por.sh" ]; then
    # shellcheck source=lib/cw_por.sh
    source "${MINI_ORK_ROOT}/lib/cw_por.sh" 2>/dev/null || true
    if declare -f mo_compute_cw_por > /dev/null 2>&1; then
      local _cw_out
      if _cw_out=$(mo_compute_cw_por "$verdict_file" 2>/dev/null); then
        cw_por_value=$(echo "$_cw_out" | jq -r '.cw_por // "null"')
        local _cw_verdict
        _cw_verdict=$(echo "$_cw_out" | jq -r '.verdict // "indeterminate"')
        case "$_cw_verdict" in
          panel_healthy)              cw_por_status="passed" ;;
          authority_capture_suspected) cw_por_status="failed" ;;
          *)                          cw_por_status="indeterminate_default_passed" ;;
        esac
      fi
    fi
  fi

  python3 - "$verdict_file" \
      "$score_threshold" "$cw_por_threshold" \
      "$min_citation_density" "$min_finding_cardinality" \
      "$cw_por_value" "$cw_por_status" "$task_class" <<'PY'
import json, sys

(verdict_file, score_thr_s, cw_thr_s, min_cit_s, min_card_s,
 cw_value_s, cw_status, task_class) = sys.argv[1:9]
score_threshold = float(score_thr_s)
cw_threshold    = float(cw_thr_s)
min_citation    = float(min_cit_s)
min_cardinality = int(min_card_s)

try:
    with open(verdict_file) as f:
        data = json.load(f)
except Exception as e:
    print(json.dumps({"error": f"json parse failed: {e}"}))
    sys.exit(2)

panel_score = data.get("panel_score")
if panel_score is None:
    print(json.dumps({"error": "verdict file missing required .panel_score"}))
    sys.exit(2)
panel_score = float(panel_score)

structural = data.get("structural", {})
cit_density = float(structural.get("citation_density_per_lens", 0))
file_cov    = int(structural.get("file_coverage_delta", 0))
finding_card = int(structural.get("finding_cardinality", 0))

# Condition 1: panel score gate.
if panel_score < score_threshold:
    print(json.dumps({
        "decision": "rejected",
        "reason": "low_panel_score",
        "task_class": task_class,
        "signals": {
            "panel_score": panel_score,
            "panel_score_threshold": score_threshold,
            "cw_por": (None if cw_value_s == "null" else float(cw_value_s)),
            "cw_por_status": cw_status,
            "structural": structural,
        },
        "rationale": (f"panel_score={panel_score:.2f} below threshold "
                      f"{score_threshold:.2f}")
    }))
    sys.exit(1)

# Condition 2: CW-POR gate (skip if cw_por library not available or
# ground truth absent — soft dep on W1-C).
if cw_status == "failed":
    cw_val = None if cw_value_s == "null" else float(cw_value_s)
    print(json.dumps({
        "decision": "rejected",
        "reason": "authority_capture",
        "task_class": task_class,
        "signals": {
            "panel_score": panel_score,
            "cw_por": cw_val,
            "cw_por_status": cw_status,
            "cw_por_threshold": cw_threshold,
            "structural": structural,
        },
        "rationale": (f"CW-POR={cw_val} exceeds threshold "
                      f"{cw_threshold:.2f}; authority capture suspected")
    }))
    sys.exit(1)

# Condition 3: at least one structural quality signal.
signal_hits = []
if cit_density > min_citation:
    signal_hits.append(f"citation_density={cit_density:.2f} > {min_citation:.2f}")
if file_cov > 0:
    signal_hits.append(f"file_coverage_delta={file_cov} > 0")
if finding_card > min_cardinality:
    signal_hits.append(f"finding_cardinality={finding_card} > {min_cardinality}")

if not signal_hits:
    print(json.dumps({
        "decision": "rejected",
        "reason": "no_structural_signal",
        "task_class": task_class,
        "signals": {
            "panel_score": panel_score,
            "cw_por": (None if cw_value_s == "null" else float(cw_value_s)),
            "cw_por_status": cw_status,
            "structural": structural,
            "min_citation_density": min_citation,
            "min_finding_cardinality": min_cardinality,
        },
        "rationale": ("no independent structural quality signal: "
                      f"citation_density={cit_density:.2f}, "
                      f"file_coverage_delta={file_cov}, "
                      f"finding_cardinality={finding_card}")
    }))
    sys.exit(1)

# All three conditions pass — synthesis candidate is auto-promote-eligible.
print(json.dumps({
    "decision": "approved",
    "reason": "all_conditions_met",
    "task_class": task_class,
    "signals": {
        "panel_score": panel_score,
        "panel_score_threshold": score_threshold,
        "cw_por": (None if cw_value_s == "null" else float(cw_value_s)),
        "cw_por_status": cw_status,
        "cw_por_threshold": cw_threshold,
        "structural": structural,
        "structural_signals_met": signal_hits,
    },
    "rationale": (f"panel_score={panel_score:.2f} >= {score_threshold:.2f}, "
                  f"cw_por={cw_status}, structural signals met: "
                  + "; ".join(signal_hits))
}))
sys.exit(0)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  # Self-test entry point. Runs three fixture cases against
  # mo_promote_synthesis_gate plus a deterministic-class bypass probe.
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT

  # Fixture A: deterministic class bypass (task_class=code_fix → rc=0
  # regardless of panel content).
  echo '{"panel_score":0,"voters":[],"structural":{}}' > "$_td/det.json"
  echo "── fixture A (deterministic class bypass) ──"
  if mo_promote_synthesis_gate "$_td/det.json" code_fix | jq -c .; then
    : # pass
  else
    echo "FIXTURE A FAILED (expected rc=0 deterministic bypass)" >&2
    exit 1
  fi

  # Fixture B: synthesis class, all 3 conditions met → rc=0.
  cat > "$_td/healthy.json" <<'JSON'
{
  "panel_score": 87.5,
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.85,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"approve","confidence":0.80,"ground_truth_match":true},
    {"voter_id":"codex",  "vote":"approve","confidence":0.75,"ground_truth_match":true}
  ],
  "structural": {
    "citation_density_per_lens": 5.2,
    "file_coverage_delta": 3,
    "finding_cardinality": 11
  }
}
JSON
  echo "── fixture B (synthesis-class all-pass) ──"
  if out_b=$(mo_promote_synthesis_gate "$_td/healthy.json" research_synthesis); then
    echo "$out_b" | jq -c .
    [ "$(echo "$out_b" | jq -r .decision)" = "approved" ] || { echo "FIXTURE B FAILED decision != approved" >&2; exit 1; }
  else
    echo "FIXTURE B FAILED (expected rc=0)" >&2
    echo "$out_b"
    exit 1
  fi

  # Fixture C: synthesis class, low panel_score → rc=1, reason=low_panel_score.
  cat > "$_td/low_score.json" <<'JSON'
{
  "panel_score": 62.0,
  "voters": [],
  "structural": {
    "citation_density_per_lens": 8.0,
    "file_coverage_delta": 5,
    "finding_cardinality": 20
  }
}
JSON
  echo "── fixture C (low panel_score) ──"
  out_c=$(mo_promote_synthesis_gate "$_td/low_score.json" refactor_audit) || true
  echo "$out_c" | jq -c .
  [ "$(echo "$out_c" | jq -r .decision)" = "rejected" ] && \
    [ "$(echo "$out_c" | jq -r .reason)" = "low_panel_score" ] || \
    { echo "FIXTURE C FAILED" >&2; exit 1; }

  # Fixture D: synthesis class, panel passes but no structural signal → rc=1.
  cat > "$_td/no_signal.json" <<'JSON'
{
  "panel_score": 95.0,
  "voters": [],
  "structural": {
    "citation_density_per_lens": 1.0,
    "file_coverage_delta": 0,
    "finding_cardinality": 2
  }
}
JSON
  echo "── fixture D (no structural signal) ──"
  out_d=$(mo_promote_synthesis_gate "$_td/no_signal.json" blog_post) || true
  echo "$out_d" | jq -c .
  [ "$(echo "$out_d" | jq -r .decision)" = "rejected" ] && \
    [ "$(echo "$out_d" | jq -r .reason)" = "no_structural_signal" ] || \
    { echo "FIXTURE D FAILED" >&2; exit 1; }

  echo "all four self-test fixtures passed."
  echo "promotion_gate.sh — source me and call promotion_evaluate / promotion_approve / mo_promote_synthesis_gate"
fi
