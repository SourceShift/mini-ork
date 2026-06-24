-- 0020: idea_tree_nodes — generic tree primitive borrowed from Arbor.
--
-- Per docs/plans/2026-06-11-arbor-techniques-into-mini-ork.md item 1, this is
-- the substrate for:
--   * Tree-of-hypotheses self-improvement (replaces linear iter retry)
--   * Recipe-creator drafter outputs as children of the planner's parent intent
--   * Per-node insight backpropagation (item 3 of the same plan)
--   * Tree visualization in the Trajectory UI page
--
-- Design notes:
--   * node_id is the primary key. We don't use rowid because nodes get
--     cross-referenced from task_runs / self_improve_runs by back-pointer.
--   * root_node_id is denormalized. Walking up from leaf to root is O(depth)
--     via recursive CTE, but root-scoped queries ("give me all nodes in
--     this iter's tree") are 10x more frequent — the denorm pays for itself.
--   * task_run_id + self_improve_run_id are nullable back-pointers. A node
--     that IS a task_run dispatch sets task_run_id; a node that IS a
--     self-improve iter sets self_improve_run_id; a synthetic root that
--     just frames the objective sets neither.
--   * score_dev / score_test are nullable. Held-out test discipline (item 2)
--     fills these in; legacy backfilled rows keep them NULL and the
--     promotion gate treats NULL as "not yet evaluated" (no merge).
--   * insights_json is the per-node lesson abstract. Item 3's
--     scope_node_id field on gradient_records will reference this PK.
--
-- backfill_idea_tree.py (companion script) materializes the existing
-- self_improve_runs history as tree nodes with chronological-linear
-- parenting per (day, recipe) cluster.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS idea_tree_nodes (
  node_id              TEXT PRIMARY KEY,
  parent_node_id       TEXT REFERENCES idea_tree_nodes(node_id) ON DELETE SET NULL,
  root_node_id         TEXT NOT NULL,                  -- denormalized root for fast subtree queries
  recipe               TEXT,                            -- which recipe authored this node; NULL for synthetic roots
  task_run_id          TEXT,                            -- back-ref: task_runs.id when this node IS a dispatch
  self_improve_run_id  TEXT,                            -- back-ref: self_improve_runs.run_id when this node IS an iter
  hypothesis           TEXT,                            -- short prose: what this node tests / proposes
  status               TEXT NOT NULL DEFAULT 'pending'
                          CHECK(status IN (
                              'pending',     -- proposed, not yet dispatched
                              'running',     -- executor in flight
                              'harvested',   -- result kept; merge threshold cleared
                              'pruned',      -- result rejected; this branch will not spawn children
                              'rejected'     -- proposed but blocked by gate (budget / scope / safety)
                          )),
  score_dev            REAL,                            -- metric on dev slice (NULL = not measured)
  score_test           REAL,                            -- metric on held-out test slice (NULL = not measured)
  insights_json        TEXT NOT NULL DEFAULT '[]',      -- per-node lessons; gradient_records.scope_node_id refs this PK
  created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  updated_at           INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Index on parent so subtree walks are O(children) per level.
CREATE INDEX IF NOT EXISTS idx_idea_tree_parent ON idea_tree_nodes(parent_node_id);

-- Index on root so "give me the whole tree" is one indexed scan.
CREATE INDEX IF NOT EXISTS idx_idea_tree_root ON idea_tree_nodes(root_node_id);

-- Partial indexes for the common back-pointer joins.
CREATE INDEX IF NOT EXISTS idx_idea_tree_task_run
    ON idea_tree_nodes(task_run_id)
    WHERE task_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_idea_tree_self_improve
    ON idea_tree_nodes(self_improve_run_id)
    WHERE self_improve_run_id IS NOT NULL;

-- Status filter for "what's pending right now" UI queries.
CREATE INDEX IF NOT EXISTS idx_idea_tree_status ON idea_tree_nodes(status);

PRAGMA foreign_keys = ON;
