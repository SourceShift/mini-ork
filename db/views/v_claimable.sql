-- v_claimable: epics eligible to be claimed by a worker agent.
-- An epic is claimable when:
--   1. status = 'not started' and not archived
--   2. All dependency epics are 'done' (dep_state = 'satisfied')
--   3. dep_state is returned as a string so callers can display blocker IDs
--   4. open_inbox_count > 0 means a human blocker exists — callers should filter on this
--
-- Usage: SELECT * FROM v_claimable WHERE dep_state = 'satisfied' AND open_inbox_count = 0;
CREATE VIEW IF NOT EXISTS v_claimable AS
SELECT
  e.id,
  e.title,
  e.status,
  e.worker_default,
  e.lane,
  e.kickoff_path,
  CASE
    WHEN NOT EXISTS (
      SELECT 1 FROM deps d
      JOIN epics de ON de.id = d.depends_on
      WHERE d.epic_id = e.id AND de.status != 'done'
    ) THEN 'satisfied'
    ELSE 'blocked-by:' || (
      SELECT GROUP_CONCAT(d.depends_on, ',')
      FROM deps d
      JOIN epics de ON de.id = d.depends_on
      WHERE d.epic_id = e.id AND de.status != 'done'
    )
  END AS dep_state,
  (SELECT COUNT(*) FROM inbox i WHERE i.epic_id = e.id AND i.resolved_at IS NULL) AS open_inbox_count
FROM epics e
WHERE e.archived_at IS NULL
  AND e.status = 'not started';
