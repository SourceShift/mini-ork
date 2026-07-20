-- gradient_records — textual gradient store.
--
-- Was originally lazy-created by the gradient schema initializer when
-- the extractor ran. Other libs (role_evolver, context_assembler) read the
-- table without first sourcing the extractor, so on a fresh DB they hit
-- 'no such table: gradient_records'. Promote to a real migration so the
-- schema is present after db/init.sh, independent of source order.
--
-- The lib's CREATE TABLE IF NOT EXISTS stays in place (idempotent) so
-- pre-migration DBs still self-heal.

CREATE TABLE IF NOT EXISTS gradient_records (
    gradient_id      TEXT PRIMARY KEY,
    target           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    suggested_change TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0
                          CHECK(confidence BETWEEN 0.0 AND 1.0),
    created_at       INTEGER NOT NULL,
    task_class       TEXT
);

CREATE INDEX IF NOT EXISTS idx_gradient_records_task_class
    ON gradient_records (task_class);
CREATE INDEX IF NOT EXISTS idx_gradient_records_confidence
    ON gradient_records (confidence DESC);
