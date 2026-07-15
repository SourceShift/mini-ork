# Operator runbook — durable-DAG recovery

How to inspect, recover, cancel, and diagnose a stalled or failed run, without
deleting valid work or paying for it twice. Companion to the design note
`internal-docs/architecture/2026-07-15-durable-dag-resume-design.md` (E1–E5).

## The three "resume" verbs — don't conflate them

mini-ork has three distinct ways to continue a run. Pick by *why* it stopped.

| Verb | Command | When | What it does |
|---|---|---|---|
| **cost-pause resume** | `mini-ork resume <run_id>` | the run hit the cost circuit and parked | clears the cost sentinel and continues the loop from where it paused |
| **steering resume** | `mini-ork resume <run_id> --steer …` | you want to nudge an in-flight run | injects operator steering, then continues |
| **execution recovery** | `mini-ork recover <run_id>` | a node FAILED or the worker/sandbox died | reuses valid checkpoints, re-dispatches only the failed-node closure |

`recover` is the durable-DAG path and the subject of this runbook. It never
re-runs a node whose checkpoint is still valid, and (E4) it resumes a claude
node at its interrupted turn when a transcript was persisted.

## Inspect — always start here (read-only, no dispatch, no spend)

```bash
mini-ork recover <run_id> --status
```

Prints the reuse/rerun split: which nodes are reusable (valid E1 checkpoint),
which will rerun, the closure entry node, and the cost boundary. It dispatches
nothing and calls no model — safe to run any time.

Web view (same data, DAG-shaped): `GET /api/v1/runs/<run_id>/recovery` renders
completed nodes as completed, the failed node with its attempt history, the
active recovery, and the lease holder — read straight from the durable tables
(`node_checkpoints`, `node_attempts`, `recovery_requests`, `run_leases`), never
log scraping.

## Recover — re-dispatch only the failed closure

```bash
mini-ork recover <run_id> --strategy resume
# widen the entry if you want to redo more than the failed node:
mini-ork recover <run_id> --strategy resume --from-node <node_id>
```

What happens (design §5/§7):
1. registers an idempotent recovery request keyed on `(run_id, from_node, strategy)`
2. acquires the run's single-writer **lease** — if another recovery already holds
   it you get a safe "already being recovered" message and nothing dispatches
   (the node runs once, never twice)
3. exports the closure env + (E4) `MO_RESUME_SESSION_ID` for any claude node
   with a persisted transcript, then hands off to `execute`, which reuses valid
   checkpoints and re-runs only the closure

Auto-recovery policy (design §5): only `infra_interrupt` stops auto-recover.
A `provider_limit` stop (including **max-turns**) or an `output_invalid` stop
needs an explicit, budget-bounded recover — it is never an unbounded retry loop.
Only a `terminal` stop marks the whole run failed; the other four leave a
recovery record you can act on.

## Cancel — stop a pending recovery without losing work

```bash
mini-ork recover --cancel <request_id>
```

Marks the recovery request cancelled, **releases the lease** (so a fresh
recovery can acquire it), and leaves every `node_checkpoints` row intact — your
completed nodes stay reusable. Use it when a recovery is wedged or you want to
re-scope with a different `--from-node`. Find the `request_id` in
`--status`/the recovery view (`active_recovery.request_id`).

## Diagnose — a short decision tree

- **"No plan.json found"** on recover → the run dir lacks a plan but you have a
  workflow. Set `MINI_ORK_WORKFLOW` (or `--workflow`) to the recipe's
  `workflow.yaml`; recovery walks the DAG from that.
- **`--status` shows every node as `rerun (hash_mismatch)`** → the recovery's
  `task_class`/`recipe` differs from the original run, so `config_hash` doesn't
  match. Pin `MINI_ORK_TASK_CLASS` / `MINI_ORK_RECIPE` to the original run's
  values.
- **Nodes read as `rerun (no_row)`** → those nodes never checkpointed (they
  failed before publishing). Expected — they will re-run. Not an error.
- **"already being recovered (lease held)"** → another worker/operator holds the
  lease. Either wait for it, or if it is stale, `--cancel <request_id>` then
  recover again.
- **A stale worker's checkpoint was rejected** (`fence rejected` in the log) →
  correct behaviour: its lease expired and a newer recovery owns the run; the
  stale write is fenced out (design §7). No action needed.
- **codex node won't turn-resume** → by design. codex has its own session model;
  it falls back to node-level resume (re-runs the node fresh). Only claude-lane
  nodes (opus/sonnet/kimi/minimax/glm) resume at the interrupted turn.

## What recovery never does

- never re-runs a node whose checkpoint is valid (input/recipe/config hashes
  match AND every artifact re-hashes)
- never invalidates checkpoints on cancel
- never lets two recoveries dispatch the same node concurrently (lease)
- never re-fires a completed non-idempotent tool call on replay (E4 receipts)
