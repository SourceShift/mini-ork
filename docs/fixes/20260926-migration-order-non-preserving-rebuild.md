# Migration ordering: a pending earlier rebuild runs after later additive migrations

**Date:** 2026-09-26
**Origin:** `mini_ork/stores/migrate.py` + `db/migrations/0054_execution_traces_status_blocked.sql`
**Status:** Open — mechanism verified, no guard in place.

## Summary

Two defects compose into silent data loss. Neither is a bug in isolation.

1. **The runner has no ordering guard.** `migrate_apply` applies pending
   `*.sql` files in lex order and checks each file only against *its own* ledger
   row. It never compares a pending migration against the highest migration
   already applied.
2. **`0054` is a non-preserving rebuild.** It is a create-copy-drop-rename that
   hard-codes the column list that existed when it was written, so anything a
   later migration added is absent from the copy — and the drop takes it away.

Together: if `0054` is ever pending while later-numbered migrations are already
applied, the rebuild runs *last* and silently removes their columns. The ledger
then records all three as applied, so every subsequent `migrate_apply` is a
no-op and the damage does not self-correct.

## Defect 1 — the runner applies pending migrations without an ordering check

`mini_ork/stores/migrate.py:285`:

```python
for f in sorted(Path(migrations_dir).glob("*.sql")):
    filename = f.name
    sum_hex = checksum(f)
    row = con.execute(
        "SELECT COALESCE(checksum,'') FROM schema_migrations WHERE filename=?",
        (filename,)).fetchone()
    applied = row is not None
    if applied:
        ...  # rehash legacy / detect drift / continue
    ...
    out.append(f"  [apply]   {filename}")
```

The loop is `sorted(glob)` — lexicographic over the whole directory — and the
only state consulted is whether *this* filename already has a row. There is no
read of `MAX(filename)` over `schema_migrations`, so nothing distinguishes
"this migration has never run" from "this migration should have run before the
ones that already did".

Lex order is correct for a database built from scratch. It is wrong for a
database that skipped a file: the skipped file sorts into its old position
relative to its peers, but it *executes* after all of them.

A guard does not need to be clever. Comparing `filename` against the highest
applied filename, and refusing (or warning) when a pending file sorts below it,
closes the whole class.

## Defect 2 — 0054 rebuilds the table with a frozen column list

`db/migrations/0054_execution_traces_status_blocked.sql` is a rebuild of
`execution_traces`, not an in-place change:

- `:12` `CREATE TABLE execution_traces_new (`
- `:45` `route_score           REAL    DEFAULT NULL` — the last column defined
- `:48` `INSERT INTO execution_traces_new (` … `:55` `… route_score`
- `:64` `… route_score` — end of the `SELECT` feeding it
- `:67` `DROP TABLE execution_traces;`
- `:68` `ALTER TABLE execution_traces_new RENAME TO execution_traces;`
- `:70`–`:87` recreate the indexes it knows about, all suffixed `_v54`

The column list at `:45`, the insert list at `:55`, and the select list at `:64`
all stop at `route_score`. Later migrations added columns after it:

- `0057` `:21` `ALTER TABLE execution_traces ADD COLUMN route_margin REAL DEFAULT NULL;`
- `0059` `:22` `ALTER TABLE execution_traces ADD COLUMN predicted_error REAL DEFAULT NULL;`

Neither appears in `execution_traces_new`, so `DROP TABLE` at `:67` discards
them along with every row's value. The same applies to their indexes, which the
rebuild does not recreate:

- `0057` `:23` `idx_et_route_margin_v57` (partial, `WHERE route_margin IS NOT NULL`)
- `0059` `:24` `idx_et_predicted_error_v59`

## Not hypothetical

This has fired. In a database whose `schema_migrations` rows for the three
`execution_traces` migrations were inspected, `0054`'s row postdates both
`0057`'s and `0059`'s, and both columns were missing from the live table —
`route_margin` had been populated by earlier runs before it was dropped. The
columns were restored by a manual repair, not by a migration.

The ordering is what to look at, not the database. A ledger where
`applied_at(0054) > applied_at(0057)` is a ledger where the rebuild ran last.

## Impact

- **Silent.** No migration fails. `migrate_verify` returns 0, because every
  applied checksum still matches its file.
- **Permanent.** The ledger now has rows for 0054, 0057 and 0059, so all three
  are skipped on every future run. Nothing re-adds the columns until a new
  migration is written for them.
