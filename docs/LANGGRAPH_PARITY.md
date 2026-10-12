# LangGraph parity — per-node `retry:` block (P1.4)

The engine ships a `retry:` block on every workflow node. Defaults and
semantics match LangGraph's [`RetryPolicy`][lg-rp] so a recipe author
who knows LangGraph can paste the block verbatim and predict what the
runtime will do.

[lg-rp]: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py

## The block

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

## Defaults — mirror LangGraph's current source

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

## Semantics

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

## What the engine does NOT copy from LangGraph

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

## Test surface

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