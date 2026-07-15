#!/usr/bin/env bash
# readme-review-ci.sh — Layer 2 (minimax) README drift reviewer for CI.
#
# ADVISORY ONLY. Runs on a PR, asks MiniMax-M3 (via the claude CLI + the
# minimax Anthropic-compatible gateway) whether any *semantic* README claim
# drifted, and posts the suggested fixes as NON-BLOCKING PR review comments
# (GitHub `suggestion` blocks where a line can be anchored, else a single
# summary comment). It NEVER fails the PR — the hard gate stays in Layer 1
# (scripts/readme-claim-check.sh, mechanical/exact/free).
#
# Pipeline (each stage fail-open):
#   1. key + tooling guard            → skip if absent
#   2. cheap MiniMax gatekeeper       → skip trivial diffs (~$0.005)
#   3. mechanical claim-check --json  → EXACT counts as ground truth for the LLM
#   4. one MiniMax reviewer call      → JSON {drifted_claims:[{excerpt,reason,fix}]}
#   5. post suggestions to the PR     → line-anchored where possible
#
# Env:
#   MINIMAX_API_KEY   (required; from the GitHub secret) — absent ⇒ skip
#   GH_TOKEN          GitHub token with pull-requests:write (for posting)
#   GH_REPO           owner/repo   (default: $GITHUB_REPOSITORY)
#   PR_NUMBER         the PR number (default: from $GITHUB_REF)
#   HEAD_SHA          PR head sha  (for line-anchored review comments)
#   MO_BASE_REF       base ref for the diff (default: origin/main)
#   MO_REVIEW_DRY=1   print the gh calls instead of running them (testing)
#   MO_REVIEW_MOCK    inject reviewer JSON instead of calling the LLM (testing)
#
# Exit: ALWAYS 0 (advisory). A non-zero would fail the PR; that is Layer 1's job.

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MO_README="${MO_README:-README.md}"
DRY="${MO_REVIEW_DRY:-0}"

log() { echo "[readme-review] $*" >&2; }
done_ok() { log "$*"; exit 0; }   # advisory: every exit is 0

# ── 1. guards (fail-open) ────────────────────────────────────────────────────
[ -f "$MO_README" ] || done_ok "$MO_README not found — skipping"
if [ -z "${MO_REVIEW_MOCK:-}" ]; then
  [ -n "${MINIMAX_API_KEY:-}" ] || done_ok "MINIMAX_API_KEY unset — skipping (advisory only)"
  command -v claude >/dev/null 2>&1 || done_ok "claude CLI not found — skipping"
fi
command -v jq >/dev/null 2>&1 || done_ok "jq not found — skipping"

# ── 2. cheap gatekeeper: is this diff worth a review at all? ──────────────────
if [ -z "${MO_REVIEW_MOCK:-}" ]; then
  gk=$(bash "$MINI_ORK_ROOT/scripts/readme-drift-gatekeeper.sh" 2>/dev/null)
  gk_rc=$?
  gk_verdict=$(printf '%s' "$gk" | jq -r '.verdict // "PANEL_SKIP"' 2>/dev/null)
  if [ "$gk_rc" -ne 0 ] || [ "$gk_verdict" != "PANEL_NEEDED" ]; then
    done_ok "gatekeeper: no review needed (rc=$gk_rc verdict=${gk_verdict:-none})"
  fi
  log "gatekeeper: PANEL_NEEDED — running minimax reviewer"
fi

# ── 3. mechanical ground truth (exact counts — the LLM must NOT re-count) ─────
mech=$(bash "$MINI_ORK_ROOT/scripts/readme-claim-check.sh" --json 2>/dev/null)
[ -n "$mech" ] || mech='{"note":"mechanical check unavailable"}'

# ── 4. the reviewer call ─────────────────────────────────────────────────────
upstream="${MO_BASE_REF:-origin/main}"
git -C "$MINI_ORK_ROOT" rev-parse "$upstream" >/dev/null 2>&1 || upstream="HEAD~1"
diff=$(git -C "$MINI_ORK_ROOT" diff "$upstream"...HEAD -- lib bin db recipes schemas "$MO_README" 2>/dev/null | head -c 12000)

read -r -d '' prompt <<EOF
You are a README drift reviewer. Find SEMANTIC drift only: prose, capability
claims, comparison-table rows, and citations in README.md that are now false
given the repo state and this diff. Do NOT re-count files — the MECHANICAL
GROUND TRUTH below already has exact counts; trust it and skip any numeric
claim it marks OK.

