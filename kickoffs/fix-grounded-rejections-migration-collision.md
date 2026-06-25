# Fix grounded_rejections migration collision (fresh init aborts)

## Goal

A fresh `./bin/mini-ork init` (clean `MINI_ORK_HOME`) must exit 0. Today it
exits 1 with `db/init.sh exited non-zero — state.db was not initialized`,
which blocks new installs, temp-home smoke gates, AND the per-tenant fresh DBs
the shared-brain RLM serving path needs.

## Root cause

Two migrations create the SAME table `grounded_rejections` with INCOMPATIBLE
schemas, both via `CREATE TABLE IF NOT EXISTS`:

- `db/migrations/0037_grounded_rejection.sql` (singular filename):
  `id TEXT PRIMARY KEY, ts, run_id, gate_name, verdict, concern,
  evidence_trace_ids, evidence_summary, …` (HarnessBridge gate schema).
- `db/migrations/0040_grounded_rejections.sql` (plural filename):
  `id INTEGER PRIMARY KEY AUTOINCREMENT, run_id, trace_id, task_class,
  node_type, claim, refutation, evidence_json, source_artifact, created_at`.

On a fresh init, migrations apply in order: 0037 creates the table with its
schema. 0040's `CREATE TABLE IF NOT EXISTS grounded_rejections` then no-ops
because the table already exists — but 0040 immediately runs
`CREATE INDEX idx_grounded_rejections_task_class ON grounded_rejections(task_class, created_at DESC)`
and `CREATE INDEX idx_grounded_rejections_trace ON grounded_rejections(trace_id …)`.
Columns `task_class`, `created_at`, `trace_id` do NOT exist in 0037's schema,
so the index creation fails with `no such column: task_class` and aborts
`db/init.sh`. Existing initialized DBs survive only because they predate the
collision.

## Scope Hint

- `db/migrations/0037_grounded_rejection.sql`
- `db/migrations/0040_grounded_rejections.sql`

## Expected Edit

Reconcile the two migrations so a fresh init applies both cleanly and ends with
ONE canonical `grounded_rejections` schema. Decide the canonical column set by
reading every consumer of `grounded_rejections` under `lib/` and `bin/` (grep
the table name) and preserving the columns those readers actually query — do
NOT guess. Then make the migrations converge: e.g. one migration owns the
`CREATE TABLE` and the other becomes idempotent `ALTER TABLE … ADD COLUMN`
guards plus indexes that only reference columns guaranteed to exist at that
point. Both fresh-init order (0037→0040) and already-initialized DBs must end
in the same final schema. Keep `PRAGMA foreign_keys` handling intact.

## Requirements

- Fresh `MINI_ORK_HOME=$(mktemp -d)/.mini-ork ./bin/mini-ork init` exits 0.
- Already-initialized DBs are not broken (migrations remain idempotent/additive;
  no destructive DROP of a populated table).
- Every `lib/`/`bin/` reader of `grounded_rejections` still resolves its columns
  against the final schema.

## Done When

- A fresh init in a temp home exits 0 and `grounded_rejections` exists with the
  canonical columns.
- `./bin/mini-ork doctor` exits 0.
- `grep -rn grounded_rejections lib/ bin/` consumers all reference columns that
  exist in the reconciled schema.
