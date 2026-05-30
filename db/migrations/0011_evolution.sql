-- mini-ork migration 0011 — evolution engine: textual gradients, pattern records,
--                             promotion records, version registry pointers
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0011_evolution.sql
--
-- Depends on: 0009_memory_namespaces.sql (workflow_memory)
--             0010_benchmarks.sql (execution_traces, workflow_candidates, benchmark_memory)
BEGIN;

-- ── textual_gradients ─────────────────────────────────────────────────────────
-- A TextualGradient is a learning signal extracted from an execution trace.
-- It points at a specific part of a workflow (node, edge, or agent prompt) and
-- proposes a concrete change with a confidence score.
--
-- Analogy: in numeric optimisation, a gradient tells you "adjust weight W in
-- direction D by magnitude M". Here: "adjust workflow_node N's prompt/param
-- in direction D with evidence from trace T".
CREATE TABLE IF NOT EXISTS textual_gradients (
  gradient_id           TEXT    PRIMARY KEY,         -- UUID
  target_kind           TEXT    NOT NULL
                        CHECK (target_kind IN ('workflow_node','workflow_edge','agent_prompt')),
  target_name           TEXT    NOT NULL,            -- node name, edge "from→to", or prompt_ref key
  signal                TEXT    NOT NULL,            -- natural language description of what went wrong/right
  suggested_change      TEXT    NOT NULL,            -- concrete proposed modification (natural language or diff)
  evidence_trace_id     TEXT    REFERENCES execution_traces(trace_id) ON DELETE SET NULL,
  confidence            REAL    NOT NULL DEFAULT 0.5
                        CHECK (confidence >= 0.0 AND confidence <= 1.0),
  extracted_by          TEXT    NOT NULL DEFAULT 'default'
                        CHECK (extracted_by IN ('default','user','hook')),
  applied               INTEGER NOT NULL DEFAULT 0   CHECK (applied IN (0,1)), -- 1 if baked into a candidate
  applied_to_candidate  TEXT    REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tg_target_kind         ON textual_gradients(target_kind);
CREATE INDEX IF NOT EXISTS idx_tg_target_name         ON textual_gradients(target_name);
CREATE INDEX IF NOT EXISTS idx_tg_confidence          ON textual_gradients(confidence);
CREATE INDEX IF NOT EXISTS idx_tg_applied             ON textual_gradients(applied);
CREATE INDEX IF NOT EXISTS idx_tg_evidence_trace      ON textual_gradients(evidence_trace_id);
CREATE INDEX IF NOT EXISTS idx_tg_created_at          ON textual_gradients(created_at);

-- ── pattern_records ───────────────────────────────────────────────────────────
-- A PatternRecord aggregates multiple execution traces into a recurring pattern.
-- When frequency crosses a threshold the evolution engine promotes it to a
-- workflow mutation candidate or a best-practice rule in the ADR log.
CREATE TABLE IF NOT EXISTS pattern_records (
  pattern_id            TEXT    PRIMARY KEY,         -- UUID
  description           TEXT    NOT NULL,            -- human-readable summary of the pattern
  evidence_trace_ids    TEXT    NOT NULL DEFAULT '[]', -- JSON array of trace_id strings
  frequency             INTEGER NOT NULL DEFAULT 1,  -- how many distinct traces exhibit this pattern
  first_seen            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_seen             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  output_type           TEXT    NOT NULL
                        CHECK (output_type IN (
                          'adr',
                          'verifier_addition',
                          'workflow_change',
                          'prompt_change',
                          'best_practice_rule'
                        )),
  promoted_to           TEXT    REFERENCES workflow_candidates(candidate_id) ON DELETE SET NULL,
  status                TEXT    NOT NULL DEFAULT 'observed'
                        CHECK (status IN ('observed','promoted','dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_pr_output_type         ON pattern_records(output_type);
CREATE INDEX IF NOT EXISTS idx_pr_frequency           ON pattern_records(frequency);
CREATE INDEX IF NOT EXISTS idx_pr_status              ON pattern_records(status);
CREATE INDEX IF NOT EXISTS idx_pr_last_seen           ON pattern_records(last_seen);

-- ── promotion_records ─────────────────────────────────────────────────────────
-- Immutable audit trail for every promotion decision: what was promoted, from
-- which version, to which version, by what gate or human, and at what cost delta.
-- Every row here has a corresponding row in audit_log (created in 0012_safety.sql).
CREATE TABLE IF NOT EXISTS promotion_records (
  promotion_id          TEXT    PRIMARY KEY,         -- UUID
  candidate_id          TEXT    NOT NULL REFERENCES workflow_candidates(candidate_id),
  from_version_id       TEXT    NOT NULL REFERENCES workflow_memory(workflow_version_id),
  to_version_id         TEXT    NOT NULL REFERENCES workflow_memory(workflow_version_id),
  utility_before        REAL    NOT NULL DEFAULT 0.0,
  utility_after         REAL    NOT NULL DEFAULT 0.0,
  benchmark_run_id      TEXT    REFERENCES benchmark_memory(summary_id),
  rationale             TEXT    NOT NULL DEFAULT '', -- LLM-generated or human-written justification
  decision              TEXT    NOT NULL
                        CHECK (decision IN (
                          'promoted',
                          'quarantined',
                          'rejected',
                          'pending_human_approval'
                        )),
  decided_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_by            TEXT    NOT NULL
                        CHECK (decided_by IN ('gate','human'))
);

CREATE INDEX IF NOT EXISTS idx_pr_candidate_id        ON promotion_records(candidate_id);
CREATE INDEX IF NOT EXISTS idx_pr_decision            ON promotion_records(decision);
CREATE INDEX IF NOT EXISTS idx_pr_decided_at          ON promotion_records(decided_at);
CREATE INDEX IF NOT EXISTS idx_pr_decided_by          ON promotion_records(decided_by);

-- ── version_registry_pointers ─────────────────────────────────────────────────
-- Single-row per (kind, name) tracking the live current version and the last
-- known-good stable version. Supports instant rollback: flip current_version_id
-- back to previous_stable_version_id and emit a promotion_record row.
CREATE TABLE IF NOT EXISTS version_registry_pointers (
  kind                      TEXT    NOT NULL CHECK (kind IN ('workflow','agent')),
  name                      TEXT    NOT NULL,        -- workflow_name or agent role+model slug
  current_version_id        TEXT    NOT NULL,        -- FK → workflow_memory or agent_performance_memory
  previous_stable_version_id TEXT,                  -- null on first promotion
  updated_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (kind, name)
);

CREATE INDEX IF NOT EXISTS idx_vrp_kind               ON version_registry_pointers(kind);
CREATE INDEX IF NOT EXISTS idx_vrp_updated_at         ON version_registry_pointers(updated_at);

-- Trigger: keep updated_at current on pointer updates
CREATE TRIGGER IF NOT EXISTS trg_vrp_updated
AFTER UPDATE ON version_registry_pointers
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE version_registry_pointers
  SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
  WHERE kind = NEW.kind AND name = NEW.name;
END;

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0011_evolution.sql', strftime('%s','now'), 'phase-a-1');
