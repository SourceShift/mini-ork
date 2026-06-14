#!/usr/bin/env bash
# verifiers/writer-completeness.sh - verify writer fanout + critique matrix coverage.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "writer-completeness", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).
#
# Asserts:
#   1. All 4 writer-lens drafts exist + non-empty
#   2. All 4 critique cells exist + each is valid chapter-review.json shape
#   3. Selection JSON exists + points at one of the 4 lenses
#   4. Selected draft is the one whose markdown appears in final_markdown
#      (OR matches revisions[0].before_markdown if revisions present)
#   5. cost_summary.sandbox_spawns matches observed (drafts.length +
#      critique_cells.length + 1 selector + revisions.length)

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-writer-completeness.log"
exec 3>"$EVIDENCE"

COMPOUND="$RUN_DIR/chapter-compound.json"
DRAFT_GLM="$RUN_DIR/drafts/draft-glm.md"
DRAFT_KIMI="$RUN_DIR/drafts/draft-kimi.md"
DRAFT_CODEX="$RUN_DIR/drafts/draft-codex.md"
DRAFT_OPUS="$RUN_DIR/drafts/draft-opus.md"
CRIT_GLM="$RUN_DIR/critiques/critique-glm.json"
CRIT_KIMI="$RUN_DIR/critiques/critique-kimi.json"
CRIT_CODEX="$RUN_DIR/critiques/critique-codex.json"
CRIT_OPUS="$RUN_DIR/critiques/critique-opus.json"
SEL="$RUN_DIR/selection.json"

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

_all_drafts_exist_nonempty() {
  for f in "$DRAFT_GLM" "$DRAFT_KIMI" "$DRAFT_CODEX" "$DRAFT_OPUS"; do
    if [ ! -s "$f" ]; then
      echo "missing or empty: $f"
      return 1
    fi
  done
  return 0
}

_all_critique_cells_valid_json() {
  python3 - "$CRIT_GLM" "$CRIT_KIMI" "$CRIT_CODEX" "$CRIT_OPUS" <<'PY'
import json, sys
REQUIRED = {"schema_version", "chapter_title", "panel", "axes",
            "fragment_suggestions", "overall_verdict", "summary",
            "panel_disagreement_score"}
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception as e:
        sys.stderr.write(f"critique cell parse fail at {path}: {e}\n"); sys.exit(1)
    missing = REQUIRED - set(d.keys())
    if missing:
        sys.stderr.write(f"critique cell {path} missing {sorted(missing)}\n"); sys.exit(1)
    if d.get("schema_version") != "1.0.0":
        sys.stderr.write(f"critique cell {path} schema_version != 1.0.0\n"); sys.exit(1)
PY
}

_selection_points_at_a_lens() {
  python3 - "$SEL" <<'PY'
import json, sys
LENSES = {"glm", "kimi", "codex", "opus"}
with open(sys.argv[1]) as f:
    s = json.load(f)
if s.get("selected_lens") not in LENSES:
    sys.stderr.write(f"selection.selected_lens not in 4 lenses\n"); sys.exit(1)
if s.get("selection_strategy") != "deterministic_weighted":
    sys.stderr.write(f"selection.selection_strategy not deterministic_weighted\n"); sys.exit(1)
PY
}

_sandbox_spawn_count_consistent() {
  python3 - "$COMPOUND" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
drafts_n   = len(d.get("drafts", []))
critiques_n= len(d.get("critique_cells", []))
revs_n     = len(d.get("revisions", []))
# selector + N revise iterations + 1 publisher prep
# expected: drafts + critiques + 1 selector + revs + 1 prep
expected_min = drafts_n + critiques_n + 1 + max(revs_n, 0) + 1
spawns = d.get("cost_summary", {}).get("sandbox_spawns", 0)
if spawns < expected_min - 2:
    sys.stderr.write(f"sandbox_spawns={spawns} less than expected_min={expected_min}\n"); sys.exit(1)
PY
}

_final_markdown_matches_a_draft_or_revision() {
  python3 - "$COMPOUND" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
final = d.get("final_markdown", "")
revs = d.get("revisions", [])
drafts = d.get("drafts", [])
selected_lens = d.get("selection", {}).get("selected_lens", "")
if revs:
    last = revs[-1].get("after_markdown", "")
    if last == final:
        return
    sys.stderr.write("final_markdown != revisions[-1].after_markdown\n"); sys.exit(1)
else:
    for dr in drafts:
        if dr.get("lens") == selected_lens and dr.get("markdown") == final:
            return
    sys.stderr.write(f"no draft for selected_lens={selected_lens} matches final_markdown\n"); sys.exit(1)
PY
}

# Run all checks
_check "W1" "all 4 writer drafts exist + non-empty"          "_all_drafts_exist_nonempty"
_check "W2" "all 4 critique cells parse + valid chapter-review shape"  "_all_critique_cells_valid_json"
_check "W3" "selection.json points at one of the 4 lenses"   "_selection_points_at_a_lens"
_check "W4" "sandbox_spawn count consistent with drafts+critiques+revs" "_sandbox_spawn_count_consistent"
_check "W5" "final_markdown matches selected draft OR last revision"    "_final_markdown_matches_a_draft_or_revision"

exec 3>&-

if [ ${#failed_checks[@]} -eq 0 ]; then
  pass="true"
else
  pass="false"
fi

python3 - "$pass" "$EVIDENCE" "${checks_run[*]}" "${failed_checks[*]}" <<'PY'
import json, sys
pass_str, evidence, checks_run_str, failed_str = sys.argv[1:5]
out = {
    "verifier": "writer-completeness",
    "pass": pass_str == "true",
    "evidence_path": evidence,
    "checks_run": checks_run_str.split() if checks_run_str else [],
    "failed_checks": failed_str.split() if failed_str else [],
}
print(json.dumps(out))
PY
