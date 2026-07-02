# M1: publisher commits in-place code changes on APPROVE (stop silent no-op)

## Problem (observed every dispatch this session)
`bin/mini-ork-execute` publisher node only knows the artifact-COPY model: read
`artifact_contract.yaml:outputs[]`, copy `source_artifact` → each output path. For `code-fix`
(and other in-place-edit recipes) there are no `outputs[]` — the real output is the implementer's
diff — so the publisher hits `[warn] artifact_contract.yaml has no outputs[] — skipping publish`
(line ~2616), sets status `published`, and **never commits**. The implementer's edits stay
uncommitted in the working tree; a human has to `git add`/`commit` them. This is the single biggest
blocker to the loop running unattended.

## Objective
On reviewer **APPROVE**, when a recipe has no artifact-copy `outputs[]` but the implementer made
in-place edits, the publisher should COMMIT those changed files on the current branch — instead of
skipping. Do not change the artifact-copy path for recipes that DO declare `outputs[]`.

## Deliverables (edit `bin/mini-ork-execute` publisher node)
1. In the `outputs[]`-empty branch (currently the silent skip), before returning:
   - Load the implementer's changed-file list from `${RUN_DIR}/implementer-summary.json`
     (`files_changed` array) if present.
   - Proceed to commit ONLY when: reviewer verdict is APPROVE (check the run's reviewer verdict /
     the same signal the publisher already gates on), `files_changed` is non-empty, and each path
     exists + is inside the target repo (`MO_TARGET_CWD`/git toplevel).
   - `git -C <target> add -- <each files_changed path>` then `git -C <target> commit` with a message
     like `mini-ork(<recipe>): <node_desc> [run <run_id>]`. Commit ONLY the files in
     `files_changed` — never `git add -A` (the 2026-06-13 OSS-leak class). Nothing outside the list.
   - Set status `published` + log `[publish] committed N file(s): <sha>`.
   - If verdict is NOT approve, or `files_changed` empty/absent → keep the current skip behavior
     (log why); do not commit.
2. Leave the existing artifact-copy publish path (non-empty `outputs[]`) untouched.
3. Do NOT auto-push or auto-merge to main here (that stays the separate gated auto-merge path). This
   only commits on the run's working branch.

## Smoke / DoD (must pass)
- `tests/unit/test_publisher_commit.sh` (source the executor with MINI_ORK_EXECUTE_SOURCE_ONLY=1
  if the publisher logic is reachable that way, else drive a minimal temp-repo fixture):
  - Given a temp git repo + a `${RUN_DIR}/implementer-summary.json` listing 1 changed file that
    exists + a simulated APPROVE + empty outputs[] → publisher creates exactly ONE commit whose
    changed files == `files_changed` (assert `git show --name-only` matches; assert an UNlisted
    dirty file is NOT committed).
  - Given verdict != approve → no commit (skip preserved).
  - Given a recipe WITH outputs[] → artifact-copy path still runs (existing behavior unchanged).
- `bash -n bin/mini-ork-execute` clean; existing executor tests (`test_executor_runtime_routing.sh`,
  `test_scaffold_tier.sh`) + `pytest` still green.

## Constraints (scope guard)
- Touch ONLY `bin/mini-ork-execute` (publisher node) + the new test. Commit strictly the
  `files_changed` set, only on APPROVE, only on the working branch — never `git add -A`, never push,
  never touch main. Default behavior for recipes with `outputs[]` unchanged.
