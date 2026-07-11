-- mini-ork migration 0048 — apply_attempts audit trail (IMPL-3, close apply loop)
--
-- Every call to `bin/mini-ork apply` writes one row here, regardless of outcome.
-- Captures the source pattern (the cause) and the resulting candidate decision
-- (the effect). promotion_records only knows the synthesized candidate_id, so
-- this table is the causal provenance: "which pattern_records / emergent_patterns
-- / gradient_records row drove this promotion decision?".
--
-- Depends on: 0011_evolution.sql (workflow_candidates, promotion_records,
--                                  pattern_records, gradient_records)
--             0008_reflection_basins.sql (emergent_patterns)
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0048_apply_attempts.sql

BEGIN;

CREATE TABLE IF NOT EXISTS apply_attempts (
    attempt_id              TEXT    PRIMARY KEY,                -- UUID
    task_class              TEXT    NOT NULL,                   -- e.g. 'reviewer', 'framework_edit'
    target_kind             TEXT    NOT NULL
                            CHECK (target_kind IN ('workflow_node','workflow_edge','agent_prompt','prompt_file')),
    target_name             TEXT    NOT NULL,                   -- node name, edge "from→to", prompt_ref key, or prompt file path
    source_kind             TEXT    NOT NULL
                            CHECK (source_kind IN ('pattern_records','emergent_patterns','gradient_records','synthesis_gate_verdict','none')),
    source_id               TEXT,                               -- pattern_id / emergent_pattern_id / gradient_id (NULL for synthesis_gate raw input)
    candidate_id            TEXT    REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
    promotion_id            TEXT    REFERENCES promotion_records(promotion_id) ON DELETE SET NULL,
    base_workflow_version_id TEXT,                               -- snapshot of workflow version the candidate mutated from
    utility_before          REAL,                               -- version_registry baseline utility_score
    utility_after           REAL,                               -- benchmark_results avg over the candidate
    utility_delta           REAL,                               -- after - before (positive = improvement, negative = regression)
    decision                TEXT    NOT NULL
                            CHECK (decision IN ('promoted','quarantined','rejected','pending_human_approval','dry_run','no_candidate')),
    rationale               TEXT    NOT NULL DEFAULT '',
    dry_run                 INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
    apply_enabled           INTEGER NOT NULL DEFAULT 0 CHECK (apply_enabled IN (0,1)),
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_apply_attempts_task_class     ON apply_attempts(task_class);
CREATE INDEX IF NOT EXISTS idx_apply_attempts_target         ON apply_attempts(target_kind, target_name);
CREATE INDEX IF NOT EXISTS idx_apply_attempts_source         ON apply_attempts(source_kind, source_id);
CREATE INDEX IF NOT EXISTS idx_apply_attempts_decision       ON apply_attempts(decision);
CREATE INDEX IF NOT EXISTS idx_apply_attempts_created_at     ON apply_attempts(created_at);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0048_apply_attempts.sql', strftime('%s','now'), 'impl-3-apply-loop');
