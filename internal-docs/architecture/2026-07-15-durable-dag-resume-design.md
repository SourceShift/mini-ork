# Durable DAG resume — architecture & design note

*2026-07-15. Deliverable #1 of `kickoffs/durable-dag-resume.md`. Decisions owned by mini-ork per that kickoff's §"Technical decisions". Mechanism for turn-level resume settled by the operator: vendor CLI `claude --resume` + a durable session-file copy.*

## 1. The load-bearing decision: two tiers, never conflated

Node/DAG resume and turn-level resume are **different reliability classes**. Keeping them separate is the whole design.

| Tier | Boundary | Guarantee | Source of truth |
|---|---|---|---|
| **B — node/DAG checkpoint** | node completion | **correctness** — deterministic, safe to skip | `node_checkpoints` row + verified artifacts |
| **A — turn continuation** | mid-node (turn N) | **best-effort** — cheap retry, not a checkpoint | vendor session jsonl + tool receipts |

A partially-streamed turn, an in-memory agent thought, or a tool call with unknown side effects is **not** a checkpoint. Turn-level resume is a *continuation optimization layered on B*, not a peer of it. The model's hidden reasoning between turns is unrecoverable; the **observable transcript** (messages + tool results) is, and it is sufficient to continue because the model re-derives reasoning from the visible history.

## 2. Durable state model (`state.db`)

`state.db` is already the durable index; extend it. New tables (migration `00NN`):

- **`node_checkpoints`** — the reuse source of truth.
  `(run_id, node_id, attempt, status, input_hash, recipe_version, config_hash, artifact_manifest_json, session_ref, failure_class, created_at)`; PK `(run_id, node_id)` keeps the *latest valid* checkpoint per node.
  - `input_hash` = hash of the node's resolved upstream inputs (parent artifact hashes + kickoff-relevant slice).
  - `recipe_version` / `config_hash` = fingerprint of the workflow + resolved config that produced it.
  - `artifact_manifest_json` = `[{path, sha256, bytes}]` for every artifact the node emitted.
  - `session_ref` = pointer to the persisted vendor session file (tier A), nullable.
- **`node_attempts`** — append-only observability, one row per attempt.
  `(run_id, node_id, attempt, node_type, started_at, ended_at, result, failure_class, checkpoint_used, checkpoint_produced, cost_usd, provider_session_id, initiator)`.
- **`run_leases`** — single-writer fencing.
  `(run_id, owner_token, acquired_at, expires_at)`; a checkpoint/terminal write must present the current `owner_token` or be rejected.
- **`recovery_requests`** — idempotency + audit.
  `(request_id, run_id, from_node, strategy, budget_json, status, created_at)`; same `(run_id, from_node, strategy)` returns the in-flight request, not a second dispatch.

The append-only event stream stays, but is **no longer required** to infer replay state — the tables above are directly queryable by UI/API.

## 3. Checkpoint validity rules

A completed node is **reusable** iff *all* hold:
1. `input_hash` matches the current run's resolved inputs for that node.
2. `recipe_version` and `config_hash` match the current run.
3. Every artifact in `artifact_manifest_json` exists and its `sha256` verifies.

Any mismatch → the checkpoint is **invalid**; the node and its transitive dependents rerun. This gives changed-inputs (kickoff/workflow/config/upstream-artifact) and artifact-mismatch handling directly: change a node's inputs → its hash changes → its checkpoint invalidates → independent branches stay valid.

**Recovery is a dependency-closure walk, not a node number.** Find the earliest node with no valid checkpoint; rerun it and its transitive dependents only. A parallel branch that succeeded is never rerun. The DAG comes from `workflow.yaml` (topology already computed by `lib/topology.sh` / the python port).

## 4. Crash-safe checkpoint publication

Two crash windows, both must fail *closed*:

1. **Artifacts exist, checkpoint not committed** → on recovery the node has no `node_checkpoints` row → rerun it (correct; no false reuse).
2. **Checkpoint row exists, artifact missing/corrupt** → validity rule 3 fails on hash verify → rerun it.

Publication order: **write artifacts → fsync → verify hashes → commit the `node_checkpoints` row in one SQLite transaction** (holding the lease). Never mark a node reusable before its artifacts *and* manifest are durable and self-consistent. This is the same lesson the framework-edit capture fix taught: never trust a record over the real bytes.

## 5. Recovery state machine

Per node attempt, classify the stop (`failure_class`):

- `infra_interrupt` — sandbox/worker/process vanished. **Auto-recoverable** (deterministic replay from last valid checkpoint).
- `provider_limit` — max-turns / timeout / transient provider / tool-loop exhaustion. **Recoverable, bounded** — a *new* LLM attempt, visible and budget-capped; not an unbounded auto-retry.
- `output_invalid` — node ran, produced no valid checkpoint. **Recoverable via retry/repair.**
- `input_required` — human answer/steering needed. **Paused** until input, then resume without replaying valid ancestors.
- `terminal` — unsafe recovery or incompatible inputs. **Only this marks the whole run failed.**

