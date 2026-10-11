# Bug-audit PR #210 — opt-in scheduler retry loop (report only)

## Problem

PR #210 (`wt/scheduler-rsi-retry`, head `192b32dd`) adds an opt-in retry loop
to `mini-ork scheduler`: a per-epic attempt cap, least-attempted-first fairness,
carry-over kickoffs, a required-verifier contract, pre-dispatch / post-verdict
hooks, a per-epic recipe, an `epic_attempts` history table created at runtime,
and `epics set` / `epics retry` CLI subcommands. Its claim: "with no new env
set, behaviour is unchanged." Find the DEFECTS in this change before it merges.

## Where the code is

- Checkout of the PR head (read-only, run every command here):
  `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review`
- Full PR diff (958 lines): `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210.diff`
- Base commit: `4b854bd1` (`git -C /Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review diff 4b854bd1 HEAD`)

## Files in scope

Changed by the PR (the review target):
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/mini_ork/scheduler.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/mini_ork/cli/epics.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/tests/unit/test_scheduler_retry.py`
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/docs/operator/scheduler-retry.md`

Unchanged code the PR depends on (read it to judge the change — cite it as the contract side):
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/mini_ork/epic_graph.py` (status transitions, `on_done`, what `blocked` means today)
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/mini_ork/cli/execute.py` and `mini_ork/cli/publisher.py` (what a real run writes into `runs/<run_id>/`: verdict files, verifier result file NAMES, reflection / cycle-report files, the `run_id=` line)
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/pr210-review/db/` (the `epics` table schema and its status values)

## Feature inventory (use these names in the "Feature" field)

- F1 retry-cap — `MO_SCHED_MAX_ATTEMPTS` / `epics.max_attempts`, `_max_attempts`, `_settle`
- F2 fairness — `pick_ready` ORDER BY attempts
- F3 carry-over — `build_carry_over_kickoff`, `_failing_verifiers`, `_excerpt`
- F4 required-verifiers — `check_required_verifiers`, `_verifier_passes`
- F5 pre-dispatch-hook — `_run_hook`, defer rc 75, `run_pool` deferred flag, `main` exit 4
- F6 post-verdict-hook — hold rc 10, `blocked` + `held_reason`
- F7 per-epic-recipe — `epics.recipe` → `resolve_kickoff` + runner argv
- F8 retry-schema — `ensure_retry_schema`, `epic_attempts`, `_record_attempt` (INSERT OR REPLACE)
- F9 epics-cli — `epics set`, `epics retry [--reset-attempts]`, `epics show`, roadmap `- recipe:` / `- max attempts:` ingest
- F10 docs-contract — every claim in `docs/operator/scheduler-retry.md` and the scheduler `_USAGE` block vs the code

## Lens guidance

The lens prompts carry heuristics written for a different (TypeScript/Express)
codebase. Ignore those specifics and apply each lens STANCE to this Python
scheduler. For the MiniMax (user-impact) lens, the "user" is the OPERATOR
running `mini-ork scheduler` under tmux/launchd and the `epics` CLI.

Risk surfaces worth examining (evaluate each against the code — confirm or
refute, do not assume):
- exceptions raised inside `dispatch_epic` (hook not executable / not found,
  malformed env, malformed verifier JSON) and what `run_pool` does with them —
  what status the epic is left in and whether the pool re-picks it
- a hook that never exits (no timeout)
- `epics retry --reset-attempts` followed by new attempts, against the
  `epic_attempts` primary key and `INSERT OR REPLACE`
- whether real runs actually write `verifier_<name>.json`, `reflection.md`,
  `cycle-report.md` — or whether only the test's fake runner does
- `_verifier_passes` when `pass` is explicitly false but `status` is a pass word
- `run_id=` extraction when the runner log carries more than one `run_id=` line,
  vs how `_verdict_from_log` picks its run
- whether anything else in the codebase already moves epics into or out of
  `blocked`, and what that does to a held epic and its `held_reason`
- the window between `pick_ready` and `_set_status('in progress')` now that
  the pre-dispatch hook runs inside it
- `ensure_retry_schema` running on every `pick_ready` poll and the
  `except OperationalError: return []` path (a locked DB looking like an empty queue)
- the daemon (`main` loop) exiting 4 on a deferral vs the docs' "requeue" wording
- default-off guarantee: does anything change when NO new env var is set?

## Definition of Done

1. Four lens reports under `${MINI_ORK_RUN_DIR}/lens-{glm,kimi,codex,minimax}.md`,
   each finding citing `file:line` in the PR checkout.
2. `${MINI_ORK_RUN_DIR}/synthesis.md` — deduplicated, consensus-marked,
   severity-sorted, referencing all four lenses.
3. `verifiers/lens-completeness.py` reports pass=true.

## Non-goals

- Report only. Do NOT edit, stage, or commit anything in either checkout.
- No patches; the fix shape is a one-line pointer.
- Do not report pre-existing defects in unchanged lines unless the PR makes them
  reachable or worse.
