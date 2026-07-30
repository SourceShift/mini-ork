# Plan — truth-grounded eval: from LLM judge to a verification stack

*2026-07-30 · supersedes the "LLM judge writes the reward" shape of the Step-3
eval node (`internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`).*

## Why

LLM-as-judge is unreliable as a reward source, and the literature is blunt about
it. On mini-ork's exact setup — critic-free GRPO on code — using an LLM as the
reward scored **5.8 points below** execution-grounded reward (76.3 vs 82.1;
EGCA, arXiv 2603.16158), and that paper uses the LLM only to *localize/explain*
an error, explicitly "not as a correctness oracle." Direct measurement of
verifier vs judge false-positives on MATH500: a **rule-based verifier had 0
false-positives out of 500; an LLM verifier had 168** (arXiv 2510.00915). The
fix for "the judge is unreliable" is not a better judge — it is **demoting the
judge** beneath signals that are true by construction.

## The stack (most → least trusted). Reward flows from the top.

| Layer | Signal | Source in mini-ork | Trust |
|---|---|---|---|
| **0 Execution** | tests + constraints actually run: `R = pass_fraction × constraint_ind` | `recipes/*/verifiers/*.py`, `cli/verify.py` verdict (`pass/partial/fail/vacuous`) | highest |
| **1 Noise model** | verifiers are ALSO noisy (FN 38%, FP 35–68%); de-bias the reward | `verifier_fp_rate` (mig 0025) + FN sibling; backward correction | high |
| **2 Metamorphic** | invariances across related runs (gold-free); catches "passed one test via a coverage gap" | new: metamorphic-relation verifier | medium |
| **3 Judgment** | decorrelated JURY, veto-only, contested cases only | coalition gate + Krippendorff-α + 4-family routing; the Phase-0 judge | lowest |

Grounding: 2603.16158 (EGCA, execution > judge, exec-only oracle), 2510.00915
(imperfect verifiers + backward/forward gradient correction + online FN
estimation via appeals), 2603.24774 / 2510.26423 (metamorphic + oracle
synthesis, gold-free), 2607.10139 (jury > PRM), 2510.20369 (escalate to a strong
judge only when uncertain), 2506.09443 (LLM judges are manipulable).

### Layer 1 — the backward correction (faithful to 2510.00915)

For an observed execution reward `R ∈ [0,1]` with verifier false-positive rate
`ρ_FP` (accepts a wrong result) and false-negative rate `ρ_FN` (rejects a correct
one), the unbiased surrogate reward is:

```
R_corrected = (R − ρ_FP) / (1 − ρ_FP − ρ_FN)          (clamp to [0,1])
```

Guard: if `1 − ρ_FP − ρ_FN ≤ 0` (rates over-estimated), skip the correction and
return `R` — the inverse factor amplifies variance, and the paper shows
over-estimation is where it degrades. Rates come from `verifier_results`
annotations (`is_false_positive` / `is_false_negative`) when labeled; otherwise
conservative priors (`MO_EVAL_VERIFIER_FP_PRIOR` / `_FN_PRIOR`), logged as priors.

### The demotion (how the judge is confined)

- **Execution signal present** (≥1 verifier reported a pass/fail): reward =
  `noise_correct(R_exec)`, `reward_source='eval-exec@v1'`. The judge runs only
  for what execution can't measure (groundedness, safety) and is **veto-only** —
  it multiplies the reward by `min(safety, groundedness) ≤ 1`, so it can pull the
  reward down but never above the execution ceiling. Mirrors the existing
  anti-Goodhart rule in `writeback.py` (reward verified execution; a judge can
  veto, never fabricate a positive).
- **No execution signal** (`vacuous`, or no verifiers ran): fall back to
  judge-only (`reward_source='eval-judge@v1'`), logged as lower-trust. A
  `vacuous` run must never earn a high reward (anti-false-completion).

## Phasing

- **Phase 0 (shipped, `fe9ee3e2`)** — advisory judge node, per-axis + fail-open. Becomes Layer 3.
- **Phase 1 (this change)** — Layers 0+1: execution-grounded reward as the backbone, backward noise correction, judge demoted to veto-only. `reward_source` splits into `eval-exec@v1` / `eval-judge@v1`.
- **Phase 2 (engine shipped)** — metamorphic-relation verifier (gold-free amplification): `mini_ork/learning/metamorphic.py` (engine: relations + universal determinism/immutability checks; execution-grounded verdict via `to_verifier_json()` → feeds Layer 0 as one more `verifier_*.json`) + `recipes/code-fix/verifiers/metamorphic.py` (spec-driven via `MO_METAMORPHIC_SPEC`; a vacuous no-op without a spec). Proven to catch a coverage-gap cheat that a single extensional test passes. **Safe auto-proposal shipped** (`mini_ork/learning/metamorphic_proposer.py`): an LLM SELECTS relations as VALIDATED DATA — names from the vetted `RELATION_LIBRARY` whitelist + JSON seed inputs — never executable code. `parse_proposal` drops any unknown/garbage name (the safety boundary; tested against `__import__` / `rm -rf` names), and the verifier gained a data-only JSON-spec path (`MO_METAMORPHIC_SPEC_JSON`) that resolves relations by name and imports only the run's own patched target. No model-authored code is ever `exec`'d (arXiv 2603.24774 — LLM proposes, execution certifies). Library: determinism, commutativity, negation_invariance, order_invariance. **Final connection (open):** a recipe proposer node that dispatches the LLM, identifies the changed function from the diff, and writes the JSON spec before the metamorphic verifier — target-identification for arbitrary code is the remaining flaky part, kept out until it's robust rather than shipped as theater.
- **Phase 3 (jury shipped)** — the veto now comes from a DECORRELATED PANEL, not one judge. Set `MO_EVAL_JURY_LANES=lane1,lane2,…` (different model families) and the eval node dispatches each, aggregates the veto by median consensus (`panel_consensus`, robust to one bad lens; 2607.10139), and gates on inter-judge agreement (`panel_agreement`, the Krippendorff pairwise-disagreement kernel): below `MO_EVAL_JURY_ALPHA_MIN` (default 0.5) the panel **abstains** — the execution reward stands rather than applying a veto the judges can't agree on (2510.20369). Degenerates cleanly to a single judge when one lane is set. **Selective escalation shipped:** on a hung jury, `MO_EVAL_JURY_ESCALATE_LANE` dispatches one strong tiebreaker judge whose veto decides (the "ask a strong judge when uncertain" rail, 2510.20369), rather than silently abstaining; and a jury drawn from a single model family logs a not-decorrelated warning via the `coalition_gate` family map (advisory, never blocks).
- **Phase 4** — online FN estimation ("appeals"): a cheap lane re-checks suspected false-negatives to estimate `ρ_FN` live and feed `verifier_results`.

## DoD (Phase 1)

A `code-fix` run derives `reward_value` from its verifiers (not the judge),
noise-corrected; the judge can only downgrade; a vacuous/no-verifier run falls
back to judge-only and is flagged; unit + integration tests cover execution
reward, the backward correction (incl. the over-estimation guard), the veto, and
the fallback.
