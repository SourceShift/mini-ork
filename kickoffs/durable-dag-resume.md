# Durable DAG resume and recovery

## Goal

Make a Mini-ork run resumable from its last **validated DAG checkpoint** after a
node, agent, runner, or sandbox failure. Recovery must reuse already-completed
work and its artifacts instead of restarting the workflow or repaying for
upstream LLM calls.

This capability must be generic: it will be used by the Researcher compose
planner, but it must work for ordinary Mini-ork recipes too.

## Why this matters

The current failure mode is expensive and misleading:

- a long-running agent can hit a model turn limit after useful upstream nodes
  completed;
- a sandbox or worker can stop after artifacts were produced;
- operators can currently resume a cost pause or provide steering, but cannot
  safely resume arbitrary DAG execution from a durable node boundary;
- re-running the whole workflow repeats prior model spend and makes the UI and
  traces look like duplicate work.

The target behaviour is: **resume the failed or first-incomplete node, never
silently replay valid ancestors.**

## Product contract

### 1. A checkpoint is a safe semantic boundary

Mini-ork must define and persist checkpoints only when all of the following
are true:

1. A node has produced its intended output.
2. Required artifacts are present, non-empty where applicable, and integrity
   checked.
3. The node's deterministic validation/verification requirements have passed.
4. The checkpoint contains enough provenance to decide whether it remains
   valid for a later recovery.

A partially streamed response, an in-memory agent thought, or a tool call with
unknown side effects is **not** automatically a resumable checkpoint.

Mini-ork may retain provider session ids, transcripts, tool receipts, and
intermediate progress to make a retry cheaper. Those are optional continuation
optimisations, not the correctness source of truth.

### 2. Recovery resumes a DAG, not just a linear script

For a recovery request, Mini-ork must determine the earliest node that is not
reusable. A completed node may be skipped only if its checkpoint and all of its
upstream inputs still match the current run's relevant inputs, recipe/workflow
version, and configuration.

For parallel DAGs, recovery must use dependency closure, not a simplistic
"last node number" rule. An unrelated successful branch must not be rerun.

### 3. Recoverable failure is not terminal failure

At minimum, distinguish these outcomes:

- infrastructure interruption: sandbox/worker/process disappeared or was
  interrupted;
- provider/agent limit: timeout, max-turns, transient provider failure, or
  exhausted tool loop;
- output/validation failure: a node ran but did not produce a valid checkpoint;
- user input required: human answer or steering is needed before proceeding;
- terminal configuration/safety failure: recovery would be unsafe or inputs
  are incompatible.

The first four must leave a durable recovery record. Only genuinely terminal
conditions should mark the entire run failed.

### 4. Recovery must be bounded and intentional

Recovery must support an operator/API choice between at least:

- resume unchanged from the first incomplete node;
- retry the failed node;
- repair the failed node with its failure evidence and a bounded turn/cost/tool
  budget;
- pause for a human decision.

Do not turn a max-turn or no-progress result into an unbounded automatic retry
loop. The default policy should be conservative: automatic recovery is suitable
for deterministic/infrastructure interruptions; a new LLM repair attempt
should be visible and bounded.

### 5. Existing operator contracts stay compatible

- Preserve the current `mini-ork resume <run_id>` cost-pause behaviour.
- Preserve existing steering-pause behaviour.
- Provide a separate, explicit entrypoint/API for execution/DAG recovery so an
  operator cannot accidentally mistake "clear a cost pause" for "rerun a
  workflow node".
- Existing recipes and historical run records must remain readable.

## Required capabilities

### Durable run and node state

Persist enough state outside a live shell process to answer, after restart:

- Which run, recipe, workflow/version, input revision, and resolved
  configuration is being recovered?
- Which node attempts started, completed, failed, or were paused?
- Which checkpoint is the latest valid checkpoint for each branch?
- Which artifacts and hashes back each checkpoint?
- Why did the latest attempt stop, and is it recoverable?
- Which recovery request/attempt currently owns the run?

The persisted state must be queryable for the UI/API and must survive a
process or sandbox stop. Run-directory files may be used, but a durable index
must make them inspectable without log scraping.

### Atomicity and recovery after a crash

Checkpoint publication must be crash-safe. Mini-ork must not mark a node
reusable before its artifacts and checkpoint manifest are durable and valid.

It must explicitly handle both crash windows:

1. artifacts exist but the checkpoint was not committed; and
2. checkpoint metadata exists but an artifact is missing or corrupt.

In either case, recovery must fail closed for that node: validate and reuse it
only when safe, otherwise rerun that node rather than trusting a partial
record.

### Single-writer execution ownership

Two workers, retries, or operator clicks must not resume the same run
concurrently. Establish a durable ownership/lease or equivalent fencing rule
so that a stale worker cannot publish a checkpoint or terminal state after a
new recovery attempt begins.

Repeated recovery requests with the same run, checkpoint, and strategy must be
idempotent and return the existing recovery rather than duplicate spend.

