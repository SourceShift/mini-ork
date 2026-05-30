#!/usr/bin/env bash
# memory.sh — shared-memory primitive for mini-ork v2+v3
#
# Wraps state.db reads/writes for the 14 memory tables added in
# migration 20260528-mini-orch-v2v3-shared-memory.sql.
#
# Design rules:
#   1. EVERY memory write captures a reflection (git SHA + timestamp +
#      reflected_substrate JSON). Read-side staleness detection is in
#      lib/reflect.sh (Phase 5.5).
#   2. JSON columns are passed/returned as raw JSON strings. Caller
#      handles JSON parsing.
#   3. cycle_id is a required input to most writes — passed by the
#      dispatcher. Default = "cycle-$(date +%Y%m%d-%H%M%S)".
#   4. via_gate defaults are baked into the table schema; callers can
#      override.
#   5. Parameter binding via python3 sqlite3 argv — no heredoc
#      interpolation (which collides with bash $$ PID expansion).

set -euo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
MO_CYCLE_ID="${MO_CYCLE_ID:-cycle-$(date +%Y%m%d-%H%M%S)}"

# ─── Internal helpers ────────────────────────────────────────────────

_mo_now() { date +%s; }

_mo_repo_root() {
  # Callers may set REPO_ROOT explicitly; fall back to git toplevel.
  if [ -n "${REPO_ROOT:-}" ]; then
    echo "$REPO_ROOT"
    return
  fi
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

_mo_git_head() {
  git -C "$(_mo_repo_root)" rev-parse HEAD 2>/dev/null || echo ""
}

# Capture a reflection JSON for an item's cited files.
# Args:
#   $1 — JSON array of cited "file:line" strings
# Echoes: reflected_substrate JSON
_mo_capture_reflection() {
  local cited_json="${1:-[]}"
  local head_sha
  head_sha="$(_mo_git_head)"
  local repo_root
  repo_root="$(_mo_repo_root)"

  python3 - "$cited_json" "$head_sha" "$repo_root" <<'PY'
import hashlib, json, os, subprocess, sys

cited_arg = sys.argv[1] if len(sys.argv) > 1 else "[]"
head_sha = sys.argv[2] if len(sys.argv) > 2 else ""
repo_root = sys.argv[3] if len(sys.argv) > 3 else "."

try:
    cited = json.loads(cited_arg)
    if not isinstance(cited, list):
        cited = []
except json.JSONDecodeError:
    cited = []

def xxh3_or_sha(path: str) -> str:
    abs_p = os.path.join(repo_root, path)
    try:
        with open(abs_p, "rb") as f:
            return "sha256:" + hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""

def fingerprint(path: str, line: int) -> str:
    abs_p = os.path.join(repo_root, path)
    try:
        with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if 0 < line <= len(lines):
            return lines[line - 1].strip()[:80]
    except OSError:
        pass
    return ""

def blame_sha(path: str, line: int) -> str:
    abs_p = os.path.join(repo_root, path)
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "blame", "-L", f"{line},{line}",
             "--porcelain", "--", path],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.split()[0][:16]
    except Exception:
        pass
    return ""

cited_files, seen = [], set()
for citation in cited:
    if not isinstance(citation, str) or ":" not in citation:
        continue
    path, _, line_str = citation.rpartition(":")
    try:
        line = int(line_str)
    except ValueError:
        continue
    if (path, line) in seen:
        continue
    seen.add((path, line))
    cited_files.append({
        "path": path,
        "content_hash_xxh3": xxh3_or_sha(path),
        "line_range_start": line,
        "line_range_end": line,
        "blame_sha_at_lines": blame_sha(path, line),
        "fingerprint_text": fingerprint(path, line),
    })

print(json.dumps({
    "cited_files": cited_files,
    "git_head_at_write": head_sha,
}))
PY
}

# ─── Public API — ARCH-SPECs ─────────────────────────────────────────

