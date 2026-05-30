-- v_failure_patterns: top REQUEST_CHANGES issue categories in the last 7 days.
-- Used by brain_decisions to bias agent selection away from failure-prone agents.
CREATE VIEW IF NOT EXISTS v_failure_patterns AS
SELECT
  json_extract(value, '$.category') AS category,
  COUNT(*) AS hits,
  GROUP_CONCAT(DISTINCT json_extract(value, '$.severity')) AS severities
FROM iters i, json_each(json_extract(i.feedback_json, '$.issues'))
WHERE i.verdict = 'REQUEST_CHANGES'
  AND date(i.started_at) > date('now', '-7 days')
  AND json_extract(value, '$.category') IS NOT NULL
GROUP BY json_extract(value, '$.category')
ORDER BY hits DESC;
