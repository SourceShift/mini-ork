-- 0064_code_findings.sql
-- learning-memory E1: code findings — what reviews/verifiers said per file.
-- Kickoff: kickoffs/auto/eng-code-findings.md,
-- plan: docs/plans/2026-10-07-learning-memory-page-refactor.md.
--
-- Two tables behind the engineer-first "Your code" tab:
--
--   code_findings       -- one row per reviewer/verifier finding, deduped by
--                          fingerprint = sha256(run_id|source|file|line|issue).
--                          `source` is `review:<role>` or `verifier:<name>`.
--   code_findings_runs  -- one row per harvested run dir; the incremental
--                          guard so `harvest` skips runs it has already seen.
--
-- Both are ADDITIVE. Nothing reads them yet — the IDE-exposure phase wires the
-- routes; the harvester (mini_ork/learning/code_findings.py) writes them.
--
-- Concurrency: same single-writer model as the rest of the control-plane DB.
-- The harvester opens with PRAGMA busy_timeout=5000, mirroring
-- mini_ork/learning/ledger.py.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS code_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,    -- review:<role> | verifier:<name>
    file         TEXT,
    line         INTEGER,
    severity     TEXT    NOT NULL,    -- high | medium | low
    category     TEXT    NOT NULL,
    issue        TEXT    NOT NULL,
    snippet      TEXT,
    verdict      TEXT,
    ts           INTEGER NOT NULL
);

-- Hot read: "what did we learn about this file?"
CREATE INDEX IF NOT EXISTS idx_code_findings_file     ON code_findings(file);

-- Hot read: "what did this run's reviewers say?"
CREATE INDEX IF NOT EXISTS idx_code_findings_run_id   ON code_findings(run_id);

-- Hot read: category rollups for the area view.
CREATE INDEX IF NOT EXISTS idx_code_findings_category ON code_findings(category);

CREATE TABLE IF NOT EXISTS code_findings_runs (
    run_id       TEXT PRIMARY KEY,
    harvested_at INTEGER NOT NULL,
    n            INTEGER NOT NULL
);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0064_code_findings.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'code-findings-v1');
