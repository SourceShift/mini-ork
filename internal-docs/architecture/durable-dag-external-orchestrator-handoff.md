# External-orchestrator handoff — durable-DAG recovery

For a caller that drives mini-ork as a sub-step (e.g. the Researcher compose
planner, or any parent orchestrator): how to supply a root trace, receive
checkpoints, and request recovery, so a mini-ork run is a resumable, traceable
unit inside your larger pipeline. Companion to the design note (E1–E5) and the
operator runbook.

## The contract in one paragraph

You give mini-ork a run id, a workflow, and (optionally) a root trace context.
mini-ork checkpoints each node as it completes, into a state.db you can read.
If a node fails or the worker dies, you (or an operator) call `recover` — mini-ork
reuses the valid checkpoints and re-runs only the failed closure, correlating
every attempt under the root trace you supplied. Recovery is idempotent and
single-writer, so it is safe to retry from your control loop.

## 1. Supply a root trace

Export the trace id you want every mini-ork span to nest under, before you start
the run:

```bash
export MINI_ORK_ROOT_TRACE_ID="<your-root-trace-id>"
mini-ork run <recipe> <kickoff.md>
```

mini-ork honours a caller-supplied `MINI_ORK_ROOT_TRACE_ID` verbatim and pins it
across the run → node → dispatch (→ sandbox) boundary. Every node attempt —
original and recovered — carries queryable attributes under that root:
`trace.root_id`, `run.id`, `node.id`, `node.attempt`, `recovery.is_recovery`,
and (on a resumed attempt) `recovery.request_id` + `resume.session_id`. A
recovered attempt is a new child span under your root, never a disconnected
synthetic trace. If you supply nothing, mini-ork derives a stable root from the
run id so attempts still correlate.

## 2. Receive checkpoints

Each node's durable checkpoint lands in `state.db`:

- `node_checkpoints` — one row per completed node: `(run_id, node_id, status,
  input_hash, recipe_version, config_hash, artifact_manifest_json, session_ref)`.
  A node is *reusable* iff the three hashes match your current run AND every
  artifact in the manifest re-hashes. This is the correctness gate — read it,
  don't guess.
- `node_attempts` — the attempt history per node (result, failure_class,
  provider_session_id, cost).

Poll or subscribe to these to know how far a run got and what it cost. The
DAG-shaped projection is available over HTTP:

```
GET /api/v1/runs/<run_id>/recovery
→ { run_id, nodes:[{node_id,status,reusable,attempts:[…]}],
    active_recovery, lease, next_action }
```

## 3. Request recovery

From your control loop, on a failed/stalled run:

```bash
mini-ork recover <run_id> --strategy resume            # reuse valid, redo the closure
mini-ork recover <run_id> --status                     # inspect first (no dispatch)
mini-ork recover --cancel <request_id>                 # abandon a recovery, keep checkpoints
```

Guarantees you can rely on:
- **Idempotent** — repeated `recover <run_id> --strategy resume` for the same
  `(run_id, from_node, strategy)` returns the in-flight request; the node runs
  once, not once-per-call. Safe to wire into a retry loop.
- **Single-writer** — a run has one recovery lease. A second concurrent recover
  gets a safe "already being recovered" result and dispatches nothing.
- **Budget-bounded** — a recovery that needs a fresh LLM attempt (provider_limit
  / repair) is capped by `recovery_requests.budget_usd`; it can never become an
  unbounded auto-retry.
- **Non-destructive** — `--cancel` releases the lease and leaves every valid
  checkpoint reusable.

## 4. Failure classes you'll observe

`node_attempts.failure_class` / `recovery_requests.failure_class`:

| class | your action |
|---|---|
| `infra_interrupt` | auto-recoverable; a plain `recover` re-runs it |
| `provider_limit` | explicit, budget-bounded recover (e.g. after a rate-limit / max-turns) |
| `output_invalid` | recover with a repair strategy (a fresh LLM attempt) |
| `input_required` | supply the missing input, then recover |
| `terminal` | the run is failed; checkpoints still readable for a manual redo |

## Minimal parent-orchestrator loop

```
set MINI_ORK_ROOT_TRACE_ID = my_span.trace_id
run mini-ork
on completion:
    view = GET /api/v1/runs/<run_id>/recovery
    if view.next_action indicates a failure:
        POST recover <run_id> --strategy resume   # idempotent, single-writer, budget-bounded
    poll view until all nodes reusable or class==terminal
```

Sandbox/worker death between your calls is fine: checkpoints are durable in
state.db and (E4) claude transcripts are persisted into the run dir, so a fresh
sandbox restores and resumes at the interrupted turn.
