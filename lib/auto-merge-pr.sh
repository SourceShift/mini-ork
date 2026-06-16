#!/usr/bin/env bash
# auto-merge-pr.sh — PR-based auto-merge gate for E8.
#
# Complements the existing lib/auto-merge.sh (which squash-merges branches
# locally). This one is GH-PR-aware:
#   1. For each epic with pr_url and status='in review' (or 'done'-but-not-merged):
#   2. Check `gh pr checks <url>` → must be all-green (no failing required checks)
#   3. Check PR age ≥ MO_PR_SOAK_HOURS (default 24h)
#   4. Call `gh pr merge <url> --squash --delete-branch`
#   5. On success → epics.status='done' (already done if scheduler set it)
#
# Public API:
#   mo_auto_merge_pr_one <epic_id>   try to merge a single epic's PR
#   mo_auto_merge_pr_sweep           iterate over all epics with pr_url
#
# Flags via env:
#   MO_AUTO_MERGE        "1" enables (default 0 — opt-in)
#   MO_PR_SOAK_HOURS     soak before merge (default 24)
#   MO_REQUIRE_REVIEWER  if "1", require approving review (default 1)
#
# Returns 0 on success, 1 on permanent failure for that PR, 2 on soak/check
# not-yet-ready (caller should retry later).

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_mo_pr_state_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
}

# Check if gh + auth available. Returns 0 if usable, else non-zero.
_mo_pr_gh_ready() {
  command -v gh >/dev/null 2>&1 || return 2
  if [ -z "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    gh auth status >/dev/null 2>&1 || return 2
  fi
  return 0
}

# All required CI checks must be in {success, neutral, skipped}. Any
# {failure, cancelled, action_required} is a fail. {pending, queued,
# in_progress} → soft-skip (retry later).
_mo_pr_checks_passing() {
  local pr_url="$1"
  local out
  out=$(gh pr checks "$pr_url" --json bucket --jq '.[] | .bucket' 2>/dev/null) || return 2
  if [ -z "$out" ]; then
    # No checks configured — treat as pass (operator can opt-in to fail).
    return 0
  fi
  if printf '%s\n' "$out" | grep -qE '^(fail|cancel)'; then
    return 1
  fi
  if printf '%s\n' "$out" | grep -qE '^(pending)'; then
    return 2
  fi
  return 0
}

# Returns 0 if PR has at least one APPROVED review (when required).
_mo_pr_has_approval() {
  local pr_url="$1"
  if [ "${MO_REQUIRE_REVIEWER:-1}" != "1" ]; then
    return 0
  fi
  local n
  n=$(gh pr view "$pr_url" --json reviewDecision --jq '.reviewDecision' 2>/dev/null)
  case "$n" in
    APPROVED) return 0 ;;
    REVIEW_REQUIRED|CHANGES_REQUESTED) return 1 ;;
    *) return 1 ;;
  esac
}

# Returns 0 when PR age ≥ soak_hours, else 1 (not soaked yet).
_mo_pr_age_ok() {
  local pr_url="$1"
  local soak="${MO_PR_SOAK_HOURS:-24}"
  local created
  created=$(gh pr view "$pr_url" --json createdAt --jq '.createdAt' 2>/dev/null) || return 1
  [ -n "$created" ] || return 1
  python3 - "$created" "$soak" <<'PY'
import datetime, sys
created = sys.argv[1].replace("Z", "+00:00")
soak_h = float(sys.argv[2])
dt = datetime.datetime.fromisoformat(created)
now = datetime.datetime.now(dt.tzinfo)
age_h = (now - dt).total_seconds() / 3600.0
sys.exit(0 if age_h >= soak_h else 1)
PY
}

mo_auto_merge_pr_one() {
  local epic_id="${1:?epic_id required}"

  if [ "${MO_AUTO_MERGE:-0}" != "1" ]; then
    echo "auto-merge-pr: MO_AUTO_MERGE not set, skip $epic_id" >&2
    return 2
  fi

  if ! _mo_pr_gh_ready; then
    echo "auto-merge-pr: gh CLI / auth unavailable, skip $epic_id" >&2
    return 2
  fi

  local pr_url
  pr_url=$(sqlite3 "$(_mo_pr_state_db)" \
    "SELECT pr_url FROM epics WHERE id='$epic_id' AND pr_url IS NOT NULL LIMIT 1;" 2>/dev/null)
  if [ -z "$pr_url" ]; then
    echo "auto-merge-pr: $epic_id has no pr_url, skip" >&2
    return 2
  fi

  local rc
  _mo_pr_checks_passing "$pr_url"; rc=$?
  case $rc in
    0) ;;
    1) echo "auto-merge-pr: $epic_id PR has failing CI checks → skip" >&2; return 1 ;;
    2) echo "auto-merge-pr: $epic_id PR checks pending → retry later" >&2; return 2 ;;
  esac

  if ! _mo_pr_has_approval "$pr_url"; then
    echo "auto-merge-pr: $epic_id PR not approved (MO_REQUIRE_REVIEWER=1) → retry later" >&2
    return 2
  fi

  if ! _mo_pr_age_ok "$pr_url"; then
    echo "auto-merge-pr: $epic_id PR not soaked ${MO_PR_SOAK_HOURS:-24}h yet → retry later" >&2
    return 2
  fi

  echo "auto-merge-pr: merging $epic_id PR $pr_url"
  if gh pr merge "$pr_url" --squash --delete-branch --auto 2>&1 | sed 's/^/  [merge] /'; then
    sqlite3 "$(_mo_pr_state_db)" "UPDATE epics SET status='done' WHERE id='$epic_id' AND status != 'done';" 2>/dev/null || true
    return 0
  else
    echo "auto-merge-pr: gh pr merge failed for $epic_id" >&2
    return 1
  fi
}

mo_auto_merge_pr_sweep() {
  local epic_ids
  epic_ids=$(sqlite3 "$(_mo_pr_state_db)" \
    "SELECT id FROM epics
      WHERE pr_url IS NOT NULL
        AND archived_at IS NULL
        AND status IN ('in review','in progress','not started')
      ORDER BY updated_at ASC;" 2>/dev/null)
  local merged=0 skipped=0
  while IFS= read -r ep; do
    [ -z "$ep" ] && continue
    if mo_auto_merge_pr_one "$ep"; then
      merged=$((merged+1))
    else
      skipped=$((skipped+1))
    fi
  done <<<"$epic_ids"
  echo "auto-merge-pr: swept ${merged} merged, ${skipped} skipped"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "auto-merge-pr.sh — source me and call mo_auto_merge_pr_one / mo_auto_merge_pr_sweep"
fi
