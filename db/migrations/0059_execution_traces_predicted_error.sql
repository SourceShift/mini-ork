-- 0059: persist the router's predicted error.
--
-- 0057 landed route_margin, the INPUT the calibration map fits. The map's
-- OUTPUT — the calibrated error probability behind an escalation decision — is
-- computed in decision_service.decide() (via calibration.should_escalate) and
-- then thrown away: the value is returned but never written, so nothing can
-- check whether the map is actually right. A stored prediction is the only
-- thing that can be compared against the observed outcome later.
--
-- This column records that prediction for every routed row that carries one,
-- so the calibration backtest (reliability / ECE / Brier / blind-spot rate)
-- has data to measure.
--
-- Nullable on purpose, for the same three reasons 0057 documents: rows written
-- before this migration, rows routed by the static/fallback path (no margin, no
-- map, no prediction), and rows where exploration replaced the pick (the
-- prediction describes a comparison the chosen lane never entered) all
-- legitimately have no prediction.

PRAGMA foreign_keys=off;

ALTER TABLE execution_traces ADD COLUMN predicted_error REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_et_predicted_error_v59
  ON execution_traces(predicted_error) WHERE predicted_error IS NOT NULL;

PRAGMA foreign_keys=on;