### Node-level observability

Expose a coherent history for every node attempt:

- node id/type, attempt number, start/end times, result, and failure class;
- checkpoint used and checkpoint produced;
- artifact references and validation result;
- model/provider session details and per-attempt cost where available;
- recovery strategy, budget, and operator/system initiator.

The existing event stream may remain append-only, but it must no longer be the
only source needed to infer replay state.

### Trace continuity

All LLM calls, node attempts, and recovery attempts for one logical Mini-ork
run must be correlated under one root trace. A resumed attempt should create
new child spans/attempt spans, not a disconnected synthetic trace.

Use the incoming/root trace context when one is supplied by a caller. Preserve
it through runner/sandbox boundaries and make the run, node, checkpoint, and
attempt identifiers queryable trace attributes.

### Safe tool behaviour

Never automatically replay a non-idempotent external action merely because a
process died mid-node. Persist a receipt/output when available and require an
explicit retry policy or human approval when side effects cannot be proven
safe. Deterministic/read-only tools may be replayed according to the selected
strategy.

## Required user and API behaviour

Mini-ork must provide a programmatic and operator-facing way to:

1. inspect a run's current recovery status and valid checkpoints;
2. see exactly which work will be reused and which node(s) will run;
3. request a bounded recovery from a selected checkpoint/node;
4. cancel a pending recovery without invalidating previous checkpoints;
5. distinguish cost-pause resume, steering resume, and execution recovery;
6. receive a clear terminal explanation if recovery cannot proceed safely.

The UI should present recovery as part of the existing run DAG: completed nodes
remain completed, the failed node shows its failed attempt and next action, and
new attempts are nested beneath that node rather than shown as a fresh,
unrelated run.

## Acceptance scenarios

Implement automated coverage for at least the following scenarios without
calling a real paid model:

1. **Failed terminal agent:** nodes A–C checkpoint successfully; node D exits
   due to max turns. Recovering retries/repairs D only. A–C receive no new
   dispatches or LLM calls.
2. **Interrupted runner:** a worker dies after a validated checkpoint. A new
   process resumes at the first incomplete node using the same logical run.
3. **Artifact mismatch:** a checkpoint points to a deleted or corrupted
   artifact. Recovery refuses to skip that node and reruns the smallest safe
   dependency closure.
4. **Changed inputs/configuration:** a changed kickoff, workflow, relevant
   prompt/configuration, or upstream artifact invalidates affected checkpoints
   but preserves independent valid work where safe.
5. **Parallel DAG:** one branch fails and another succeeds. Recovery reruns
   only the failed branch and dependent nodes.
6. **Duplicate recovery request:** two concurrent requests cannot execute the
   same node twice; one acquires ownership and the other returns a safe,
   descriptive result.
7. **Human pause:** a steering or answer-required state stays paused until
   input arrives and then resumes without repeating valid nodes.
8. **Non-idempotent tool:** a side-effecting tool failure is never silently
   replayed.
9. **Trace grouping:** original and recovered LLM calls appear under the same
   logical trace/run, with distinct node-attempt spans.
10. **Compatibility:** current cost-pause resume and legacy completed runs
    continue to work unchanged.

## Deliverables

1. A concise architecture/design note explaining the chosen durable state
   model, checkpoint validity rules, recovery state machine, and migration
   compatibility strategy.
2. The generic Mini-ork runtime capability, CLI/API/UI affordances, and test
   coverage needed to satisfy the acceptance scenarios.
3. A migration and rollback plan for existing state databases and legacy runs.
4. An operator runbook: how to inspect, recover, cancel, and diagnose a
   failed run without deleting valid artifacts or repeating completed spend.
5. A short handoff describing how an external orchestrator (such as the
   Researcher compose planner) supplies a root trace, receives checkpoints,
   and requests recovery.

## Technical decisions owned by Mini-ork

Mini-ork should choose the implementation details, including:

- database schema/table names and migration approach;
- whether the checkpoint writer is Bash, Python, or a shared runtime seam;
- exact CLI/API names and JSON shapes;
- artifact storage/manifest format and integrity algorithm;
- lease/locking/fencing mechanism;
- retry/repair budget representation and policy hooks;
- how the current Bash/Python parity and runtime-cutover strategy applies;
- UI layout and event projection.

The implementation must explain these choices in its design note and show how
they meet the contracts above rather than merely adding another retry loop.

## Out of scope for this kickoff

- Rewriting Mini-ork into a wholly new scheduler or actor framework.
- Guaranteeing continuation from arbitrary hidden LLM reasoning tokens.
- Automatically replaying unsafe external side effects.
- Changing Researcher planner business logic; that integration follows after
  Mini-ork exposes a tested, generic recovery capability.

## Definition of done

The feature is complete only when the acceptance scenarios pass, a run can be
recovered from a durable node checkpoint without replaying valid ancestors, and
an operator can see the exact recovery decision, cost boundary, artifacts, and
trace lineage before and after recovery.
