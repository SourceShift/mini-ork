# researcher-qdrant-contract

PG/Qdrant indexing and retrieval contract remediation recipe for the researcher
repo.

## Topology

```text
planner -> [contract_architect, creation_flow_auditor, retrieval_hydration_lens, migration_backfill_lens]
                                                          -> implementer
                                                          -> deterministic_verifier
                                                          -> reviewer
                                                          -> publisher

reviewer / deterministic_verifier -> rollback on failure
```

The recipe has exactly 10 nodes:

1. `planner`
2. `contract_architect`
3. `creation_flow_auditor`
4. `retrieval_hydration_lens`
5. `migration_backfill_lens`
6. `implementer`
7. `deterministic_verifier`
8. `reviewer`
9. `publisher`
10. `rollback`

## Model Lanes

The non-deterministic lenses intentionally span four distinct model families:

- `contract_architect`: `opus_lens` (Anthropic Opus)
- `creation_flow_auditor`: `kimi_lens` (Moonshot Kimi)
- `retrieval_hydration_lens`: `codex_lens` (OpenAI Codex)
- `migration_backfill_lens`: `minimax_lens` (MiniMax)

This satisfies the hard heterogeneity floor (≥3 families) with one family of
headroom.

## Artifacts

Required run-local artifacts:

- `${MINI_ORK_RUN_DIR}/qdrant-contract-remediation-plan.md`
- `${MINI_ORK_RUN_DIR}/qdrant-contract-findings.json`
- `${MINI_ORK_RUN_DIR}/qdrant-contract-patch-summary.md`
- `${MINI_ORK_RUN_DIR}/qdrant-contract-verification.md`

## Verifiers

The recipe ships two verifier entry points:

1. `verifiers/recipe-validator.sh` — structural validation (used by
   recipe-creator meta-recipe). Checks workflow parses, heterogeneity,
   refs resolve, and artifact contract completeness.

2. `verifiers/deterministic-checks.sh` — task-specific validation (used when
   the recipe is executed). Checks:
   - Required artifacts exist and parse.
   - `qdrant-contract-findings.json` follows strict schema.
   - No new direct Qdrant writer outside canonical sync path.
   - `bookChapterEmbeddingService` retirement is addressed.
   - Retrieval allowlist coverage is declared.
   - `text_preview` and `source_kind` are in payload contract.
   - Reconciliation/backfill supports `--dry-run`.

Both verifiers emit structured JSON:

```json
{
  "verifier": "name",
  "pass": true,
  "verdict": "pass",
  "evidence_path": "...",
  "checks_run": ["..."],
  "failed_checks": ["..."],
  "checks": [{"name":"...","expected":"...","actual":"...","pass":true}],
  "reasons": ["..."],
  "checked_criteria": ["..."],
  "artifact_ref": "..."
}
```

## Rollback Strategy

`rollback_strategy: keep_run_artifacts_discard_partial_outputs`.

On verifier or reviewer failure, the recipe keeps lens outputs, patch summaries,
and verifier logs under the run directory. It does NOT commit any changes to the
researcher repo.

## Diverges From

- `recipes/framework-edit`: code-edit recipe with 9 nodes, no PG/Qdrant domain.
- `recipes/recipe-creator`: meta-recipe that authors recipes; this is a domain
  recipe that remediates a specific repo.
- `recipes/code-fix`: general bug-fix recipe without the four-lens contract
  architecture.

## Failure-Mode Coverage

The verifier contracts guard against the following known failure modes:

1. **Split-brain writer introduction** — `deterministic-checks.sh` greps the
   patch summary for `upsert`/`upload_points` and verifies canonical sync path
   language is present.
2. **Blind full reindex** — the `migration_backfill_lens` prompt explicitly
   forbids `blind_full_reindex` in the reconciliation strategy.
3. **Dry-run flag that does not gate writes** — the `migration_backfill_lens`
   prompt requires a write-gating conditional, and `deterministic-checks.sh`
   verifies `--dry-run` appears in the remediation plan.
4. **Missing payload keys** — `deterministic-checks.sh` asserts `text_preview`
   and `source_kind` are present in the remediation plan.
5. **Meta-recipe node leakage** — `recipe-validator.sh` grep-denies
   `opus_arbiter`, `verifier_smith`, and drafter names in the workflow.
6. **Non-deterministic verifier scripts** — all verifier stubs require
   `set -uo pipefail`, avoid `$RANDOM`, timestamps, and network calls.
