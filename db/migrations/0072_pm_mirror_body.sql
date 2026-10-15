-- 0072_pm_mirror_body.sql
-- PM layer: let the mirror hold the *body* of a ticket it has read live, so a
-- ticket page can show a description, a created date and a comments thread
-- without a second network call, and can degrade to the last thing the source
-- said instead of to nothing when the source is down.
--
-- 0069 kept pm_issues deliberately body-less: an INDEX, not a source of truth,
-- so no caller could be fooled into thinking the mirror was the whole issue.
-- That property is kept, not revoked. What changes is that a read which
-- actually happened is allowed to be remembered, and remembered *as a cache*:
--
--   description     TEXT  -- the body text of the last successful live read
--   created_at      TEXT  -- when the source says the issue was opened
--   body_synced_at  TEXT  -- when that read happened (NULL: never read live)
--   body_error      TEXT  -- why the most recent read failed; cleared on success
--
-- All four are NULLABLE with NO DEFAULT, and that is the point: NULL means
-- "this column has never been filled by a live read", which a page must be able
-- to tell apart from "the source answered, and the body is empty" — the writer
-- records '' for the latter. A DEFAULT '' would collapse the two, and an
-- "unreachable, never cached" ticket would read exactly like an empty one.
--
-- ADDITIVE only. No existing column is altered, dropped or backfilled, and
-- 0069_pm_mirror.sql is untouched — editing a shipped migration trips the
-- checksum-drift guard in mini_ork/stores/migrate.py, and existing mirror rows
-- stay valid as they are (every new column is NULL for them, correctly meaning
-- "never read live").
--
-- The reader/writer is mini_ork/pm/store.py, which runs the same ALTERs at
-- runtime (_ensure_body_columns, driven by BODY_COLUMNS) so a state.db that
-- never ran this migration still works — the precedent set by scheduler's
-- ensure_priority_column / ensure_retry_schema. tests/unit/test_pm_store.py
-- asserts the two texts agree, so they cannot drift apart quietly.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and
-- here a reapply is not hypothetical — a runtime ensure_schema may have added
-- these columns before this migration was ever recorded. Same `.read|sh` idiom
-- as 0037/0041/0042/0055/0058; db/init.sh and migrate.py's _dot_env both export
-- MINI_ORK_DB, so ${MINI_ORK_DB:?} resolves to the DB being migrated. The
-- existence check on `description` gates all four — they are added together,
-- so if one is present the whole block is skipped.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"pm_issues\\\") WHERE name = \\\"description\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE pm_issues ADD COLUMN description TEXT;\" \"ALTER TABLE pm_issues ADD COLUMN created_at TEXT;\" \"ALTER TABLE pm_issues ADD COLUMN body_synced_at TEXT;\" \"ALTER TABLE pm_issues ADD COLUMN body_error TEXT;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0072_pm_mirror_body.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'pm-mirror-body-v1');
