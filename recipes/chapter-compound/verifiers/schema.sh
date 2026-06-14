#!/usr/bin/env bash
# verifiers/schema.sh - verify chapter-compound.json syntax and strict shape.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "schema", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).
#
# Schema source-of-truth: libwit/shared/types/chapterCompound.ts:isChapterCompoundJson
# Drift between this verifier and the TS guard will cause silent persistence
# of malformed artifacts — keep both in lockstep.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-schema.log"
exec 3>"$EVIDENCE"

TARGET="$RUN_DIR/chapter-compound.json"

checks_run=()
failed_checks=()

_check() {
  local id="$1" expr_desc="$2" cond="$3"
  checks_run+=("$id")
  echo "[$id] $expr_desc" >&3
  if eval "$cond" >&3 2>&1; then
    echo "  ok" >&3
  else
    echo "  FAIL" >&3
    failed_checks+=("$id")
  fi
}

_file_exists() {
  [ -f "$TARGET" ]
}

_json_parses() {
  python3 - "$TARGET" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    json.load(f)
PY
}

_top_level_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys

REQUIRED_TOP = {
    "schema_version", "chapter_title", "chapter_number",
    "drafts", "critique_cells", "selection", "revisions",
    "final_markdown", "revised", "cost_summary", "total_duration_ms",
}

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

missing = REQUIRED_TOP - set(data.keys())
if missing:
    sys.stderr.write(f"missing top-level keys: {sorted(missing)}\n")
    sys.exit(1)

if data["schema_version"] != "1.0.0":
    sys.stderr.write(f"schema_version must be '1.0.0', got {data['schema_version']!r}\n")
    sys.exit(1)

if not isinstance(data["chapter_title"], str) or not data["chapter_title"].strip():
    sys.stderr.write("chapter_title must be non-empty string\n")
    sys.exit(1)

cn = data["chapter_number"]
if not isinstance(cn, int) or cn < 1:
    sys.stderr.write(f"chapter_number must be int >= 1, got {cn!r}\n")
    sys.exit(1)
PY
}

_drafts_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys
LENSES = {"glm", "kimi", "codex", "opus"}
with open(sys.argv[1]) as f:
    data = json.load(f)
drafts = data.get("drafts", [])
if not isinstance(drafts, list) or len(drafts) == 0:
    sys.stderr.write("drafts must be non-empty list\n"); sys.exit(1)
for d in drafts:
    if not isinstance(d, dict):
        sys.stderr.write(f"draft not dict: {d!r}\n"); sys.exit(1)
    if d.get("lens") not in LENSES:
        sys.stderr.write(f"draft.lens invalid: {d.get('lens')!r}\n"); sys.exit(1)
    if not isinstance(d.get("markdown"), str):
        sys.stderr.write("draft.markdown not str\n"); sys.exit(1)
    for k in ("bytes", "duration_ms", "cost_usd"):
        v = d.get(k)
        if not isinstance(v, (int, float)) or v < 0:
            sys.stderr.write(f"draft.{k} not non-neg number: {v!r}\n"); sys.exit(1)
    if not isinstance(d.get("accepted_for_review"), bool):
        sys.stderr.write("draft.accepted_for_review not bool\n"); sys.exit(1)
PY
}

_critique_cells_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys
LENSES = {"glm", "kimi", "codex", "opus"}
REVIEW_REQUIRED = {"schema_version", "chapter_title", "panel", "axes",
                   "fragment_suggestions", "overall_verdict", "summary",
                   "panel_disagreement_score"}
with open(sys.argv[1]) as f:
    data = json.load(f)
cells = data.get("critique_cells", [])
if not isinstance(cells, list):
    sys.stderr.write("critique_cells must be list\n"); sys.exit(1)
