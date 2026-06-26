# code-fix: resolve grounded_rejections migration collision (0037 vs 0040)

## Goal
`db/init.sh` must run to completion with ZERO errors on a copy of the live
`.mini-ork/state.db`, and `grounded_rejections` must end up with the canonical
HarnessBridge-T4 schema from `0037_grounded_rejection.sql` — NOT Phase 0's simpler
`0040` schema, which currently shadows it on the live DB.

## Root cause (verified)
Two committed migrations create the SAME table `grounded_rejections` with
incompatible schemas:
- `db/migrations/0037_grounded_rejection.sql` — canonical: columns `id (TEXT PK)`,
  `ts`, `run_id`, `gate_name`, `verdict`, `concern`, `evidence_trace_ids`,
  `evidence_summary`, `suggestion`, `consumed_by_reflector_ts`, plus append-only
  immutability triggers `grounded_rejections_no_immutable_update` /
  `grounded_rejections_no_delete`. This is the schema task #9 (wire grounded
  rejection into the 5 oracle gates) depends on.
- `db/migrations/0040_grounded_rejections.sql` — Phase 0 smoke surface: columns
  `id (INTEGER PK)`, `run_id`, `trace_id`, `task_class`, `node_type`, `claim`,
  `refutation`, `evidence_json`, `source_artifact`, `created_at`. No triggers.

On the live DB the 0040 schema won (live `grounded_rejections` has `claim`/
`refutation`/`created_at`, no `ts`). So re-running `db/init.sh` aborts: 0037's
`CREATE INDEX ... ON grounded_rejections(ts ...)` fails with "no such column: ts"
because the existing table is the 0040 shape. `0037` is lower-numbered and is the
correct owner.

## Resolution (do exactly this — do not widen scope)
1. **0037 owns the table.** Make `0037_grounded_rejection.sql` fully idempotent if it
   is not already (it uses `CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`, which is fine).
   Do not change its schema.
2. **Neutralize the colliding 0040.** Rewrite `db/migrations/0040_grounded_rejections.sql`
   so it no longer creates a conflicting `grounded_rejections` table. It must become a
   superseded no-op that documents the merge into 0037 (a comment header plus a
   harmless `PRAGMA foreign_keys = ON;` is acceptable). Keep the filename so
   `schema_migrations` history is preserved.
3. **Add a reconciliation migration** `db/migrations/0041_grounded_rejections_reconcile.sql`
   that converges any DB still on the 0040 shape onto the 0037 shape: if a
   `grounded_rejections` table exists WITHOUT a `ts` column AND has zero rows, DROP it
   and recreate it with the exact 0037 schema + indexes + triggers. Must be idempotent
   (a DB already on the 0037 shape is left untouched; never drops a table that has
   rows). Use a guarded approach (e.g. check `pragma_table_info` for `ts` and a row
   count) implemented via the runner-compatible pattern; do NOT use
   `PRAGMA writable_schema`.
4. **Point the smoke harness at canonical columns.** Update the grounded_rejections
   assertion in `scripts/smoke-learning-loops.sh` to insert/verify using the 0037
   columns (`id`, `gate_name`, `verdict`, `concern`, `evidence_trace_ids`,
   `evidence_summary`, `suggestion`), respecting the append-only triggers. All other
   smoke phases must still pass.

## Scope (explicit)
- IN: `db/migrations/0037_grounded_rejection.sql` (idempotency only),
  `db/migrations/0040_grounded_rejections.sql` (neutralize),
  `db/migrations/0041_grounded_rejections_reconcile.sql` (new),
  `scripts/smoke-learning-loops.sh` (grounded_rejections assertion only).
- OUT: no changes to `bin/mini-ork-execute`, no other migrations, no other smoke
  phases, no learning-loop logic, no live-DB mutation inside implementer nodes
  (validate on a COPY only).

## Proof command
```
cp .mini-ork/state.db /tmp/grc.db && \
  MINI_ORK_DB=/tmp/grc.db bash db/init.sh >/tmp/grc-init.log 2>&1 && \
  ! grep -qiE 'parse error|^Error:' /tmp/grc-init.log && \
  sqlite3 /tmp/grc.db "SELECT COUNT(*) FROM pragma_table_info('grounded_rejections') WHERE name IN ('ts','concern','evidence_trace_ids','suggestion','consumed_by_reflector_ts');" | grep -q '^5$' && \
  sqlite3 /tmp/grc.db "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ('grounded_rejections_no_immutable_update','grounded_rejections_no_delete');" | grep -q '^2$' && \
  MINI_ORK_DB=/tmp/grc.db bash db/init.sh >/tmp/grc-init2.log 2>&1 && \
  ! grep -qiE 'parse error|^Error:' /tmp/grc-init2.log && \
  bash scripts/smoke-learning-loops.sh
```
(Second init run proves idempotency: re-applying produces zero errors.)

## Done when
Proof command exits 0: clean init, canonical 0037 schema present (5 columns + 2
triggers), idempotent on re-run, and all smoke-learning-loops phases pass.
