-- 0073_diagnosis_ledger.sql
-- SDE-3b2: the diagnosis ledger behind the calibrated escalation bar.
--
-- The frozen escalation policy (mini_ork/recovery/escalation_policy.py) admits a
-- model-made engine diagnosis above a bar: 0.85 until 30 labelled outcomes
-- exist, then a calibrated Learn-Then-Test bar. Nothing recorded those outcomes,
-- so the calibrated path could never activate. This table is that record: one
-- row per diagnosis the self-driving loop ACTED on (escalated to a fix epic, or
-- refused and handed to a human), labelled right or wrong once the outcome is
-- known (SDE-4 labels a row when the revived run gets past — or fails again at —
-- the node that failed).
--
--   run_id       TEXT NOT NULL   the run the diagnosis was made for
--   source       TEXT NOT NULL   "rule" | "model" -- only model rows calibrate
--   layer        TEXT            the diagnosis layer ("engine" is escalatable)
--   routes_to    TEXT            "fix_epic" | "human" | ...
--   confidence   REAL            the diagnoser's stated confidence; NULL for a
--                                diagnosis that stated none (never calibrates)
--   model_lane   TEXT            the model lane that made a model diagnosis
--   signature    TEXT            the diagnosis signature (the (run_id, source,
--                                signature) dedupe key; the writer always
--                                stamps one, so the key can never be NULL-split)
--   action       TEXT            what the loop did: "escalate" | "human"
--   fix_epic_id  TEXT            the fix epic an escalation opened, if any
--   created_at   INTEGER NOT NULL epoch seconds, stamped by the writer
--   label        INTEGER         NULL = unlabelled, 1 = the diagnosis was right,
--                                0 = it was wrong. NULL is the load-bearing
--                                default: an unlabelled row has no outcome yet,
--                                which a reader must tell apart from a labelled
--                                "wrong" (0). A DEFAULT would collapse the two.
--   labelled_at  INTEGER         when the label was applied
--   label_reason TEXT            the labeller's note (evidence for the label)
--
-- The dedupe key is a UNIQUE INDEX rather than a table constraint -- the same
-- shape as 0061/0062 -- so a retried `record` cannot double-count a diagnosis.
--
-- ADDITIVE only: no existing table or column is touched.
--
-- The writer is mini_ork/stores/diagnosis_ledger.py, which runs the same DDL at
-- runtime (`_ensure_table`) so a state.db that never ran this migration still
-- works -- the precedent set by 0056's lesson_text healing and 0072's
-- _ensure_body_columns. tests/unit/test_diagnosis_ledger.py exercises the store
-- against a hand-rolled DB, so the two texts must not drift apart.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS diagnosis_ledger (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    layer        TEXT,
    routes_to    TEXT,
    confidence   REAL,
    model_lane   TEXT,
    signature    TEXT,
    action       TEXT,
    fix_epic_id  TEXT,
    created_at   INTEGER NOT NULL,
    label        INTEGER,
    labelled_at  INTEGER,
    label_reason TEXT
);

-- The dedupe key: recording the same diagnosis twice must reuse the row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_diagnosis_ledger_unique
  ON diagnosis_ledger(run_id, source, signature);

-- Hot read: `labelled(db, "model")` -- every labelled row of one source.
CREATE INDEX IF NOT EXISTS idx_diagnosis_ledger_source_label
  ON diagnosis_ledger(source, label);

-- Hot read (SDE-4): "what diagnosis did this fix epic act on?"
CREATE INDEX IF NOT EXISTS idx_diagnosis_ledger_epic
  ON diagnosis_ledger(fix_epic_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0073_diagnosis_ledger.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'diagnosis-ledger-v1');
