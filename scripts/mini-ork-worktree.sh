#!/usr/bin/env bash
# mini-ork-worktree.sh — worktree-first dev for mini-ork.
#
# Keep `main` clean: never branch/commit implementation work in the main
# checkout. Each task gets its own worktree + branch; when green it rebases onto
# origin/main and pushes straight to main; then the worktree is torn down.
#
# Adapted from researcher's scripts/codex-worktree.sh — the pnpm/node_modules
# linking and sparse-cone machinery are dropped (mini-ork is bash+python, no
# dependency tree to share), but the CAID file-ownership registry is kept: two
# concurrent agents that edit the SAME file are where parallel dev collapses, so
# `--owns` refuses a second worktree whose claimed paths overlap a live one.
set -euo pipefail

# ROOT = the main checkout. Auto-detected as the worktree currently on `main`,
# so this works no matter which linked worktree invokes the script.
detect_root() {
  git worktree list --porcelain 2>/dev/null | awk '
    /^worktree /   { wt=$2 }
    /^branch /     { if ($2=="refs/heads/main") { print wt; exit } }'
}
ROOT="${MINI_ORK_ROOT:-$(detect_root)}"
WORKTREES_DIR="${MINI_ORK_WORKTREES_DIR:-/Volumes/docker-ssd/ps/mini-ork-worktrees}"
BRANCH_PREFIX="${MINI_ORK_BRANCH_PREFIX:-wt}"
OWNERSHIP_FILE="${MINI_ORK_OWNERSHIP_FILE:-$WORKTREES_DIR/.ownership}"

usage() {
  cat <<'EOF'
Usage:
  scripts/mini-ork-worktree.sh create <slug> [--owns <path>...] [--branch <name>]
  scripts/mini-ork-worktree.sh merge  [<slug>]        # rebase origin/main, test, push HEAD:main
  scripts/mini-ork-worktree.sh clean  <slug>          # remove worktree + delete branch + release claims
  scripts/mini-ork-worktree.sh owners [--json]        # list active file claims
  scripts/mini-ork-worktree.sh release <slug>         # drop a slug's claims
  scripts/mini-ork-worktree.sh list                   # git worktree list

Dev loop:
  create → work + commit in the worktree → merge (green-gated push to main) → clean

--owns <path> (repeatable) CLAIMS those paths; creation is refused if a claim
overlaps a live worktree's claim (path-prefix aware). Released on `clean`/`release`
or when the worktree dir disappears.
EOF
}

die() { echo "[mo-worktree] $*" >&2; exit 1; }

sanitize_slug() {
  local slug="$1"
  slug="${slug//[^A-Za-z0-9._-]/-}"; slug="${slug##-}"; slug="${slug%%-}"
  [ -n "$slug" ] || die "slug must contain at least one alphanumeric character"
  printf '%s\n' "$slug"
}

assert_root() {
  [ -n "$ROOT" ] || die "could not locate the main worktree; set MINI_ORK_ROOT"
  [ -d "$ROOT/.git" ] || git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || die "ROOT is not a git checkout: $ROOT"
}

# ── CAID file-ownership registry ───────────────────────────────────────────
normalize_path() { local p="$1"; p="${p#./}"; p="${p%/}"; printf '%s' "$p"; }

paths_overlap() {
  local a b; a="$(normalize_path "$1")"; b="$(normalize_path "$2")"
  [ "$a" = "$b" ] && return 0
  case "$b/" in "$a/"*) return 0 ;; esac
  case "$a/" in "$b/"*) return 0 ;; esac
  return 1
}

prune_ownership() {
  [ -f "$OWNERSHIP_FILE" ] || return 0
  local tmp slug path; tmp="$(mktemp)"
  while IFS=$'\t' read -r slug path; do
    [ -n "$slug" ] || continue
    [ -d "$WORKTREES_DIR/$slug" ] && printf '%s\t%s\n' "$slug" "$path" >>"$tmp"
  done < "$OWNERSHIP_FILE"
  mv "$tmp" "$OWNERSHIP_FILE"
}

assert_no_ownership_conflict() {
  local slug="$1"; shift; local claims=("$@")
  prune_ownership
  [ -f "$OWNERSHIP_FILE" ] || return 0
  local rslug rpath claim
  while IFS=$'\t' read -r rslug rpath; do
    [ -n "$rslug" ] || continue
    [ "$rslug" = "$slug" ] && continue
    for claim in "${claims[@]}"; do
      if paths_overlap "$claim" "$rpath"; then
        die "ownership conflict: '$claim' overlaps '$rpath' held by live worktree '$rslug'. Pick a non-overlapping surface, wait for '$rslug' to merge, or 'release $rslug' if it's stale."
      fi
    done
  done < "$OWNERSHIP_FILE"
}

register_ownership() {
  local slug="$1"; shift
  mkdir -p "$WORKTREES_DIR"
  local claim
  for claim in "$@"; do
    printf '%s\t%s\n' "$slug" "$(normalize_path "$claim")" >> "$OWNERSHIP_FILE"
  done
}

