#!/usr/bin/env bash
# verifiers/link-verifier.sh — relative-link integrity for `docs` recipe.
#
# Walks every doc file named in the plan's verifier_contract.checks[] where
# `kind == "link_integrity"`, extracts every `[label](path)` markdown link,
# and confirms each relative path resolves to a real file/directory.
# External URLs (http/https/mailto), anchors (#section), and reserved
# autolinks (e.g. <https://...>) are skipped.
#
# rc=0 when ALL relative links resolve. rc=1 on ANY broken link.
#
# Env (same as grep-assert.sh):
#   MINI_ORK_PLAN_PATH    path to the plan JSON
#   MINI_ORK_HOME         project home
#   MINI_ORK_RUN_ID       current run id

set -Eeuo pipefail

MINI_ORK_HOME="${MINI_ORK_HOME:-$(pwd)/.mini-ork}"
MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-unknown-run}"
PLAN_PATH="${MINI_ORK_PLAN_PATH:-$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/plan.json}"
LOG_DIR="$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/verifier_link.log"

if [ ! -f "$PLAN_PATH" ]; then
  printf '{"verifier":"link","status":"skipped","reason":"plan not found: %s"}\n' "$PLAN_PATH"
  exit 0
fi

# Collect doc files named for link checking; dedupe.
mapfile -t docs < <(jq -r '.verifier_contract.checks[]? | select(.kind == "link_integrity") | .file' "$PLAN_PATH" 2>/dev/null | sort -u | grep -v '^$' || true)

if [ "${#docs[@]}" -eq 0 ]; then
  printf '{"verifier":"link","status":"skipped","reason":"no link_integrity assertions in plan"}\n' | tee -a "$LOG_PATH"
  exit 0
fi

n_total=0
n_broken=0
broken_details=()

for doc in "${docs[@]}"; do
  if [ ! -f "$doc" ]; then
    n_broken=$((n_broken + 1))
    broken_details+=("doc file itself missing: $doc")
    echo "  [FAIL] doc not found: $doc" | tee -a "$LOG_PATH"
    continue
  fi

  doc_dir=$(dirname "$doc")
  # Extract every [text](url) form. python because bash regex is tedious
  # on nested brackets / multi-line links.
  mapfile -t links < <(python3 - "$doc" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
# Match [..](url) where url is anything-not-paren OR balanced single-paren
for m in re.finditer(r'\[([^\]]*)\]\(([^)\s]+)\)', text):
    print(m.group(2))
PY
)

  for link in "${links[@]}"; do
    n_total=$((n_total + 1))
    case "$link" in
      http://*|https://*|mailto:*|tel:*|ftp://*|ftps://*|sftp://*) continue ;;
      \#*) continue ;;
    esac
    # Strip fragment / query if present
    path_only="${link%%\#*}"
    path_only="${path_only%%\?*}"
    [ -z "$path_only" ] && continue
    # Resolve relative to the doc's directory
    if [ "${path_only:0:1}" = "/" ]; then
      resolved="$path_only"
    else
      resolved="$doc_dir/$path_only"
    fi
    if [ ! -e "$resolved" ]; then
      n_broken=$((n_broken + 1))
      broken_details+=("in $doc: $link → $resolved (not found)")
      echo "  [FAIL] broken link: $doc → $link" | tee -a "$LOG_PATH"
    fi
  done
done

if [ "$n_broken" -eq 0 ]; then
  printf '{"verifier":"link","status":"pass","docs":%d,"links_checked":%d,"broken":0}\n' "${#docs[@]}" "$n_total" | tee -a "$LOG_PATH"
  exit 0
else
  broken_arr=$(printf '%s\n' "${broken_details[@]}" | jq -R . | jq -s -c .)
  printf '{"verifier":"link","status":"fail","docs":%d,"links_checked":%d,"broken":%d,"failures":%s}\n' \
    "${#docs[@]}" "$n_total" "$n_broken" "$broken_arr" | tee -a "$LOG_PATH"
  exit 1
fi