# mo_mem_put_arch_spec <arch_id> <feature> <title> <pre> <post> <verifier>
#                      [frame_json] [evidence_json] [info_gain]
mo_mem_put_arch_spec() {
  local arch_id="$1" feature="$2" title="$3"
  local pre="$4" post="$5" verifier="$6"
  local frame_json="${7:-[]}"
  local evidence_json="${8:-[]}"
  local info_gain="${9:-0.0}"

  local now reflection head
  now="$(_mo_now)"
  head="$(_mo_git_head)"
  reflection="$(_mo_capture_reflection "$evidence_json")"

  python3 - \
    "$STATE_DB" "$arch_id" "$feature" "$MO_CYCLE_ID" "$title" \
    "$pre" "$post" "$frame_json" "$info_gain" "$verifier" \
    "$evidence_json" "$head" "$reflection" "$now" \
    <<'PY'
import sqlite3, sys
(db, arch_id, feature, cycle_id, title, pre, post, frame_json,
 info_gain, verifier, evidence_json, head, reflection, now) = sys.argv[1:15]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO arch_specs (
    arch_id, feature, cycle_id, title, precondition, postcondition,
    frame_json, info_gain, verifier, evidence_for_pre, status,
    via_gate, reflection_at, reflection_sha, reflected_substrate,
    reflection_status, reflection_last_check, created_at, updated_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(arch_id) DO UPDATE SET
    title=excluded.title,
    precondition=excluded.precondition,
    postcondition=excluded.postcondition,
    frame_json=excluded.frame_json,
    info_gain=excluded.info_gain,
    verifier=excluded.verifier,
    evidence_for_pre=excluded.evidence_for_pre,
    reflection_at=excluded.reflection_at,
    reflection_sha=excluded.reflection_sha,
    reflected_substrate=excluded.reflected_substrate,
    reflection_status='fresh',
    reflection_last_check=excluded.reflection_last_check,
    updated_at=excluded.updated_at
""", (
    arch_id, feature, cycle_id, title, pre, post,
    frame_json, float(info_gain), verifier, evidence_json, "proposed",
    "architectural_decision_gate", int(now), head, reflection,
    "fresh", int(now), int(now), int(now),
))
con.commit()
con.close()
print(f"arch_spec put: {arch_id}", file=sys.stderr)
PY
}

# mo_mem_list_arch_specs <feature> [status]
mo_mem_list_arch_specs() {
  local feature="$1"
  local status="${2:-accepted}"
  sqlite3 -separator $'\t' "$STATE_DB" \
    "SELECT arch_id, title, info_gain, reflection_status, status
     FROM arch_specs
     WHERE feature='$feature' AND status='$status'
     ORDER BY info_gain DESC, arch_id ASC"
}

# mo_mem_get_arch_spec <arch_id> → JSON or "null"
mo_mem_get_arch_spec() {
  local arch_id="$1"
  python3 - "$STATE_DB" "$arch_id" <<'PY'
import sqlite3, json, sys
db, arch_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute("SELECT * FROM arch_specs WHERE arch_id=?", (arch_id,)).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# ─── Public API — NodeAnnotations (with content-hash cache) ──────────

# mo_mem_get_node_annotation <node_id> <content_hash> → JSON or "null"
mo_mem_get_node_annotation() {
  local node_id="$1"
  local content_hash="$2"
  python3 - "$STATE_DB" "$node_id" "$content_hash" <<'PY'
import sqlite3, json, sys
db, node_id, ch = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM node_annotations WHERE node_id=? AND content_hash=?",
    (node_id, ch),
).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# mo_mem_put_node_annotation <node_id> <file_path> <symbol> <content_hash>
#                            <task> <pre_state_json> <post_state_json>
#                            [mutating 0|1] [side_effects_json]
mo_mem_put_node_annotation() {
  local node_id="$1" file_path="$2" symbol="$3" content_hash="$4"
  local task="$5" pre_state="$6" post_state="$7"
  local mutating="${8:-0}"
  local side_effects="${9:-[]}"

  local now head reflection
  now="$(_mo_now)"
  head="$(_mo_git_head)"
  reflection="$(_mo_capture_reflection "[\"${file_path}:1\"]")"

  python3 - \
    "$STATE_DB" "$node_id" "$file_path" "$symbol" "$content_hash" \
    "$task" "$pre_state" "$post_state" "$mutating" "$side_effects" \
    "$now" "$MO_CYCLE_ID" "$head" "$reflection" \
    <<'PY'
import sqlite3, sys
(db, node_id, file_path, symbol, content_hash, task, pre_state, post_state,
 mutating, side_effects, now, cycle_id, head, reflection) = sys.argv[1:15]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO node_annotations (
    node_id, file_path, symbol_name, content_hash, task,
    pre_state_json, post_state_json, mutating, side_effects_json,
    annotated_at, annotated_by_cycle,
    via_gate, reflection_at, reflection_sha, reflected_substrate,
    reflection_status, reflection_last_check
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(node_id) DO UPDATE SET
    content_hash=excluded.content_hash,
    task=excluded.task,
    pre_state_json=excluded.pre_state_json,
    post_state_json=excluded.post_state_json,
    mutating=excluded.mutating,
    side_effects_json=excluded.side_effects_json,
    annotated_at=excluded.annotated_at,
    reflection_at=excluded.reflection_at,
    reflection_sha=excluded.reflection_sha,
    reflected_substrate=excluded.reflected_substrate,
    reflection_status='fresh',
    reflection_last_check=excluded.reflection_last_check
""", (
    node_id, file_path, symbol, content_hash,
    task, pre_state, post_state, int(mutating), side_effects,
    int(now), cycle_id,
    "verifier_run_gate", int(now), head, reflection,
    "fresh", int(now),
))
con.commit()
con.close()
print(f"node_annotation put: {node_id}", file=sys.stderr)
PY
}

