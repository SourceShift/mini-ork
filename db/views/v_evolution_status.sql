-- v_evolution_status: count of workflow candidates by status, sliced by time window.
-- Gives the evolution engine and `mini-ork doctor` a quick snapshot of the
-- current candidate pipeline health:
--   - how many are waiting (candidate)
--   - how many are in shadow testing (shadow)
--   - how many have been promoted, quarantined, or deprecated
-- Time windows: last 24h | last 7d | last 30d | all time
--
-- Usage:
--   SELECT * FROM v_evolution_status;
--   SELECT * FROM v_evolution_status WHERE window = '7d';
CREATE VIEW IF NOT EXISTS v_evolution_status AS

-- All-time totals per status
SELECT
  'all'                                         AS window,
  wc.status,
  COUNT(*)                                      AS candidate_count,
  ROUND(AVG(wc.utility_delta), 4)               AS avg_utility_delta,
  MAX(wc.created_at)                            AS most_recent
FROM workflow_candidates wc
GROUP BY wc.status

UNION ALL

-- Last 24 hours
SELECT
  '24h',
  wc.status,
  COUNT(*),
  ROUND(AVG(wc.utility_delta), 4),
  MAX(wc.created_at)
FROM workflow_candidates wc
WHERE wc.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')
GROUP BY wc.status

UNION ALL

-- Last 7 days
SELECT
  '7d',
  wc.status,
  COUNT(*),
  ROUND(AVG(wc.utility_delta), 4),
  MAX(wc.created_at)
FROM workflow_candidates wc
WHERE wc.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days')
GROUP BY wc.status

UNION ALL

-- Last 30 days
SELECT
  '30d',
  wc.status,
  COUNT(*),
  ROUND(AVG(wc.utility_delta), 4),
  MAX(wc.created_at)
FROM workflow_candidates wc
WHERE wc.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')
GROUP BY wc.status;
