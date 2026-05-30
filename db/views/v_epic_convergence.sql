-- v_epic_convergence: tracks how quickly agents converge (issue count declines)
-- across iterations within the last 30 days.
-- Lower avg_issues at higher iter_n = good convergence.
CREATE VIEW IF NOT EXISTS v_epic_convergence AS
SELECT
  r.agent,
  i.n AS iter_n,
  COUNT(*) AS sample_size,
  ROUND(AVG(json_array_length(json_extract(i.feedback_json, '$.issues'))), 1) AS avg_issues
FROM iters i
JOIN runs r ON r.id = i.run_id
WHERE i.verdict IN ('APPROVE','REQUEST_CHANGES','ESCALATE')
  AND i.feedback_json IS NOT NULL
  AND date(i.started_at) > date('now', '-30 days')
GROUP BY r.agent, i.n
ORDER BY r.agent, i.n;
