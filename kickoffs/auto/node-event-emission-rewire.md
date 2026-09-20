# Re-wire node lifecycle events into `dispatch_node`

## Goal

Every node dispatch writes a `node_start` row and, on completion, a `node_end` row to
`run_events`, so the DAG view, the ACP projection, and the recovery/control-plane views
all have a live per-node feed again.

## Why (the finding this fixes)

The `run_events` node-lifecycle feed is **dead**. `mini_ork/observability/node_events.py`
is a faithful port of the old bash `lib/mo_node_events.sh` — `mo_node_start`
(`:244`), `mo_node_end` (`:261`), `mo_node_emit` (`:165`, the `INSERT INTO run_events`)
— and it has its own unit tests, but **nothing in the runtime calls it**. The bash
library it was ported from was removed in the 2026-07 bash-removal, and the port was
never wired into the Python dispatcher.

Evidence (all verified against this tree at `c1f88f1c`):

- `/tmp/mo-rsi-home/state.db` holds **`run_events` = 0 rows** and `mo_events` = 0 rows,
  while `task_runs` = 5 and `llm_calls` = 63. Runs record cost, not node transitions.
- Every `node_start` string emitted anywhere in the tree is under `tests/`.
- `mini_ork/cli/execute.py` only **reads** `node_start`/`node_end` (`:1327`, dangling-event
  detection); it never writes them and does not import `node_events`.
- `mini_ork/web/routes/run_detail.py:600` documents the intended contract in its own
  docstring: node status is "derived from node_start/node_end events emitted by
  `mini_ork/cli/execute.py:_dispatch_node` via `lib/mo_node_events.sh`".

So `/dag` currently reports `never_seen` for every node of every run, and any consumer
built on the read model (including the Zed ACP projection) sees no per-node progress.
This is a restoration of intended behaviour, not a new feature. **Do not invent a new
event format** — call the existing `mo_node_start` / `mo_node_end` so the reader, the
event-id scheme, and the mirror tests stay authoritative.

## Mechanisms (exact spec)

`dispatch_node` is at `mini_ork/cli/execute_handlers.py:145`; the live call site is
`mini_ork/cli/execute.py:399`. It already funnels every node completion through one
wrapper:

```python
def trace(node_id, status, node_type, output_file="", verdict="", finish_reason=""):
    _base_trace(...)
    _base_checkpoint(node_id, status, node_type, output_file)
```

### 1. `node_start` — immediately before the node is dispatched

Emit once per dispatch, before the handler runs:

```python
mo_node_start(run_id, node_id, node_type, model_lane=lane)
```

`lane` is the already-resolved routing lane in `dispatch_node` (do not re-resolve).
Record the start time so the matching `node_end` can carry a duration — a
`dict[str, int]` keyed by `node_id` local to the dispatch is enough.

### 2. `node_end` — at the single completion seam

Emit inside the **existing `trace()` wrapper**, so all ~20 node handlers are covered
without touching any of them:

```python
mo_node_end(run_id, node_id, node_type, duration_ms,
            verdict=verdict, artifact_path=output_file, finish_reason=finish_reason)
```

`status` is `"success"`/`"failure"`; it is **not** the `verdict` argument — pass the
`verdict` and `finish_reason` the wrapper already receives. If a node ends without a
recorded start (an early-return path), still emit `node_end` with `duration_ms=0` —
a `node_end` with no `node_start` is the reader's "failed" signal and is better than
silence.

### 3. Write to the run's db, not whatever the environment says

`mo_node_emit` resolves its target via `_resolve_db()` (`node_events.py:68`), which
reads `os.environ` (`$MINI_ORK_DB` → `$MINI_ORK_HOME/state.db` → `$(pwd)/.mini-ork/state.db`).
`dispatch_node` already **has** the correct handle as its `db` parameter, and the
in-flight env-isolation work means `os.environ` is not a reliable carrier mid-run.

Add a **keyword-only** `db: str | None = None` to `mo_node_emit`, `mo_node_start` and
`mo_node_end` that takes precedence when supplied, falling back to `_resolve_db()`
otherwise. Keyword-only and defaulted, so the existing mirror tests
(`tests/unit/test_mo_node_events_py.py`, which call these positionally) stay green —
verify that, do not assume it. Pass the run's db path down from `dispatch_node`.
These three functions must remain fail-silent: they return 0 rather than raise, and a
missing/absent db is a no-op. Observability must never break execution.

### 4. Exactly once

A pool child re-enters this path (see the `_bootstrap_recipe_register` comment at
`execute.py:392`). Make sure a node produces **one** `node_start` and **one** `node_end`,
not one per process. If you cannot guarantee that structurally, say so in your summary
with the evidence, rather than leaving a duplicate-emitting path.

