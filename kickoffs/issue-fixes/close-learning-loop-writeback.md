# Close the learning-loop write-back: gradients → patterns → durable suggestions

## Context (grounded in DB analysis)
`internal-docs/research/2026-07-01-miniork-least-surprise-researcher-db.md`: researcher's
mini-ork extracted **8,951 `gradient_records`** but `pattern_records` = `emergent_patterns` =
`promotion_records` = **0**. The learn half runs; the improve→promote write-back is inert, so the
system contains surprise but never reduces it (flat weekly failure rate). Two concrete breaks:

1. **Pattern miner gated OFF.** `bin/mini-ork-reflect` runs `pattern_store_mine_from_traces` only
   when `MO_PATTERN_MINER=1` (default 0). Researcher never sets it → no `pattern_records`.
2. **Suggestions never persisted.** `lib/reflection_pipeline.sh:reflection_suggest_promotions`
   only `print`s JSON (the "N promotion suggestions generated" line); nothing writes them to a
   durable table, so each run's ~33 candidates vanish.

Note `promotion_evaluate` (`lib/promotion_gate.sh`) is the heavy benchmark-gated APPLY path — NOT
what to call here. This phase closes the RECORD/SURFACE write-back; apply/approve stays gated.

## Deliverables
1. **Default the pattern miner ON.** In `bin/mini-ork-reflect`, change the gate so the miner runs
   by default: `MO_PATTERN_MINER` default 0 → 1 (keep `MO_PATTERN_MINER=0` as the opt-out). This
   only writes `pattern_records` (read-as-context telemetry) — it does NOT change dispatch/exec, so
   it's safe for researcher + all consumers. Keep the window/min-cluster env knobs.
2. **Persist promotion suggestions durably.** After `reflection_run`/`reflection_suggest_promotions`
   in the reflect path, write each suggestion to a durable, queryable, evidence-linked row (status
   `suggested`/`proposed`) — reuse the existing `emergent_patterns` table (has a `status` column) OR
   `promotion_records` with a pre-benchmark `suggested` status, whichever fits the schema with least
   change. Include: pattern_id, description/label, frequency/strength, suggested type, and the
   `evidence_trace_ids`. Idempotent upsert (re-running reflect must not duplicate the same
   pattern_id). Add a `reflection_persist_suggestions` helper in `lib/reflection_pipeline.sh` and
   call it from the reflect flow.
3. Emit a one-line summary: `[learning] persisted N patterns, M suggestions`.

## Smoke / DoD (must pass)
- `tests/unit/test_learning_loop_writeback.sh`: seed a temp `state.db` with a handful of
  `execution_traces` rows (≥ min-cluster of the same task_class+status) and some `gradient_records`;
  run the reflect miner + suggestion-persist path; assert `pattern_records` > 0 AND the suggestions
  table (emergent_patterns/promotion_records) has ≥1 `suggested`/`proposed` row with non-empty
  `evidence_trace_ids`. Re-run and assert NO duplicates (idempotent).
- `bash -n bin/mini-ork-reflect lib/reflection_pipeline.sh` (+ any changed lib) clean.
- Existing reflect/pattern tests + `pytest` still green. The miner default-on must not break a
  reflect run on an empty DB (0 traces → 0 patterns, no error).

## Constraints (scope guard)
- Touch: `bin/mini-ork-reflect`, `lib/reflection_pipeline.sh`, optionally `lib/pattern_store.sh`
  (only if needed for the persist helper), and the new test. Do NOT invoke the benchmark-gated
  `promotion_evaluate`/`promotion_approve` apply path, and do NOT auto-apply any recipe/prompt
  change — this phase only RECORDS patterns + suggestions. Default execution behavior unchanged.
