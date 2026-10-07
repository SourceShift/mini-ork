-- 0063_lesson_themes.sql
-- Group gradient_records into themes so the prompt block stops carrying 10k
-- near-duplicates of the same observation. P2 of the learning-memory refactor
-- (D1 + D2): build the theme cluster, leave the injection filter for D3.
--
-- `gradient_records` holds 10,248 rows today. 5,964 (58%) describe mini-ork's
-- own trace/telemetry fields (verifier_output, tool_calls, files_read,
-- duration_ms, cost_usd, prompt_version_hash, context_bundle_hash, …). Those
-- are bugs in mini-ork's tracing, not guidance for the task, yet they can be
-- injected into task prompts. Nobody can read 10k rows. The IDE needs one row
-- per idea.
--
-- ADDITIVE only. No guarded ALTERs: both tables are NEW, so a plain
-- CREATE TABLE IF NOT EXISTS is enough. A database predating this migration
-- still works because every entry point calls themes.ensure_schema() first;
-- the migration exists for version-migration discipline, not correctness.
--
-- Three tables:
--   lesson_themes — one row per theme (kind, representative, centroid, sizes).
--   gradient_theme — many-to-one gradient → theme mapping, with similarity.
--   theme_idf — per-token document frequency for the TF-IDF centroid.
--
-- `bug_report_id` is a soft FK to bug_reports(id) — no constraint, since the
-- migration is additive and we do not want a circular dependency at apply.
--
-- theme_idf: ``df`` counts how many gradient_records.signal contain the token.
-- One sentinel row — token = the NUL-prefixed string '\x00N' (which
-- normalize() can never emit; its output is printable ASCII) — stores the
-- total gradient count N in its ``df`` column. themes.assign_new re-reads this
-- table on every call and rebuilds it when N has grown by >= 25%.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS lesson_themes (
  theme_id      TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK(kind IN ('task','framework')),
  role          TEXT,
  task_class    TEXT,
  representative TEXT NOT NULL,
  centroid      BLOB NOT NULL,
  n_gradients   INTEGER NOT NULL DEFAULT 0,
  n_runs        INTEGER NOT NULL DEFAULT 0,
  n_at_refresh  INTEGER NOT NULL DEFAULT 0,
  first_seen    INTEGER,
  last_seen     INTEGER,
  lesson_text   TEXT,
  status        TEXT NOT NULL DEFAULT 'candidate',
  bug_report_id INTEGER,
  updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS gradient_theme (
  gradient_id   TEXT PRIMARY KEY,
  theme_id      TEXT NOT NULL,
  similarity    REAL
);

CREATE INDEX IF NOT EXISTS idx_gradient_theme_theme
  ON gradient_theme(theme_id);

CREATE TABLE IF NOT EXISTS theme_idf (
  token TEXT PRIMARY KEY,
  df    INTEGER NOT NULL
);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0063_lesson_themes.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'lesson-themes-v1');