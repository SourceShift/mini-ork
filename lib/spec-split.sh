#!/usr/bin/env bash
# mini-ork Visible/Hidden Spec Split — Phase A.3
# Adapted from TDAD paper (arXiv 2603.08806 §2.4 Visible/Hidden Test Split).
#
# Convention: spec author marks scenarios for the hidden suite with a
# `// @hidden — <reason>` line comment immediately above the `test(...)`
# call. The visible suite is what the worker sees + runs locally; the
# hidden suite runs only at the final validation gate (Phase 2 of v2).
#
# This module ONLY builds the hidden spec — the visible spec stays at
# its original e2e/<EPIC>_*.spec.ts location (already there for the
# worker). The hidden spec is written out-of-tree to:
#     ${MINI_ORK_HOME}/runs/<job>/<epic>/iter-<n>/hidden_spec.ts
#
# Worker MUST NOT see this file. The validation gate runner reads from there.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Args: epic iter visible_spec_path
# Writes:
#   <iter-dir>/hidden_spec.ts (the hidden subset, or empty if none)
#   <iter-dir>/spec-split-report.json
mo_split_visible_hidden() {
  local epic="$1" iter="$2" visible_spec="$3"
  local iter_dir
  iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local hidden_path="$iter_dir/hidden_spec.ts"
  local report_path="$iter_dir/spec-split-report.json"
  mkdir -p "$iter_dir"

  if [ ! -f "$visible_spec" ]; then
    jq -n '{visible: 0, hidden: 0, error: "visible spec missing"}' > "$report_path"
    return 1
  fi

  # Strategy: extract the file's import block, the describe wrapper, and
  # only the test() blocks tagged with `// @hidden`. Re-emit as a
  # standalone hidden spec.
  python3 - "$visible_spec" "$hidden_path" "$report_path" <<'PY'
import re, sys, json, pathlib

src = pathlib.Path(sys.argv[1]).read_text()
hidden_out = pathlib.Path(sys.argv[2])
report_out = pathlib.Path(sys.argv[3])

lines = src.split("\n")

# Pass 1: collect imports (everything until first non-import / non-blank).
imports = []
i = 0
while i < len(lines):
    L = lines[i].rstrip()
    if L.startswith("import ") or L.startswith("//") or L == "":
        imports.append(L)
        i += 1
        continue
    break

# Pass 2: locate the test.describe wrapper + beforeEach.
header = []
j = i
describe_re = re.compile(r"^\s*test\.describe\(")
while j < len(lines) and not describe_re.match(lines[j]):
    j += 1
if j == len(lines):
    # No describe — bail with empty hidden.
    hidden_out.write_text("// no test.describe found — no hidden spec\n")
    report_out.write_text(json.dumps({"visible": 0, "hidden": 0, "error": "no describe"}))
    sys.exit(0)

# Capture from describe to first test( occurrence — include beforeEach.
test_re = re.compile(r"^\s*test\(")
k = j
while k < len(lines) and not test_re.match(lines[k]):
    header.append(lines[k])
    k += 1

# Pass 3: walk tests; pick out @hidden ones.
hidden_blocks = []
visible_count = 0
hidden_count = 0
m = k
hidden_marker_re = re.compile(r"^\s*//\s*@hidden")

def is_hidden(idx):
    """Look up to 3 lines BACK from idx for a @hidden marker."""
    for back in range(1, 4):
        if idx - back < 0:
            return False
        if hidden_marker_re.match(lines[idx - back]):
            return True
        if lines[idx - back].strip() == "":
            continue
        return False
    return False

while m < len(lines):
    if test_re.match(lines[m]):
        start = m
        depth = 0
        end = start
        for n in range(start, len(lines)):
            depth += lines[n].count("{") - lines[n].count("}")
            if depth == 0 and n > start:
                end = n
                break
        block = "\n".join(lines[start:end+1])
        if is_hidden(start):
            hidden_blocks.append(block)
            hidden_count += 1
        else:
            visible_count += 1
        m = end + 1
    else:
        m += 1

if hidden_count == 0:
    hidden_out.write_text("// no @hidden scenarios in source spec\n")
else:
    out = []
    out.append("// AUTOGEN: hidden spec — DO NOT commit to worktree.\n"
               "// Worker MUST NOT see this file. Runs only at Phase 2 validation gate.\n")
    out.extend(imports)
    out.append("")
    out.extend(header)
    out.extend(hidden_blocks)
    out.append("});\n")
    hidden_out.write_text("\n".join(out))

report_out.write_text(json.dumps({
    "visible": visible_count,
    "hidden": hidden_count,
    "ratio": (hidden_count / (visible_count + hidden_count)) if (visible_count + hidden_count) > 0 else 0,
}))
PY
}

# Run the hidden suite at validation-gate time.
# Args: epic iter worktree
mo_run_hidden_suite() {
  local epic="$1" iter="$2" worktree="$3"
  local iter_dir
  iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  local hidden_path="$iter_dir/hidden_spec.ts"
  local verdict_path="$iter_dir/hidden-verdict.json"
  local log_path="$iter_dir/hidden-runner.log"

  if [ ! -f "$hidden_path" ] || ! grep -q '^test(' "$hidden_path" 2>/dev/null; then
    echo "[mini-ork] hidden-suite: no hidden scenarios for $epic — skipping" >&2
    jq -n '{verdict: "PASS", scenarios_run: 0, skipped: true, reason: "no @hidden scenarios"}' > "$verdict_path"
    return 0
  fi

  # Stage the hidden spec into the worktree's e2e under a nonce name so
  # Playwright finds it via the existing webServer config.
  local stage_path="$worktree/e2e/__hidden_${epic}_iter${iter}.spec.ts"
  cp "$hidden_path" "$stage_path"

  echo "[mini-ork] hidden-suite epic=$epic iter=$iter (running staged spec)" >&2
  local rc=0
  (
    cd "$worktree" || exit 1
    npx playwright test "$stage_path" --reporter=line >"$log_path" 2>&1
  )
  rc=$?
  rm -f "$stage_path"

  local verdict
  if [ "$rc" -eq 0 ]; then verdict="PASS"; else verdict="FAIL"; fi
  jq -n \
    --arg verdict "$verdict" \
    --argjson rc "$rc" \
    --arg ran_at "$(date -u +%FT%TZ)" \
    '{verdict: $verdict, rc: $rc, ran_at: $ran_at, skipped: false}' \
    > "$verdict_path"
  echo "[mini-ork] hidden-suite epic=$epic verdict=$verdict" >&2
  return "$rc"
}
