# Planner Prompt — researcher-qdrant-contract

You are planning a PG/Qdrant contract remediation in the researcher repo.

Inputs:
- Target repo path (via env: RESEARCHER_REPO or inferred from kickoff).
- Operator-supplied markdown audit summary of known PG/Qdrant contract gaps.
- Optional exact document UUID / user UUID for live validation.
- Optional command allowlist for targeted tests.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

Produce a concise JSON plan with this strict schema:

```json
{
  "outcome": "string — one sentence describing the remediation goal",
  "candidate_globs": ["string[] — files or globs to inspect in researcher repo"],
  "out_of_scope": ["string[] — files or operations explicitly excluded"],
  "verifier_commands": ["string[] — expected deterministic verifier invocations"],
  "artifact_manifest": {
    "qdrant-contract-remediation-plan.md": "phased implementation plan, owned file/module list, risk table, rollback/backfill strategy",
    "qdrant-contract-findings.json": "machine-readable list of contract gaps by flow",
    "qdrant-contract-patch-summary.md": "what was changed or exact patch queue in plan-only mode",
    "qdrant-contract-verification.md": "commands run, DB/Qdrant dry-run evidence, remaining blockers"
  },
  "lens_scope": {
    "contract_architect": "canonical payload schema + embedding-text contract",
    "creation_flow_auditor": "map every content creation path to post-persist sync point",
    "retrieval_hydration_lens": "fix retrieval allowlists + degraded-preview fallback",
    "migration_backfill_lens": "reconciliation + scoped repair with mandatory --dry-run"
  },
  "verifier_contract": {
    "checks": [
      {
        "id": "artifacts_exist",
        "description": "all four qdrant-contract artifacts exist and are non-empty",
        "command": "bash recipes/researcher-qdrant-contract/verifiers/deterministic-checks.sh"
      },
      {
        "id": "payload_contract_keys",
        "description": "remediation plan documents text_preview and source_kind payload keys",
        "command": "grep -qi text_preview \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\" && grep -qi source_kind \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\""
      },
      {
        "id": "canonical_sync_writer",
        "description": "plan and patch summary route Qdrant writes through knowledgeNodeQdrantSync and reject new direct upsert/upload_points writers",
        "command": "grep -qi knowledgeNodeQdrantSync \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\" \"$MINI_ORK_RUN_DIR/qdrant-contract-patch-summary.md\""
      },
      {
        "id": "book_chapter_service_normalized",
        "description": "bookChapterEmbeddingService is retired or normalized behind the canonical sync writer",
        "command": "grep -qi bookChapterEmbeddingService \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\" \"$MINI_ORK_RUN_DIR/qdrant-contract-patch-summary.md\""
      },
      {
        "id": "retrieval_allowlist_coverage",
        "description": "retrieval coverage includes text_chunk, highlight, ai_annotation, generated_content, and book_chapter_content",
        "command": "grep -qi allowlist \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\""
      },
      {
        "id": "dry_run_write_gate",
        "description": "reconciliation/backfill supports --dry-run and explains how it gates writes",
        "command": "grep -qi -- --dry-run \"$MINI_ORK_RUN_DIR/qdrant-contract-remediation-plan.md\" \"$MINI_ORK_RUN_DIR/qdrant-contract-verification.md\""
      }
    ]
  }
}
```

Constraints:
- Do NOT include blind full Qdrant reindexing in the plan.
- Do NOT touch the mini-ork framework core.
- Do NOT run the researcher frontend or backend servers.
- Preserve the invariant: PostgreSQL `blocks` is canonical; Qdrant `knowledge_nodes_unified` is derived.
- The JSON plan MUST include `verifier_contract.checks[]` with at least the
  checks shown above. Do not omit or rename this key; mini-ork rejects plans
  without it before dispatching researcher lenses.