release_ownership() {
  local slug="$1"
  [ -f "$OWNERSHIP_FILE" ] || return 0
  local tmp rslug rpath; tmp="$(mktemp)"
  while IFS=$'\t' read -r rslug rpath; do
    [ "$rslug" = "$slug" ] && continue
    [ -n "$rslug" ] && printf '%s\t%s\n' "$rslug" "$rpath" >>"$tmp"
  done < "$OWNERSHIP_FILE"
  mv "$tmp" "$OWNERSHIP_FILE"
}

list_owners() {
  prune_ownership
  if [ "${1:-}" = "--json" ]; then
    local first=1 rslug rpath
    printf '['
    if [ -f "$OWNERSHIP_FILE" ]; then
      while IFS=$'\t' read -r rslug rpath; do
        [ -n "$rslug" ] || continue
        [ $first -eq 1 ] && first=0 || printf ','
        printf '{"slug":"%s","path":"%s"}' "$rslug" "$rpath"
      done < "$OWNERSHIP_FILE"
    fi
    printf ']\n'
  else
    if [ -s "$OWNERSHIP_FILE" ]; then cat "$OWNERSHIP_FILE"; else echo "(no active claims)"; fi
  fi
}

# ── commands ───────────────────────────────────────────────────────────────
create_worktree() {
  local slug="$1"; shift
  local branch="" owns=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --owns)   [ $# -ge 2 ] || die "--owns requires a path"; owns+=("$2"); shift 2 ;;
      --branch) [ $# -ge 2 ] || die "--branch requires a name"; branch="$2"; shift 2 ;;
      *) die "unknown create option: $1" ;;
    esac
  done
  assert_root
  local safe_slug wt; safe_slug="$(sanitize_slug "$slug")"
  [ -n "$branch" ] || branch="${BRANCH_PREFIX}/${safe_slug}"
  wt="$WORKTREES_DIR/$safe_slug"

  if [ "${#owns[@]}" -gt 0 ]; then
    assert_no_ownership_conflict "$safe_slug" "${owns[@]}"
  fi
  [ ! -e "$wt" ] || die "worktree path already exists: $wt"
  mkdir -p "$WORKTREES_DIR"

  # Sync to origin/main so the branch starts from the latest published tip.
  git -C "$ROOT" fetch --quiet origin main || true
  local base; base="$(git -C "$ROOT" rev-parse --verify --quiet origin/main || git -C "$ROOT" rev-parse HEAD)"
  # ALLOW_WORKTREE_BRANCH_CREATE=1 satisfies the reference-transaction guard.
  ALLOW_WORKTREE_BRANCH_CREATE=1 git -C "$ROOT" worktree add -b "$branch" "$wt" "$base"

  if [ "${#owns[@]}" -gt 0 ]; then
    register_ownership "$safe_slug" "${owns[@]}"
    echo "[mo-worktree] claimed: ${owns[*]}" >&2
  fi
  echo "[mo-worktree] ready: $wt  (branch $branch)"
}

merge_worktree() {
  local wt slug branch
  if [ $# -ge 1 ]; then
    slug="$(sanitize_slug "$1")"; wt="$WORKTREES_DIR/$slug"
    [ -d "$wt" ] || die "no worktree for slug '$slug' at $wt"
  else
    wt="$(git rev-parse --show-toplevel)"; slug="$(basename "$wt")"
  fi
  [ "$wt" != "$ROOT" ] || die "refusing to merge from the main checkout; run merge inside a task worktree"
  [ -z "$(git -C "$wt" status --porcelain)" ] || die "worktree is dirty: commit or stash before merging: $wt"
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD)"

  git -C "$wt" fetch origin main
  git -C "$wt" rebase origin/main
  # Green gate: never push a red branch to main. Override the command per-task
  # with MINI_ORK_TEST_CMD (e.g. a scoped pytest path for a fast, focused gate).
  local test_cmd="${MINI_ORK_TEST_CMD:-python3 -m pytest -q}"
  ( cd "$wt" && eval "$test_cmd" ) || die "green gate failed ($test_cmd) in $wt; fix before merging"
  git -C "$wt" push origin "HEAD:main"
  echo "[mo-worktree] merged $branch -> origin/main. Tear down with: scripts/mini-ork-worktree.sh clean $slug"
}

clean_worktree() {
  local slug wt branch; slug="$(sanitize_slug "$1")"; wt="$WORKTREES_DIR/$slug"
  assert_root
  if [ -d "$wt" ]; then
    branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    git -C "$ROOT" worktree remove "$wt" || git -C "$ROOT" worktree remove --force "$wt"
    [ -n "$branch" ] && [ "$branch" != "main" ] && git -C "$ROOT" branch -d "$branch" 2>/dev/null || true
  fi
  release_ownership "$slug"
  echo "[mo-worktree] cleaned $slug"
}

cmd="${1:-}"
case "$cmd" in
  create)  [ $# -ge 2 ] || die "usage: create <slug> [--owns <path>...] [--branch <name>]"; shift; create_worktree "$@" ;;
  merge)   shift; merge_worktree "$@" ;;
  clean)   [ $# -eq 2 ] || die "usage: clean <slug>"; clean_worktree "$2" ;;
  owners)  list_owners "${2:-}" ;;
  release) [ $# -eq 2 ] || die "usage: release <slug>"; release_ownership "$(sanitize_slug "$2")"; echo "[mo-worktree] released claims for $2" ;;
  list)    git worktree list ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
