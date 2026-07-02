#!/usr/bin/env bash
# tests/unit/test_reviewer_input_assembly.sh — D-RIA (2026-07-02).
#
# Covers the reviewer-input assembly step in bin/mini-ork-execute:
#
#   1. _mo_assemble_reviewer_inputs writes $RUN_DIR/review-diff.patch
#      (non-empty, contains the implementer's diff) before the reviewer
#      node is dispatched.
#   2. The function returns a context block that embeds the implementer
#      summary, BOTH verifier JSONs, and the diff inline.
#   3. When the four artifacts are present, the assembled block does NOT
#      take the "missing inputs" abstention path — the reviewer's hard-
#      abstain instruction fires only when BOTH the diff and the summary
#      are absent (genuine no-op).
#   4. Missing inputs degrade to "(not available)" placeholders, not
#      errors or empty blocks.
#
# Also performs two static structural checks on bin/mini-ork-execute:
#
#   A. The verifier handler persists verifier_<vstem>.json to $RUN_DIR
#      (not just the timestamped evidence log).
#   B. The reviewer handler (classic path) invokes _mo_assemble_reviewer_inputs
#      and embeds the block in PROMPT_CONTENT before the JSON envelope.
#
# Filename ends in .sh so pytest's default discovery skips it.
# Run with: bash tests/unit/test_reviewer_input_assembly.sh
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
EXECUTOR="$MINI_ORK_ROOT/bin/mini-ork-execute"
PROMPT="$MINI_ORK_ROOT/recipes/code-fix/prompts/reviewer.md"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

WORKSPACE=""
cleanup() {
  if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
    rm -rf "$WORKSPACE"
  fi
}

seed_repo_with_change() {
  local repo="$1"
  rm -rf "$repo"
  mkdir -p "$repo"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email "tester@local"
  git -C "$repo" config user.name  "tester"
  git -C "$repo" config commit.gpgsign false
  printf 'tracked line one\ntracked line two\n' > "$repo/seed.md"
  git -C "$repo" add seed.md
  git -C "$repo" commit -q -m "initial"
  # Modify seed.md so the diff is non-empty + reproducible.
  printf 'tracked line one\ntracked line two\nimplementer-added-line three\n' > "$repo/seed.md"
}