for c in cells:
    if c.get("writer_lens") not in LENSES:
        sys.stderr.write(f"cell.writer_lens invalid\n"); sys.exit(1)
    crit = c.get("critique", {})
    missing = REVIEW_REQUIRED - set(crit.keys())
    if missing:
        sys.stderr.write(f"cell.critique missing keys: {sorted(missing)}\n"); sys.exit(1)
    if crit.get("schema_version") != "1.0.0":
        sys.stderr.write("cell.critique.schema_version must be '1.0.0'\n"); sys.exit(1)
PY
}

_selection_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys
LENSES = {"glm", "kimi", "codex", "opus"}
with open(sys.argv[1]) as f:
    data = json.load(f)
sel = data.get("selection", {})
if sel.get("selected_lens") not in LENSES:
    sys.stderr.write(f"selection.selected_lens invalid: {sel.get('selected_lens')!r}\n"); sys.exit(1)
if sel.get("selection_strategy") != "deterministic_weighted":
    sys.stderr.write("selection.selection_strategy must be 'deterministic_weighted'\n"); sys.exit(1)
if not isinstance(sel.get("rationale"), str) or not sel["rationale"].strip():
    sys.stderr.write("selection.rationale must be non-empty\n"); sys.exit(1)
cs = sel.get("candidate_scores", [])
if not isinstance(cs, list):
    sys.stderr.write("selection.candidate_scores not list\n"); sys.exit(1)
for c in cs:
    if c.get("lens") not in LENSES:
        sys.stderr.write(f"candidate.lens invalid: {c.get('lens')!r}\n"); sys.exit(1)
PY
}

_final_markdown_present() {
  python3 - "$TARGET" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
fm = data.get("final_markdown", "")
if not isinstance(fm, str) or len(fm) == 0:
    sys.stderr.write("final_markdown must be non-empty string\n"); sys.exit(1)
if not isinstance(data.get("revised"), bool):
    sys.stderr.write("revised must be bool\n"); sys.exit(1)
PY
}

_cost_summary_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys
REQUIRED = {"total_usd", "writer_usd", "critique_usd", "selection_usd",
            "revise_usd", "sandbox_spawns"}
with open(sys.argv[1]) as f:
    data = json.load(f)
cs = data.get("cost_summary", {})
missing = REQUIRED - set(cs.keys())
if missing:
    sys.stderr.write(f"cost_summary missing: {sorted(missing)}\n"); sys.exit(1)
for k, v in cs.items():
    if not isinstance(v, (int, float)) or v < 0:
        sys.stderr.write(f"cost_summary.{k} not non-neg number: {v!r}\n"); sys.exit(1)
td = data.get("total_duration_ms")
if not isinstance(td, (int, float)) or td < 0:
    sys.stderr.write(f"total_duration_ms not non-neg number: {td!r}\n"); sys.exit(1)
PY
}

# Run all checks
_check "S1" "chapter-compound.json exists"        "_file_exists"
_check "S2" "chapter-compound.json parses as JSON" "_json_parses"
_check "S3" "top-level shape + schema_version + chapter_title/_number" "_top_level_shape"
_check "S4" "drafts array shape (lens + markdown + bytes + costs)" "_drafts_shape"
_check "S5" "critique_cells nest valid ChapterReviewJson"  "_critique_cells_shape"
_check "S6" "selection shape (deterministic_weighted + lens + candidates)" "_selection_shape"
_check "S7" "final_markdown non-empty + revised bool"      "_final_markdown_present"
_check "S8" "cost_summary 6 fields + total_duration_ms"     "_cost_summary_shape"

exec 3>&-

if [ ${#failed_checks[@]} -eq 0 ]; then
  pass="true"
else
  pass="false"
fi

# Emit JSON result
python3 - "$pass" "$EVIDENCE" "${checks_run[*]}" "${failed_checks[*]}" <<'PY'
import json, sys
pass_str, evidence, checks_run_str, failed_str = sys.argv[1:5]
out = {
    "verifier": "schema",
    "pass": pass_str == "true",
    "evidence_path": evidence,
    "checks_run": checks_run_str.split() if checks_run_str else [],
    "failed_checks": failed_str.split() if failed_str else [],
}
print(json.dumps(out))
PY
