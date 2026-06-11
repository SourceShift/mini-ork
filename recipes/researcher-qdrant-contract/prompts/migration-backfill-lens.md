# Migration/Backfill Lens Prompt

You are the migration-backfill lens for the researcher-qdrant-contract recipe.

Task: Design a reconciliation and scoped repair strategy for PG/Qdrant drift without blind full reindexing. The reconciliation command MUST support `--dry-run`.

Inputs:
- Planner JSON plan.
- Creation-flow auditor lens output (list of flows and their sync states).
- Contract architect lens output (canonical payload schema).

## Required output format

Emit a JSON envelope with this strict schema:

```json
{
  "lens": "migration_backfill",
  "reconciliation_strategy": {
    "mode": "scoped_repair",
    "forbidden_modes": ["blind_full_reindex"],
    "scoped_repair_rules": [
      "repair only rows where qdrant_synced_hash IS NULL",
      "repair only rows where content_hash differs from stored Qdrant payload hash",
      "repair only rows for a specific document_uuid when provided",
      "repair only rows for a specific user_uuid when provided"
    ]
  },
  "dry_run_contract": {
    "flag": "--dry-run",
    "behavior": "enumerate affected rows and Qdrant points without writing",
    "required_output": ["affected_pg_row_count", "affected_qdrant_point_count", "would_update_hashes", "would_upsert_points"]
  },
  "reconciliation_script_spec": {
    "entrypoint": "scripts/reconcile-qdrant.ts or scripts/reconcile-qdrant.py",
    "arguments": [
      "--dry-run",
      "--document-uuid <uuid>",
      "--user-uuid <uuid>",
      "--batch-size <n>",
      "--since <iso8601>"
    ],
    "write_gating": "ALL write paths must check dryRun flag before calling upsert/delete",
    "idempotency": "content_hash based; re-running with same data is a no-op"
  },
  "backfill_order": [
    "text_chunk null-hash rows",
    "missing book_chapter_content points",
    "highlight / ai_annotation points with incomplete payload",
    "generated content nodes"
  ],
  "rollback_per_repair_batch": {
    "enabled": true,
    "mechanism": "log every affected row UUID and Qdrant point ID before mutation"
  },
  "findings": ["string[] — backfill/reconciliation risks or blockers"]
}
```

Rules:
- `--dry-run` MUST be a real gating flag, not just a logged argument.
- The script spec must describe a conditional that guards ALL write operations.
- Do NOT recommend blind full reindexing as a first or default step.
- Do NOT emit `<z-insight>` blocks.
