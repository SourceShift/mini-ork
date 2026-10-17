-- 0079_code_findings_landing.sql — stop offering a finding whose run landed.
--
-- LF-R11b. R11a decays findings by age and by whether the quoted code still
-- exists at HEAD. That misses the common case where the run's own change
-- LANDED on origin/main but the finding points at a line the change did not
-- touch: the snippet is still there, so the row looks live, yet the run it came
-- from is already merged and its reviewers' complaints answered or superseded.
--
-- The identity is git's own: ``git patch-id --stable`` is invariant to rebase,
-- cherry-pick and line drift, so a run's ``framework-edit.diff`` and the commit
-- that merged it share an id even after the merge rebased the branch. One row
-- per harvested run records the outcome of the check:
--
--   run_id        TEXT PRIMARY KEY   the harvested run that was checked
--   patch_id      TEXT NOT NULL      git patch-id of the run's diff
--   checked_ref   TEXT NOT NULL      the ref that was searched ("origin/main")
--   landed_commit TEXT               the commit carrying the patch-id, or NULL.
--                                    NULL is load-bearing: "checked and not
--                                    landed" must stay distinguishable from
--                                    "never checked" (the row's absence).
--   checked_at    INTEGER NOT NULL   epoch seconds, stamped by the writer
--
-- A run that shipped several commits has a union diff whose patch-id no single
-- commit carries, so it never matches and stays not-landed. The check fails
-- open: a missed landing keeps a finding, never hides one wrongly.
--
-- The defaults on patch_id/checked_ref are for hand-written rows and the test
-- fixtures; the writer always supplies both. ADDITIVE only: no existing table
-- or column is touched. ``code_findings.ensure_schema`` runs the same DDL at
-- runtime (the 0073/0078 precedent), so a state.db that never runs this
-- migration still works -- the two texts must not drift apart.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS code_findings_landing (
    run_id        TEXT PRIMARY KEY,
    patch_id      TEXT NOT NULL DEFAULT '',
    checked_ref   TEXT NOT NULL DEFAULT 'origin/main',
    landed_commit TEXT,
    checked_at    INTEGER NOT NULL
);

-- Hot read: "which commit carried this run's diff?"
CREATE INDEX IF NOT EXISTS idx_code_findings_landing_patch
    ON code_findings_landing(patch_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0079_code_findings_landing.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'code-findings-landing-v1');
