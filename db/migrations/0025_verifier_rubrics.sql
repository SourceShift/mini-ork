-- 0025_verifier_rubrics.sql — verifier rubrics + ground-truth feedback chain.
--
-- Implements roadmap Phase 3 item 9: verifier rubrics with ground-truth
-- feedback (LobeHub deep-review). Adds three correlated tables that
-- give operators a structured way to score verifier outputs against
-- known-good (or known-bad) examples and to chain auto-repair runs
-- from a failed verifier dispatch.
--
-- Tables:
--   verifier_rubrics  - reusable scoring criteria (rubric_id + axes)
--   verifier_results  - one row per verifier dispatch with verdict +
--                       confidence + operator-set is_false_positive /
--                       is_false_negative flags + optional repair_run_id
--                       linking the auto-repair run that mini-ork
--                       dispatched in response to a failure
--   verifier_criteria - axis dimensions for the rubric (one rubric to
--                       many criteria)
--
-- Why this exists: the learning_record loop already exists in
-- migration 0017 but currently has no ground-truth feedback channel.
-- Operators reviewing audit findings have no place to record
-- "this was actually a false positive" — that signal is lost. The
-- new schema gives them a typed column to set, the self-improve
-- runner reads it, and verifier prompts evolve away from the
-- patterns that produce false positives.

CREATE TABLE IF NOT EXISTS verifier_rubrics (
    rubric_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT,
    task_class       TEXT,
    -- Free-form JSON axes definition. Pinned by verifier_criteria
    -- relational rows when the operator wants typed grading.
    axes_json        TEXT,
    created_at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at       INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    is_active        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_verifier_rubrics_task_class
    ON verifier_rubrics(task_class)
    WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS verifier_criteria (
    criterion_id     TEXT PRIMARY KEY,
    rubric_id        TEXT NOT NULL REFERENCES verifier_rubrics(rubric_id),
    axis_name        TEXT NOT NULL,
    -- Scale guidance: e.g. "0-3 integer (0=fail, 3=excellent)" or
    -- "0-1 float (probability)". Free-text — operators describe.
    scale_hint       TEXT,
    weight           REAL NOT NULL DEFAULT 1.0,
    sort_order       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(rubric_id, axis_name)
);

CREATE INDEX IF NOT EXISTS idx_verifier_criteria_rubric
    ON verifier_criteria(rubric_id);

CREATE TABLE IF NOT EXISTS verifier_results (
    result_id            TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    verifier_name        TEXT NOT NULL,
    rubric_id            TEXT REFERENCES verifier_rubrics(rubric_id),
    verdict              TEXT NOT NULL
                            CHECK(verdict IN ('pass', 'fail', 'indeterminate', 'vacuous')),
    confidence           REAL,
    scored_axes_json     TEXT,
    -- Ground-truth feedback columns. Operators set these AFTER the
    -- fact, typically via a UI or `mini-ork verifier annotate`. They
    -- mutually exclusive — a real false positive cannot also be a
    -- real false negative on the same finding.
    is_false_positive    INTEGER NOT NULL DEFAULT 0
                            CHECK(is_false_positive IN (0, 1)),
    is_false_negative    INTEGER NOT NULL DEFAULT 0
                            CHECK(is_false_negative IN (0, 1)),
    annotated_by         TEXT,
    annotated_at         INTEGER,
    -- Chain to an auto-repair run that mini-ork dispatched in response
    -- to this failure. Lets the self-improve loop count "verifier X
    -- caused N repair runs, M succeeded" as a quality signal.
    repair_run_id        TEXT,
    notes                TEXT,
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    CHECK(NOT (is_false_positive = 1 AND is_false_negative = 1))
);

CREATE INDEX IF NOT EXISTS idx_verifier_results_run
    ON verifier_results(run_id);

CREATE INDEX IF NOT EXISTS idx_verifier_results_repair
    ON verifier_results(repair_run_id)
    WHERE repair_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_verifier_results_groundtruth
    ON verifier_results(verifier_name)
    WHERE is_false_positive = 1 OR is_false_negative = 1;
