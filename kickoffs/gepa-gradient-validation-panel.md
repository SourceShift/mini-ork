# Validate the GEPA + gradient-pipeline issues (4-lens adversarial panel)

## Problem

We compiled a defect report on why mini-ork's self-improvement (GEPA + the reflection/gradient
pipeline) has never changed a single prompt in production. We need an **adversarial 4-lens
validation**: independently confirm or refute each claimed issue with your own evidence, and
surface any NEW issues the report missed. Do NOT take the report on faith — re-run the queries,
read the cited code, trace the logic.

**The report under validation (read it fully, absolute path):**
`/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-10-gepa-gradient-issues.md`

**Code to inspect (absolute paths):**
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/gepa.py` (the optimizer loop + acceptance gate)
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/miniork_adapter.py` (the offline `evaluate()` — issue G2)
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-reflect` (the `MO_OPTIMIZER` gate + `--since` window)
- `/Volumes/docker-ssd/ps/mini-ork/lib/gradient_extractor.sh` (extraction + containment dedup)
- `/Volumes/docker-ssd/ps/mini-ork/lib/reflection_pipeline.sh` (difflib dedup + clustering)

**Live evidence DB (query it directly, read-only):**
`/Volumes/docker-ssd/ps/researcher/.mini-ork/state.db` — reproduction queries are in the report.

## Issues to validate (label each CONFIRM / REFUTE / PARTIAL with your own evidence)

- **G1** GEPA never enabled (`MO_OPTIMIZER` unset).
- **G2** GEPA structurally cannot accept a mutation (offline hash-scoring → parent≡child → strict-improvement gate always rejects). *This is the load-bearing claim — scrutinize it hardest.*
- **G3** Mutation model is `stub`.
- **G4** Suggest-only; nothing applies (`workflow_candidates=0`).
- **Gr1** Runaway re-extraction: 9,777 gradients from 1,603 traces; no per-trace watermark; `--since 24h` overlap.
- **Gr2** Dedup keyed on trace-specific `signal` → cannot collapse semantic duplicates (172 reviewer gradients / 172 distinct).
- **Gr3** No apply path: 39 `emergent_patterns` all `proposed`.
- **X1** The whole loop diagnoses but never acts (shared root cause).
- **X2** Diagnosis quality is high but wasted (the "agents claim without reading/verifying" theme).

## What each lens does (validate the SAME issues from your angle; find new ones)

- **codex lens (ARCHITECTURE / code-truth):** read the actual code paths. Verify the *mechanism* claims — especially G2's `evaluate()` hash-fallback logic and Gr2's dedup key. Trace whether the acceptance gate can ever be satisfied. Cite file:line.
- **kimi lens (CORRECTNESS / rigor):** re-run the DB queries to confirm the numbers; check the causal logic of each claim; try to falsify. Are any claims overstated or is the true cause different?
- **minimax lens (PERFORMANCE / systems):** the scale/cost angle — unbounded gradient growth, re-extraction cost, DB bloat, cost of running GEPA/reflect repeatedly, any O(N²) or runaway paths.
- **opus lens (DEEP / architectural):** the systemic "diagnose-but-never-act" pattern (X1). Are there deeper or additional structural issues? Second-order risks a naive fix would introduce? New issues not in the report.

## Definition of Done

1. Four lens reports at `${MINI_ORK_RUN_DIR}/lens-*.md`. Each: a per-issue verdict table (CONFIRM/REFUTE/PARTIAL + evidence anchored to query output or file:line), plus a **NEW ISSUES** section.
2. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md`: the consolidated validated issue list (each with cross-lens verdict count + confidence), disputed claims reported as disputes (no vote-rule), and a ranked list of NEW issues ≥2 lenses agree on.
3. Publisher writes the synthesis to `docs/_meta/architecture/20260629-findings-validation-panel.md`.

## Scope

Read-only validation. Do NOT edit any code or the DB. Depth: 4 parallel lenses + 1 synthesizer. Budget: $5–15.
