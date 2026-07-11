# Propose fixes for the validated GEPA + gradient issues (4-lens panel)

## Problem

A prior 4-lens panel validated a set of issues in mini-ork's self-improvement loop (GEPA +
the reflection/gradient pipeline). Now each lens must propose **how to fix** the validated
issues — concrete, implementable, mapped to the real code — from its own angle. This is a
design panel, not a critique: the output is a fix plan.

**Read both, absolute paths:**
- Validated issues (panel-1 synthesis): `/Volumes/docker-ssd/ps/mini-ork/docs/_meta/architecture/20260629-findings-validation-panel.md`
- Original defect report: `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-10-gepa-gradient-issues.md`

**Code to design fixes against (absolute paths):**
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/gepa.py` and `miniork_adapter.py`
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-reflect`
- `/Volumes/docker-ssd/ps/mini-ork/lib/gradient_extractor.sh` and `lib/reflection_pipeline.sh`
- Existing lifecycle to reuse: `workflow_candidates` → shadow → promote (`bin/mini-ork-improve/eval/promote`, `lib/promotion_gate.sh`).

## The issues to fix (use panel-1's verdicts; skip any it REFUTED)

- **G2** GEPA cannot accept a mutation (offline hash-scoring). Needs an **online evaluator** that can score a *new* prompt, + a real mutation model. This is the unblock-everything fix.
- **G1/G3/G4** enable path, real model, apply path.
- **Gr1** runaway re-extraction → a per-trace watermark / high-watermark so a trace is mined once.
- **Gr2** dedup keyed on trace-specific `signal` → re-key dedup on the *fix* (normalized `suggested_change` per `target`), or semantic (embedding) dedup.
- **Gr3 / X1** the missing **apply → gate → promote** loop that turns clustered gradients into applied prompt/recipe changes.

## What each lens proposes (fixes from your angle)

- **codex lens (IMPLEMENTATION):** concrete code-level design for each fix — exact functions/files to change, the watermark mechanism, the dedup-normalization function, the online-eval harness for GEPA. Sketch the diff shape and the minimal change set. Prefer reuse over rewrite.
- **kimi lens (CORRECTNESS):** will each proposed fix actually work? Edge cases, failure modes, why a naive version breaks (e.g. does normalizing the dedup key over-merge distinct fixes? does the watermark drop legitimate re-analysis?). Propose the guardrails.
- **minimax lens (PERFORMANCE / SEQUENCING):** cheapest-highest-impact ordering. Which fixes are small self-contained bug fixes (Gr1/Gr2) vs larger builds (G2 online-eval, X1 apply loop)? Cost and blast-radius of each. The phased plan.
- **opus lens (ARCHITECTURE):** design the **apply → online-eval → gate → promote** loop properly (closes G2, G4, Gr3, X1 at once). How gradients/patterns become `workflow_candidates`, how non-regression is enforced, how it stays suggest-safe. The end-state architecture.

## Definition of Done

1. Four lens reports at `${MINI_ORK_RUN_DIR}/lens-*.md`, each proposing concrete fixes for the validated issues from its angle, anchored to real file:line.
2. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md`: a **ranked, sequenced fix plan** — per issue: the recommended fix, the files it touches, effort (S/M/L), risk, and dependencies; consensus-marked where ≥2 lenses agree; disputes reported as disputes. Lead with the smallest changes that stop the bleeding (Gr1/Gr2) and the single unblock-everything change (G2 online-eval).
3. Publisher writes the synthesis to `docs/_meta/architecture/20260629-findings-validation-panel.md`.

## Scope

Design + proposal only — do NOT implement or edit code. 4 parallel lenses + 1 synthesizer. Budget: $5–15.
