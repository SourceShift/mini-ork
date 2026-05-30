# TODO — Mirror: kickoff `Branch:` field sync to scaffold-derived branch

> **Source:** researcher repo commit `fae347a64` (2026-05-30) — `.agentflow/mini-orch/deliver.sh`

## Why this matters here

The downstream researcher fork hit the same bug class **three times in one day**: kickoff doc author declares a `Branch:` field independently of the branch-name derived from the job-id at scaffold time. When they diverge, the run-loop's worktree lookup (run.sh:164 in researcher version) fails with `ERROR: no worktree for branch <kickoff-declared-branch>` and exits before any worker dispatches.

Hit log: UCP-A2, CEB-1, CEB-2 — each required ~5 min of manual kickoff editing + retry.

## Where it lands in upstream mini-ork

`~/ps/mini-ork` uses a different architecture (CLI binaries `mini-ork-execute` etc, not a single `deliver.sh`). The equivalent surface is whatever runs **after** plan/decompose produces the per-sub-epic kickoff files and **before** `mini-ork-execute` looks up the worktree by branch.

Likely files to inspect:
- `bin/mini-ork-plan` — does it write the kickoff with a Branch field? If so, this is the right place to sync.
- `bin/mini-ork-execute` — does it parse the kickoff's Branch field to find the worktree? If yes, mirror the run.sh:164-equivalent check.

## The fix (researcher form, for reference)

`.agentflow/mini-orch/deliver.sh` got a new block right after the kickoff is copied into its canonical `_kickoff.md` variant:

```bash
# Sync kickoff's `Branch:` field to match BRANCH_BASE
if [ "${MINIORCH_BRANCH_SYNC_DISABLED:-0}" != "1" ] && [ -n "${BRANCH_BASE:-}" ]; then
  # Safe single-double-single quote concat (escaped backticks in "..." trigger
  # bash command-substitution on macOS — avoid that form)
  _new_branch_line='> **Branch:** `'"$BRANCH_BASE"'`'
  for _kf in "$KICKOFF" "$KICKOFF_DIR/${PARENT_ID}_kickoff.md"; do
    [ -f "$_kf" ] || continue
    if grep -qE '^>?[[:space:]]*\*\*Branch:\*\*' "$_kf"; then
      awk -v new="$_new_branch_line" '
        /^>?[[:space:]]*\*\*Branch:\*\*/ && !done { print new; done=1; next }
        { print }
      ' "$_kf" > "${_kf}.tmp" && mv "${_kf}.tmp" "$_kf"
    fi
  done
fi
```

## Equivalent fix in mini-ork architecture (recommended approaches)

1. **Authoritative-branch-from-jobid path** — make `mini-ork-execute` derive the branch from the job-id directly (ignore kickoff's Branch field). Removes the divergence class entirely. Trade-off: kickoff authors lose the ability to pin a specific branch.

2. **Sync-at-plan-time** — `mini-ork-plan` rewrites the kickoff's Branch field to match what the planner is about to scaffold (similar to the researcher patch). Trade-off: a kickoff that's hand-edited later can still drift.

3. **Fallback-on-mismatch** — `mini-ork-execute` first tries the kickoff's declared branch; on miss, falls back to job-id-derived branch with a warning. Trade-off: silent drift hides the original intent.

Recommend #1 for upstream (cleanest contract). #2 if back-compat with existing kickoffs is required.

## Acceptance

- Repro recipe: write a kickoff with `> **Branch:** \`feat/wrong-name\``, run `mini-ork-plan` + `mini-ork-execute` against it; verify execute succeeds (vs current behavior which would fail with no-worktree-for-branch).
- Add the kickoff-author-divergence case to upstream's test suite.

## Related researcher-side commits

- `fae347a64` fix(mini-orch): kickoff Branch sync + cn_now_iso RFC3339-strict
- Prior workarounds: `ac8318b54` (UCP-A2 manual fix), `a61bb199a` (CEB _kickoff variant manual fix)