- **Downstream.** `route_margin` is the input the UCCI calibration map fits
  (`mini_ork/dispatch/calibration.py`); `predicted_error` records its output.
  With the columns gone, `sqlite3.OperationalError` is caught and
  `load_margin_rows` returns `[]` (fail-open), so routing silently reverts to
  uncalibrated rather than erroring — the failure mode is degraded behaviour,
  not a crash.

## Recommended fixes

Two independent changes; either alone would have prevented the observed
damage, and they defend different things.

**Runner (prevents the class).** In `migrate_apply`, before applying a pending
file, compare against the highest already-applied filename. A pending file that
sorts below it is out of order: fail with the two filenames, or warn loudly and
require an explicit override such as `MO_MIGRATE_ALLOW_REORDER=1`. Failing closed
matches how the runner already treats checksum drift.

**Migration (removes the specific hazard).** A destructive rebuild should not
enumerate columns by hand. Ways to make `0054`-style rebuilds preserving:

- Build the new table's column list from `PRAGMA table_info(execution_traces)`
  at run time, so later columns are carried through, rather than freezing it in
  the file. This needs the runner to run SQL generated at apply time rather than
  a static `.sql` body (the dot-command machinery in `_exec_statements` is the
  existing precedent for non-static bodies).
- Or recreate every index on the rebuilt table by querying `sqlite_master` for
  the table's indexes instead of listing them, so a dropped-then-renamed table
  comes back with the indexes its migrations created.

The generic rule: a migration that drops and recreates a table it does not own
exclusively must derive the schema from the live table, or it will be wrong the
moment a sibling migration lands after it.

## Verification

**Against a live ledger** — no reproduction needed. A `0054` row whose
`applied_at` sorts above a `0057` row *is* the out-of-order apply:

```bash
sqlite3 "$MINI_ORK_DB" \
  "SELECT filename, applied_at FROM schema_migrations
    WHERE filename LIKE '005%' ORDER BY applied_at;"
```

**Minimal reproduction** — a synthetic pair in a scratch root. Run from the
repo checkout; `init_db` resolves migrations as `<root>/db/migrations`.

```bash
D=$(mktemp -d); R="$D/root"; DB="$D/t.db"; mkdir -p "$R/db/migrations"

sqlite3 "$DB" "CREATE TABLE execution_traces
  (id INTEGER PRIMARY KEY, run_id TEXT, status TEXT);"
printf 'ALTER TABLE execution_traces ADD COLUMN route_margin REAL DEFAULT NULL;\n' \
  > "$R/db/migrations/00AA_add_margin.sql"
cat > "$R/db/migrations/00AB_rebuild.sql" <<'SQL'
CREATE TABLE execution_traces_new
  (id INTEGER PRIMARY KEY, run_id TEXT, status TEXT);   -- frozen list
INSERT INTO execution_traces_new (id, run_id, status)
  SELECT id, run_id, status FROM execution_traces;
DROP TABLE execution_traces;
ALTER TABLE execution_traces_new RENAME TO execution_traces;
SQL

sqlite3 "$DB" "CREATE TABLE schema_migrations
  (filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT,
   mini_ork_version TEXT);"
# 00AA is already applied; 00AB — which sorts after it — is still pending.
sqlite3 "$DB" "INSERT INTO schema_migrations(filename, applied_at, checksum)
  VALUES ('00AA_add_margin.sql','2026-01-01T00:00:00','deadbeef');"

sqlite3 "$DB" "SELECT group_concat(name) FROM pragma_table_info('execution_traces');"
python3 -m mini_ork.stores.migrate --db "$DB" --root "$R"
sqlite3 "$DB" "SELECT group_concat(name) FROM pragma_table_info('execution_traces');"
```

Observed on 2026-09-26: the column list goes from
`id,run_id,status,route_margin` to `id,run_id,status`. The runner reports
`[apply] 00AB_rebuild.sql` / `[ok]`, then commit — `route_margin` is gone, and
because `00AA_add_margin.sql` still has its ledger row, re-running does not put
it back. The guard described above would instead stop at `00AB` and name
`00AA`.

Note the tail of that run: `init_db`'s "expected >= 20 tables" sanity check trips
*after* the migrations have been applied and committed. A table-count check is
not an ordering check, and it does not undo anything.

A regression test belongs next to the runner's existing tests, over a scratch
database with a synthetic pair like the above. It does not need the real `0054`.
