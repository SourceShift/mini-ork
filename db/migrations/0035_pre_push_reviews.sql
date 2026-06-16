-- 0035_pre_push_reviews.sql
-- Pre-push code-review gate (Layer 3 of .githooks/pre-push).
-- Reviews the diff before push, raises bug_reports for issues, optionally
-- dispatches a mini-ork fix epic via the existing scheduler, and gates
-- push to main on a clean verdict.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS pre_push_reviews (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  reviewed_at         INTEGER NOT NULL,
  source_sha          TEXT NOT NULL,                 -- local SHA being pushed
  target_branch       TEXT NOT NULL,                 -- main / feature/...
  reviewer_mode       TEXT NOT NULL DEFAULT 'heuristic'
                      CHECK (reviewer_mode IN ('heuristic','llm_panel','hybrid')),
  files_changed       INTEGER NOT NULL DEFAULT 0,
  lines_added         INTEGER NOT NULL DEFAULT 0,
  lines_removed       INTEGER NOT NULL DEFAULT 0,
  verdict             TEXT NOT NULL DEFAULT 'pending'
                      CHECK (verdict IN ('pending','approve','warn','block','aborted')),
  issues_open         INTEGER NOT NULL DEFAULT 0,
  issues_critical     INTEGER NOT NULL DEFAULT 0,
  fix_epic_id         TEXT,                          -- when block + auto-fix raised an epic
  cost_usd            REAL NOT NULL DEFAULT 0.0,
  rationale           TEXT
);

CREATE INDEX IF NOT EXISTS idx_pre_push_reviews_target
  ON pre_push_reviews(target_branch, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_pre_push_reviews_verdict
  ON pre_push_reviews(verdict) WHERE verdict != 'approve';

CREATE TABLE IF NOT EXISTS pre_push_review_issues (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id           INTEGER NOT NULL REFERENCES pre_push_reviews(id) ON DELETE CASCADE,
  lens                TEXT NOT NULL,                 -- heuristic.<check> or llm.<lane>
  severity            TEXT NOT NULL DEFAULT 'medium'
                      CHECK (severity IN ('info','low','medium','high','critical')),
  file_path           TEXT,
  line_no             INTEGER,
  title               TEXT NOT NULL,
  description         TEXT,
  suggested_fix       TEXT,
  status              TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','dismissed','fixed','wontfix')),
  bug_report_id       INTEGER                        -- when forwarded to bug_reports
);

CREATE INDEX IF NOT EXISTS idx_pre_push_issues_review ON pre_push_review_issues(review_id);
CREATE INDEX IF NOT EXISTS idx_pre_push_issues_severity
  ON pre_push_review_issues(severity, status) WHERE status='open';

PRAGMA foreign_keys = ON;
