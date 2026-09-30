# Scheduler retry loop — RSI across many epics

`mini-ork scheduler` drains a dependency-ordered queue of epics. By default each
epic gets **one** attempt: a failed run is `escalated` and stays there. That is
fine for one-shot delivery and wrong for a recursive self-improvement (RSI)
campaign, where a failed attempt is information: the next attempt should read
what the last one measured and try a different approach.

Everything below is **opt-in**. With no new env vars set, the scheduler behaves
exactly as before (one attempt, `escalated` on failure).

## What it adds

| Feature | Knob | Behaviour |
|---|---|---|
| Retry with a cap | `MO_SCHED_MAX_ATTEMPTS=N` or `epics.max_attempts` | A failed attempt puts the epic back to `not started` until N attempts are used; then `escalated`. |
| Fair retry order | — | Within one priority tier, the **least-attempted** ready epic goes first, so one stubborn epic cannot starve the other failures. |
| Carry-over | `MO_SCHED_CARRY_OVER` (default `1`) | Attempt N's kickoff = the original kickoff + `## Previous attempts`: verdict, reason, every failing `verifier_*.json` with its reason, and an excerpt of the run's `reflection.md` / `cycle-report.md`. Written to `runs/scheduler/kickoffs/<epic>-attempt-<N>.md`. |
| Verifier contract | `MO_SCHED_REQUIRED_VERIFIERS=a,b` | `done` also requires `runs/<run>/verifier_<name>.json` to exist and pass (`pass: true`, or `status`/`verdict` in pass/passed/success/proven/ok). A verifier that never ran counts as failing. |
| Pre-dispatch hook | `MO_SCHED_PRE_DISPATCH_HOOK=/path` | Exit `0` = dispatch; `75` = **defer** (epic returns to the queue, no attempt used, the pool stops admitting, scheduler exits `4`); anything else = a failed attempt. |
| Post-verdict hook | `MO_SCHED_POST_VERDICT_HOOK=/path` | Exit `0` = accept the computed outcome; `10` = **hold** (status `blocked`, `held_reason` = the hook's last stdout line, never auto-retried); anything else = a failed attempt. |
| Per-epic recipe | `epics.recipe` | Overrides `MO_SCHED_RECIPE` for that epic. |

Hook environment: `MO_EPIC_ID`, `MO_EPIC_ATTEMPT`, `MO_EPIC_KICKOFF`,
`MO_EPIC_RECIPE`; the post-verdict hook also gets `MO_RUN_ID`, `MO_RUN_DIR`,
`MO_VERDICT`, `MO_OUTCOME` (`done` / `failed`), `MO_OUTCOME_REASON`.

History lives in `epic_attempts` (one row per attempt: run id, recipe, kickoff,
verdict, outcome, reason). The table and the new `epics` columns are created at
runtime by `scheduler.ensure_retry_schema`, like `epics.priority`.

## CLI

```bash
mini-ork epics set <id> --recipe my-rsi --max-attempts 3
mini-ork epics retry <id> [--reset-attempts]   # release an escalated or held epic
mini-ork epics show <id>                       # now lists every attempt
```

Roadmaps can carry the settings under an epic's heading; `epics ingest` applies them:

```markdown
## Shop listing sync (id: shop-s1)
- recipe: libwit-shop-rsi
- max attempts: 3

## Checkout (id: shop-s8)
- depends on: shop-s1
```

## Example: an RSI campaign over many epics

```bash
export MO_SCHED_MAX_ATTEMPTS=3
export MO_SCHED_REQUIRED_VERIFIERS=cycle-gate,live-smoke,no-scope-creep
export MO_SCHED_PRE_DISPATCH_HOOK=$PWD/hooks/pre.sh     # probe surfaces, rebase
export MO_SCHED_POST_VERDICT_HOOK=$PWD/hooks/post.sh    # ship, or hold
mini-ork epics ingest roadmap.md
mini-ork scheduler            # run under tmux/launchd, not inside an agent turn
```

`hooks/pre.sh` — refuse to spend a run whose outcome is already known:

```bash
#!/usr/bin/env bash
curl -sf localhost:7825/api/health >/dev/null || { echo "BE down"; exit 75; }
git -C "$TARGET" fetch -q origin main && git -C "$TARGET" rebase -q origin/main \
  || { echo "rebase conflict"; exit 1; }
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
budget cap · `3` max-iters · **`4` a pre-dispatch hook deferred**.
