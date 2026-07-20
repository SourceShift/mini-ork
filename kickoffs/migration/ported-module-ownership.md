# Audit and close ownership of `mini_ork/ported/` recursively

## Mode

Start with a **report-only inventory**. Do not edit, delete, commit, merge, or
push source code in the first run.

The governing plan is
`docs/migration/ported-module-ownership-recursive-plan.md`. Follow its safety
invariants, classifications, per-module contract, provider policy, artifacts,
and completion conditions exactly.

## Goal

Produce a complete evidence-backed inventory of every
`mini_ork/ported/*.py` module, then propose the first bounded ownership unit.
The first candidate is `similarity.py`: confirm whether its pure algorithm is
duplicated in `mini_ork/context_assembler.py` and whether integration is the
correct terminal decision.

## Required discovery

- Read current migration and architecture documents.
- Reconstruct Git introduction, port, adoption, and retirement history.
- Retrieve relevant ContextNest sessions and decisions.
- Map static and dynamic runtime callers, duplicate implementations, tests,
  integration/E2E/security coverage, benchmark references, and fixtures.
- Distinguish intentional external-process adapters from accidental Bash
  dependencies.

## Required artifacts

Write to the run directory only:

- `ported-module-inventory.json`
- `history-intent.md`
- `ownership-map.json`
- `proposed-queue.json`
- `verdict.json`

Every module must be classified as `active`, `duplicated`, `dormant`, `dead`,
or `external_adapter`, with a proposed decision of `integrate`, `retain`,
`delete`, or `defer`. Every decision must cite concrete evidence and name an
owner or target consumer.

## Provider policy

- Kimi: discovery and history synthesis.
- GLM 5.2: classification and independent review.
- Codex: no implementation in this report-only run; reserve it for the bounded
  source-change unit after approval.
- Deterministic scripts: reference, fixture, schema, and completeness gates.

Load Kimi and GLM credentials process-locally from
`/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh` using a temporary
`MINI_ORK_HOME`. Never persist secrets. Do not use MiniMax, DeepSeek, or Opus,
and do not permit implicit fallback to them.

## Report-only acceptance criteria

- Inventory row count equals the current `mini_ork/ported/*.py` file count.
- Every row contains runtime, duplicate, test, benchmark/fixture, docs/history,
  classification, decision, owner, confidence, and risk fields.
- Every `delete` proposal proves all deletion gates from the governing plan.
- Every `integrate` proposal names the canonical target and consumer.
- Every external adapter states why the process boundary is intentional.
- The proposed queue respects dependency order and contains one ownership seam
  per unit.
- `similarity.py` has a complete contract proposal covering threshold, source
  tables, top-three selection, citations, suggested fixes, ordering, rounding,
  and missing-table behavior.
- `verdict.json` fails closed when evidence is missing.

## Stop condition

Stop after emitting the reviewed inventory and first-unit proposal. Source
implementation begins only in a new isolated worktree and a separately
reviewable migration commit.
