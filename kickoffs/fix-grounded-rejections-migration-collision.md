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

The two migrations are NOT two versions of one table — they are semantically
distinct tables that collided on the name `grounded_rejections`, with
INCOMPATIBLE primary keys that cannot be reconciled by additive ALTER:
- `0037` = gate-failure tuples (`id TEXT PRIMARY KEY`, `gate_name/verdict/
  concern/evidence_trace_ids`, plus immutability triggers
  `grounded_rejections_no_immutable_update` / `grounded_rejections_no_delete`).
  Its ONLY consumer is `lib/gates_common.sh`, which writes this shape.
- `0040` = refuted-draft events (`id INTEGER PRIMARY KEY AUTOINCREMENT`,
  `trace_id/task_class/node_type/claim/refutation/evidence_json`). It has
  ZERO consumers in `lib/` or `bin/`.

Fix by KEEPING `0037` as the canonical `grounded_rejections` (unchanged,
triggers intact) and RENAMING `0040`'s table + its three indexes to a distinct,
non-colliding name (e.g. `grounded_draft_rejections`) so the index creation no
longer targets 0037's columns. The implementer may instead drop-and-recreate
`0040`'s table under the new name. Do NOT merge the two. First grep
`grounded_rejections` across `lib/`/`bin/` to confirm the consumer inventory
before choosing; if any consumer of 0040's columns turns up, repoint it to the
renamed table.

## Requirements

- Fresh `MINI_ORK_HOME=$(mktemp -d)/.mini-ork ./bin/mini-ork init` exits 0.
- Already-initialized DBs are not broken (idempotent; no destructive DROP of a
  populated table — guard any rename so it is safe on a DB that already has
  0040's table).
- `0037` stays canonical: its columns, indexes, AND immutability triggers are
  preserved unchanged.
- `0040`'s columns and indexes SURVIVE under the renamed table — they must not
  be silently dropped (the rename target must exist with those columns/indexes).
- `lib/gates_common.sh` still resolves its `grounded_rejections` columns.

## Done When

- A fresh init in a temp home exits 0; `grounded_rejections` exists with the
  0037 schema and the renamed table exists with 0040's columns/indexes.
- `./bin/mini-ork doctor` exits 0.
- Any `verdict.json` example the change documents uses keys in exactly this
  order: `files_changed, tests_pass, static_pass, pass`.
