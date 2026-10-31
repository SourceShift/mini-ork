# LangGraph parity

LangGraph is the most widely used Python framework for stateful, multi-actor
LLM graphs. Recipes that author a mini-ork `workflow.yaml` are conceptually
close to authoring a LangGraph `StateGraph`, but the two runtimes differ in
what they treat as first-class. This document is the **12-row parity table**
that maps each concept LangGraph exposes onto the closest surface in
mini-ork, with a status (parity, partial, divergent-by-design, or
planned-P2) and at least one file:line citation into the working tree.

The table is scoped to the twelve concepts a recipe author moving from
LangGraph is most likely to ask about. Four of them — interrupts, Command,
Send, time-travel — are intentionally marked **`planned-P2`**
per the P1.5 spec card: no partial implementation in the codebase counts as
parity, even where a near-surface exists. See the "Absence evidence"
section at the end of this doc for the exact grep commands that justify
each `planned-P2` label.

The point of the table is not "mini-ork is a LangGraph clone" — it is
"given a LangGraph concept X, here is what mini-ork already does, here is
how close it is, and here is where to look in the source for the proof."
If a row says `divergent-by-design`, the divergence is intentional and
explained in the rationale.

## How to read this table

The four status values mean:

- **parity** — the concept exists in mini-ork with the same shape, the
  same names, and the same semantics a LangGraph user would expect.
  Cited lines cover the load-bearing surface.
- **partial** — a real implementation exists but the surface is narrower
  than LangGraph's. The row says exactly what is and isn't covered; the
  gap is documented, not waved at.
- **divergent-by-design** — mini-ork intentionally does the opposite of
  LangGraph here, for a reason stated in the rationale. Treat the row as
  a one-sentence RFC for "if you port this from LangGraph, expect X".
- **planned-P2** — there is no first-class implementation today. The row
  cites a near-surface where one exists and explains why it is a
  deliberate deferral rather than an oversight. Status does not flip to
  `parity` or `partial` mid-sprint; the five labelled rows will not
  change without an explicit P2 spec. (Subgraphs left this set on 2026-10-31: row 10 is now `partial`.)

A file:line citation means **a real position in the working tree**. The
verifier tier checks the file and the line range, so a stale citation is
treated as a hard error. If a row's line numbers drift, this doc is
wrong; re-author the row rather than letting the stale cite ship.

## The table

