-- 0087_theme_clusters.sql — split the clustering store out of lesson_themes.
--
-- lesson_themes was doing two jobs: assign_new wrote one cluster row per novel
-- gradient (lesson_text NULL, status 'candidate') and promote authored lessons
-- into the same table. db:L6 measures blank lesson_text over every row, so a
-- producer that can never reach the authoring bar keeps the ratio high
-- forever. Move the cluster statistics to theme_clusters, carry today's blank
-- rows across once, and leave lesson_themes holding only the authored rows
-- promote() writes.
--
-- Additive and idempotent. gradient_theme, theme_idf, bug_reports and the
-- authored lesson rows are untouched; the delete removes only rows that were
-- actually moved into theme_clusters.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS theme_clusters (
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
  bug_report_id INTEGER,
  updated_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_theme_clusters_kind ON theme_clusters(kind);

-- Preserve every cluster column on the move. Or IGNORE covers the case where
-- runtime ensure_schema() already created the row before this migration ran.
INSERT OR IGNORE INTO theme_clusters (
  theme_id, kind, role, task_class, representative, centroid,
  n_gradients, n_runs, n_at_refresh, first_seen, last_seen,
  bug_report_id, updated_at
)
SELECT theme_id, kind, role, task_class, representative, centroid,
       n_gradients, n_runs, n_at_refresh, first_seen, last_seen,
       bug_report_id, updated_at
  FROM lesson_themes
 WHERE COALESCE(lesson_text, '') = '';

-- Delete only the rows that were moved; authored lessons (and any row that
-- failed the WHERE above) stay in lesson_themes.
DELETE FROM lesson_themes WHERE COALESCE(lesson_text, '') = '' AND theme_id IN (SELECT theme_id FROM theme_clusters);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0087_theme_clusters.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'theme-clusters-v1');
