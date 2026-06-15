# Epic: researcher-qdrant-contract remediation

## Goal

Fix inconsistent PG/Qdrant indexing and retrieval contracts across PDF uploads,
imported arXiv/markdown files, generated books, highlights, AI annotations, and
generated content nodes in the `researcher` repo.

## Scope

- `researcher` repo PG/Qdrant sync modules (especially `<canonical_sync_module>`,
  `knowledgeNodeEmbeddingBuilder`, `bookChapterEmbeddingService`).
- Retrieval and hydration code paths.
- Reconciliation/backfill script design.

## Expected Outputs

1. `qdrant-contract-remediation-plan.md` — phased plan, risk table, rollback strategy.
2. `qdrant-contract-findings.json` — machine-readable contract gaps by flow.
3. `qdrant-contract-patch-summary.md` — patch queue or actual diff summary.
4. `qdrant-contract-verification.md` — commands run, dry-run evidence, blockers.

## Constraints

- PostgreSQL `blocks` is canonical; Qdrant `knowledge_nodes_unified` is derived.
- All Qdrant writes must route through `<canonical_sync_module>`.
- `bookChapterEmbeddingService` must be retired or normalized behind the canonical writer.
- Reconciliation/backfill must support `--dry-run`.
- No blind full Qdrant reindexing.
- No destructive DB operations.
- Do not edit mini-ork framework core.
- Do not run researcher frontend/backend servers.

## Verification Command

```bash
bash recipes/researcher-qdrant-contract/verifiers/deterministic-checks.sh
```
