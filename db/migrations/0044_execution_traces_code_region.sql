-- 0044_execution_traces_code_region.sql
-- Stamp fresh execution traces with the top-level code region inferred from
-- the dispatched node's touched files. Existing rows intentionally keep NULL:
-- old traces did not have enough canonical provenance to backfill safely.

PRAGMA foreign_keys = OFF;

-- Idempotent ADD COLUMN for re-runnable local/dev DB initialization. The
-- column is nullable so existing execution_traces rows remain compatible.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"execution_traces\\\") WHERE name = \\\"code_region\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE execution_traces ADD COLUMN code_region TEXT DEFAULT NULL;\"; fi'"

CREATE INDEX IF NOT EXISTS idx_et_code_region
  ON execution_traces(code_region) WHERE code_region IS NOT NULL;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0044_execution_traces_code_region.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'execution-traces-code-region-v1');
