-- mini-ork migration 0010 — benchmark infrastructure + execution traces
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0010_benchmarks.sql
--
-- Tables: execution_traces | benchmark_tasks | benchmark_results | workflow_candidates
-- Depends on: 0001_core.sql (runs), 0009_memory_namespaces.sql (workflow_memory, benchmark_memory)
BEGIN;

-- ── execution_traces ──────────────────────────────────────────────────────────
-- The raw trace for each agent turn — the primary input to the TextualGradient
-- learning engine. Captures full context: what was read, written, which tools
-- were called, what verifiers said, and the final artifact produced.
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id              TEXT    PRIMARY KEY,         -- UUID
  run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  workflow_version_id   TEXT    REFERENCES workflow_memory(workflow_version_id),
  agent_version_id      TEXT    NOT NULL DEFAULT '', -- references agent_performance_memory
  task_class            TEXT    NOT NULL,
  prompt_version_hash   TEXT    NOT NULL DEFAULT '', -- SHA of resolved prompt text
  context_bundle_hash   TEXT    NOT NULL DEFAULT '', -- SHA of full context sent to LLM
  tool_calls            TEXT    NOT NULL DEFAULT '[]', -- JSON array of {tool, args, result_excerpt}
  files_read            TEXT    NOT NULL DEFAULT '[]', -- JSON array of file paths
  files_written         TEXT    NOT NULL DEFAULT '[]', -- JSON array of {path, hash}
  verifier_output       TEXT    NOT NULL DEFAULT '{}', -- JSON map of verifier_name → {pass, evidence_path, score}
  reviewer_verdict      TEXT,                        -- 'APPROVE'|'REQUEST_CHANGES'|'ESCALATE'|null
  cost_usd              REAL    NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,                        -- artifact_id from artifact_memory; null if failed
  status                TEXT    NOT NULL
                        CHECK (status IN ('success','failure')),
  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_et_run_id              ON execution_traces(run_id);
CREATE INDEX IF NOT EXISTS idx_et_task_class          ON execution_traces(task_class);
CREATE INDEX IF NOT EXISTS idx_et_workflow_version    ON execution_traces(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_et_agent_version       ON execution_traces(agent_version_id);
CREATE INDEX IF NOT EXISTS idx_et_status              ON execution_traces(status);
CREATE INDEX IF NOT EXISTS idx_et_created_at          ON execution_traces(created_at);

-- ── benchmark_tasks ───────────────────────────────────────────────────────────
-- The ground-truth benchmark suite. Each row is one task the evolution engine
-- can run against any workflow candidate to produce a comparable utility score.
CREATE TABLE IF NOT EXISTS benchmark_tasks (
  benchmark_id          TEXT    PRIMARY KEY,         -- UUID or human slug, e.g. 'code_review_001'
  task_class            TEXT    NOT NULL,
  input_payload         TEXT    NOT NULL DEFAULT '{}', -- JSON: the task description + relevant context
  expected_artifact_hash TEXT   NOT NULL DEFAULT '', -- SHA-256 of the gold-standard artifact
  expected_criteria     TEXT    NOT NULL DEFAULT '{}', -- JSON: rubric for llm_judge verifiers
  success_verifiers     TEXT    NOT NULL DEFAULT '[]', -- JSON array of verifier_contract names
  baseline_utility_score REAL   NOT NULL DEFAULT 0.0,  -- score of the current promoted workflow
  source                TEXT    NOT NULL DEFAULT 'human'
                        CHECK (source IN ('human','synthetic')),
  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_bt_task_class          ON benchmark_tasks(task_class);
CREATE INDEX IF NOT EXISTS idx_bt_source              ON benchmark_tasks(source);
CREATE INDEX IF NOT EXISTS idx_bt_created_at          ON benchmark_tasks(created_at);

-- ── benchmark_results ─────────────────────────────────────────────────────────
-- Per-task per-candidate result. Each row records whether a specific candidate
-- workflow passed one benchmark task and at what utility score.
CREATE TABLE IF NOT EXISTS benchmark_results (
  result_id             TEXT    PRIMARY KEY,         -- UUID
  benchmark_id          TEXT    NOT NULL REFERENCES benchmark_tasks(benchmark_id) ON DELETE CASCADE,
  candidate_id          TEXT    NOT NULL,            -- FK → workflow_candidates (declared below)
  run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  pass                  INTEGER NOT NULL DEFAULT 0   CHECK (pass IN (0,1)), -- SQLite bool
  utility_score         REAL    NOT NULL DEFAULT 0.0,
  evidence_path         TEXT    NOT NULL DEFAULT '', -- path to verifier evidence file
  ran_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_br_benchmark_id        ON benchmark_results(benchmark_id);
CREATE INDEX IF NOT EXISTS idx_br_candidate_id        ON benchmark_results(candidate_id);
CREATE INDEX IF NOT EXISTS idx_br_run_id              ON benchmark_results(run_id);
CREATE INDEX IF NOT EXISTS idx_br_pass                ON benchmark_results(pass);
CREATE INDEX IF NOT EXISTS idx_br_ran_at              ON benchmark_results(ran_at);

-- ── workflow_candidates ───────────────────────────────────────────────────────
-- A candidate is a proposed mutation of an existing workflow version. It is
-- created by the evolution engine, shadow-deployed, benchmarked, and either
-- promoted to the registry or quarantined. The lifecycle mirrors the status
-- column in workflow_memory.
CREATE TABLE IF NOT EXISTS workflow_candidates (
  candidate_id              TEXT    PRIMARY KEY,     -- UUID
  base_workflow_version_id  TEXT    NOT NULL REFERENCES workflow_memory(workflow_version_id),
  mutations                 TEXT    NOT NULL DEFAULT '[]', -- JSON array of {kind, node_name, field, old_val, new_val}
  status                    TEXT    NOT NULL DEFAULT 'candidate'
                            CHECK (status IN ('candidate','shadow','promoted','quarantined','deprecated')),
  benchmark_summary_id      TEXT    REFERENCES benchmark_memory(summary_id),
  utility_delta             REAL    NOT NULL DEFAULT 0.0,  -- score vs baseline (positive = better)
  created_by                TEXT    NOT NULL DEFAULT 'evolution_engine'
                            CHECK (created_by IN ('evolution_engine','human')),
  created_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_wc_base_version        ON workflow_candidates(base_workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_wc_status              ON workflow_candidates(status);
CREATE INDEX IF NOT EXISTS idx_wc_created_by          ON workflow_candidates(created_by);
CREATE INDEX IF NOT EXISTS idx_wc_created_at          ON workflow_candidates(created_at);

-- Back-fill the FK that benchmark_results.candidate_id references.
-- SQLite doesn't enforce cross-migration FK ordering, but record it for clarity.
-- The application layer must insert into workflow_candidates BEFORE benchmark_results.

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0010_benchmarks.sql', strftime('%s','now'), 'phase-a-1');
