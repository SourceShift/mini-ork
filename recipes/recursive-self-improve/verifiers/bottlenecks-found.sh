#!/usr/bin/env bash
# verifiers/bottlenecks-found.sh — gate that the bottleneck scanner
# produced an actionable ranked list and the opus synthesizer ranked
# at least one patch.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout. Exit 0 always (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-bottlenecks-found.log"
exec 3>"$EVIDENCE"

missing=()

# Dispatcher names: researcher node `bottleneck_lens` → lens-bottleneck.md;
# `arxiv_lens` → lens-arxiv.md (per the _lens-suffix heuristic in
# bin/mini-ork-execute:410-415).
SCAN="$RUN_DIR/lens-bottleneck.md"
SYNTH="$RUN_DIR/synthesis.md"
ARXIV="$RUN_DIR/lens-arxiv.md"
# Back-compat: also accept the older names so an in-flight loop pinned
# to a prior workflow.yaml does not have its verifier-result inverted.
[ ! -f "$SCAN" ]  && [ -f "$RUN_DIR/bottleneck-scan.md" ]  && SCAN="$RUN_DIR/bottleneck-scan.md"
[ ! -f "$ARXIV" ] && [ -f "$RUN_DIR/arxiv-refs.md" ]       && ARXIV="$RUN_DIR/arxiv-refs.md"
[ ! -f "$ARXIV" ] && [ -f "$RUN_DIR/arxiv-research.md" ]   && ARXIV="$RUN_DIR/arxiv-research.md"

[ -f "$SCAN" ]  || missing+=("lens-bottleneck.md")
[ -f "$SYNTH" ] || missing+=("synthesis.md")
[ -f "$ARXIV" ] || missing+=("lens-arxiv.md")

# Converged → pass with a soft signal so the outer runner terminates.
converged=0
if [ -f "$SCAN" ] && grep -qi "^## Status: converged" "$SCAN"; then
  converged=1
  echo "scanner reported convergence" >&3
fi

ranked_rows=0
if [ -f "$SYNTH" ]; then
  # Count rows in the ranked patch table (lines starting with `| 1 ` … `| 5 `)
  ranked_rows=$(grep -cE '^\| *[1-5] +\|' "$SYNTH" 2>/dev/null || echo 0)
  echo "synthesis ranked_rows=$ranked_rows" >&3
fi

# Sanitize-then-check pattern. Codex agents (the executable-wrapper
# family used by codex_lens, arch_lens, arxiv_lens, bottleneck_lens
# planner-lane on planner=codex) reliably leak the `★ Insight ────`
# rule banner and `<z-insight>{...}</z-insight>` JSON envelope from
# their CLI runtime output into the Write tool's content because
# learning-mode framing is part of their emission contract. Iter 3
# rejected for this reason on 3 of 5 lenses; iter 2's own patch #3
# only addressed the verifier's narrow scope, not the source emission.
# Per arXiv 2604.01350 (Yang 2026, shared-state contamination) +
# 2605.16746 (Wang 2026, memory laundering): the durable fix is a
# post-write sanitizer that strips the framing before durable consumers
# see it, while preserving every byte of the agent's actual analysis.
_sanitize_artifact() {
  local f="$1"
  [ -f "$f" ] || return 0
  python3 - "$f" <<'PY'
import re, sys
p = sys.argv[1]
with open(p, encoding="utf-8", errors="replace") as fh:
    src = fh.read()

# Strip <z-insight>...</z-insight> blocks (greedy across lines).
src2 = re.sub(r'<z-insight>.*?</z-insight>\s*', '', src, flags=re.DOTALL)
# Strip "★ Insight ─────…" banner pairs: from a line starting with
# ★ Insight ─ up to (and including) the next line of only ─ chars.
src2 = re.sub(
    r'^★ Insight ─+\s*\n.*?^─+\s*\n',
    '',
    src2,
    flags=re.DOTALL | re.MULTILINE,
)
# Remaining single-line ★ Insight banners with no closer — drop them.
src2 = re.sub(r'^★ Insight ─.*\n', '', src2, flags=re.MULTILINE)

if src2 != src:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(src2)
    print(f"sanitized: {p}", file=sys.stderr)
PY
}

_polluted_remaining=()
for _polluted in "$SCAN" "$SYNTH" "$ARXIV" \
                 "$RUN_DIR/lens-perf.md" "$RUN_DIR/lens-correctness.md" \
                 "$RUN_DIR/lens-arch.md"; do
  [ -f "$_polluted" ] || continue
  _sanitize_artifact "$_polluted" 2>>"$EVIDENCE"
  # Anything still matching after sanitization is un-strippable corruption
  # (deeply embedded envelope, novel pattern) — those still reject.
  if grep -qE '^(★ Insight ─|<z-insight>)' "$_polluted"; then
    _polluted_remaining+=("$(basename "$_polluted")")
  fi
done

if [ "${#_polluted_remaining[@]}" -gt 0 ]; then
  missing+=("un-strippable envelope leak in: ${_polluted_remaining[*]}")
fi

# Pass condition: either converged, or we have all 3 artifacts AND >=1 ranked patch
pass=0
if [ "$converged" -eq 1 ]; then
  pass=1
elif [ "${#missing[@]}" -eq 0 ] && [ "$ranked_rows" -ge 1 ]; then
  pass=1
fi

python3 - "$pass" "$ranked_rows" "$converged" "$EVIDENCE" "${missing[@]}" <<'PY'
import json, sys
pass_, ranked, converged, ev, *missing = sys.argv[1:]
print(json.dumps({
    "verifier": "bottlenecks-found",
    "pass": pass_ == "1",
    "evidence_path": ev,
    "ranked_patches": int(ranked),
    "converged": converged == "1",
    "missing": missing,
}))
PY