## Tests — `tests/unit/test_node_event_emission_py.py` (new)

Hermetic: a real throwaway state.db, no lane, no network, no real run.

1. Calling `dispatch_node(...)` with a stub `dispatch_fn` that returns `(0, "done")`
   writes exactly one `node_start` row and one `node_end` row for that node, with the
   right `run_id`, `node_id` and `node_type`.
2. The `node_end` row's payload carries the `verdict` and `finish_reason` the stub
   produced, plus a non-negative `duration_ms`.
3. A stub `dispatch_fn` returning a failing rc still produces a `node_end` (with a
   start), i.e. failures are recorded, not dropped.
4. A model lane passed to the node appears in the `node_start` payload's `model_lane`.
5. `mo_node_start(..., db=<explicit path>)` writes to that path even when
   `MINI_ORK_DB`/`MINI_ORK_HOME` point somewhere else — the explicit-db precedence.
6. The whole path is fail-silent: with a db path that does not exist, `dispatch_node`
   still returns its normal `(rc, finish_reason)` and writes nothing.

`tests/unit/test_mo_node_events_py.py` must stay green unmodified (it pins the ported
semantics) — if it needs a change, that is a signal you altered behaviour, not the
signature.

## Files in scope

- `mini_ork/cli/execute_handlers.py` (`dispatch_node`: the two emits; the duration map)
- `mini_ork/observability/node_events.py` (the keyword-only `db=` on the three functions)
- `tests/unit/test_node_event_emission_py.py` (new)

Do not touch `mini_ork/web/**`, any recipe, any probe, or the ACP module
(`mini_ork/acp/**`) — the ACP projection already consumes this feed and must start
working *because of* this change, not because of a change to it. Do not add a second
event table, a new event type, or a new writer.

## Verification commands

Run from the worktree root with `python3.11` explicitly (the ambient `python3` is 3.9
and dies at collection).

```bash
# 1. the new wiring tests
python3.11 -m pytest tests/unit/test_node_event_emission_py.py -q

# 2. the ported writer's own tests — must stay green, unmodified
python3.11 -m pytest tests/unit/test_mo_node_events_py.py -q

# 3. the dispatch layer this touches
python3.11 -m pytest tests/unit/ -q -k "dispatch or node_event or execute"

# 4. compiles
python3.11 -m py_compile mini_ork/cli/execute_handlers.py mini_ork/observability/node_events.py
```

## Self-application measurement

The claim is "a real run now records its node transitions." Hermetic tests prove the
wiring; they do not prove a real run reaches it. Prove it end-to-end with the cheapest
real dispatch you can find, then count rows:

```bash
sqlite3 /tmp/mo-rsi-home/state.db "
  SELECT run_id, event_type, COUNT(*)
  FROM run_events GROUP BY 1,2 ORDER BY 1 DESC LIMIT 20;"
```

Accepted iff a run that dispatched N nodes shows **N `node_start` and N `node_end`
rows** for its `run_id` (allowing for genuinely skipped nodes), and
`GET /api/v1/task-runs/<id>/dag` reports `done`/`failed` per node instead of
`never_seen` for every one.

If you cannot run a real dispatch within budget, say so explicitly and report the
hermetic counts plus the exact command you would have run — **do not** claim the
end-to-end result you did not observe.

Evidence artifact: `${MINI_ORK_RUN_DIR}/node-events-rewire.json`:

```json
{"dispatch_node_emits_start": true, "dispatch_node_emits_end": true,
 "explicit_db_precedence": true, "mirror_tests_unmodified_green": true,
 "real_run_node_start_rows": <int>, "real_run_node_end_rows": <int>,
 "real_run_id": "<run id observed, or null if not observed>"}
```

## Done When

- Verification 1, 2 and 3 are green; verification 2 with the file **unmodified**.
- `dispatch_node` emits exactly one `node_start` before dispatch and one `node_end` at
  the `trace()` completion seam — verifiable by reading it, not by reading a summary.
- `mo_node_emit` / `mo_node_start` / `mo_node_end` accept an optional explicit db that
  wins over `_resolve_db()`, and remain fail-silent.
- `${MINI_ORK_RUN_DIR}/node-events-rewire.json` exists with the fields above, and
  `real_run_node_start_rows` is either a number > 0 or honestly `null`.

## If you cannot finish

Say so, with the failing command and its output. This is execution-core plumbing that
every consumer depends on: a partial change that emits `node_start` without `node_end`
is worse than none, because the reader reports every node as permanently `running`.
A precise partial result beats a green claim that does not survive `git diff`.