| # | LangGraph concept | mini-ork surface | Status | Rationale (with file:line citation) |
|---|-------------------|------------------|--------|--------------------------------------|
| 1 | **StateGraph** — typed graph that compiles to a per-recipe runtime | `mini_ork.workflow.compiler.CompiledWorkflow` (compile-time) walks each `CompiledRetryEdge` / `recursion:` block; `mini_ork.cli.execute._dispatch_dependency_graph` is the runtime walker | parity | `CompiledWorkflow` (a frozen dataclass with `nodes`, `declared_order`, `topological_order`, `control_parents`, `retry_edges`, `escalation_edges`, `recursive_children`) lives at `mini_ork/workflow/compiler.py:95-123`, and the runtime pass that walks it in readiness waves lives at `mini_ork/cli/execute.py:1037-1044`. Together they are the per-recipe typed-graph container LangGraph names `StateGraph`. |
| 2 | **conditional edges** — router function on a node decides which downstream fires | `condition:` block on an edge → `mini_ork.recovery.condition.CompiledCondition` + `evaluate_condition` (jsonschema predicate) | partial | `CompiledCondition` (`pointer`, `match`, `negate`) is a frozen dataclass at `mini_ork/recovery/condition.py:82-101` and the runtime predicate that evaluates it against the producer's output artifact is at `mini_ork/recovery/condition.py:287-314`. The schema (oneOf: legacy string vs object form) lives at `schemas/workflow.schema.json:286-317`. Narrower than LangGraph: the schema description explicitly says `condition` is "currently consumed on `retries` edges only; the rc channel stays authoritative on every other edge type" (`schemas/workflow.schema.json:286`). To make a sibling node `depends_on` branch on an artifact field today, model the branch as a `retries`-edge back to a hidden revise node. |
| 3 | **cycles** — graph loop (a node's downstream eventually returns to it) | `recursive: true` edge + declared `recursion:` block (max_iterations, convergence_check, divergence_kill) | parity | `mini_ork/workflow/recursion.py:43-51` defines `RecursionConfig` with the five declared caps/conditions verbatim from the recipe. A live example of a recursive back-edge is `recipes/recursive-validate-impl/workflow.yaml:54` (`{from: replanner, to: implementer, edge_type: depends_on, recursive: true}`); the runtime carries it in `CompiledWorkflow.recursive_children` (`mini_ork/workflow/compiler.py:119-123`). Bounded cycle = a `recursion:` block; unbounded cycle = compile-time error. |
| 4 | **checkpointer + thread/checkpoint ids** — persistent per-thread state with resume-by-id | `mini_ork.checkpoints.CompletionRecord` / `record_completion` / `list_completions` / `filter_resume_set` (F2/F3/F4 vocabulary) | parity | `CompletionRecord` (the SQL-backed, JSON-friendly per-step record) is a frozen dataclass at `mini_ork/checkpoints/__init__.py:109-127`. `run_id` is the LangGraph thread id; `step_id` is the checkpoint id. Resume-by-thread/checkpoint is the `mini_ork/cli/run_resume.py` flow — see row 12 for the citation. The module docstring explicitly mirrors LangGraph's `_reapply_writes_to_succeeded_nodes` (`mini_ork/checkpoints/__init__.py:1-39`). |
| 5 | **write-replay** — re-apply a failed node's channel writes on retry | `mini_ork.workflow.retry.mark_retry_error` writes an `ErrorRow` sidecar; the P1.2 resume path then re-dispatches from scratch. Failed-attempt outputs are **discarded before retry** | divergent-by-design | The sidecar write is `mark_retry_error` at `mini_ork/workflow/retry.py:332-356`. The intentional discard is the existing P1.4 retry spec: "replay = re-run, never re-apply" (see `docs/ARCHITECTURE.md` Failure Recovery). The divergence is deliberate — the artifact-ledger model treats outputs as all-or-nothing per attempt, so re-applying a half-written file would violate the ledger's validity contract. LangGraph's superstep replay is the inverse of mini-ork's discard-and-retry; port from one to the other by gut, not by refactor. |
| 6 | **RetryPolicy** — first-class retry with backoff + jitter | `mini_ork.workflow.retry.RetryPolicy` (frozen dataclass) + `compute_backoff` (half-jitter envelope) | parity | `RetryPolicy` is `mini_ork/workflow/retry.py:87-104` (defaults `max_attempts=3, initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, jitter=True`). The runtime envelope `rng.uniform(base/2, base)` (half-jitter, never full jitter so the cap is respected on every sample) lives in `compute_backoff` at `mini_ork/workflow/retry.py:244-276`. Same fields, same defaults, same cap semantics as LangGraph's `RetryPolicy` NamedTuple; the source URL is pinned at `mini_ork/workflow/retry.py:LANGGRAPH_DEFAULTS_URL`. See the per-node `retry:` block doc (P1.4) at the bottom of this file. |
| 7 | **interrupts** — long-pause / human-in-the-loop suspend + resume | none first-class. Closest surface: `human_gate` (a registered gate that blocks downstream edges until operator decision) | planned-P2 | No `Interrupt` class, `interrupt()` callable, or LangGraph-style suspend/resume primitive exists in `mini_ork/`. See `Absence evidence` below. `human_gate` is a registry key (`mini_ork/gates/gate_registry.py:70`) and a gate semantics — "block downstream edges until the human writes APPROVE to the inbox file" — but it does NOT pause and resume a node mid-execution. The five forced-P2 rows cannot be promoted to `parity` or `partial` without a dedicated P2 spec; this row is `planned-P2` by spec, regardless of incidental closeness to LangGraph's `interrupt()`. |
| 8 | **Command** — explicit goto / dynamic-routing operator | none first-class. Closest surface: the 8-value `edge_type` enum + `condition:` predicates on `retries` edges | planned-P2 | No `Command(goto=...)` constructor or imperative-routing operator exists in `mini_ork/`. See `Absence evidence` below. mini-ork routes declaratively: every routing decision is an edge in `workflow.yaml`, and the closest dynamic-routing surface is the `condition:` predicate on a `retries` edge (`schemas/workflow.schema.json:286-317`). The `edge_type` enum at `schemas/workflow.schema.json:272-285` lists the eight declared routing shapes (`depends_on`, `supplies_context_to`, `verifies`, `blocks`, `retries`, `escalates_to`, `human_decision_gate`, `verifies_user_choice`); together with `condition:` they cover the routing decisions `Command` exists to express, but declaratively. Status is `planned-P2` per the P1.5 spec card. |
| 9 | **Send / map-reduce** — fan out one input to multiple sub-nodes, merge results | none first-class. Closest surface: `dispatch_mode: parallel` (fan-out across same-declaration siblings) + `mini_ork.orchestration.item_fanout.run_items` (per-item worker pool + JSON result merge) | planned-P2 | No first-class `Send(..., input=...)` graph primitive exists in `mini_ork/`. See `Absence evidence` below. The closest surfaces are `_run_parallel_batch` at `mini_ork/cli/execute.py:489-522` (sibling nodes already declared in the DAG, one entry per `dispatch_fields` line) and `run_items` at `mini_ork/orchestration/item_fanout.py:206-270` (a recipe-side library imported by a driver script). Neither is a graph primitive a recipe author wires from `workflow.yaml` today — `dispatch_mode: parallel` requires the fan-out targets to be declared siblings up front (e.g. the four `tier4_*` lens nodes in `recipes/recursive-validate-impl/workflow.yaml:18-21`), the inverse of LangGraph's ad-hoc `Send(...)` invocations. Status is `planned-P2` per the P1.5 spec card, regardless of the partial fan-out surface. |
| 10 | **subgraphs** — nested graph (one recipe uses another as a node) | `type: subgraph` with `recipe:` (and an optional `kickoff:`, else the parent run's) runs that recipe as a child `mini-ork run` under run id `<parent>--<node>`, approved and bounded by the recursive spawn policy (`MINI_ORK_RECURSIVE_MAX_DEPTH`, child and descendant caps); the node passes exactly when the child run passes, and the child's source artifact is copied into the parent run dir (`mini_ork/orchestration/subgraph.py:110-154`, handler `mini_ork/cli/execute_handlers.py:4088-4103`, schema `schemas/workflow.schema.json:92-93`, `:114`, `:132-140`) | partial | Recipe-as-node exists (2026-10-31). LangGraph's shared-state subgraphs do not: there is no parent/child state-schema mapping and child events are not streamed into the parent — the two runs exchange only the kickoff going in and the child's source artifact coming out, and the child gets a clean environment (none of the parent's run- or node-scoped variables, grants or envelope). |
| 11 | **streaming** — token-by-token / event-stream output to a consumer | `mini_ork.client.MiniOrk.stream` returning `Iterable[RunEvent]` + `mini_ork.dispatch.live_stream.LiveWriter` (line-level streaming) | partial | `MiniOrk.stream` (`mini_ork/client.py:143-145`) yields one `RunEvent(line, stream)` per stdout / stderr line; `RunEvent` is the frozen dataclass at `mini_ork/types.py:165-167`. Narrower than LangGraph's `astream` / `astream_events`: no `stream_mode="events"`, no chain-event surface (no `on_chain_start`, no `on_llm_new_token`), only line-level post-hoc delivery after the run completes. A live, mid-execution token stream is **not** exposed by the Python SDK today; consumers wanting token-level output read from `state.db.events` and reconstruct. |
| 12 | **time-travel** — rewind state to an earlier checkpoint and replay | none first-class. Closest surface: `mini-ork run --resume <run_id> --checkpoint-id <id>` rewind on the CLI | planned-P2 | No first-class `get_state_history` / `update_state` / fork primitive exists in `mini_ork/`. See `Absence evidence` below. The closest surface is the `--checkpoint-id <id>` rewind at `mini_ork/cli/run_resume.py:276-285` (`rewound = set(order[order.index(checkpoint_id):])`), wired by `_run_handler` at `mini_ork/cli/main.py:1551-1564`. That rewind is a one-shot resume mask — there is no arbitrary `state_snapshot_at(step_id)` browse API, no history diff, no Python equivalent of `get_state_history` / `update_state`, and no branching from an arbitrary past step into a new run. Status is `planned-P2` per the P1.5 spec card, regardless of the partial CLI rewind. |

## Pointer

- The canonical mini-ork architecture (node types, edge types, gate types,
  memory namespaces, lifecycle sequence) is at [docs/ARCHITECTURE.md](ARCHITECTURE.md).
- The per-node `retry:` block (P1.4) — which is the surface that closes
  the `RetryPolicy` row above — was specified for this same docs path in
  an earlier deliverable and now lives at `tests/langgraph_parity/test_retry_policy.py`
  plus the module docstring `mini_ork/workflow/retry.py:1-70`. The D3
  parity suite that drives it is in `tests/langgraph_parity/`.

## Absence evidence (five forced `planned-P2` rows)

Every command below was run from the repo root of the worktree that
authored this doc. An empty/no-match result is the deliverable; that is
what justifies a `planned-P2` label rather than an oversight.

### 7. interrupts

```bash
grep -rEn "class\s+Interrupt\b|Interrupt\(\)|def\s+interrupt\b|from\s+langgraph\b" \
  mini_ork/ recipes/ lib/ schemas/ 2>/dev/null
```

Output: (no matches).

Existing-analogue justification: `human_gate` is registered at
`mini_ork/gates/gate_registry.py:70`. The semantics are "an operator
writes APPROVE or REJECT to the inbox file before downstream edges
fire" — it gates the DAG, it does NOT pause and resume a single node.
This is the closest existing surface and is documented at row 7; the
deferral to P2 is intentional.

### 8. Command

```bash
grep -rEn "class\s+Command\b|\bCommand\(\s*goto=|from\s+langgraph\b" \
  mini_ork/ recipes/ lib/ schemas/ 2>/dev/null
```

Output: (no matches).

Existing-analogue justification: dynamic routing flows through the
`edge_type` enum (`schemas/workflow.schema.json:272-285`) plus the
`condition:` predicate on `retries` edges (`schemas/workflow.schema.json:286-317`,
runtime at `mini_ork/recovery/condition.py:287-314`). There is no
imperative `Command(goto=...)` operator and none is planned pre-P2.

### 9. Send

```bash
grep -rEn "class\s+Send\b|\bSend\(\s*['\"]" \
  mini_ork/ recipes/ lib/ schemas/ 2>/dev/null
```

Output: (no matches).

Existing-analogue justification: `dispatch_mode: parallel` at
`mini_ork/cli/execute.py:489-522` runs sibling nodes already declared
in the DAG; `run_items` at `mini_ork/orchestration/item_fanout.py:206-270`
is a recipe-side library for per-item worker pools. Neither is a graph
primitive an author can wire from `workflow.yaml` — both require the
fan-out targets to be declared as siblings up front, the inverse of
LangGraph's ad-hoc `Send(..., input=...)` invocations. The status is
`planned-P2` per the P1.5 spec card, regardless of this partial
fan-out surface.

### 10. subgraphs

No longer absent — see row 10. `grep -n '"subgraph"' schemas/workflow.schema.json mini_ork/cli/execute_handlers.py` lists the node type and its handler; `tests/unit/test_subgraph_node.py` pins the child run, the clean child environment, the spawn-policy depth bound and the schema requirement for `recipe`.

### 12. time-travel

```bash
grep -rEn "get_state_history|update_state\b|history_browse|state_snapshot|fork_state" \
  mini_ork/ recipes/ lib/ schemas/ 2>/dev/null
```

Output: (no matches).

Existing-analogue justification: the `--checkpoint-id <id>` rewind at
`mini_ork/cli/run_resume.py:276-285`
(`rewound = set(order[order.index(checkpoint_id):])`) resumes a run
to step N and re-dispatches everything ≥ it. That is the partial CLI
surface, not a first-class time-travel primitive — LangGraph's
`get_state_history`, `update_state`, and explicit fork are absent
here. The status is `planned-P2` per the P1.5 spec card, regardless
of the working CLI rewind.

---

## Per-node `retry:` block (P1.4)

The engine ships a `retry:` block on every workflow node. Defaults and
semantics match LangGraph's [`RetryPolicy`][lg-rp] so a recipe author
who knows LangGraph can paste the block verbatim and predict what the
runtime will do.

[lg-rp]: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py

### The block

```yaml
nodes:
  - name: implementer
    type: implementer
    dispatch_mode: serial
    retry:
      max_attempts: 3
      initial_interval: 0.5
      backoff_factor: 2.0
      max_interval: 128.0
      jitter: true
```

All five fields are optional; omitting the whole block is equivalent
to the snippet above.

### Defaults — mirror LangGraph's current source

The five defaults are read directly off LangGraph's
[`RetryPolicy` NamedTuple][urls]:

| Field             | Default | Source field              |
| ----------------- | ------- | ------------------------- |
| `max_attempts`    | 3       | `RetryPolicy.max_attempts`|
| `initial_interval`| 0.5 s   | `RetryPolicy.initial_interval` |
| `backoff_factor`  | 2.0     | `RetryPolicy.backoff_factor` |
| `max_interval`    | 128.0 s | `RetryPolicy.max_interval` |
| `jitter`          | `true`  | `RetryPolicy.jitter`      |

Source snapshot (commit `main`, accessed 2026-10-12):

```python
class RetryPolicy(NamedTuple):
    initial_interval: float = 0.5
    backoff_factor:   float = 2.0
    max_interval:     float = 128.0
    max_attempts:     int   = 3
    jitter:           bool  = True
    retry_on: ... = default_retry_on
```

[urls]: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py

The same URL lives in `mini_ork.workflow.retry.LANGGRAPH_DEFAULTS_URL`
so a reviewer diffing the module against the LangGraph source has one
and only one URL to copy. Any change to the defaults MUST update the URL
in both the module docstring and this table — the contract is
"defaults stay in lockstep with LangGraph's named tuple."

### Semantics

- **Attempt loop** with backoff + jitter (F1, F6): the dispatcher
  re-runs a node that returned a `finish_reason` in the retryable set
  (`timeout`, `infra_interrupt`, `provider_limit`, `network`,
  `overloaded`, `rate_limited`, `killed`, `sigterm`, `rc_137`,
  `rc_143`). Other finish reasons (verifier refute, reviewer reject)
  fall through to the existing `retries` edge path; the retry block
  never loops on those.
- **Discard failed-attempt outputs** (F3): before each retry the
  declared output files are deleted so a partial write from the failed
  attempt does not carry into the next. The discard is best-effort —
  a missing file is the same end state as a deleted file.
- **Bounded jitter** (F6): with `initial_interval = I` and
  `backoff_factor = B`, attempt N+1 waits
  `min(I * B**(N-1), max_interval)` seconds, then samples the real
  sleep uniformly from `[wait/2, wait]`. Half-jitter, never full jitter,
  so the envelope is respected on every sample.
- **ERROR checkpoint row** (F4): on terminal failure an
  `ErrorRow` is persisted to
  `<run_dir>/retry-exhausted/<node>.json` and (when the
  `node_attempts` table from migration 0050 exists) a row is appended
  with `result='failure'` and `failure_class='retry_exhausted'`.
  The P1.2 / P1.3 resume path reads the sidecar and re-dispatches the
  node.
- **Compose with `retries` edge** (F5): a node that carries BOTH a
  `retry:` block AND a `retries` edge has two independent caps. The
  retry block governs infra-level attempts (transient provider /
  network / infra failures) bounded by `max_attempts`. The `retries`
  edge governs revise rounds (a reviewer rejected the change, the
  implementer is re-run with feedback) bounded by `max_rounds`. The
  two do not share state and they cannot starve the other — an infra
  burst of three timeouts on the implementer does not consume a
  revise round, and a revise round does not bump the retry counter.

### What the engine does NOT copy from LangGraph

- **`retry_on` predicate**: LangGraph lets the user supply a callable
  that decides which exceptions trigger a retry. mini-ork's retry
  block gates on the dispatcher's `finish_reason` enum, which is
  stricter (a `ValueError` raised by the agent IS auto-reverted by
  the dispatcher into a `crash` finish reason — never an exception
  that bubbles up to this loop). A future iteration can add
  `retry_on:` if a workflow author needs custom classification.
- **Channel-version re-apply**: LangGraph's superstep model replays
  failed nodes with the channel writes preserved. mini-ork's executor
  discards the failed attempt's writes (F3) by design — the
  artifact-ledger model (E1) treats outputs as all-or-nothing per
  attempt, so re-applying a half-written file would violate the
  ledger's validity contract (`is_node_reusable` returns False on
  any hash drift). The F3 discard is the right half of the
  superstep-pair: replay = re-run, never re-apply.

### Test surface

`tests/langgraph_parity/test_retry_policy.py` exercises every clause:

- F1 — defaults match LangGraph's source (test_default_policy_matches_langgraph).
- F2 — the source URL is cited in both the module and this doc.
- F3 — see `test_zero_overhead_on_first_attempt_success` and the
  integration tests in the executor suite (the unit-level F3 is
  pinned by the `record_attempt` / `_discard_attempt_outputs` helpers
  exporting their behaviour).
- F4 — `test_mark_retry_error_writes_sidecar`,
  `test_mark_retry_error_appends_node_attempts_when_migration_present`,
  `test_mark_retry_error_writes_are_visible_to_resume`.
- F5 — `test_retry_module_does_not_for_existing_retries_handler` plus
  the executor-level composition test (a node carrying both a retry
  block and a `retries` edge gets exactly the union of the two
  behaviours, with independent caps).
- F6 — `test_compute_backoff_*` (envelope + jitter bounds).
- C1 — `test_retry_module_does_not_for_existing_retries_handler`.

The full pytest suite (`tests/`) must remain green — see AC2 of the
spec card.
