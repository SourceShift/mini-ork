-- 0057: persist the routing margin.
--
-- 0054 landed route_source / route_explore / route_score, which say WHICH
-- policy chose a lane and how good the pick looked. None of them says how
-- decisively it won. The router already computes the winner's score minus the
-- runner-up's and then discards it, because the policy layer only ever needed a
-- lane string back.
--
-- That margin is the input a calibration map needs: UCCI fits a monotone
-- function from margin to observed error rate and escalates when the
-- calibrated error exceeds the target. Without the column the map has nothing
-- to fit, and every routing decision stays uncalibrated.
--
-- Nullable on purpose: rows written before this migration, rows routed by the
-- static/fallback path, and rows where exploration replaced the pick (the
-- margin describes a comparison the chosen lane never entered) all legitimately
-- have no margin.

PRAGMA foreign_keys=off;

ALTER TABLE execution_traces ADD COLUMN route_margin REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_et_route_margin_v57
  ON execution_traces(route_margin) WHERE route_margin IS NOT NULL;

PRAGMA foreign_keys=on;