The first four leave a durable recovery record. Operator strategy choice: `resume` (from first incomplete node) · `retry` (rerun failed node) · `repair` (rerun with failure evidence + bounded turn/cost/tool budget) · `pause`. Default policy conservative: auto only for `infra_interrupt`; any new LLM attempt is explicit + bounded.

## 6. Tier A — turn-level continuation (mechanism: `--resume` + durable session file)

mini-ork dispatches a node as **one shot** (`claude --print …` runs all N turns internally). It does not see turn boundaries. So turn-9 resume rides on the vendor CLI:

1. Capture the `session_id` claude emits per node attempt → `node_attempts.provider_session_id`.
2. **Persist the CLI session store durably.** The claude session store is the jsonl transcript under `~/.claude/projects/<hash>/*.jsonl`. On checkpoint (and on recoverable failure) copy that jsonl into the run dir / durable store; record it as `node_checkpoints.session_ref`. This is the operator-chosen mechanism: keep `--resume` simplicity, but make the session survive **sandbox death** (kickoff scenario 2) — restore the jsonl into `~/.claude/projects/…` on a fresh sandbox, then `claude --resume <session_id>`.
3. **Tool receipts.** For every side-effecting tool call, persist a receipt (input + output) *before* the node is considered done. On any replay, a completed non-idempotent tool returns its receipt — never re-fires (kickoff scenario 8). Read-only tools may replay per the selected strategy.

**Honest limits (state them, don't hide):**
- Cross-lane: kimi/minimax/glm route through the `claude` binary → same jsonl store → `--resume` works. **codex** has its own session model → codex nodes get **node-level resume only** (tier B) in v1, or a separate codex-session handler later.
- Fidelity: high when the session file survives (now durable); observable-state-only if a receipt/transcript is all that remains. Hidden reasoning tokens are never guaranteed (kickoff explicitly out-of-scope).

## 7. Single-writer ownership

`run_leases` with fencing tokens. A recovery acquires the lease (or the current holder's token) before any dispatch or checkpoint write; a stale worker presenting an expired token is rejected (kickoff scenario 6). Repeated identical recovery requests are idempotent via `recovery_requests`.

## 8. Trace continuity

All LLM calls, node attempts, and recovery attempts for one logical run correlate under **one root trace**. Use the caller-supplied root trace context (e.g. the Researcher compose planner) when present; preserve it across runner/sandbox boundaries. A resumed attempt creates **new child/attempt spans**, not a disconnected synthetic trace. Run/node/checkpoint/attempt ids are queryable trace attributes.

## 9. CLI / API surface (kept distinct from existing pauses)

- New: `mini-ork recover <run_id> [--from-node X] [--strategy resume|retry|repair|pause] [--budget …]` and a matching API.
- **Unchanged and deliberately separate:** `mini-ork resume <run_id>` (cost pause), steering pause. An operator must not mistake "clear a cost pause" for "rerun a node".
- `mini-ork recover --status <run_id>` → shows valid checkpoints, exactly which nodes will be reused vs rerun, and the cost boundary, *before* acting (kickoff §"Required user and API behaviour").
- UI: recovery renders inside the existing run DAG — completed nodes stay completed, the failed node shows its failed attempt + next action, new attempts nest beneath that node (not a fresh run).

## 10. Migration & compatibility

- New tables added by a forward migration; **legacy runs with no checkpoint rows are treated as "not resumable, fully complete"** — they render and read unchanged.
- Rollback: the tables are additive and read-through; dropping them disables recovery without corrupting historical runs.
- Bash/Python parity: the **checkpoint writer + validity check is a shared runtime seam** (Python, matching the current cutover direction), invoked from both the bash and python execute paths so a run is recoverable regardless of `MO_DISPATCH_BACKEND`.

## 11. Build sequence (why B before A)

Ship the correct, deterministic core first; layer best-effort continuation on top:

1. **E1** — `node_checkpoints` + hash validity + crash-safe publish. *(Kills "rerun whole DAG"; correct on its own.)*
2. **E2** — dependency-closure recovery + `mini-ork recover` CLI/API + `--status`.
3. **E3** — `run_leases` fencing + `recovery_requests` idempotency + failure-class state machine.
4. **E4** — tier-A turn continuation: session-id capture + durable session-file copy + `--resume` restore + tool receipts.
5. **E5** — trace continuity (root-trace propagation) + UI projection + operator runbook.

E1–E3 = the correctness foundation. Do not build E4 (turn resume) before E1–E3 exist, or turn-resume sits on sand. Each maps to one kickoff (`kickoffs/durable-dag-0N-*.md`).
