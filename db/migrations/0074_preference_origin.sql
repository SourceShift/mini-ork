-- 0074_preference_origin.sql — let a preference record WHERE it came from and
-- whether an agent may be told it yet.
--
-- LF-R4. The "Your code" tab's "Make it a rule" action used to write a rule that
-- was operator-authored, area-wide and raw. It now writes a DRAFT: a rule
-- LEARNED from review findings, scoped to the files the problem was found in,
-- and inactive until a human approves it on the Rules tab. Three columns carry
-- that:
--
--   origin      TEXT NOT NULL DEFAULT 'operator'  'operator' | 'learned'
--   source_ids  TEXT                              JSON array of the finding
--                                                 keys the rule was derived
--                                                 from; NULL for operator rules
--   active      INTEGER NOT NULL DEFAULT 1        0 = draft, never injected
--
-- The defaults are what make this additive for every existing row: a rule that
-- predates this migration was written by the operator and is live, so it reads
-- back origin='operator', active=1 with no backfill. A NULL `source_ids` means
-- "no finding provenance recorded", which a reader must tell apart from "an
-- empty provenance list" — hence no DEFAULT ''.
--
-- ADDITIVE only: no existing column is altered, dropped or backfilled, and
-- 0065_preference_path_scope.sql is untouched (editing a shipped migration trips
-- the checksum-drift guard in mini_ork/stores/migrate.py).
--
-- The runtime twin is mini_ork/memory/preferences.py `_ensure_schema_con`, which
-- adds the same three columns at the first `set_pref` on an unmigrated DB ("a
-- state.db that never ran this migration still works" — the precedent set by
-- 0072's _ensure_body_columns). tests/unit/test_prefs_learned_rules.py and the
-- blind spec suite assert the two agree.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and a
-- reapply is not hypothetical — the runtime twin (_ensure_schema_con) adds these
-- columns at the first set_pref on new code, which can happen before this file
-- is ever recorded. Same `.read|sh` idiom as 0072; migrate.py's _dot_env exports
-- MINI_ORK_DB. The check on `origin` gates all three: they are added together.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"user_preference_memory\\\") WHERE name = \\\"origin\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE user_preference_memory ADD COLUMN origin TEXT NOT NULL DEFAULT '\''operator'\'';\" \"ALTER TABLE user_preference_memory ADD COLUMN source_ids TEXT;\" \"ALTER TABLE user_preference_memory ADD COLUMN active INTEGER NOT NULL DEFAULT 1;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0074_preference_origin.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'preference-origin-v1');
