# Framework Edit: Wire task_memory + failure_memory writers in bin/mini-ork-execute

## Goal

Make per-task-class memory namespaces (`task_memory`, `failure_memory`)
populate on every node execution so the planner's "prior 5 runs by task_class"
injection has data to read.

Today both tables have zero rows despite 1115 `execution_traces` and 140
distinct run_ids. The schemas exist (`db/migrations/0009_memory_namespaces.sql`)
but no `INSERT INTO task_memory` or `INSERT INTO failure_memory` exists
anywhere in `lib/`, `bin/`, or `recipes/`.

## Scope Hint

- `bin/mini-ork-execute` (writers at trace-write time)
- `lib/memory.sh` (add `memory_write_task` + `memory_write_failure` helpers
  alongside the existing arch_specs/atom_prs/adrs writers)

## Schemas to Honor (no migration needed)

```sql
-- task_memory
run_id INTEGER NOT NULL REFERENCES runs(id),
task_class TEXT NOT NULL,
kickoff_hash TEXT NOT NULL,
outcome TEXT NOT NULL CHECK (outcome IN ('success','failure','partial')),
artifacts_produced TEXT NOT NULL DEFAULT '[]',  -- JSON array
duration_ms INTEGER, cost_usd REAL

-- failure_memory
failure_id TEXT PRIMARY KEY,                    -- generated UUID
run_id INTEGER NOT NULL REFERENCES runs(id),
workflow_stage TEXT NOT NULL,                   -- node name
failure_category TEXT NOT NULL,                 -- 'verifier_fail'|'timeout'|'cost_overrun'|'dispatch_error'
error_message TEXT, stack_trace TEXT
```

Note: `run_id` is an INTEGER FK to `runs(id)`, not the textual `MINI_ORK_RUN_ID`.
Resolve `runs.id` via `SELECT id FROM runs WHERE run_uid = ?` where `run_uid`
matches `MINI_ORK_RUN_ID`. If the row doesn't exist (run was never registered),
skip the write — best-effort, do not fail the node.

## Expected Edit

**`lib/memory.sh` additions:**

1. `memory_write_task <task_class> <outcome> <duration_ms> <cost_usd> <artifacts_json>`:
   - Computes `kickoff_hash = sha256sum "$MINI_ORK_KICKOFF_PATH"`.
   - Resolves integer `runs.id` from `MINI_ORK_RUN_ID`.
   - Inserts row; routes failures through `trace_write_or_log` (D2 wrapper).

2. `memory_write_failure <stage> <category> <error_message>`:
   - Generates UUID via `python3 -c 'import uuid; print(uuid.uuid4())'`.
   - Resolves integer `runs.id`.
   - Inserts; best-effort.

**`bin/mini-ork-execute` invocations:**

1. On successful node completion (after `trace_write_node`): call
   `memory_write_task` once per *run* (not per node). Track via a sentinel
   file `${MINI_ORK_RUN_DIR}/.task_memory_written` to ensure idempotence.
2. On node failure (verifier reject, timeout, dispatch error): call
   `memory_write_failure` with appropriate category.
3. Map failure category from `status` field:
   - `vacuous` → `verifier_fail`
   - timeout exit → `timeout`
   - cost-pause sentinel → `cost_overrun`
   - default → `dispatch_error`

## Requirements

- Writers MUST be best-effort: a missing FK or invalid JSON in
  `artifacts_produced` must not fail the run. Route through
  `trace_write_or_log` (D2's wrapper).
- Idempotent at the run level: a single run writes one `task_memory` row
  even if `bin/mini-ork-execute` is re-entered.
- Do NOT modify `.mini-ork/config/**`.
- Do NOT modify migrations.
- Add `tests/unit/test_memory_write_task.sh`: seeds a run row, invokes the
  helper, asserts a `task_memory` row appears with the right `task_class`
  and `outcome`.
- Add `tests/unit/test_memory_write_failure.sh`: invokes with a fake stage
  + category, asserts row inserted + UUID is well-formed.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` covers
  `bin/mini-ork-execute`, `lib/memory.sh`, and two new unit tests.
- `${MINI_ORK_RUN_DIR}/verdict.json`:
  `{ "tests_pass": true, "static_pass": true, "pass": true }`
- Static (shellcheck) passes.
- Tests pass.

## Verification commands

- `shellcheck bin/mini-ork-execute lib/memory.sh`
- `bash tests/unit/test_memory_write_task.sh`
- `bash tests/unit/test_memory_write_failure.sh`

## Out of Scope

- `agent_performance_memory` upserts — defer until multi-lane agent routing
  consumes them.
- `recovery_memory`, `user_preference_memory`, `artifact_memory` — defer.
- Patch #1 (llm_calls producer) — defer.
