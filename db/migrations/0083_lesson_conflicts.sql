-- 0083_lesson_conflicts.sql — record the near-duplicate / contradictory
-- relations a candidate lesson has with the lessons already live for its task
-- class.
--
-- Trace2Skill consolidates lessons distilled from many traces. mini-ork writes
-- lessons in two places — `pattern_records`/`emergent_patterns` via
-- `pattern_induction.induce_pending`, and `lesson_themes` via `themes.promote` —
-- and neither compared the new text with the lessons already live. Two
-- contradictory lessons could therefore both reach a prompt, and near-duplicates
-- piled up. `mini_ork.learning.lesson_conflict.check` now runs one judge call
-- before the write and records every non-independent relation here; a
-- `contradicts` also enqueues one `lesson-conflict` oversight item.
--
-- ADDITIVE only: the table is new, so a plain `CREATE TABLE IF NOT EXISTS` is
-- enough — no guarded ALTER. `lesson_conflict.py` carries a verbatim copy of
-- this DDL for databases that have not run the migration runner (the same
-- self-bootstrap discipline `oversight_inbox` and `themes._ensure_schema_con`
-- use).
--
-- The unique key makes the relation write idempotent: a second identical check
-- adds no row. `why` and `candidate_text` are diagnostic only — the operator
-- reading the oversight item needs to see what was withheld.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS lesson_conflicts (
  id             INTEGER PRIMARY KEY,
  source_kind    TEXT NOT NULL,
  source_id      TEXT NOT NULL,
  against_kind   TEXT NOT NULL,
  against_id     TEXT NOT NULL,
  relation       TEXT NOT NULL,
  why            TEXT,
  candidate_text TEXT,
  created_at     INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_lesson_conflicts_pair
  ON lesson_conflicts(source_kind, source_id, against_kind, against_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0083_lesson_conflicts.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'lesson-conflicts-v1');
