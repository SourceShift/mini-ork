#!/usr/bin/env bash
# lib/repo_integrity_guard.sh — standing guard against cross-repo clobbers
# of the current branch ref.
#
# Problem this solves: a foreign dispatch (codex sandbox cleanup, framework
# edit, or rogue subagent) has been observed orphaning the current branch
# tip onto an UNRELATED, OLDER commit. The post-commit watchdog
# (.githooks/post-commit) catches this only when a local commit just
# happened — there's a window (between commits, or when no commit ever
# happened locally on this clone) where the clobber slips through.
#
# This guard runs on every mini-ork startup so the window collapses to
# "next dispatch step". It is best-effort: a transient git error must
# never abort the calling mini-ork run.
#
# Strategy:
#   1. Resolve baseline SHA — priority:
#        a. .mini-ork/last-known-good-ref.<branch> (preferred)
#        b. origin/main  (fallback for fresh clones / no prior file)
#        c. Most recent reflog entry for the branch (last-resort fallback)
#   2. If no baseline exists  → cold start, just record current tip and exit.
#   3. If baseline == current tip → up-to-date, re-record, exit.
#   4. Two-condition clobber test (mirrors .githooks/post-commit lines 80-94):
#        a. ! merge-base --is-ancestor <baseline> <tip>     (commit orphaned)
#        b. tip_ct < baseline_ct                            (not a fresh amend)
#      Both true → CLOBBER. Restore via `git update-ref` only — never the
#      destructive git operations that mutate HEAD or the working tree;
#      those are out of scope for a guard that runs on every startup.
#   5. Otherwise → legitimate advance / fast-forward. Record new tip.
#
# Recovery log lives at .mini-ork/repo-integrity-guard.log (TSV):
#   <iso-timestamp>\t<baseline>\t<old-tip>\t<action>
#
# Escape hatch:
#   MO_REPO_INTEGRITY_GUARD_DISABLED=1   → exit 0 immediately, no I/O.

repo_integrity_check_and_heal() {
  set -uo pipefail

  # Escape hatch + non-worktree short-circuit (per kickoff).
  if [ "${MO_REPO_INTEGRITY_GUARD_DISABLED:-0}" = "1" ]; then
    return 0
  fi
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$REPO_ROOT" ] || return 0
  cd "$REPO_ROOT" || return 0

  # Detached HEAD → nothing to guard (no branch ref to clobber).
  BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  [ -n "$BRANCH" ] || return 0

  CUR_TIP="$(git rev-parse --verify -q HEAD 2>/dev/null || true)"
  [ -n "$CUR_TIP" ] || return 0

  mkdir -p "$REPO_ROOT/.mini-ork" 2>/dev/null || true
  LKG_FILE="$REPO_ROOT/.mini-ork/last-known-good-ref.$BRANCH"
  LOG_FILE="$REPO_ROOT/.mini-ork/repo-integrity-guard.log"

  # ── baseline resolution (priority: file → origin/main → reflog) ─────────
  baseline=""
  if [ -s "$LKG_FILE" ]; then
    baseline="$(cat "$LKG_FILE" 2>/dev/null || true)"
  fi
  if [ -z "$baseline" ] && git rev-parse --verify -q origin/main >/dev/null 2>&1; then
    baseline="$(git rev-parse --verify -q origin/main 2>/dev/null || true)"
  fi
  if [ -z "$baseline" ]; then
    # reflog fallback — most recent entry for this branch ref. `git reflog
    # show refs/heads/<branch>` lists SHA + action; take the first column.
    baseline="$(git reflog show "refs/heads/$BRANCH" --format='%H' -n 1 2>/dev/null \
                | head -1 || true)"
  fi

  # ── cold start: no baseline anywhere → record and exit ──────────────────
  if [ -z "$baseline" ]; then
    printf '%s\n' "$CUR_TIP" > "$LKG_FILE" 2>/dev/null || true
    return 0
  fi

  # ── identity: tip equals baseline → up-to-date, re-record ───────────────
  if [ "$CUR_TIP" = "$baseline" ]; then
    printf '%s\n' "$CUR_TIP" > "$LKG_FILE" 2>/dev/null || true
    return 0
  fi

  # ── two-condition clobber test ──────────────────────────────────────────
  # Condition 1: baseline is NOT an ancestor of current tip (commit was
  #              orphaned by a sideways reset). merge-base --is-ancestor
  #              returns 0 (true) when baseline is reachable from tip;
  #              a tip == baseline would also return 0, but we already
  #              handled that case above.
  orphan=1
  if git merge-base --is-ancestor "$baseline" "$CUR_TIP" 2>/dev/null; then
    orphan=0
  fi

  # Condition 2: tip_ct < baseline_ct. A fresh amend/rebase/fast-forward
  #              produces a tip with commit-time >= baseline; only a
  #              reset to a stale/foreign commit satisfies this.
  tip_ct="$(git show -s --format=%ct "$CUR_TIP" 2>/dev/null || echo 0)"
  base_ct="$(git show -s --format=%ct "$baseline" 2>/dev/null || echo 0)"
  older=0
  if [ "${tip_ct:-0}" -lt "${base_ct:-0}" ]; then
    older=1
  fi

  if [ "$orphan" -eq 1 ] && [ "$older" -eq 1 ]; then
    # ── CLOBBER DETECTED — heal via update-ref only ─────────────────────
    # 3-arg form: refs/heads/<branch> = <baseline>, expecting <CUR_TIP>
    # gives us atomic compare-and-swap, so we don't clobber a concurrent
    # legitimate change that landed between our read and write.
    if git update-ref "refs/heads/$BRANCH" "$baseline" "$CUR_TIP" 2>/dev/null; then
      printf '%s\t%s\t%s\trestored-branch-clobbered-from-%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$baseline" "$CUR_TIP" "$CUR_TIP" \
        >> "$LOG_FILE" 2>/dev/null || true
    fi
    # Per kickoff: do NOT rewrite LKG_FILE on heal — the recorded good SHA
    # is still the right baseline for future comparisons.
    return 0
  fi

  # ── legitimate advance — re-record ─────────────────────────────────────
  printf '%s\n' "$CUR_TIP" > "$LKG_FILE" 2>/dev/null || true
  return 0
}