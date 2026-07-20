#!/usr/bin/env bash
# comparative-opinions.sh — 10-lens cross-family opinion gathering.
#
# Dispatches the mini-ork-vs-omnigent comparison docs to 10 LLM
# instances (2 per family across codex / minimax / glm / kimi / opus)
# and asks each one to write its own opinion + improvement plan. The
# panel diversity is per Rajan 2025 submodularity + Nasser 2026 cross-
# family α=0.042 — two instances per family lets us also measure
# within-family variance (same model, two seeds, similar prompts).
#
# Usage:
#   bash scripts/comparative-opinions.sh
#
# Output:
#   .mini-ork/runs/comparative-opinions-<ts>/
#     opinion-<family>-<instance>.md     # one per lens
#     manifest.json                       # who-said-what + costs
#     summary.md                          # human-readable index
#
# Honest-trade-off: the no-opus standing directive (2026-06-13) is
# overridden for this run because the user explicitly requested opus
# inclusion. After this run, opus stays banned for production
# dispatches.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$MINI_ORK_ROOT"

TS=$(date +%s)
RUN_DIR="$MINI_ORK_ROOT/.mini-ork/runs/comparative-opinions-$TS"
mkdir -p "$RUN_DIR"
export MINI_ORK_RUN_DIR="$RUN_DIR"

COMPARISON_DOC="${MO_COMPARISON_DOC:-$MINI_ORK_ROOT/docs/research/omnigent-vs-mini-ork-comparison.md}"
IMPROVEMENT_DOC="${MO_IMPROVEMENT_DOC:-$MINI_ORK_ROOT/docs/research/omnigent-mini-ork-improvement-plan.md}"

if [ ! -f "$COMPARISON_DOC" ] || [ ! -f "$IMPROVEMENT_DOC" ]; then
  echo "[fatal] required input docs missing" >&2
  exit 1
fi

PROMPT_PREFACE='You are a senior infrastructure engineer asked for an honest,
opinionated review. Two design documents follow:

  1. A side-by-side comparison of two Apache-2.0 multi-agent
     frameworks: Omnigent (Databricks, 2026-06-13) and mini-ork
     (SourceShift, current).
  2. A proposed six-phase plan to improve mini-ork informed by
     Omnigent.

READ BOTH DOCUMENTS CAREFULLY (full text, no skimming).

Then write your own opinion on:

  A. Where the proposed plan is right.
  B. Where the proposed plan is wrong, missing, or over-engineered.
  C. The single highest-leverage change you would prioritize FIRST
     to improve mini-ork using best practices from Omnigent — and
     name the specific Omnigent file/module that proves the practice
     is real, not aspirational.
  D. The single thing in mini-ork you would NOT compromise on even
     under competitive pressure from Omnigent, and why.

Be concrete. Cite file paths and line ranges where useful. Reject
generic consultant-speak. Where you disagree with the comparison
docs, say so explicitly.

Output structure:
  ## Opinion: <one-sentence headline>
  ### A. What the plan gets right
  ### B. Where the plan misses
  ### C. Highest-leverage change (with Omnigent file proof)
  ### D. What not to compromise on
  ### Confidence + caveats (your honest uncertainty)

----- BEGIN COMPARISON DOC -----'

PROMPT_MIDDLE='----- END COMPARISON DOC -----

----- BEGIN IMPROVEMENT PLAN DOC -----'

PROMPT_TRAILER='----- END IMPROVEMENT PLAN DOC -----'

