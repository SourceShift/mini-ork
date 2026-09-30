# Scheduler retry loop — RSI across many epics

`mini-ork scheduler` drains a dependency-ordered queue of epics. By default each
epic gets **one** attempt: a failed run is `escalated` and stays there. That is
fine for one-shot delivery and wrong for a recursive self-improvement (RSI)
campaign, where a failed attempt is information: the next attempt should read
what the last one measured and try a different approach.

Everything below is **opt-in**. With no new env vars set, the scheduler behaves
exactly as before (one attempt, `escalated` on failure). The one new path that
re-dispatches an epic without any env — `epics retry` — gets the carry-over
kickoff; set `MO_SCHED_CARRY_OVER=0` to hand it the original kickoff instead.

## What it adds

| Feature | Knob | Behaviour |
|---|---|---|
| Retry with a cap | `MO_SCHED_MAX_ATTEMPTS=N` or `epics.max_attempts` | A failed attempt puts the epic back to `not started` until N attempts are used; then `escalated`. |
| Fair retry order | — | Within one priority tier, the **least-attempted** ready epic goes first, so one stubborn epic cannot starve the other failures. |
| Carry-over | `MO_SCHED_CARRY_OVER` (default `1`) | Re-dispatching an epic that has recorded attempts hands the runner the original kickoff + `## Previous attempts` for the last two: verdict, reason, every failing `verifier_*.json` with its reason (`reason`, else `error_summary`), and the run's own notes — `reflection.md` / `cycle-report.md` when the recipe writes one, else the reviewer's `review-reviewer.json`. Written to `runs/scheduler/kickoffs/<epic>-attempt-<N>.md` (N = history number). |
| Verifier contract | `MO_SCHED_REQUIRED_VERIFIERS=a,b` | `done` also requires `runs/<run>/verifier_<name>.json` to exist and pass. A payload with a `pass` key is judged by it alone (`true` passes); without one, `status`/`verdict` must be pass/passed/success/proven/ok. The executor writes this file as a copy of the verifier's log, so log lines before the JSON are expected — the last top-level JSON object is read. A verifier that never ran counts as failing. |
| Pre-dispatch hook | `MO_SCHED_PRE_DISPATCH_HOOK=/path` | Exit `0` = dispatch; `75` = **defer** (epic returns to the queue, no attempt used, the pool stops admitting; `--once` exits `4`, the daemon idles `--idle-secs` and re-probes); anything else = a failed attempt. |
| Post-verdict hook | `MO_SCHED_POST_VERDICT_HOOK=/path` | Exit `0` = accept the computed outcome; `10` = **hold** (status `blocked`, `held_reason` = the hook's last stdout line); anything else = a failed attempt. A held epic is never released by a dependency finishing — only `epics retry` releases it. |
| Hook timeout | `MO_SCHED_HOOK_TIMEOUT_S` (default `600`) | A hook still running at the limit is killed with its child processes. A timed-out hook (rc `124`) or one that cannot start — missing, not executable (rc `126`) — is a failed attempt. |
| Per-epic recipe | `epics.recipe` | Overrides `MO_SCHED_RECIPE` for that epic. |

Hook environment: `MO_EPIC_ID`, `MO_EPIC_ATTEMPT`, `MO_EPIC_KICKOFF`,
`MO_EPIC_RECIPE`; the post-verdict hook also gets `MO_RUN_ID`, `MO_RUN_DIR`,
`MO_VERDICT`, `MO_OUTCOME` (`done` / `failed`), `MO_OUTCOME_REASON`.

The value is a path to one executable, not a command line. Hook output goes to
`runs/scheduler/dispatch-<ts>-<epic>-a<N>.hooks.log`, next to the run's own log.

History lives in `epic_attempts`: one row per attempt (run id, recipe, kickoff,
verdict, outcome, reason, start and finish time), numbered 1, 2, 3… over the
epic's whole life. `epics.attempts` counts the attempts in the current cap cycle;
`epics retry --reset-attempts` restarts that count, never the history. The table
and the new `epics` columns are created at runtime by
`scheduler.ensure_retry_schema`, like `epics.priority`.

### What the gates do not undo

The required verifiers and the post-verdict hook decide the epic's **status**
after the run. They do not revert what the recipe already did inside the run —
`epic-runner` and other in-place recipes commit their edits to the target tree
before the scheduler sees the verdict. If a rejected attempt must not leave its
commit behind for the next attempt to build on, reset the target to a clean base
in the pre-dispatch hook (see the example below).

## CLI

```bash
mini-ork epics set <id> --recipe my-rsi --max-attempts 3
mini-ork epics retry <id> [--reset-attempts]   # release an escalated or held epic:
                                               #   one attempt past its cap, or with
                                               #   the flag a fresh cycle of max_attempts
mini-ork epics show <id>                       # now lists every attempt
```

Roadmaps can carry the settings under an epic's heading; `epics ingest` applies them:

```markdown
## Data import (id: import-1)
- recipe: my-rsi
- max attempts: 3

## Reporting (id: report-1)
- depends on: import-1
```

## Example: an RSI campaign over many epics

```bash
export MO_SCHED_MAX_ATTEMPTS=3
export MO_SCHED_REQUIRED_VERIFIERS=cycle-gate,live-smoke,no-scope-creep
export MO_SCHED_PRE_DISPATCH_HOOK=$PWD/hooks/pre.sh     # probe surfaces, clean base
export MO_SCHED_POST_VERDICT_HOOK=$PWD/hooks/post.sh    # ship, or hold
mini-ork epics ingest roadmap.md
mini-ork scheduler            # run under tmux/launchd, not inside an agent turn
```

`hooks/pre.sh` — refuse to spend a run whose outcome is already known, and start
every attempt from `origin/main` so a rejected attempt's commit is not inherited
(`$TARGET` must be a worktree dedicated to the campaign — this discards its
local commits):

```bash
#!/usr/bin/env bash
curl -sf --max-time 10 "$APP_HEALTH_URL" >/dev/null || { echo "app down"; exit 75; }
git -C "$TARGET" fetch -q origin main && git -C "$TARGET" reset -q --hard origin/main \
  || { echo "cannot reset $TARGET to origin/main"; exit 1; }
```

`hooks/post.sh` — ship what passed, hold what needs a human:

```bash
#!/usr/bin/env bash
if grep -q destructive "$MO_RUN_DIR/verifier_cycle-gate.json" 2>/dev/null; then
  echo "destructive migration held for review"; exit 10
fi
[[ "$MO_OUTCOME" == done ]] || exit 0          # failures retry on their own
git -C "$TARGET" push -q origin HEAD:main || { echo "push failed"; exit 1; }
```

Exit codes of `mini-ork scheduler`: `0` drained · `1` fatal · `2` cost pause /
budget cap · `3` max-iters · **`4` a pre-dispatch hook deferred under `--once`**
(the daemon idles and re-probes instead of exiting).
