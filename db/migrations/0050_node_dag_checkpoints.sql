-- 0050_node_dag_checkpoints.sql
-- E1 of kickoff feat/durable-dag: crash-safe per-node checkpoints with hash
-- validity checks. Two additive tables, no ALTERs on existing schema.
--
-- Source of truth: internal-docs/architecture/2026-07-15-durable-dag-resume-design.md
-- §2 (durable state model), §3 (validity rules), §4 (crash-safe publication order).
-- Recovery/E2 (run_leases, recovery_requests) is NOT in this migration — the
-- design explicitly sequences those as E3. Their absence is deliberate; this
-- migration must NOT introduce surrogate columns for them.
--
-- Concurrency model: in this E1 a single-writer model is assumed (one process
-- owns the run, no concurrent checkpoint races). The PK (run_id, node_id) on
-- node_checkpoints makes the writer INSERT-or-REPLACE the *latest valid* row
-- per node; if E2/E3 introduces contention the schema stays compatible
-- (a fence token column can be added via a later migration).
--
-- Artifact paths in artifact_manifest_json are ALWAYS relative to
-- MINI_ORK_RUN_DIR (no leading '/', no '..') — same convention as run_artifacts
-- (0047), so a run is portable across vendor / foreign-home / cloud exec run
-- dirs. is_node_reusable resolves each entry against MINI_ORK_RUN_DIR.

PRAGMA foreign_keys = OFF;

-- node_checkpoints: the reuse source of truth. One row per (run_id, node_id).
-- A node is reusable iff the row exists AND every validity rule (§3) passes
-- at read time. Row absence = "not resumable" (legacy run semantics from §10).
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT    NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash            TEXT    NOT NULL,   -- sha256 of resolved upstream inputs
    recipe_version        TEXT    NOT NULL,   -- workflow version (e.g. "1.2.0")
    config_hash           TEXT    NOT NULL,   -- sha256 of resolved config slice
    artifact_manifest_json TEXT   NOT NULL,   -- JSON: [{path,sha256,bytes}, ...] relative to MINI_ORK_RUN_DIR
    session_ref           TEXT,                -- nullable; populated by E4 (tier-A turn resume)
    failure_class         TEXT,                -- nullable on success; populated on failure
    created_at            INTEGER NOT NULL,    -- unix epoch seconds
    PRIMARY KEY (run_id, node_id)
);

-- Hot read: "is this node reusable right now?" — the only read path the
-- runtime needs. Indexed on (run_id, node_id) which is the PK so an
-- additional index here is redundant; the PK covers the lookup. We
-- intentionally do NOT add an index on recipe_version/config_hash — those
-- are equality checks the validity function performs after the PK fetch,
-- and adding them would mislead E2's expected "rerun on config drift" path
-- into thinking this E1 already supports recovery lookup (it does not).

-- node_attempts: append-only observability, one row per attempt. E1 only
-- writes a row on successful checkpoint publication; the attempt counter
-- in node_checkpoints is the canonical "how many tries" signal.
CREATE TABLE IF NOT EXISTS node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,   -- monotonic per (run_id, node_id)
    node_type             TEXT,                -- 'implementer' | 'researcher' | 'reviewer' | ...
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class         TEXT,                -- 'infra_interrupt' | 'provider_limit' | 'output_invalid' | 'input_required' | 'terminal' (E3)
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,  -- 1 if this attempt reused a prior checkpoint (E2)
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,  -- 1 if this attempt wrote a new node_checkpoints row
    cost_usd              REAL,
    provider_session_id   TEXT,                -- vendor session id (E4)
    initiator             TEXT                 -- 'python' | 'bash' | 'recover' (E2 adds 'recover')
);

-- Hot read: per-run attempt history. The PK is auto-increment so a covering
-- index on (run_id, node_id, attempt_no DESC) is the access path for
-- "show me the last attempt of node X in run Y".
CREATE INDEX IF NOT EXISTS idx_node_attempts_run_node_attempt
    ON node_attempts(run_id, node_id, attempt_no DESC);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0050_node_dag_checkpoints.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'durable-dag-e1-v1');
