-- 0043_lane_domain_advantage.sql
-- review-36 #193 (HIGH): per-objective_domain routing was COMPUTED in
-- lib/lane_router.sh (groups keyed by objective_domain,task_class,node_type)
-- but PERSISTED into agent_performance_memory whose PRIMARY KEY is only
-- (agent_version_id, task_class). Different objective domains therefore
-- collided on the same storage row — the "shared brain learns per
-- objective_domain" guarantee held only at the slice-mean compute level,
-- never durably. The reviewer's suggested fix: store/read per-domain routing
-- from a dedicated table keyed including objective_domain.
--
-- Additive + idempotent (partial-apply safe, addresses review-36 #194's
-- discipline): CREATE TABLE/INDEX IF NOT EXISTS only — no rebuild of
-- agent_performance_memory, so the global slice + every existing reader is
-- untouched. agent_performance_memory stays the cross-domain (global) slice;
-- lane_domain_advantage is the per-domain slice the router prefers when it
-- clears the sample floor.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS lane_domain_advantage (
  agent_version_id   TEXT    NOT NULL,
  task_class         TEXT    NOT NULL,
  node_type          TEXT    NOT NULL DEFAULT '',
  objective_domain   TEXT    NOT NULL DEFAULT '',
  relative_advantage REAL    NOT NULL DEFAULT 0.0,
  runs_count         INTEGER NOT NULL DEFAULT 0,
  success_count      INTEGER NOT NULL DEFAULT 0,
  last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain)
);

CREATE INDEX IF NOT EXISTS idx_lane_domain_adv
  ON lane_domain_advantage(task_class, node_type, objective_domain, relative_advantage DESC);

PRAGMA foreign_keys = ON;
