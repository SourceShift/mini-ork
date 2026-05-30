-- v_agent_performance: per-agent pass rate, average iterations, and cost over 30 days.
-- Used by the brain advisor to route new epics to the best available agent.
CREATE VIEW IF NOT EXISTS v_agent_performance AS
SELECT
  r.agent,
  COUNT(DISTINCT r.id) AS runs,
  ROUND(AVG(CASE WHEN r.final_verdict IN ('APPROVE','MERGED') THEN 1.0 ELSE 0.0 END), 2) AS pass_rate,
  ROUND(AVG((SELECT COUNT(*) FROM iters i2 WHERE i2.run_id = r.id)), 1) AS avg_iters,
  ROUND(SUM(r.cost_usd), 2) AS total_cost_usd,
  ROUND(AVG(r.cost_usd), 2) AS avg_run_cost_usd
FROM runs r
WHERE r.ended_at IS NOT NULL
  AND date(r.started_at) > date('now', '-30 days')
GROUP BY r.agent
ORDER BY runs DESC;
