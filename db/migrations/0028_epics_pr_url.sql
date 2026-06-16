-- 0028_epics_pr_url.sql
-- E3: Track which PR each epic produced, so the auto-merge gate (E8) and
-- the scheduler (E4) can poll PR status without scraping report files.

PRAGMA foreign_keys = OFF;

ALTER TABLE epics ADD COLUMN pr_url TEXT;
ALTER TABLE epics ADD COLUMN branch  TEXT;

CREATE INDEX IF NOT EXISTS idx_epics_pr_url
  ON epics(pr_url) WHERE pr_url IS NOT NULL;

PRAGMA foreign_keys = ON;
