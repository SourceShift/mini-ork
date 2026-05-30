-- mini-ork migration 0012 — safety constraints, audit log, gate registry
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0012_safety.sql
--
-- Key design principles:
--   1. safety_constraints is IMMUTABLE after first write (UPDATE/DELETE triggers raise).
--   2. audit_log is APPEND-ONLY (UPDATE/DELETE triggers raise).
--   3. registered_gates is the canonical registry of all gate types used by workflow nodes.
--
-- Depends on: 0001_core.sql (base tables present)
BEGIN;

-- ── safety_constraints ────────────────────────────────────────────────────────
-- Each row declares a system-wide safety constraint that must be checked before
-- any gate allows a promotion, deployment, or high-risk operation.
-- Rows are IMMUTABLE after insert: enforced by triggers below.
CREATE TABLE IF NOT EXISTS safety_constraints (
  constraint_id         TEXT    PRIMARY KEY,         -- UUID or slug, e.g. 'no_prompt_injection'
  name                  TEXT    NOT NULL UNIQUE,     -- short human-readable identifier
  description           TEXT    NOT NULL,            -- full explanation of what is forbidden
  applies_to            TEXT    NOT NULL DEFAULT '[]', -- JSON array of task_class names; empty = all
  check_script_path     TEXT    NOT NULL DEFAULT '', -- path to script that verifies the constraint
  severity              TEXT    NOT NULL DEFAULT 'hard_fail'
                        CHECK (severity IN ('warn','hard_fail')),
  enforced_since        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  can_be_overridden     INTEGER NOT NULL DEFAULT 0   CHECK (can_be_overridden IN (0,1))
                        -- 0 = truly immutable; 1 = human operator can temporarily suspend
);

CREATE INDEX IF NOT EXISTS idx_sc_severity            ON safety_constraints(severity);
CREATE INDEX IF NOT EXISTS idx_sc_name                ON safety_constraints(name);

-- IMMUTABILITY TRIGGERS — deny any UPDATE or DELETE on safety_constraints
CREATE TRIGGER IF NOT EXISTS trg_sc_no_update
BEFORE UPDATE ON safety_constraints
BEGIN
  SELECT RAISE(ABORT,
    'safety_constraints rows are immutable after insert — UPDATE denied by trg_sc_no_update');
END;

CREATE TRIGGER IF NOT EXISTS trg_sc_no_delete
BEFORE DELETE ON safety_constraints
BEGIN
  SELECT RAISE(ABORT,
    'safety_constraints rows are immutable — DELETE denied by trg_sc_no_delete');
END;

-- ── audit_log ─────────────────────────────────────────────────────────────────
-- Append-only trail of every significant system event: promotions, quarantines,
-- rollbacks, safety constraint hits, and human overrides.
-- No row in audit_log may ever be modified or deleted.
CREATE TABLE IF NOT EXISTS audit_log (
  audit_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type            TEXT    NOT NULL,            -- e.g. 'promotion','quarantine','rollback',
                                                     --   'safety_hit','human_override','gate_pass','gate_fail'
  actor                 TEXT    NOT NULL,            -- agent_version_id | 'human:<user_id>' | 'gate:<gate_id>'
  target                TEXT    NOT NULL,            -- what the event affected, e.g. 'workflow:code_review_v3'
  payload               TEXT    NOT NULL DEFAULT '{}', -- JSON with full event context
  occurred_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_al_event_type          ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_al_actor               ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_al_target              ON audit_log(target);
CREATE INDEX IF NOT EXISTS idx_al_occurred_at         ON audit_log(occurred_at);

-- APPEND-ONLY TRIGGERS — deny UPDATE and DELETE on audit_log
CREATE TRIGGER IF NOT EXISTS trg_al_no_update
BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT,
    'audit_log is append-only — UPDATE denied by trg_al_no_update');
END;

CREATE TRIGGER IF NOT EXISTS trg_al_no_delete
BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT,
    'audit_log is append-only — DELETE denied by trg_al_no_delete');
END;

-- ── registered_gates ──────────────────────────────────────────────────────────
-- Canonical registry of all gate definitions. Workflow nodes reference gates
-- by gate_id. The gate runner resolves condition_script_path at runtime.
--
-- Six gate types (from the book's gate taxonomy):
--   deterministic_verifier — script exits 0/non-0 (fast, cheap, no LLM)
--   reviewer_gate          — LLM reviewer returns APPROVE/REQUEST_CHANGES/ESCALATE
--   human_gate             — blocks until a human resolves the inbox item
--   budget_gate            — aborts if projected cost_usd exceeds threshold
--   scope_gate             — verifies no files outside the declared frame were touched
--   deployment_gate        — checks external CI/CD status before deploy step
CREATE TABLE IF NOT EXISTS registered_gates (
  gate_id               TEXT    PRIMARY KEY,         -- slug, e.g. 'verifier_tests_pass'
  gate_type             TEXT    NOT NULL
                        CHECK (gate_type IN (
                          'deterministic_verifier',
                          'reviewer_gate',
                          'human_gate',
                          'budget_gate',
                          'scope_gate',
                          'deployment_gate'
                        )),
  condition_script_path TEXT    NOT NULL DEFAULT '', -- path to script; empty for human_gate/budget_gate
  task_class_filter     TEXT,                        -- JSON array of task_classes; null = applies to all
  is_safety             INTEGER NOT NULL DEFAULT 0   CHECK (is_safety IN (0,1)),
                        -- 1 = this gate enforces a safety_constraint; failures trigger audit_log
  registered_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  description           TEXT    NOT NULL DEFAULT ''  -- human-readable explanation
);

CREATE INDEX IF NOT EXISTS idx_rg_gate_type           ON registered_gates(gate_type);
CREATE INDEX IF NOT EXISTS idx_rg_is_safety           ON registered_gates(is_safety);
CREATE INDEX IF NOT EXISTS idx_rg_registered_at       ON registered_gates(registered_at);

-- ── seed: core safety gates ───────────────────────────────────────────────────
-- Pre-register the non-negotiable system gates so new installs start with a
-- minimal safe configuration. These rows are themselves protected by the
-- registered_gates table (no immutability trigger here — gates CAN be
-- supplemented; only safety_constraints are truly locked).
INSERT OR IGNORE INTO registered_gates
  (gate_id, gate_type, is_safety, description)
VALUES
  ('verifier_all_pass',
   'deterministic_verifier', 1,
   'All declared success_verifiers in the artifact_contract must exit 0.'),

  ('reviewer_approve',
   'reviewer_gate', 0,
   'Reviewer agent must return APPROVE verdict before promotion proceeds.'),

  ('human_sign_off',
   'human_gate', 1,
   'A human must explicitly resolve the pending inbox item before the gate clears.'),

  ('budget_cap',
   'budget_gate', 1,
   'Aborts the run if projected or actual cost_usd exceeds the task_class budget_cap_usd.'),

  ('scope_no_foreign_writes',
   'scope_gate', 1,
   'No files outside the declared artifact_contract frame may be written or deleted.'),

  ('deployment_ci_green',
   'deployment_gate', 0,
   'External CI pipeline must report green status before the deployment gate passes.');

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0012_safety.sql', strftime('%s','now'), 'phase-a-1');
