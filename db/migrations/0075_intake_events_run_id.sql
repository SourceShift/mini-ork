-- 0075_intake_events_run_id.sql — let an arrival record which run it started.
--
-- The ledger had no `run_id`. `intake/dispatch.py` recorded the arrival's
-- thread, and a thread is not the run: it carries one run per attempt, so
-- "which run did this ticket start?" could not be answered from the ledger —
-- only guessed from the thread's newest row, which is a different question
-- after a continuation.
--
--   run_id  TEXT   the run this arrival started; NULL when it started none
--                  (a replay that folded into an existing run) and NULL for
--                  every row written before this column existed
--
-- NULLABLE with NO DEFAULT, and that is the point: NULL means "this arrival
-- started no run", which a page must be able to tell apart from "it started
-- one, and the id is ''" — the latter never happens. A DEFAULT '' would
-- collapse the two and an unlinked arrival would render as a broken one.
--
-- ADDITIVE only. No existing column is altered, dropped or backfilled, and
-- 0070_threads.sql is untouched — editing a shipped migration trips the
-- checksum-drift guard in mini_ork/stores/migrate.py, and 0070's own docstring
-- requires its text to stay identical to the runtime DDL.
--
-- The runtime twin is mini_ork/threads/store.py `_ensure_intake_columns`,
-- driven by INTAKE_COLUMNS, which widens an existing ledger on the first
-- write ("a state.db that never ran this migration still works" — the
-- precedent set by 0072's _ensure_body_columns). tests/unit/test_threads_store.py
-- asserts the two agree, so they cannot drift apart quietly.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and a
-- reapply is not hypothetical — the runtime twin adds the column on the first
-- write of new code, which can happen before this file is ever recorded. Same
-- `.read|sh` idiom as 0072/0074; migrate.py's _dot_env exports MINI_ORK_DB.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"intake_events\\\") WHERE name = \\\"run_id\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE intake_events ADD COLUMN run_id TEXT;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0075_intake_events_run_id.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'intake-events-run-id-v1');
