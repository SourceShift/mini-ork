#!/usr/bin/env bash
# readme-drift-gatekeeper.sh — Layer 2a (single MiniMax-M3 LLM gate, ~$0.005).
#
# Cheap, fast veto-or-allow gate that decides whether the full 4-lens
# drift panel should fire. Most pushes touch typo / link / formatting
# changes that don't move a claim's truth value — the gatekeeper waves
# those through without paying for the panel.
#
# Why MiniMax-M3 here: cheap + fast + distinct family from the panel
# voters' aggregate (it shares family with one panel lens, minimax_lens,
# but as a ROUTER not a voter, that's not a coalition risk — the
# downstream panel still spans 4 distinct families).
#
# Exit codes:
#   0  panel SHOULD fire (gatekeeper said YES)
#   1  panel CAN BE SKIPPED (gatekeeper said NO)
#   2  invocation error (provider unreachable, API down, etc)
#
# The hook treats rc=2 as fail-open (skip panel, push proceeds) so a
# provider outage doesn't block local development. Hard-block remains
# in Layer 1 (mechanical).
#
# Output: a JSON object on stdout with shape
#   { "verdict": "PANEL_NEEDED" | "PANEL_SKIP",
#     "reason":  "<one sentence>",
#     "cost_estimate_usd": <float> }

set +e
MO_README="${MO_README:-README.md}"
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SECRETS="${MO_SECRETS:-$MINI_ORK_ROOT/.mini-ork/config/secrets.local.sh}"
[ -f "$SECRETS" ] || SECRETS="$HOME/.config/mini-ork/secrets.local.sh"
[ -f "$SECRETS" ] && source "$SECRETS"

if [ -z "${MINIMAX_API_KEY:-}" ]; then
  printf '{"verdict":"PANEL_SKIP","reason":"MINIMAX_API_KEY unset — fail-open to keep push moving","cost_estimate_usd":0}\n' >&2
  exit 2
fi

# Compute the diff that triggered this check (relative to origin/main if
# we have it, else HEAD~1).
upstream_ref="origin/main"
git rev-parse "$upstream_ref" >/dev/null 2>&1 || upstream_ref="HEAD~1"

# What changed?
changed_files=$(git diff --name-only "$upstream_ref"...HEAD 2>/dev/null | head -50)
readme_diff_lines=$(git diff --stat "$upstream_ref"...HEAD -- "$MO_README" 2>/dev/null | tail -1)
roadmap_diff_lines=$(git diff --stat "$upstream_ref"...HEAD -- ROADMAP.md 2>/dev/null | tail -1)

# Did any structural file change (would invalidate a claim numerically)?
structural_changes=$(echo "$changed_files" | grep -cE '^(lib/|bin/|recipes/|db/migrations/|schemas/|install.sh|examples/)')

# Build the gatekeeper prompt. Compact — minimax answers in 1-2 sentences.
prompt=$(cat <<EOF
You are a fast router. Given a git diff summary, decide whether a 4-lens README claim-drift audit should fire.

The audit costs ~\$0.30 and takes ~30 seconds, so it must NOT fire for trivial changes (typos, formatting, link updates that don't shift a claim's truth value). It MUST fire when the diff plausibly invalidates a load-bearing claim in README.md (counts, capabilities, comparisons, citations, architecture description).

Files changed in this push:
$changed_files

README.md diff stat: ${readme_diff_lines:-no change}
ROADMAP.md diff stat: ${roadmap_diff_lines:-no change}
Structural files changed (lib/ bin/ recipes/ migrations/ schemas/): $structural_changes

Respond with exactly two lines:
VERDICT: PANEL_NEEDED   (if drift audit should fire)
REASON: <one short sentence>

OR

VERDICT: PANEL_SKIP
REASON: <one short sentence>
EOF
)

# Source the minimax wrapper in a subshell, fire claude --print with
# prompt as POSITIONAL ARG (claude --print does not read stdin).
out=$(
  source "$MINI_ORK_ROOT/lib/providers/cl_minimax.sh" 2>/dev/null
  timeout 45 claude --print --output-format text "$prompt" < /dev/null 2>/dev/null
)
rc=$?
if [ $rc -ne 0 ] || [ -z "$out" ]; then
  printf '{"verdict":"PANEL_SKIP","reason":"gatekeeper LLM unreachable (rc=%d) — fail-open","cost_estimate_usd":0}\n' "$rc" >&2
  exit 2
fi

# Parse minimax's text response. Look for VERDICT line.
verdict=$(echo "$out" | grep -m1 -E "^VERDICT:" | sed 's/^VERDICT:[[:space:]]*//')
reason=$(echo "$out"   | grep -m1 -E "^REASON:"  | sed 's/^REASON:[[:space:]]*//' | head -c 200)

case "$verdict" in
  PANEL_NEEDED)
    printf '{"verdict":"PANEL_NEEDED","reason":%s,"cost_estimate_usd":0.005}\n' \
      "$(printf '%s' "$reason" | jq -Rs .)"
    exit 0
    ;;
  PANEL_SKIP)
    printf '{"verdict":"PANEL_SKIP","reason":%s,"cost_estimate_usd":0.005}\n' \
      "$(printf '%s' "$reason" | jq -Rs .)"
    exit 1
    ;;
  *)
    # Unparseable — fail-open
    printf '{"verdict":"PANEL_SKIP","reason":"gatekeeper output unparseable (got: %s) — fail-open","cost_estimate_usd":0.005}\n' \
      "$(printf '%s' "$verdict" | head -c 100)" >&2
    exit 2
    ;;
esac
