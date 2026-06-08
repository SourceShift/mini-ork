-- 0017_self_improve_learning.sql — cross-iteration memory for the
-- recursive-self-improve recipe. Reuses MINI_ORK_DB so pattern_records
-- and benchmark_results join naturally.

CREATE TABLE IF NOT EXISTS self_improve_runs (
    run_id              TEXT PRIMARY KEY,
    started_at          INTEGER NOT NULL,
    finished_at         INTEGER,
    iter                INTEGER NOT NULL,
    worktree_path       TEXT,
    branch_name         TEXT,
    soft_deadline_at    INTEGER NOT NULL,   -- epoch seconds (3h)
    hard_deadline_at    INTEGER NOT NULL,   -- epoch seconds (5h)
    parent_run_id       TEXT,
    outcome             TEXT NOT NULL DEFAULT 'pending'
                            CHECK(outcome IN (
                                'pending','success','partial','rejected',
                                'failed','converged','timed_out','aborted'
                            )),
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_self_improve_runs_iter
    ON self_improve_runs(iter);
CREATE INDEX IF NOT EXISTS idx_self_improve_runs_outcome
    ON self_improve_runs(outcome);

-- One row per ranked bottleneck the planner emitted; persists even when
-- the iteration fails so the next iteration can dedupe / build on it.
CREATE TABLE IF NOT EXISTS learning_record (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    iter                INTEGER NOT NULL,
    rank                INTEGER NOT NULL,
    category            TEXT NOT NULL
                            CHECK(category IN ('perf','correctness','arch','meta')),
    title               TEXT NOT NULL,
    evidence_paths      TEXT NOT NULL DEFAULT '[]',  -- JSON array
    arxiv_refs          TEXT NOT NULL DEFAULT '[]',  -- JSON array of arxiv ids
    patch_summary       TEXT,
    outcome             TEXT NOT NULL DEFAULT 'open'
                            CHECK(outcome IN (
                                'open','queued','attempted','resolved',
                                'rejected','deferred','superseded'
                            )),
    severity            TEXT NOT NULL DEFAULT 'medium'
                            CHECK(severity IN ('low','medium','high','critical')),
    confidence          REAL NOT NULL DEFAULT 0.0,
    benchmark_delta     REAL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    FOREIGN KEY(run_id) REFERENCES self_improve_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_learning_record_category
    ON learning_record(category);
CREATE INDEX IF NOT EXISTS idx_learning_record_outcome
    ON learning_record(outcome);
CREATE INDEX IF NOT EXISTS idx_learning_record_run
    ON learning_record(run_id);

-- Many-to-many between iterations and the arxiv papers they cited. Lets a
-- future cross-iteration analyzer ask "which papers actually correlated
-- with resolved bottlenecks?".
CREATE TABLE IF NOT EXISTS self_improve_arxiv_refs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    arxiv_id            TEXT NOT NULL,
    title               TEXT,
    technique           TEXT,
    mapped_file         TEXT,
    confidence          REAL NOT NULL DEFAULT 0.0,
    used_in_patch       INTEGER NOT NULL DEFAULT 0,  -- 0/1
    created_at          INTEGER NOT NULL,
    UNIQUE(run_id, arxiv_id)
);

CREATE INDEX IF NOT EXISTS idx_self_improve_arxiv_used
    ON self_improve_arxiv_refs(used_in_patch);
