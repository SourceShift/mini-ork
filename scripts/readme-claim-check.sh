#!/usr/bin/env bash
# readme-claim-check.sh — Layer 1 (mechanical, sub-second, FREE).
#
# Probes the load-bearing numerical + path claims in README.md against
# the live repo state. Catches the 90% class of drift surfaced in the
# 2026-06-05 audit (docs/audits/20260605-readme-claims-audit.md).
#
# Designed to run from a git hook OR a Make target OR ad-hoc.
#
# Exit codes:
#   0  no drift
#   1  mechanical drift detected — README is out of sync with the repo
#   2  invocation error (missing dep, missing README, etc)
#
# Usage:
#   scripts/readme-claim-check.sh              # full check
#   scripts/readme-claim-check.sh --verbose    # print every probe
#   scripts/readme-claim-check.sh --json       # JSON output (CI-friendly)
#
# Env knobs:
#   MO_README                 path to README.md (default: ./README.md)
#   MO_README_SKIP_MIGRATIONS 1 to skip migration-count check
#   MO_README_DRIFT_TOLERANCE 0|integer — how far off a count can be
#                             before flagging drift (default: 0 — strict)

set +e
MO_README="${MO_README:-README.md}"
TOLERANCE="${MO_README_DRIFT_TOLERANCE:-0}"
VERBOSE=0
JSON=0
for arg in "$@"; do
  case "$arg" in
    --verbose|-v) VERBOSE=1 ;;
    --json)       JSON=1 ;;
    --help|-h)    sed -n '2,28p' "$0"; exit 0 ;;
  esac
done

[ -f "$MO_README" ] || { echo "readme-claim-check: $MO_README not found" >&2; exit 2; }

# ── helpers ──────────────────────────────────────────────────────────────────
readme_int_after() {
  # Pulls the integer that appears in README.md after a fixed phrase.
  # Example: readme_int_after '38 framework primitives'  → 38
  # If the README mentions multiple integers near the phrase, picks the
  # FIRST integer in the matching line.
  local needle="$1"
  grep -m1 -oE "[0-9]+ ${needle}" "$MO_README" 2>/dev/null \
    | grep -oE '^[0-9]+' | head -1
}

count_dir() {
  # Count TRACKED files matching a pattern under a dir (direct children
  # only — never nested). Use git ls-files (not find) so untracked
  # working-tree files don't inflate the count — at push-time, the
  # population that matters is what's tracked, not what's on disk.
  #
  # Gotcha: git ls-files pathspec is NOT shell glob — `*` matches
  # across `/` boundaries. So we use a regex post-filter on the result
  # to keep only direct-child paths.
  local dir="$1" pat="$2"
  [ -d "$dir" ] || { echo 0; return; }
  # Convert shell glob `cl_*.sh` to regex `cl_[^/]+\.sh`.
  local re
  re=$(printf '%s' "$pat" | sed 's/\./\\./g; s/\*/[^\/]+/g')
  git ls-files "$dir/" 2>/dev/null \
    | grep -E "^${dir}/${re}$" \
    | wc -l | tr -d ' '
}

count_subdirs() {
  # Count tracked subdirs (subdirs with at least one tracked file).
  local dir="$1"
  [ -d "$dir" ] || { echo 0; return; }
  git ls-files "$dir/" 2>/dev/null \
    | awk -F/ '{print $2}' \
    | sort -u | grep -c .
}

# ── probes ───────────────────────────────────────────────────────────────────
declare -a probe_names=()
declare -a probe_claims=()
declare -a probe_actuals=()
declare -a probe_verdicts=()
fail_count=0

add_probe() {
  # add_probe <name> <claimed> <actual>
  local name="$1" claimed="$2" actual="$3"
  probe_names+=("$name")
  probe_claims+=("$claimed")
  probe_actuals+=("$actual")
  local diff=$(( actual > claimed ? actual - claimed : claimed - actual ))
  if [ -z "$claimed" ] || [ "$diff" -gt "$TOLERANCE" ]; then
    probe_verdicts+=("DRIFT")
    fail_count=$((fail_count + 1))
  else
    probe_verdicts+=("OK")
  fi
}

# Probe 1 — lib/*.sh count claim
lib_count_claim=$(readme_int_after 'framework primitives')
lib_count_actual=$(count_dir lib '*.sh')
add_probe "lib/*.sh count"  "${lib_count_claim:-0}"  "$lib_count_actual"

# Probe 2 — bin/mini-ork-* entrypoint count claim
bin_count_claim=$(readme_int_after 'user-facing.*bin/mini-ork.*entrypoints')
bin_count_actual=$(ls bin/mini-ork* 2>/dev/null | wc -l | tr -d ' ')
add_probe "bin/mini-ork-* entrypoints" "${bin_count_claim:-0}"  "$bin_count_actual"

