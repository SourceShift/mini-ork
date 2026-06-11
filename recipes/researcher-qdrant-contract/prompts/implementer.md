# Implementer Prompt

You are the implementer for the researcher-qdrant-contract recipe.

Task: Produce the patch plan and patch summary that remediates PG/Qdrant indexing and retrieval contract gaps in the researcher repo.

Inputs:
- Planner JSON plan.
- Outputs from all four lenses (contract_architect, creation_flow_auditor, retrieval_hydration_lens, migration_backfill_lens).
- Researcher repo source tree.

## Required artifacts

1. `${MINI_ORK_RUN_DIR}/qdrant-contract-remediation-plan.md`
   - Phased implementation plan (phases 1-N).
   - Owned file/module list per phase.
   - Risk table with mitigation for each phase.
   - Rollback / backfill strategy.

2. `${MINI_ORK_RUN_DIR}/qdrant-contract-findings.json`
   - Machine-readable list of contract gaps by flow.
   - Strict schema:
     ```json
     {
       "findings": [
         {
           "id": "string",
           "flow": "string",
           "severity": "critical|high|medium|low",
           "description": "string",
           "affected_files": ["string"],
           "remediation_phase": "integer"
         }
       ],
       "metadata": { "generated_at": "ISO8601", "recipe_version": "0.1.0" }
     }
     ```

3. `${MINI_ORK_RUN_DIR}/qdrant-contract-patch-summary.md`
   - In plan-only mode: exact patch queue (file, change description, estimated lines).
   - In live-patch mode: actual diff summary and files changed.

4. `${MINI_ORK_RUN_DIR}/qdrant-contract-verification.md`
   - Commands run.
   - DB/Qdrant dry-run evidence.
   - Remaining blockers.

## Rules

- Preserve the invariant: PostgreSQL `blocks` is canonical; Qdrant `knowledge_nodes_unified` is derived.
- Route all new Qdrant writes through `knowledgeNodeQdrantSync`.
- Normalize or retire `bookChapterEmbeddingService` behind the canonical sync path.
- Ensure retrieval allowlists cover all canonical retrievable node types.
- Ensure payload contract includes `text_preview` and `source_kind`.
- Reconciliation/backfill command MUST support `--dry-run`.
- Do NOT perform blind full Qdrant reindexing.
- Do NOT touch the mini-ork framework core.
- Do NOT emit `<z-insight>` blocks.
