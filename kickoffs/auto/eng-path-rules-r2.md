# Path-scoped rules — revision 2: the table rebuild must survive dependent views

WIP commit 83d864da holds revision 1. Opus passed it: 62 tests, ruff clean, throwaway-home proofs.
On a backup copy of the LIVE `state.db`, though, the first path rule fails:

```
$ mini-ork prefs set ide-tests "After editing ide_pages, run tests/unit/test_ide_pages_*.py" --scope path --target 'mini_ork/ide_pages/**'
sqlite3.OperationalError: error in view v_memory_health: no such table: main.user_preference_memory
```

The live DB has `CREATE VIEW v_memory_health AS SELECT … FROM task_memory UNION ALL … FROM
user_preference_memory …`. SQLite's table rebuild (create new, copy, drop old, rename) fails while
a view references the table: the rename re-parses dependent views and finds the table missing. The
unit tests use DBs without that view, so they never saw it.

## Files in scope

- `mini_ork/memory/preferences.py`: ONLY the CHECK-widening rebuild in `ensure_schema` (and the
  helper it uses)
- `db/migrations/0065_preference_path_scope.sql`
- `tests/unit/test_prefs_path_scope.py`

Do NOT modify any other file.

## Fix (exact)

In BOTH the migration and `ensure_schema`'s rebuild:

1. Collect every `view` and `trigger` in `sqlite_master` whose `sql` references
   `user_preference_memory`. Save each `(type, name, sql)`.
2. In one transaction: drop those views and triggers; create `user_preference_memory_new` with
   the widened CHECK; copy rows; drop the old table; `ALTER TABLE … RENAME TO
   user_preference_memory`; recreate every saved view and trigger from its exact SQL.
3. `PRAGMA foreign_keys` handling as before. Never leave the DB without the views: on any error,
   roll back the whole transaction.
4. Idempotent: if the CHECK already allows `'path'`, do nothing.
5. The SQL migration can't introspect, so 0065 explicitly drops and recreates `v_memory_health`
   with its current definition. Copy it byte-for-byte from the live DB: `SELECT sql FROM
   sqlite_master WHERE name='v_memory_health'` on `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/state.db`,
   read-only. `ensure_schema` stays generic (steps 1–4).

## Tests (add to `tests/unit/test_prefs_path_scope.py`)

- A temp DB with the OLD table, a view `v_memory_health` selecting `COUNT(*)` from it (plus one
  other table), and a trigger on it. After `ensure_schema`: a path rule inserts; the view still
  exists and returns the right count; the trigger still exists.
- Apply the migration 0065 SQL to a temp DB with that view: same assertions.

## Verification command

The command that proves this run succeeded:

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_prefs_path_scope.py tests/unit/test_prefs.py tests/unit/test_learned_record.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/memory/preferences.py tests/unit/test_prefs_path_scope.py` → clean.
- Proof on a BACKUP of the live DB, in a throwaway home: `prefs set … --scope path --target
  'mini_ork/ide_pages/**'` succeeds, `SELECT COUNT(*) FROM v_memory_health` still works, and
  `prefs preview` of a kickoff scoping `mini_ork/ide_pages/node.py` shows the rule. Paste it, then
  delete the temp home.
- `git diff 83d864da --stat` touches only the files in scope.