# ─── Public API — Inspector runs (dual-inspector audit) ──────────────

# mo_mem_record_inspector_run <site> <prompt_hash>
#                              <opus_verdict_json> <codex_verdict_json>
#                              <opus_rc> <codex_rc>
#                              <agreement 0|1> <actions_diff_json>
#                              <final_verdict_json>
#                              [dur_opus] [dur_codex] [fallback_reason]
mo_mem_record_inspector_run() {
  local site="$1" prompt_hash="$2"
  local opus_v="$3" codex_v="$4"
  local opus_rc="$5" codex_rc="$6"
  local agreement="$7" actions_diff="${8:-null}" final_v="$9"
  local dur_opus="${10:-0}" dur_codex="${11:-0}"
  local fallback="${12:-}"

  local now
  now="$(_mo_now)"

  python3 - \
    "$STATE_DB" "$site" "$MO_CYCLE_ID" "$prompt_hash" \
    "$opus_v" "$codex_v" "$opus_rc" "$codex_rc" \
    "$agreement" "$actions_diff" "$final_v" \
    "$dur_opus" "$dur_codex" "$fallback" "$now" \
    <<'PY'
import sqlite3, sys
(db, site, cycle_id, prompt_hash, opus_v, codex_v, opus_rc, codex_rc,
 agreement, actions_diff, final_v, dur_opus, dur_codex, fallback, now) = sys.argv[1:16]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO inspector_runs (
    site, cycle_id, prompt_hash, opus_verdict_json, codex_verdict_json,
    opus_rc, codex_rc, agreement, actions_diff_json, final_verdict_json,
    fallback_reason, duration_ms_opus, duration_ms_codex, ran_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    site, cycle_id, prompt_hash,
    opus_v, codex_v,
    int(opus_rc), int(codex_rc), int(agreement),
    actions_diff if actions_diff != "null" else None,
    final_v,
    fallback if fallback else None,
    int(dur_opus), int(dur_codex), int(now),
))
con.commit()
con.close()
print(f"inspector_run recorded: site={site}, agreement={agreement}", file=sys.stderr)
PY
}

# ─── Public API — MODULE-PLAN candidates (Pareto-front) ─────────────

