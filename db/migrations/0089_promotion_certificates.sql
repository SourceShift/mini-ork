-- 0089_promotion_certificates.sql
-- Anytime-valid promotion certificates (test-by-betting e-values) plus the
-- per-task-class alpha ledger the apply gate spends across its test stream.
-- Additive only.
--
-- Renumbered from 0086: 0086_intake_events_mandate_json.sql landed on main
-- first, and a migration file whose number is already taken would be applied
-- in the wrong order (migrate.py sorts by filename).
--
-- The table is APPEND-ONLY, enforced by triggers. The alpha ledger's guarantee
-- rests on `MAX(slot) + 1` being the next test's slot: an UPDATE or DELETE
-- could move that head backwards and silently hand a class a fresh error
-- budget it already spent. Retention tooling must not touch this table.
--
-- One machine-readable outcome per row (`status`), not a boolean: a test can
-- fail to certify for three reasons that need different operator responses —
-- it measured less than the bar (uncertified), no amount of probe data could
-- have reached the bar at its size (unreachable), or it certified.

CREATE TABLE IF NOT EXISTS promotion_certificates (
  id              INTEGER PRIMARY KEY,
  task_class      TEXT    NOT NULL,
  task_class_raw  TEXT    NOT NULL DEFAULT '',
  slot            INTEGER NOT NULL CHECK (slot >= 1),
  candidate_id    TEXT    NOT NULL,
  run_id          TEXT,
  scorer          TEXT,
  dry_run         INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
  probe_set_hash  TEXT,
  probe_ids       TEXT,
  gains           INTEGER NOT NULL CHECK (gains >= 0),
  losses          INTEGER NOT NULL CHECK (losses >= 0),
  n               INTEGER NOT NULL CHECK (n >= 0),
  bet             REAL    NOT NULL,
  e_value         REAL    NOT NULL,
  ledger          TEXT    NOT NULL CHECK (ledger IN ('spend','none')),
  alpha_total     REAL    NOT NULL,
  alpha_k         REAL    NOT NULL,
  threshold       REAL    NOT NULL,
  status          TEXT    NOT NULL CHECK (status IN
                                  ('certified','uncertified','unreachable')),
  created_at      INTEGER NOT NULL,
  UNIQUE (task_class, slot)
);

-- No separate (task_class, slot) index: the UNIQUE constraint above already
-- creates one, and a second copy only doubles the write cost.

CREATE TRIGGER IF NOT EXISTS promotion_certificates_no_update
BEFORE UPDATE ON promotion_certificates
BEGIN
  SELECT RAISE(ABORT, 'promotion_certificates is append-only');
END;

CREATE TRIGGER IF NOT EXISTS promotion_certificates_no_delete
BEFORE DELETE ON promotion_certificates
BEGIN
  SELECT RAISE(ABORT, 'promotion_certificates is append-only');
END;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0089_promotion_certificates.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'promotion-certificates-v2');
