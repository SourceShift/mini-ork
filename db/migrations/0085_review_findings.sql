-- 0085_review_findings.sql
-- Pre-push review finding lifecycle: stable fingerprints, per-finding status,
-- and the link from an observation row to its finding. The old per-review
-- rows in pre_push_review_issues stay untouched observations.

CREATE TABLE IF NOT EXISTS review_findings (
  fingerprint      TEXT PRIMARY KEY,
  lens_kind        TEXT NOT NULL CHECK (lens_kind IN ('heuristic','llm')),
  file_path        TEXT,
  title            TEXT NOT NULL,
  severity         TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical')),
  status           TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','fixed','dismissed','pre_existing')),
  first_review_id  INTEGER NOT NULL,
  last_review_id   INTEGER NOT NULL,
  last_issue_id    INTEGER,
  times_seen       INTEGER NOT NULL DEFAULT 1,
  reopened         INTEGER NOT NULL DEFAULT 0,
  reason           TEXT,
  resolved_by      TEXT,
  resolved_at      INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rf_status_file ON review_findings(status, file_path);

CREATE TABLE IF NOT EXISTS review_finding_links (
  issue_id     INTEGER PRIMARY KEY,
  fingerprint  TEXT NOT NULL,
  review_id    INTEGER NOT NULL,
  pre_existing INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rfl_review ON review_finding_links(review_id);

CREATE TABLE IF NOT EXISTS review_lens_runs (
  id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, lens TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','dispatch_failed','unparseable','error')),
  issues INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rlr_review ON review_lens_runs(review_id);

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0085_review_findings.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'review-findings-v1');
