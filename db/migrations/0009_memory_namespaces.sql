-- mini-ork migration 0009 — 8 memory namespace tables
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0009_memory_namespaces.sql
--
-- Namespaces: task | workflow | agent_performance | failure | recovery |
--             user_preference | artifact | benchmark (summary view)
BEGIN;

-- ── task_memory ───────────────────────────────────────────────────────────────
-- Per-run outcome history. One row per completed run; drives learning signals.
CREATE TABLE IF NOT EXISTS task_memory (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  task_class          TEXT    NOT NULL,
  kickoff_hash        TEXT    NOT NULL,              -- SHA of kickoff document content
  outcome             TEXT    NOT NULL
                      CHECK (outcome IN ('success','failure','partial')),
  artifacts_produced  TEXT    NOT NULL DEFAULT '[]', -- JSON array of artifact_id refs
  duration_ms         INTEGER NOT NULL DEFAULT 0,
  cost_usd            REAL    NOT NULL DEFAULT 0.0,
  created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_task_memory_run        ON task_memory(run_id);
CREATE INDEX IF NOT EXISTS idx_task_memory_class      ON task_memory(task_class);
CREATE INDEX IF NOT EXISTS idx_task_memory_outcome    ON task_memory(outcome);
CREATE INDEX IF NOT EXISTS idx_task_memory_created_at ON task_memory(created_at);

-- ── workflow_memory ───────────────────────────────────────────────────────────
-- Workflow version history including mutation lineage. Mirrors workflow YAML as
-- canonical TEXT blob; mutations column records the delta from base_version.
CREATE TABLE IF NOT EXISTS workflow_memory (
  workflow_version_id       TEXT    PRIMARY KEY,     -- e.g. 'code_review_v3'
  workflow_name             TEXT    NOT NULL,
  base_version_id           TEXT    REFERENCES workflow_memory(workflow_version_id),
  yaml_hash                 TEXT    NOT NULL,        -- SHA-256 of yaml_blob
  yaml_blob                 TEXT    NOT NULL,        -- full YAML text
  mutations                 TEXT    NOT NULL DEFAULT '[]', -- JSON array of {kind, path, old, new}
  created_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  status                    TEXT    NOT NULL DEFAULT 'candidate'
                            CHECK (status IN ('candidate','shadow','promoted','quarantined','deprecated')),
  previous_stable_version_id TEXT   REFERENCES workflow_memory(workflow_version_id)
);

CREATE INDEX IF NOT EXISTS idx_wf_memory_name        ON workflow_memory(workflow_name);
CREATE INDEX IF NOT EXISTS idx_wf_memory_status      ON workflow_memory(status);
CREATE INDEX IF NOT EXISTS idx_wf_memory_created_at  ON workflow_memory(created_at);

-- ── agent_performance_memory ──────────────────────────────────────────────────
-- Aggregated per-agent-version stats per task class. Updated by the evolution
-- engine after each run; used by the brain advisor for routing decisions.
CREATE TABLE IF NOT EXISTS agent_performance_memory (
  agent_version_id    TEXT    NOT NULL,
  role                TEXT    NOT NULL,
  model               TEXT    NOT NULL,
  task_class          TEXT    NOT NULL,
  runs_count          INTEGER NOT NULL DEFAULT 0,
  success_count       INTEGER NOT NULL DEFAULT 0,
  avg_cost_usd        REAL    NOT NULL DEFAULT 0.0,
  avg_duration_ms     REAL    NOT NULL DEFAULT 0.0,
  top_failure_modes   TEXT    NOT NULL DEFAULT '[]', -- JSON array of {mode, count}
  last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (agent_version_id, task_class)
);

CREATE INDEX IF NOT EXISTS idx_agent_perf_role       ON agent_performance_memory(role);
CREATE INDEX IF NOT EXISTS idx_agent_perf_model      ON agent_performance_memory(model);
CREATE INDEX IF NOT EXISTS idx_agent_perf_updated    ON agent_performance_memory(last_updated);

-- ── failure_memory ────────────────────────────────────────────────────────────
-- One row per distinct failure event, linked to the run that produced it.
-- Enables pattern detection across task classes and workflow stages.
CREATE TABLE IF NOT EXISTS failure_memory (
  failure_id          TEXT    PRIMARY KEY,           -- UUID
  run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  workflow_stage      TEXT    NOT NULL,              -- node name that failed
  failure_category    TEXT    NOT NULL,              -- e.g. 'verifier_fail','timeout','cost_overrun'
  error_message       TEXT    NOT NULL DEFAULT '',
  stack_trace         TEXT    NOT NULL DEFAULT '',
  occurred_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_failure_run_id        ON failure_memory(run_id);
CREATE INDEX IF NOT EXISTS idx_failure_category      ON failure_memory(failure_category);
CREATE INDEX IF NOT EXISTS idx_failure_stage         ON failure_memory(workflow_stage);
CREATE INDEX IF NOT EXISTS idx_failure_occurred_at   ON failure_memory(occurred_at);

-- ── recovery_memory ───────────────────────────────────────────────────────────
-- Records what intervention was taken after a failure and whether it worked.
-- Drives the recovery suggestion engine in the reflector node.
CREATE TABLE IF NOT EXISTS recovery_memory (
  recovery_id         TEXT    PRIMARY KEY,           -- UUID
  failure_id          TEXT    NOT NULL REFERENCES failure_memory(failure_id) ON DELETE CASCADE,
  recovery_action     TEXT    NOT NULL,              -- e.g. 'retry_same_agent','switch_agent','human_fix'
  agent_used          TEXT    NOT NULL DEFAULT '',   -- agent_version_id or 'human'
  outcome             TEXT    NOT NULL
                      CHECK (outcome IN ('resolved','escalated','abandoned')),
  cost_usd            REAL    NOT NULL DEFAULT 0.0,
  recovered_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_recovery_failure_id   ON recovery_memory(failure_id);
CREATE INDEX IF NOT EXISTS idx_recovery_outcome      ON recovery_memory(outcome);
CREATE INDEX IF NOT EXISTS idx_recovery_action       ON recovery_memory(recovery_action);

-- ── user_preference_memory ────────────────────────────────────────────────────
-- Per-user preference overrides. Scoped to global, a task_class, or a workflow.
-- The preference_key is a dot-path into the relevant config, e.g.
--   'budget.cap_usd', 'model_lane.implementer', 'gates.skip_reviewer'.
CREATE TABLE IF NOT EXISTS user_preference_memory (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}', -- JSON scalar or object
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow')),
  -- scope_target is '' (empty string) for global scope, never NULL,
  -- so the composite PK is safe to use in SQLite without expression tricks.
  scope_target        TEXT    NOT NULL DEFAULT '',   -- task_class name or workflow_version_id
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);

CREATE INDEX IF NOT EXISTS idx_user_pref_user_id     ON user_preference_memory(user_id);
CREATE INDEX IF NOT EXISTS idx_user_pref_key         ON user_preference_memory(preference_key);
CREATE INDEX IF NOT EXISTS idx_user_pref_scope       ON user_preference_memory(scope);

-- ── artifact_memory ───────────────────────────────────────────────────────────
-- Provenance record for every artifact emitted by a run. Pairs with artifact_contract
-- to enforce retention and verify expected outputs were produced.
CREATE TABLE IF NOT EXISTS artifact_memory (
  artifact_id         TEXT    PRIMARY KEY,           -- UUID
  run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  artifact_path       TEXT    NOT NULL,
  artifact_hash       TEXT    NOT NULL,              -- SHA-256 hex
  artifact_type       TEXT    NOT NULL,              -- matches artifact_contract.expected_artifact enum
  produced_by_agent   TEXT    NOT NULL DEFAULT '',   -- agent_version_id
  produced_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retained_until      TEXT                           -- ISO date; null = retain indefinitely
);

CREATE INDEX IF NOT EXISTS idx_artifact_run_id       ON artifact_memory(run_id);
CREATE INDEX IF NOT EXISTS idx_artifact_type         ON artifact_memory(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifact_hash         ON artifact_memory(artifact_hash);
CREATE INDEX IF NOT EXISTS idx_artifact_produced_at  ON artifact_memory(produced_at);
CREATE INDEX IF NOT EXISTS idx_artifact_retained     ON artifact_memory(retained_until)
  WHERE retained_until IS NOT NULL;

-- ── benchmark_memory ──────────────────────────────────────────────────────────
-- Summary-level benchmark results per candidate per benchmark suite.
-- This is the SUMMARY view stored for the evolution engine; per-task detail
-- rows live in benchmark_results (created in 0010_benchmarks.sql).
CREATE TABLE IF NOT EXISTS benchmark_memory (
  summary_id          TEXT    PRIMARY KEY,           -- UUID
  candidate_id        TEXT    NOT NULL,              -- FK → workflow_candidates (created in 0010)
  benchmark_suite_id  TEXT    NOT NULL,              -- human-readable suite name, e.g. 'code_review_suite_v1'
  total_tasks         INTEGER NOT NULL DEFAULT 0,
  passed_tasks        INTEGER NOT NULL DEFAULT 0,
  failed_tasks        INTEGER NOT NULL DEFAULT 0,
  aggregate_utility   REAL    NOT NULL DEFAULT 0.0,  -- weighted sum of utility_score across tasks
  ran_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_bm_memory_candidate   ON benchmark_memory(candidate_id);
CREATE INDEX IF NOT EXISTS idx_bm_memory_suite       ON benchmark_memory(benchmark_suite_id);
CREATE INDEX IF NOT EXISTS idx_bm_memory_ran_at      ON benchmark_memory(ran_at);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0009_memory_namespaces.sql', strftime('%s','now'), 'phase-a-1');
