-- 0015_panel_topology_telemetry.sql — E-MO-01 of the multi-agent orchestrator
-- epic catalogue (.agentflow/mini-orch/handoffs/20260602-2100-...md).
--
-- Stores realised (ρ, C, I) per panel run.
--
-- ρ — output correlation       — Rajan 2025, Nasser 2026 — DO agents agree?
-- C — context formation dist   — Stable LLM Ensemble 2025, AuthTrace 2026,
--                                 Diverse Interpretations 2025 — HOW did agents
--                                 build their working evidence?
-- I — inductive prior dist     — SYMPHONY 2026, DEI 2026 — WHAT priors did
--                                 each agent bring?
--
-- Source: docs/_meta/research/20260602-2030-context-formation-diversity-
--         framework-multi-agent-panels.md

CREATE TABLE IF NOT EXISTS panel_topology_telemetry (
  telemetry_id      TEXT    PRIMARY KEY,            -- 'pt-<panel_run_id>-<short_uuid>'
  panel_run_id      TEXT    NOT NULL,                -- groups traces of one panel cycle
                                                     -- (mini_ork_run_id for now)
  recipe            TEXT    NOT NULL,                -- task_class from execution_traces

  -- Realised metrics — [0.0, 1.0] except rho which can be negative
  rho               REAL    NOT NULL DEFAULT 0.0,
                            -- output correlation; 1.0 = perfect agreement,
                            -- 0.0 = independent, -1.0 = anti-correlated
  context_distance  REAL    NOT NULL DEFAULT 0.0,
                            -- 1 - mean_pairwise_jaccard(file_read ∪ tool_calls)
                            -- 1.0 = each agent saw entirely distinct context
                            -- 0.0 = all agents saw identical context (coalition along C)
  inductive_distance REAL   NOT NULL DEFAULT 0.0,
                            -- 1 - same_family_fraction(agents)
                            -- 1.0 = every agent a distinct family
                            -- 0.0 = all agents same family (coalition along I)

  -- Panel shape
  agent_count       INTEGER NOT NULL DEFAULT 0,
  n_traces          INTEGER NOT NULL DEFAULT 0,      -- traces contributing to metrics

  -- Target topology (from recipe panel_topology: block, E-MO-03)
  -- Stored as JSON: {"target_rho":"low","target_C":"high","target_I":"high"}
  -- Null when recipe doesn't declare a target.
  target_topology   TEXT,

  -- Quadrant classification (auto-computed from rho/C/I + thresholds)
  -- One of: coalition | noise | convergent_corroboration |
  --         genuine_perspective_split | forced_consensus_shared_evidence |
  --         prior_driven_disagreement | submodular_gain_target |
  --         high_variance_discovery
  quadrant          TEXT,

  computed_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_ptt_recipe       ON panel_topology_telemetry(recipe);
CREATE INDEX IF NOT EXISTS idx_ptt_panel_run    ON panel_topology_telemetry(panel_run_id);
CREATE INDEX IF NOT EXISTS idx_ptt_quadrant     ON panel_topology_telemetry(quadrant);
CREATE INDEX IF NOT EXISTS idx_ptt_computed_at  ON panel_topology_telemetry(computed_at);
