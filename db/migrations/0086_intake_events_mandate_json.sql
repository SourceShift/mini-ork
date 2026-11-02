-- 0086_intake_events_mandate_json.sql — record who admitted a ticket, under
-- which rules.
--
-- A ledger row said *that* an arrival acted and, since 0075, which run it
-- started. It could not say *why it was allowed*: "no trigger is configured, so
-- nothing matched" and "the poller has not run yet" both rendered as silence, and
-- a policy that refused admission left no trace at all.
--
--   mandate_json  TEXT  JSON admission record (mini_ork/intake/mandate.py):
--                       actor, source, scope_ref, rule_ids, policy_version,
--                       strength (granted/refused/denied), detail, decided_by,
--                       decision_id, at.
--                       NULL for rows written before this column existed.
--
-- NULLABLE with no DEFAULT, as with run_id: NULL means "this row predates the
-- mandate", which is a different statement from "an empty mandate was recorded".
--
-- ADDITIVE only. 0070_threads.sql is untouched — its text must stay identical to
-- the runtime DDL, and editing a shipped migration trips the checksum-drift guard
-- in mini_ork/stores/migrate.py.
--
-- The runtime twin is mini_ork/threads/store.py `_ensure_intake_columns`, driven
-- by INTAKE_COLUMNS; tests/unit/test_threads_store.py asserts each runtime column
-- is declared by a migration, so the two cannot drift apart quietly.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and a
-- reapply is not hypothetical — the runtime twin adds the column on the first
-- write of new code, which can happen before this file is ever recorded. Same
-- `.read|sh` idiom as 0072/0074/0075; migrate.py's _dot_env exports MINI_ORK_DB.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"intake_events\\\") WHERE name = \\\"mandate_json\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE intake_events ADD COLUMN mandate_json TEXT;\"; fi'"

PRAGMA foreign_keys = ON;