Output ONLY a single JSON object, no prose, no code fences:
{"drifted_claims":[{"excerpt":"<verbatim current README line or a unique
substring of it>","reason":"<one sentence: why it is now wrong>",
"suggested_fix":"<the corrected single line to replace the excerpt with>"}]}
Return {"drifted_claims":[]} if nothing drifted.

--- MECHANICAL GROUND TRUTH (authoritative; do not re-derive) ---
$mech

--- git diff (what changed) ---
$diff

--- README.md (current) ---
$(cat "$MO_README")
EOF

if [ -n "${MO_REVIEW_MOCK:-}" ]; then
  out="$MO_REVIEW_MOCK"                       # test injection
else
  out=$(
    source "$MINI_ORK_ROOT/lib/providers/cl_minimax.sh" 2>/dev/null
    timeout 150 claude --print --output-format text "$prompt" </dev/null 2>/dev/null
  )
fi
[ -n "$out" ] || done_ok "reviewer returned nothing — skipping (advisory)"

# Extract the JSON object even if the model wrapped it in prose/fences.
json=$(printf '%s' "$out" | python3 -c '
import sys, json, re
s = sys.stdin.read()
m = re.search(r"\{.*\}", s, re.DOTALL)
if not m:
    print("{\"drifted_claims\":[]}"); sys.exit(0)
try:
    obj = json.loads(m.group(0))
    print(json.dumps(obj))
except Exception:
    print("{\"drifted_claims\":[]}")
' 2>/dev/null)
count=$(printf '%s' "$json" | jq '.drifted_claims | length' 2>/dev/null || echo 0)
[ "${count:-0}" -gt 0 ] 2>/dev/null || done_ok "no semantic drift found — nothing to post"
log "reviewer flagged $count claim(s)"

# ── 5. post suggestions to the PR (line-anchored where possible) ──────────────
GH_REPO="${GH_REPO:-$GITHUB_REPOSITORY}"
PR_NUMBER="${PR_NUMBER:-$(printf '%s' "${GITHUB_REF:-}" | sed -n 's#refs/pull/\([0-9]*\)/.*#\1#p')}"
HEAD_SHA="${HEAD_SHA:-$(git -C "$MINI_ORK_ROOT" rev-parse HEAD 2>/dev/null)}"

post_review_comment() { # $1=line $2=body
  if [ "$DRY" = "1" ]; then
    echo "DRY gh api pulls/$PR_NUMBER/comments path=$MO_README line=$1"; echo "----"; echo "$2"; echo "----"
    return 0
  fi
  gh api "repos/$GH_REPO/pulls/$PR_NUMBER/comments" \
    -f body="$2" -f commit_id="$HEAD_SHA" -f path="$MO_README" -F line="$1" -f side=RIGHT >/dev/null 2>&1 \
    && log "posted suggestion at $MO_README:$1" \
    || log "could not post line-anchored comment at $1 (will fall back to summary)"
}

summary_items=()
for i in $(seq 0 $((count - 1))); do
  excerpt=$(printf '%s' "$json" | jq -r ".drifted_claims[$i].excerpt // \"\"")
  reason=$(printf  '%s' "$json" | jq -r ".drifted_claims[$i].reason // \"\"")
  fix=$(printf     '%s' "$json" | jq -r ".drifted_claims[$i].suggested_fix // \"\"")
  [ -n "$excerpt" ] || continue
  line=$(grep -n -F -m1 "$excerpt" "$MO_README" 2>/dev/null | head -1 | cut -d: -f1)
  if [ -n "$line" ] && [ -n "$fix" ] && [ -n "$PR_NUMBER" ] && [ -n "$HEAD_SHA" ]; then
    body=$'```suggestion\n'"$fix"$'\n```\n\n**README drift (minimax):** '"$reason"
    post_review_comment "$line" "$body"
  else
    summary_items+=("- \`${excerpt:0:80}\` — ${reason}"$'\n'"  → suggested: ${fix}")
  fi
done

# Any claim we could not anchor to a line → one consolidated advisory comment.
if [ "${#summary_items[@]}" -gt 0 ]; then
  body=$'**README drift review (minimax, advisory — not a gate)**\n\nCould not anchor these to a line; please review:\n\n'
  for it in "${summary_items[@]}"; do body+="$it"$'\n\n'; done
  if [ "$DRY" = "1" ]; then
    echo "DRY gh pr comment $PR_NUMBER:"; echo "$body"
  elif [ -n "$PR_NUMBER" ]; then
    printf '%s' "$body" | gh pr comment "$PR_NUMBER" --repo "$GH_REPO" --body-file - >/dev/null 2>&1 \
      && log "posted summary comment" || log "summary comment post failed (advisory — ignoring)"
  fi
fi

done_ok "review complete ($count claim(s) surfaced)"