seed_run_dir() {
  local run_dir="$1" repo="$2"
  rm -rf "$run_dir"
  mkdir -p "$run_dir"
  # Implementer summary (hyphen form — what the implementer actually writes).
  python3 - "$run_dir" "$repo" <<'PY' 2>/dev/null
import json, sys, os
run_dir = sys.argv[1]
repo    = sys.argv[2]
summary = {
    "files_changed": ["seed.md"],
    "rationale":     "Test edit: append a third line to seed.md.",
    "confidence":    0.8,
    "scope_violations": [],
    "skipped_steps":  [],
    "notes":         "",
    "worktree_path": repo,
}
with open(os.path.join(run_dir, "implementer-summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)
# Verifier verdicts — pass:true for both (pretty-printed so the assembled
# block matches what real verifier JSONs look like).
for stem in ("typecheck", "test"):
    payload = {
        "verifier":      stem,
        "pass":          True,
        "evidence_path": "/tmp/{0}.log".format(stem),
        "error_summary": "",
    }
    with open(os.path.join(run_dir, "verifier_" + stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
PY
}

echo ""
echo "── unit: bin/mini-ork-execute _mo_assemble_reviewer_inputs ──"

if [ ! -f "$EXECUTOR" ]; then
  _skip "bin/mini-ork-execute missing"
else
  WORKSPACE="$(mktemp -d /tmp/mo-reviewer-input-XXXXXX)"
  trap cleanup EXIT

  if ! grep -q '^_mo_assemble_reviewer_inputs()' "$EXECUTOR"; then
    _skip "_mo_assemble_reviewer_inputs not defined in $EXECUTOR — D-RIA edit missing"
  else
    set +e
    export MINI_ORK_EXECUTE_SOURCE_ONLY=1
    # shellcheck source=/dev/null
    source "$EXECUTOR" >/dev/null 2>&1
    if ! declare -f _mo_assemble_reviewer_inputs >/dev/null; then
      _skip "_mo_assemble_reviewer_inputs not loaded by source-only mode"
    else

      # ── (a) positive: all 4 inputs present → block contains all of them ──
      echo ""
      echo "--- (a) positive: summary + both verifiers + diff present ---"
      REPO_A="$WORKSPACE/a-repo"
      RUN_A="$WORKSPACE/a-run"
      seed_repo_with_change "$REPO_A"
      seed_run_dir      "$RUN_A" "$REPO_A"

      BLOCK_A="$(_mo_assemble_reviewer_inputs "$RUN_A" 2>/dev/null)"
      DIFF_A="$RUN_A/review-diff.patch"

      ok=1
      if [ ! -s "$DIFF_A" ]; then
        echo "    review-diff.patch missing or empty at $DIFF_A"
        ok=0
      fi
      if ! grep -q '^--- a/seed.md' "$DIFF_A" 2>/dev/null \
         && ! grep -q '^diff --git a/seed.md' "$DIFF_A" 2>/dev/null; then
        echo "    review-diff.patch does not contain the seed.md diff"
        echo "    first 200 bytes: $(head -c 200 "$DIFF_A")"
        ok=0
      fi
      if ! grep -q 'implementer-added-line three' "$DIFF_A" 2>/dev/null; then
        echo "    review-diff.patch missing the implementer-added line"
        ok=0
      fi
      for needle in \
          '--- Reviewer inputs (assembled by mini-ork-execute) ---' \
          '# implementer-summary.json' \
          'Test edit: append a third line to seed.md.' \
          '"confidence": 0.8' \
          '# verifier_typecheck.json' \
          '"verifier": "typecheck"' \
          '"pass": true' \
          '# verifier_test.json' \
          '"verifier": "test"' \
          '# review-diff.patch' \
          'implementer-added-line three' \
          '--- End reviewer inputs ---' \
          'REVIEWER NOTE'; do
        if ! printf '%s' "$BLOCK_A" | grep -Fq -- "$needle"; then
          echo "    assembled block missing needle: $needle"
          ok=0
        fi
      done
      # The hard-abstain-only branch is gated on BOTH diff+summary missing —
      # here both are present, so the assembly must NOT trigger that branch.
      # (No structural distinction in the block today, but we assert the block
      # was actually populated.)
      if [ -z "$BLOCK_A" ]; then
        echo "    assembled block is empty"
        ok=0
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(a) all four inputs assembled into context block + diff artifact non-empty"
      else
        _fail "(a) positive assembly"
      fi

      # ── (b) degraded: missing summary → "(not available)" placeholder, no crash ──
      echo ""
      echo "--- (b) degraded: summary absent → '(not available)' placeholder ---"
      REPO_B="$WORKSPACE/b-repo"
      RUN_B="$WORKSPACE/b-run"
      seed_repo_with_change "$REPO_B"
      mkdir -p "$RUN_B"
      # Verifier verdicts present but no implementer-summary.json.
      for stem in typecheck test; do
        printf '{"verifier":"%s","pass":true}\n' "$stem" > "$RUN_B/verifier_${stem}.json"
      done

      BLOCK_B="$(_mo_assemble_reviewer_inputs "$RUN_B" 2>/dev/null)"
      DIFF_B="$RUN_B/review-diff.patch"

      ok=1
      if ! printf '%s' "$BLOCK_B" | grep -Fq '# implementer-summary.json'; then
        echo "    block missing summary header"
        ok=0
      fi
      if ! printf '%s' "$BLOCK_B" | grep -Fq '(not available)'; then
        echo "    block missing '(not available)' placeholder"
        ok=0
      fi
      if [ ! -e "$DIFF_B" ]; then
        echo "    review-diff.patch should still be written (empty is OK)"
        ok=0
      fi
      # Both verifier verdicts still rendered.
      for stem in typecheck test; do
        if ! printf '%s' "$BLOCK_B" | grep -Fq "# verifier_${stem}.json"; then
          echo "    block missing # verifier_${stem}.json header"
          ok=0
        fi
      done
      if [ "$ok" -eq 1 ]; then
        _ok "(b) degraded: summary missing → '(not available)' + other inputs still present"
      else
        _fail "(b) degraded assembly"
      fi

      # ── (c) genuine no-op: BOTH diff and summary missing → empty diff is acceptable ──
      echo ""
      echo "--- (c) genuine no-op: BOTH diff + summary missing → empty inputs are tolerated ---"
      RUN_C="$WORKSPACE/c-run"
      mkdir -p "$RUN_C"
      # Nothing at all in RUN_C.
      BLOCK_C="$(_mo_assemble_reviewer_inputs "$RUN_C" 2>/dev/null)"
      DIFF_C="$RUN_C/review-diff.patch"

      ok=1
      # Both diff and summary should report missing in the block.
      if ! printf '%s' "$BLOCK_C" | grep -Fq '(no diff)'; then
        echo "    block missing '(no diff)' marker when no worktree"
        ok=0
      fi
      if ! printf '%s' "$BLOCK_C" | grep -Fq '(not available)'; then
        echo "    block missing '(not available)' placeholder for summary"
        ok=0
      fi
      if [ -z "$BLOCK_C" ]; then
        echo "    block is empty even when nothing is available — must still emit instructions"
        ok=0
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(c) genuine no-op: empty inputs tolerated + '(no diff)' marker written"
      else
        _fail "(c) no-op assembly"
      fi

      # ── (d) reviewer prompt contract reconciliation ──
      echo ""
      echo "--- (d) reviewer prompt references hyphen-form summary name ---"
      ok=1
      if [ ! -f "$PROMPT" ]; then
        echo "    $PROMPT missing"
        ok=0
      else
        # The reviewer prompt should now reference implementer-summary.json
        # (matching what the implementer actually writes) — and explain that
        # the inputs are pre-assembled into the prompt by the executor.
        if ! grep -q 'implementer-summary.json' "$PROMPT"; then
          echo "    reviewer prompt still references the wrong summary filename"
          ok=0
        fi
        if grep -q 'implementer_summary\.json' "$PROMPT"; then
          echo "    reviewer prompt still has the underscore-form reference"
          ok=0
        fi
        if ! grep -q 'pre-assembled for you' "$PROMPT"; then
          echo "    reviewer prompt missing the 'pre-assembled' hint to the LLM"
          ok=0
        fi
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(d) reviewer prompt name reconciliation"
      else
        _fail "(d) reviewer prompt reconciliation"
      fi

      # ── (e) static structural checks on the executor wiring ──
      echo ""
      echo "--- (e) static wiring checks on bin/mini-ork-execute ---"
      ok=1
      # E1. verifier-persist: cp of evidence → verifier_<vstem>.json
      if ! grep -q 'verifier_${_vstem}.json' "$EXECUTOR"; then
        echo "    executor no longer writes verifier_<vstem>.json — D-RIA persist edit missing"
        ok=0
      fi
      # E2. reviewer handler calls the assembler before the JSON envelope.
      if ! grep -q '_mo_assemble_reviewer_inputs' "$EXECUTOR"; then
        echo "    executor no longer calls _mo_assemble_reviewer_inputs"
        ok=0
      fi
      if ! grep -q 'D-RIA' "$EXECUTOR"; then
        echo "    executor missing D-RIA marker comment (regression tripwire)"
        ok=0
      fi
      # E3. The persistence must happen on BOTH pass and fail paths (the cp
      #     is OUTSIDE the if/else). Guard against future refactors that
      #     move it back inside the success branch.
      _persist_line=$(grep -n 'verifier_${_vstem}.json' "$EXECUTOR" | head -1 | cut -d: -f1)
      _fail_line=$(grep -n 'verifier_ref failed' "$EXECUTOR" | head -1 | cut -d: -f1)
      if [ -n "$_persist_line" ] && [ -n "$_fail_line" ]; then
        if [ "$_persist_line" -lt "$_fail_line" ]; then
          : # persist appears BEFORE the failure log; that means it runs on
          #   BOTH paths. Good.
          :
        else
          echo "    verifier-persist line ($_persist_line) appears AFTER the failure log ($_fail_line) — may be inside the success branch only"
          ok=0
        fi
      fi
      if [ "$ok" -eq 1 ]; then
        _ok "(e) executor wiring (verifier-persist + reviewer assemble + D-RIA marker)"
      else
        _fail "(e) executor wiring"
      fi
    fi
  fi
fi

echo ""
echo "── unit summary: PASS=$PASS FAIL=$FAIL SKIP=$SKIP ──"
[ "$FAIL" -eq 0 ]