-- 0054: preserve planner profile-gate lifecycle traces.
--
-- bin/mini-ork-plan has emitted status='blocked' since the profile gate was
-- introduced, but the execution_traces CHECK constraint rejected that value.
-- The caller intentionally treats tracing as best-effort, so the rejection was
-- silent and blocked plans appeared to have no terminal trace.  The Python-only
-- planner keeps the same status contract; widen the canonical schema so both
-- historical and migrated runtimes can persist it honestly.

PRAGMA foreign_keys=off;

CREATE TABLE execution_traces_new (
  trace_id              TEXT    PRIMARY KEY,
  run_id                INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  workflow_version_id   TEXT    REFERENCES workflow_memory(workflow_version_id),
  agent_version_id      TEXT    NOT NULL DEFAULT '',
  task_class            TEXT    NOT NULL,
  prompt_version_hash   TEXT    NOT NULL DEFAULT '',
  context_bundle_hash   TEXT    NOT NULL DEFAULT '',
  tool_calls            TEXT    NOT NULL DEFAULT '[]',
  files_read            TEXT    NOT NULL DEFAULT '[]',
  files_written         TEXT    NOT NULL DEFAULT '[]',
  verifier_output       TEXT    NOT NULL DEFAULT '{}',
  reviewer_verdict      TEXT,
  cost_usd              REAL    NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,
  status                TEXT    NOT NULL
                        CHECK (status IN ('success','failure','pending','running','vacuous','blocked')),
  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  process_reward        REAL    DEFAULT NULL,
  objective_domain      TEXT    NOT NULL DEFAULT 'code-delivery',
  segment               TEXT    DEFAULT NULL,
  reward_primary_metric TEXT    DEFAULT NULL,
  reward_direction      TEXT    NOT NULL DEFAULT 'higher_is_better',
  reward_value          REAL    DEFAULT NULL,
  reward_anchor         REAL    DEFAULT NULL,
  reward_g              REAL    DEFAULT NULL,
  reward_vector_json    TEXT    DEFAULT NULL,
  reward_source         TEXT    NOT NULL DEFAULT 'verifier@v1',
  validity              TEXT    NOT NULL DEFAULT 'valid',
  code_region           TEXT    DEFAULT NULL,
  route_source          TEXT    DEFAULT NULL,
  route_explore         INTEGER DEFAULT NULL,
  route_score           REAL    DEFAULT NULL
);

INSERT INTO execution_traces_new (
  trace_id, run_id, workflow_version_id, agent_version_id, task_class,
  prompt_version_hash, context_bundle_hash, tool_calls, files_read,
  files_written, verifier_output, reviewer_verdict, cost_usd, duration_ms,
  final_artifact_ref, status, created_at, process_reward, objective_domain,
  segment, reward_primary_metric, reward_direction, reward_value,
  reward_anchor, reward_g, reward_vector_json, reward_source, validity,
  code_region, route_source, route_explore, route_score
)
SELECT
  trace_id, run_id, workflow_version_id, agent_version_id, task_class,
  prompt_version_hash, context_bundle_hash, tool_calls, files_read,
  files_written, verifier_output, reviewer_verdict, cost_usd, duration_ms,
  final_artifact_ref, status, created_at, process_reward, objective_domain,
  segment, reward_primary_metric, reward_direction, reward_value,
  reward_anchor, reward_g, reward_vector_json, reward_source, validity,
  code_region, route_source, route_explore, route_score
FROM execution_traces;

DROP TABLE execution_traces;
ALTER TABLE execution_traces_new RENAME TO execution_traces;

CREATE INDEX idx_et_run_id_v54 ON execution_traces(run_id);
CREATE INDEX idx_et_task_class_v54 ON execution_traces(task_class);
CREATE INDEX idx_et_workflow_version_v54 ON execution_traces(workflow_version_id);
CREATE INDEX idx_et_agent_version_v54 ON execution_traces(agent_version_id);
CREATE INDEX idx_et_status_v54 ON execution_traces(status);
CREATE INDEX idx_et_created_v54 ON execution_traces(created_at DESC);
CREATE INDEX idx_execution_traces_process_reward_v54
  ON execution_traces(process_reward) WHERE process_reward IS NOT NULL;
CREATE INDEX idx_et_objective_domain_v54 ON execution_traces(objective_domain);
CREATE INDEX idx_et_objective_segment_v54
  ON execution_traces(objective_domain, segment) WHERE segment IS NOT NULL;
CREATE INDEX idx_et_reward_g_v54 ON execution_traces(reward_g) WHERE reward_g IS NOT NULL;
CREATE INDEX idx_et_reward_source_v54 ON execution_traces(reward_source);
CREATE INDEX idx_et_validity_v54
  ON execution_traces(validity) WHERE validity != 'valid';
CREATE INDEX idx_et_code_region_v54
  ON execution_traces(code_region) WHERE code_region IS NOT NULL;
CREATE INDEX idx_et_route_source_v54
  ON execution_traces(route_source) WHERE route_source IS NOT NULL;

PRAGMA foreign_keys=on;