# mo_mem_put_module_plan <module_id> <candidate_id> <arch_id> <label>
#                        <files_touched> <new_files_json> <cohesion>
#                        <coupling> <files_score> <volatility>
#                        <frame_json> <is_recommended 0|1>
mo_mem_put_module_plan() {
  local module_id="$1" candidate_id="$2" arch_id="$3" label="$4"
  local files_touched="${5:-0}"
  local new_files_json="${6:-[]}"
  local cohesion="${7:-0.0}"
  local coupling="${8:-0.0}"
  local files_score="${9:-0.0}"
  local volatility="${10:-0.0}"
  local frame_json="${11:-[]}"
  local is_rec="${12:-0}"

  local now head reflection
  now="$(_mo_now)"
  head="$(_mo_git_head)"
  reflection="$(_mo_capture_reflection "$new_files_json")"

  python3 - \
    "$STATE_DB" "$module_id" "$candidate_id" "$arch_id" "$MO_CYCLE_ID" \
    "$label" "$files_touched" "$new_files_json" "$cohesion" "$coupling" \
    "$files_score" "$volatility" "$frame_json" "$is_rec" \
    "$now" "$head" "$reflection" \
    <<'PY'
import sqlite3, sys
(db, module_id, candidate_id, arch_id, cycle_id, label, files_touched,
 new_files_json, cohesion, coupling, files_score, volatility,
 frame_json, is_rec, now, head, reflection) = sys.argv[1:18]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO module_plans (
    module_id, candidate_id, arch_id, cycle_id, label, files_touched,
    new_files_json, cohesion_score, coupling_score, files_touched_score,
    volatility_score, frame_json, is_recommended, status,
    via_gate, reflection_at, reflection_sha, reflected_substrate,
    reflection_status, reflection_last_check, created_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(module_id, candidate_id) DO UPDATE SET
    cohesion_score=excluded.cohesion_score,
    coupling_score=excluded.coupling_score,
    is_recommended=excluded.is_recommended,
    reflection_at=excluded.reflection_at,
    reflection_sha=excluded.reflection_sha,
    reflected_substrate=excluded.reflected_substrate,
    reflection_status='fresh'
""", (
    module_id, candidate_id, arch_id, cycle_id, label, int(files_touched),
    new_files_json, float(cohesion), float(coupling), float(files_score),
    float(volatility), frame_json, int(is_rec), "proposed",
    "architectural_decision_gate", int(now), head, reflection,
    "fresh", int(now), int(now),
))
con.commit()
con.close()
print(f"module_plan put: {module_id}/{candidate_id}", file=sys.stderr)
PY
}

# mo_mem_list_module_plans <arch_id>
mo_mem_list_module_plans() {
  local arch_id="$1"
  sqlite3 -separator $'\t' "$STATE_DB" \
    "SELECT module_id, candidate_id, label, cohesion_score, coupling_score, is_recommended
     FROM module_plans
     WHERE arch_id='$arch_id'
     ORDER BY is_recommended DESC, cohesion_score DESC"
}

# ─── Public API — ATOM-PRs (DAG nodes) ───────────────────────────────

# mo_mem_put_atom_pr <pr_id> <module_id> <candidate_id> <title> <kind>
#                    <frame_json> <depends_on_json> <test_gate>
#                    [functoriality_check]
mo_mem_put_atom_pr() {
  local pr_id="$1" module_id="$2" candidate_id="${3:-}" title="$4" kind="$5"
  local frame_json="${6:-[]}" depends="${7:-[]}" test_gate="${8:-true}"
  local functor_check="${9:-true}"

  local now head reflection
  now="$(_mo_now)"
  head="$(_mo_git_head)"
  reflection="$(_mo_capture_reflection "$frame_json")"

  python3 - \
    "$STATE_DB" "$pr_id" "$module_id" "$candidate_id" "$MO_CYCLE_ID" \
    "$title" "$kind" "$frame_json" "$depends" "$test_gate" \
    "$functor_check" "$now" "$head" "$reflection" \
    <<'PY'
import sqlite3, sys
(db, pr_id, module_id, candidate_id, cycle_id, title, kind, frame_json,
 depends, test_gate, functor_check, now, head, reflection) = sys.argv[1:15]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO atom_prs (
    pr_id, module_id, candidate_id, cycle_id, title, kind,
    frame_json, depends_on_json, test_gate, functoriality_check, status,
    via_gate, reflection_at, reflection_sha, reflected_substrate,
    reflection_status, reflection_last_check, created_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(pr_id) DO UPDATE SET
    title=excluded.title,
    depends_on_json=excluded.depends_on_json,
    test_gate=excluded.test_gate,
    reflection_at=excluded.reflection_at,
    reflection_sha=excluded.reflection_sha,
    reflected_substrate=excluded.reflected_substrate,
    reflection_status='fresh'
""", (
    pr_id, module_id, candidate_id if candidate_id else None, cycle_id,
    title, kind, frame_json, depends, test_gate, functor_check, "pending",
    "artifact_committed_gate", int(now), head, reflection,
    "fresh", int(now), int(now),
))
con.commit()
con.close()
print(f"atom_pr put: {pr_id} kind={kind}", file=sys.stderr)
PY
}

