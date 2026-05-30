#!/usr/bin/env bash
# tests/run-all.sh — execute the entire mini-ork test pyramid.
#
# Layers (run in order, fast → slow):
#   1. tests/smoke.sh           — dependency + DB-init + syntax + shellcheck pass
#   2. tests/unit/test_*.sh     — per-lib primitive coverage (13 libs)
#   3. tests/integration/*.sh   — per-bin end-to-end with isolated tmp project (9 bins + dispatcher)
#   4. tests/e2e/*.sh           — self-improvement cycle (trace→gradient→pattern→candidate→benchmark→promote→rollback)
#   5. tests/security/*.sh      — injection / traversal / symlink / oversized-input / perms / supply-chain
#
# Aggregate pass/fail counts emitted at the end.
# Exit 0 on all green; exit 1 on any failure (CI-friendly).
#
# Usage:
#   bash tests/run-all.sh                        # everything
#   bash tests/run-all.sh unit                   # only unit layer
#   bash tests/run-all.sh unit integration       # both layers
#   FILTER='trace|reflect' bash tests/run-all.sh # regex filter on test filenames
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$MINI_ORK_ROOT"

# ── what to run ───────────────────────────────────────────────────────────────
LAYERS=()
if [ $# -eq 0 ]; then
  LAYERS=(smoke unit integration e2e security)
else
  LAYERS=("$@")
fi
FILTER="${FILTER:-}"

# ── colors (only when stdout is a tty) ────────────────────────────────────────
if [ -t 1 ]; then
  C_OK=$'\e[32m'; C_FAIL=$'\e[31m'; C_DIM=$'\e[2m'; C_BOLD=$'\e[1m'; C_RST=$'\e[0m'
else
  C_OK=""; C_FAIL=""; C_DIM=""; C_BOLD=""; C_RST=""
fi

# ── aggregate counters ────────────────────────────────────────────────────────
TOTAL_OK=0
TOTAL_FAIL=0
TOTAL_FILES=0
FAILED_FILES=()

# ── runner per layer ──────────────────────────────────────────────────────────
_run_layer() {
  local layer="$1"
  local layer_ok=0 layer_fail=0 layer_files=0
  echo ""
  echo "${C_BOLD}==> Layer: $layer${C_RST}"

  case "$layer" in
    smoke)
      [ -f "tests/smoke.sh" ] || { echo "  ${C_DIM}(no tests/smoke.sh, skipping)${C_RST}"; return 0; }
      if bash tests/smoke.sh 2>&1 | tail -5; then
        local out
        out=$(bash tests/smoke.sh 2>&1 | tail -3 | grep -E "Results.*OK")
        echo "  ${C_OK}smoke: $out${C_RST}"
        layer_ok=1; layer_files=1
      else
        echo "  ${C_FAIL}smoke: FAIL${C_RST}"
        layer_fail=1; layer_files=1
        FAILED_FILES+=("tests/smoke.sh")
      fi
      ;;
    unit|integration|e2e|security)
      local dir="tests/$layer"
      [ -d "$dir" ] || { echo "  ${C_DIM}(no $dir/, skipping)${C_RST}"; return 0; }
      for f in "$dir"/*.sh; do
        [ -f "$f" ] || continue
        if [ -n "$FILTER" ] && ! echo "$f" | grep -qE "$FILTER"; then continue; fi
        layer_files=$((layer_files + 1))
        # Capture last-line summary; surface PASS/FAIL terminal lines.
        local out rc
        out=$(bash "$f" 2>&1)
        rc=$?
        local oks=$(echo "$out" | grep -cE '^\s*\[OK\]' || true)
        local fails=$(echo "$out" | grep -cE '^\s*\[FAIL\]' || true)
        if [ "$rc" -eq 0 ] && [ "$fails" -eq 0 ]; then
          echo "  ${C_OK}[OK]${C_RST}   $(basename "$f")  ${C_DIM}(${oks} assertions)${C_RST}"
          layer_ok=$((layer_ok + 1))
          TOTAL_OK=$((TOTAL_OK + oks))
        else
          echo "  ${C_FAIL}[FAIL]${C_RST} $(basename "$f")  ${C_DIM}(${oks} ok / ${fails} fail / rc=${rc})${C_RST}"
          layer_fail=$((layer_fail + 1))
          TOTAL_FAIL=$((TOTAL_FAIL + fails))
          FAILED_FILES+=("$f")
          # Show first 6 fail lines for diagnostics
          echo "$out" | grep -E '^\s*\[FAIL\]' | head -6 | sed 's/^/      /'
        fi
      done
      ;;
    *)
      echo "  ${C_FAIL}unknown layer: $layer${C_RST}"
      ;;
  esac

  TOTAL_FILES=$((TOTAL_FILES + layer_files))
  echo "  ${C_DIM}layer summary: ${layer_ok} files OK, ${layer_fail} files FAIL, ${layer_files} files total${C_RST}"
}

# ── execute layers ────────────────────────────────────────────────────────────
echo "${C_BOLD}mini-ork test suite${C_RST}"
echo "${C_DIM}root: $MINI_ORK_ROOT${C_RST}"
echo "${C_DIM}layers: ${LAYERS[*]}${C_RST}"
[ -n "$FILTER" ] && echo "${C_DIM}filter: $FILTER${C_RST}"

for l in "${LAYERS[@]}"; do
  _run_layer "$l"
done

# ── final summary ─────────────────────────────────────────────────────────────
echo ""
echo "${C_BOLD}==> Final${C_RST}"
echo "  total files: $TOTAL_FILES"
echo "  total assertions: ${C_OK}${TOTAL_OK} OK${C_RST} / ${C_FAIL}${TOTAL_FAIL} FAIL${C_RST}"

if [ "$TOTAL_FAIL" -eq 0 ] && [ "${#FAILED_FILES[@]}" -eq 0 ]; then
  echo "  ${C_OK}${C_BOLD}PASS${C_RST}"
  exit 0
else
  echo "  ${C_FAIL}${C_BOLD}FAIL${C_RST}  (${#FAILED_FILES[@]} files with failures)"
  printf "    %s\n" "${FAILED_FILES[@]}"
  exit 1
fi
