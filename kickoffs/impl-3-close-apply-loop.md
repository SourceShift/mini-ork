# IMPL-3 — Close the apply loop: turn diagnoses into applied prompt changes (X1)

## Goal (one deliverable)

Build the missing **Apply → online-eval → Gate → Promote** stage that the panel identified as the
load-bearing root cause (X1). This is what finally makes BOTH GEPA suggestions and clustered gradient
patterns actually change a prompt — the shared piece that "uses gradients properly" and "integrates
GEPA" for real. Depends on IMPL-2's online evaluator.

## Background (validated)

Today the loop diagnoses and clusters but never acts: `pattern_records(output_type='prompt_change')`,
`emergent_patterns(status='proposed')` (39 rows), and gradient clusters all sit unconsumed —
`workflow_candidates`, `promotion_records`, `version_registry`, `textual_gradients` are all empty.
GEPA (G4) and gradients (Gr3) fail for the same reason: no consumer promotes a proposed change.

## Files / mechanisms in scope

- Reuse the existing lifecycle: `workflow_candidates` → shadow → promote (`bin/mini-ork-improve`,
  `bin/mini-ork-eval`, `bin/mini-ork-promote`, `lib/promotion_gate.sh`).
- Consume from: `pattern_records` (GEPA `prompt_change`), `emergent_patterns`/`gradient_records`
  (clustered prompt gradients).
- The online evaluator from IMPL-2.

## Acceptance criteria

1. **Apply consumer:** a step (in reflect or a new `bin/mini-ork apply` verb) that takes the
   highest-confidence proposed prompt change for a `(task_class, target)` and materializes it as a
   `workflow_candidates` row with the concrete prompt mutation.
2. **Online-eval gate:** the candidate is scored on a held-out set (reuse IMPL-2's evaluator) vs the
   current prompt; it is promoted only if it does not regress (non-regression gate) — reuse
   `promotion_gate` conjunction discipline. Applying WITHOUT a reward comparison is explicitly
   forbidden (panel: that recreates theater).
3. **Promotion writes the real prompt + a version:** on promote, the recipe's prompt file is updated
   and a `version_registry` entry recorded, so the change is auditable and reversible
   (release-engineering discipline).
4. **Suggest-safe by default:** the apply step is gated (dry-run / human-approval env) so it never
   silently rewrites prompts without the gate passing; quarantine on regression.
5. **End-to-end proof:** a test where a known-good gradient ("reviewer must cite evidence before
   verdict") flows cluster → candidate → online-eval → promote → the reviewer prompt file now
   contains the rule, and a regression case is quarantined.

## Out of scope

New diagnosis logic (that already works), cross-repo apply, auto-enabling on every run without the gate.

## Verification

- Unit + integration test of the full cluster → candidate → gate → promote path (happy + regression).
- Smoke: run apply on the existing `agent.reviewer.prompt` "evidence before verdict" cluster; confirm
  a candidate is created, evaluated, and either promoted (prompt file changed) or quarantined with a reason.
