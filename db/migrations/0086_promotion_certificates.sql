-- 0086_promotion_certificates.sql
-- Anytime-valid promotion certificates (test-by-betting e-values) plus the
-- per-task-class alpha ledger the apply gate spends across its test stream.
-- Additive only.

CREATE TABLE IF NOT EXISTS promotion_certificates (
  id            INTEGER PRIMARY KEY,
  task_class    TEXT    NOT NULL,
  slot          INTEGER NOT NULL CHECK (slot >= 1),
  candidate_id  TEXT    NOT NULL,
  gains         INTEGER NOT NULL CHECK (gains >= 0),
  losses        INTEGER NOT NULL CHECK (losses >= 0),
  n             INTEGER NOT NULL CHECK (n >= 0),
  bet           REAL    NOT NULL,
  e_value       REAL    NOT NULL,
  ledger        TEXT    NOT NULL CHECK (ledger IN ('spend','none')),
  alpha_total   REAL    NOT NULL,
  alpha_k       REAL    NOT NULL,
  threshold     REAL    NOT NULL,
  certified     INTEGER NOT NULL CHECK (certified IN (0,1)),
  created_at    INTEGER NOT NULL,
  UNIQUE (task_class, slot)
);
CREATE INDEX IF NOT EXISTS idx_promotion_certificates_class ON promotion_certificates(task_class, slot);

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0086_promotion_certificates.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'promotion-certificates-v1');
