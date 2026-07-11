# IMPL-1 — Make the gradient pipeline idempotent + dedup effective ("gradients used properly", part 1)

## Goal (one deliverable)

Stop the reflection/gradient pipeline from generating a runaway pile of duplicate gradients, so the
diagnoses it produces are usable. This is the safe, high-confidence first slice validated by the
4-lens panel (`docs/_meta/architecture/20260629-findings-validation-panel.md`). It does NOT touch
GEPA or add an apply path — those are IMPL-2 and IMPL-3.

## Background (validated evidence)

In the researcher DB there are **9,777 gradient rows from only 1,603 traces** (worst trace re-mined
29×), and `agent.reviewer.prompt` has **172 rows, 0 collapsed** despite ~57 saying the same thing.
Root causes confirmed by the panel:
- **Gr1** `extract_gradients` has no per-trace watermark; `reflect --since 24h` overlaps every run, so
  the same traces are re-mined repeatedly. `__reflect__` traces alone account for ~926 rows.
- **Gr2** dedup keys on lexical form and, critically, the cross-target pass **skips same-target rows**,
  so concentrated same-target duplicates (the 172 reviewer rows) are never compared; dedup is also
  per-`(task_class,target)` so the same insight across task classes never collapses.

## Files in scope (absolute paths)

- `/Volumes/docker-ssd/ps/mini-ork/lib/gradient_extractor.sh` — extraction + `gradient_store` containment dedup.
- `/Volumes/docker-ssd/ps/mini-ork/lib/reflection_pipeline.sh` — `reflection_deduplicate` (difflib) + `extract_gradients` stage.
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-reflect` — the `--since` window default.

## Acceptance criteria

1. **Idempotent extraction (Gr1):** `extract_gradients` skips any `trace_id` that already has a
   gradient in `gradient_records.evidence` (per-trace watermark), so re-running reflect over an
   overlapping window does not create duplicate gradients. Add a test proving a second reflect over
   the same window adds 0 new rows.
2. **Exclude reflect-noise (Gr1):** `__reflect__` (and other framework-internal `__*__`) traces are
   excluded from gradient extraction. Test: a `__reflect__` trace produces 0 gradients.
3. **Same-target semantic dedup (Gr2):** the dedup must collapse near-identical gradients that share
   the same `target` but differ only in trace-specific tokens (numbers, `$costs`, durations,
   trace-ids, timestamps). Normalize those tokens out of the dedup key, and ensure the same-target
   case is actually compared (not skipped). Test: 3 reviewer gradients that differ only in
   "2.7min/$1.62" vs "633s/$3.53" collapse to 1.
4. **No behavioral regression:** existing reflection_pipeline / gradient_extractor tests still pass;
   distinct insights (different `suggested_change` intent) are NOT over-merged.
5. Bash/Python parity preserved where both copies exist.

## Out of scope (do NOT do here)

GEPA changes, the apply→gate→promote loop, cross-`(task_class)` dedup (leave a TODO referencing X1),
schema migrations beyond what's needed for the watermark.

## Verification

- Unit tests for each acceptance criterion above (idempotent re-run, `__reflect__` exclusion, semantic collapse).
- A smoke run: extract twice over the same trace window; assert row count stable on the second pass.