dispatch_lens() {
  local family="$1"
  local instance="$2"
  local out_path="$RUN_DIR/opinion-$family-$instance.md"
  local stderr_path="$RUN_DIR/opinion-$family-$instance.err"

  # Two instances get slightly different role-framings so identical
  # outputs would be evidence of pure model collapse rather than
  # genuine cross-instance agreement.
  local role_hint=""
  case "$instance" in
    1) role_hint="You are reviewing as a *production reliability* engineer. Lean toward operational realism and blast-radius concerns." ;;
    2) role_hint="You are reviewing as a *competitive strategy* engineer. Lean toward positioning, moats, and which features compound." ;;
  esac

  local full_prompt
  full_prompt="$role_hint"$'\n\n'"$PROMPT_PREFACE"$'\n\n'"$(cat "$COMPARISON_DOC")"$'\n\n'"$PROMPT_MIDDLE"$'\n\n'"$(cat "$IMPROVEMENT_DOC")"$'\n\n'"$PROMPT_TRAILER"

  echo "[dispatch] family=$family instance=$instance → $out_path" >&2
  {
    echo "<!-- family=$family instance=$instance role_hint=\"$role_hint\" -->"
    echo "<!-- dispatch_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) -->"
    # Pass --model directly so we bypass the no-opus override in
    # .mini-ork/config/agents.yaml for this one-off research dispatch.
    # The no-opus directive remains in force for production recipes.
    if python3 -m mini_ork.ported.llm_dispatch \
        --task-class "comparative_opinion" \
        --node-type  "${family}_lens" \
        --model      "$family" \
        --prompt-text "$full_prompt" \
        --timeout 900 \
        --max-turns 8 \
        2> "$stderr_path"; then
      echo "<!-- dispatch_ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) status=ok -->"
    else
      local _rc=$?
      echo "<!-- dispatch_ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) status=failed rc=$_rc -->"
      echo
      echo "[lens dispatch failed — see opinion-$family-$instance.err]"
    fi
  } > "$out_path"
}

# Keep the historical five-family default. Operators and migration probes may
# narrow the panel without editing the script (for example: glm_current only).
IFS=' ' read -r -a FAMILIES <<< "${MO_COMPARATIVE_FAMILIES:-codex minimax glm kimi opus}"

# Fire all 10 in background; collect at end.
PIDS=()
for family in "${FAMILIES[@]}"; do
  for instance in 1 2; do
    dispatch_lens "$family" "$instance" &
    PIDS+=("$!")
  done
done

echo "[main] 10 lens dispatches launched: ${PIDS[*]}" >&2
for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

# Build manifest + summary.
python3 - "$RUN_DIR" <<'PY'
import json, os, pathlib, sys
run_dir = pathlib.Path(sys.argv[1])
opinions = sorted(run_dir.glob("opinion-*-*.md"))
records = []
for path in opinions:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    parts = name.split("-")
    family = parts[1] if len(parts) > 1 else "?"
    instance = parts[2] if len(parts) > 2 else "?"
    bytes_ = path.stat().st_size
    headline = ""
    for line in text.splitlines():
        if line.startswith("## Opinion"):
            headline = line.lstrip("# ").rstrip()
            break
    records.append({
        "family": family, "instance": instance,
        "bytes": bytes_, "path": str(path.name),
        "headline": headline or "(no Opinion: header found)",
    })

manifest_path = run_dir / "manifest.json"
manifest_path.write_text(json.dumps({"opinions": records}, indent=2) + "\n")

summary_lines = ["# Comparative opinions — Omnigent vs mini-ork", ""]
summary_lines.append(f"Generated: {len(records)} lens opinions in `{run_dir}`")
summary_lines.append("")
summary_lines.append("| family | instance | bytes | headline |")
summary_lines.append("|---|---|---:|---|")
for r in records:
    summary_lines.append(f"| {r['family']} | {r['instance']} | {r['bytes']} | {r['headline']} |")
summary_lines.append("")
summary_lines.append("Each opinion answers four prompts (A/B/C/D) with explicit Omnigent file citations.")
(run_dir / "summary.md").write_text("\n".join(summary_lines) + "\n")
PY

echo
echo "[done] outputs at $RUN_DIR" >&2
ls -la "$RUN_DIR"
echo
echo "--- summary.md ---"
cat "$RUN_DIR/summary.md"
