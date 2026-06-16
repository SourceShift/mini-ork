#!/usr/bin/env bash
# pr-create.sh — open a GitHub PR for an epic's branch and persist the URL.
#
# Public API:
#   mo_open_pr <epic_id> <branch> [kickoff_path]
#       Opens (or reuses) a PR. Writes URL back to epics.pr_url.
#       Returns the URL on stdout. Returns 0 on success, 1 on permanent
#       failure, 2 on "not configured" (no gh, no token, no remote — caller
#       can treat as soft-skip).
#
# Configuration:
#   MO_OPEN_PR              "1" to enable (default off — feature-flagged)
#   MO_PR_BASE              base branch (default: main)
#   MO_PR_DRAFT             "1" to open as draft PR (default 0)
#   GH_TOKEN / GITHUB_TOKEN  passed through to `gh` if set
#
# Idempotence: if epics.pr_url is already set, returns it without re-creating.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_mo_pr_state_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
}

_mo_pr_get_existing_url() {
  local epic_id="$1"
  sqlite3 "$(_mo_pr_state_db)" \
    "SELECT pr_url FROM epics WHERE id='$epic_id' AND pr_url IS NOT NULL LIMIT 1;" \
    2>/dev/null
}

_mo_pr_write_url() {
  local epic_id="$1" url="$2" branch="$3"
  sqlite3 "$(_mo_pr_state_db)" \
    "UPDATE epics SET pr_url='$url', branch='$branch' WHERE id='$epic_id';" \
    2>/dev/null || true
}

# Print the inferred title/body from the kickoff if available, else fall back.
_mo_pr_build_title() {
  local epic_id="$1" kickoff_path="$2"
  local title=""
  if [ -n "$kickoff_path" ] && [ -f "$kickoff_path" ]; then
    title=$(awk '/^# /{sub(/^# /,""); print; exit}' "$kickoff_path" 2>/dev/null)
  fi
  if [ -z "$title" ]; then
    # fall back to epics.title
    title=$(sqlite3 "$(_mo_pr_state_db)" \
      "SELECT title FROM epics WHERE id='$epic_id';" 2>/dev/null)
  fi
  [ -n "$title" ] || title="epic $epic_id"
  # Cap title length for GitHub (256 chars max but keep <80 in practice).
  printf '%s\n' "${title:0:200}"
}

_mo_pr_build_body() {
  local epic_id="$1" kickoff_path="$2"
  cat <<EOF
Auto-opened by mini-ork epic delivery for **$epic_id**.

EOF
  if [ -n "$kickoff_path" ] && [ -f "$kickoff_path" ]; then
    echo "## Kickoff"
    echo
    cat "$kickoff_path"
  else
    echo "_No kickoff document found at \`$kickoff_path\`._"
  fi
}

mo_open_pr() {
  local epic_id="${1:?epic_id required}"
  local branch="${2:?branch required}"
  local kickoff_path="${3:-}"

  if [ "${MO_OPEN_PR:-0}" != "1" ]; then
    echo "mo_open_pr: skipped (MO_OPEN_PR != 1)" >&2
    return 2
  fi

  # Idempotence — if a URL is already on the epics row, just echo it.
  local existing
  existing="$(_mo_pr_get_existing_url "$epic_id")"
  if [ -n "$existing" ]; then
    printf '%s\n' "$existing"
    return 0
  fi

  # Pre-flight checks. Soft-skip on any missing prerequisite.
  command -v gh >/dev/null 2>&1 || {
    echo "mo_open_pr: 'gh' CLI not on PATH — skip" >&2
    return 2
  }
  if [ -z "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
    if ! gh auth status >/dev/null 2>&1; then
      echo "mo_open_pr: no GH_TOKEN/GITHUB_TOKEN AND 'gh auth status' fails — skip" >&2
      return 2
    fi
  fi
  if ! git -C "${REPO_ROOT:-$(pwd)}" remote get-url origin >/dev/null 2>&1; then
    echo "mo_open_pr: no 'origin' remote in $(pwd) — skip" >&2
    return 2
  fi

  # Make sure the branch exists on origin. Push if local-only.
  if ! git -C "${REPO_ROOT:-$(pwd)}" rev-parse --verify "$branch" >/dev/null 2>&1; then
    echo "mo_open_pr: branch '$branch' not found locally" >&2
    return 1
  fi
  if ! git -C "${REPO_ROOT:-$(pwd)}" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    git -C "${REPO_ROOT:-$(pwd)}" push -u origin "$branch" 2>&1 | sed 's/^/  [pr-create] /' || {
      echo "mo_open_pr: push of '$branch' to origin failed" >&2
      return 1
    }
  fi

  local title body_file url base draft_flag
  title="$(_mo_pr_build_title "$epic_id" "$kickoff_path")"
  body_file="$(mktemp /tmp/mo-pr-body-XXXXXX.md)"
  _mo_pr_build_body "$epic_id" "$kickoff_path" > "$body_file"
  base="${MO_PR_BASE:-main}"
  draft_flag=""
  [ "${MO_PR_DRAFT:-0}" = "1" ] && draft_flag="--draft"

  url=$(gh pr create \
        --base "$base" \
        --head "$branch" \
        --title "$title" \
        --body-file "$body_file" \
        $draft_flag 2>&1 | grep -E '^https://github.com/[^/]+/[^/]+/pull/[0-9]+' | head -1)
  rm -f "$body_file"

  if [ -z "$url" ]; then
    # gh prints "a pull request for branch ... already exists" on dupe;
    # recover the URL via `gh pr view`.
    url=$(gh pr view "$branch" --json url --jq .url 2>/dev/null)
  fi

  if [ -z "$url" ]; then
    echo "mo_open_pr: gh pr create produced no URL" >&2
    return 1
  fi

  _mo_pr_write_url "$epic_id" "$url" "$branch"
  printf '%s\n' "$url"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "pr-create.sh — source me and call mo_open_pr <epic_id> <branch> [kickoff_path]"
fi