# Probe 3 — migrations count claim
if [ "${MO_README_SKIP_MIGRATIONS:-0}" != "1" ]; then
  mig_count_claim=$(readme_int_after 'schema migrations')
  mig_count_actual=$(count_dir db/migrations '*.sql')
  add_probe "db/migrations/*.sql count" "${mig_count_claim:-0}" "$mig_count_actual"
fi

# Probe 4 — recipes table row count vs actual recipes/ dirs
recipes_actual=$(count_subdirs recipes)
recipes_table_rows=$(awk '/^### RECIPES/,/^Add your own/' "$MO_README" \
                     | grep -cE '^\| `[a-z-]+` \|')
add_probe "recipes table rows" "$recipes_actual" "$recipes_table_rows"

# Probe 5 — providers count claim
providers_claim=$(readme_int_after 'model-family wrappers ship')
providers_actual=$(count_dir lib/providers 'cl_*.sh')
add_probe "lib/providers/cl_*.sh count" "${providers_claim:-0}" "$providers_actual"

# ── regression-guard probes (specific strings that MUST or MUST NOT be present)
# Probe 6 — `install.sh --check` MUST NOT come back (audit closed it)
regression_install_check=$(grep -c "install.sh --check" "$MO_README")
probe_names+=("regression: install.sh --check banned phrase")
probe_claims+=("0")
probe_actuals+=("$regression_install_check")
if [ "$regression_install_check" -gt 0 ]; then
  probe_verdicts+=("DRIFT")
  fail_count=$((fail_count + 1))
else
  probe_verdicts+=("OK")
fi

# Probe 7 — every cited file/dir path actually exists on disk
missing_paths=()
# Extract every `<path>` style backtick-quoted path that looks like a file/dir
# under recipes/ docs/ lib/ bin/ schemas/ db/ examples/ kickoffs/ — verify it exists.
mapfile -t cited_paths < <(grep -oE '`[a-zA-Z_./-]+`' "$MO_README" \
                            | tr -d '`' \
                            | grep -E '^(recipes|docs|lib|bin|schemas|db|examples|kickoffs)/' \
                            | sort -u)
for p in "${cited_paths[@]}"; do
  # Strip trailing slash; tolerate it as a dir indicator
  p_clean="${p%/}"
  [ -e "$p_clean" ] || missing_paths+=("$p")
done
probe_names+=("cited paths exist")
probe_claims+=("0 missing")
probe_actuals+=("${#missing_paths[@]} missing")
if [ "${#missing_paths[@]}" -gt 0 ]; then
  probe_verdicts+=("DRIFT")
  fail_count=$((fail_count + 1))
else
  probe_verdicts+=("OK")
fi

# ── render ───────────────────────────────────────────────────────────────────
if [ "$JSON" = "1" ]; then
  printf '{"verdict":"%s","fail_count":%d,"probes":[' \
    "$( [ "$fail_count" -eq 0 ] && echo CLEAN || echo DRIFT )" "$fail_count"
  for i in "${!probe_names[@]}"; do
    [ $i -gt 0 ] && printf ','
    printf '{"name":"%s","claimed":"%s","actual":"%s","verdict":"%s"}' \
      "${probe_names[$i]}" "${probe_claims[$i]}" "${probe_actuals[$i]}" "${probe_verdicts[$i]}"
  done
  printf '],"missing_paths":['
  for i in "${!missing_paths[@]}"; do
    [ $i -gt 0 ] && printf ','
    printf '"%s"' "${missing_paths[$i]}"
  done
  printf ']}\n'
else
  echo "── README claim-check (mechanical, Layer 1) ──"
  printf '%-45s  %-12s %-12s %s\n' "PROBE" "CLAIMED" "ACTUAL" "VERDICT"
  printf '%-45s  %-12s %-12s %s\n' "─────" "───────" "──────" "───────"
  for i in "${!probe_names[@]}"; do
    printf '%-45s  %-12s %-12s %s\n' \
      "${probe_names[$i]}" "${probe_claims[$i]}" "${probe_actuals[$i]}" "${probe_verdicts[$i]}"
  done
  if [ "${#missing_paths[@]}" -gt 0 ]; then
    echo
    echo "Missing paths cited in README:"
    for p in "${missing_paths[@]}"; do
      echo "  - $p"
    done
  fi
  echo
  if [ "$fail_count" -eq 0 ]; then
    echo "✓ CLEAN — $((${#probe_names[@]})) probes passed"
  else
    echo "✗ DRIFT — $fail_count / ${#probe_names[@]} probes failed"
    echo
    echo "Fix the README to match the repo state, OR fix the repo state to"
    echo "match the README (whichever is wrong). Bypass with"
    echo "MO_README_DRIFT_SKIP=1 git push  (the pre-push hook honors it)"
    echo "or  git push --no-verify  for one-shot."
  fi
fi

[ "$fail_count" -eq 0 ] && exit 0 || exit 1