# mo_mem_list_atom_prs <module_id> [status]
mo_mem_list_atom_prs() {
  local module_id="$1"
  local status="${2:-pending}"
  sqlite3 -separator $'\t' "$STATE_DB" \
    "SELECT pr_id, kind, title, status FROM atom_prs
     WHERE module_id='$module_id' AND status='$status'
     ORDER BY pr_id"
}

# ─── Public API — ADRs (durable architectural decisions) ─────────────

# mo_mem_put_adr <adr_id> <arch_id> <title> <pre> <post> <verifier> <body_md>
#                [supersedes]
mo_mem_put_adr() {
  local adr_id="$1" arch_id="$2" title="$3"
  local pre="$4" post="$5" verifier="$6" body="$7"
  local supersedes="${8:-}"

  local now head reflection
  now="$(_mo_now)"
  head="$(_mo_git_head)"
  reflection="$(_mo_capture_reflection "[]")"

  python3 - \
    "$STATE_DB" "$adr_id" "$arch_id" "$title" "$pre" "$post" \
    "$verifier" "$body" "$supersedes" "$MO_CYCLE_ID" \
    "$now" "$head" "$reflection" \
    <<'PY'
import sqlite3, sys
(db, adr_id, arch_id, title, pre, post, verifier, body, supersedes,
 cycle_id, now, head, reflection) = sys.argv[1:14]

con = sqlite3.connect(db)
con.execute("""
  INSERT INTO adrs (
    adr_id, arch_id, title, status, supersedes, precondition, postcondition,
    verifier, body_md,
    via_gate, reflection_at, reflection_sha, reflected_substrate,
    reflection_status, reflection_last_check, written_at, written_by_cycle
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(adr_id) DO UPDATE SET
    title=excluded.title,
    body_md=excluded.body_md,
    reflection_at=excluded.reflection_at,
    reflection_sha=excluded.reflection_sha,
    reflection_status='fresh'
""", (
    adr_id, arch_id if arch_id else None, title, "accepted",
    supersedes if supersedes else None,
    pre, post, verifier, body,
    "architectural_decision_gate", int(now), head, reflection,
    "fresh", int(now), int(now), cycle_id,
))
con.commit()
con.close()
print(f"adr put: {adr_id}", file=sys.stderr)
PY
}

# mo_mem_list_adrs [status]
mo_mem_list_adrs() {
  local status="${1:-accepted}"
  sqlite3 -separator $'\t' "$STATE_DB" \
    "SELECT adr_id, title, supersedes FROM adrs WHERE status='$status' ORDER BY adr_id"
}

# ─── Public API — health checks ──────────────────────────────────────

mo_mem_smoke() {
  local cnt
  cnt=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM (
    SELECT name FROM sqlite_master WHERE type='table' AND name IN (
      'arch_specs','module_plans','atom_prs','adrs','node_annotations',
      'communities','validations','fixes','cascade_invalidations',
      'reflection_log','decision_basins','decision_basin_membership',
      'emergent_patterns','inspector_runs'
    )
  )")
  if [[ "$cnt" == "14" ]]; then
    echo "OK — 14 memory tables present"
    return 0
  else
    echo "FAIL — expected 14 tables, found $cnt"
    return 1
  fi
}

# When invoked directly, run the smoke check.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  mo_mem_smoke
fi
