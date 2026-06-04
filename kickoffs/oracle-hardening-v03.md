# Epic — Oracle Hardening for v0.3

**Status:** ready for dispatch (decomposable into 3 waves × 2-3 sub-epics each)
**Target:** `~/ps/mini-ork/` (upstream framework)
**Prereqs landed:** 5-commit OSS push (origin range `82164b1..72e54a3`) — recipe family-diversity swap + 2 self-audit synthesis publishes + 2 fix-specs + example kickoff
**Estimated total effort:** ~9-12 dev-days across 3 waves (Waves 1+2 parallelizable; Wave 3 sequential)
**Audit synthesis driving this:** `docs/refactor/synthesis-latest.md` (run-1780604422-58608)
**Research brief grounding 9 papers:** companion at downstream `docs/_meta/research/20260605-self-evolution-oracle-arxiv-summaries.md`

---

## Problem statement

The mini-ork framework's "self-improving" claim has a class-restricted truth. For task classes with deterministic oracles — `code_fix` (typecheck + targeted test), `db_migration` (apply + idempotent re-apply) — the `improve → eval → promote` loop is safe to auto-promote because the oracle is external ground truth, not an LLM panel verdict.

For task classes WITHOUT deterministic oracles — `research_synthesis`, `refactor_audit`, `blog-post`, `ui-audit` — the benchmark suite's promotion gate is LLM-judged by the same family distribution that produces candidates. When all four families share a systematic blind spot, the promotion gate cannot detect it: it measures **consensus of a coalition**, not external ground truth (Zietsman's circularity gap applied to the *evolution loop itself*).

Three things are now established:

1. **Formal**: [Zenil 2026](https://arxiv.org/abs/2601.05280) proves that if the proportion of exogenous grounding signal α_t → 0, the system undergoes degenerative dynamics (entropy decay + drift), both unavoidable in the limit.
2. **Empirical**: [DeVilling 2025](https://arxiv.org/abs/2510.21861) shows across 144 reasoning sequences × 3 model families × 4 task families that recursive self-evaluation without external feedback yields reformulation, not progress.
3. **Foundational**: [Setlur 2025](https://arxiv.org/abs/2502.12118) (ICML, 82 citations) proves test-time-compute scaling without verification or RL is suboptimal — the marginal return on more sampling collapses past a small budget.

The framework's own 4-lens self-audit (2026-06-04, synthesis published 2026-06-04) surfaced this open question as Section 5 of `docs/refactor/synthesis-latest.md`. This epic closes the gap with three waves of architectural + documentation work, ranked by leverage ÷ effort.

---

## Wave 1 — Honesty + cheap diagnostics (~2 dev-days, parallelizable)

Four sub-epics, each ~0.5 day, that together upgrade the framework's epistemic honesty + panel-failure detection. Highest leverage-per-day in the epic.

### W1-A — Positioning honesty patch (~0.5d)

**Sub-epic:** Add an explicit class-restriction paragraph to `docs/positioning/why-mini-ork.md`.

**Spec:**

Insert after the existing self-improvement section: a paragraph stating that auto-promotion via `mini-ork promote` is restricted to task classes with deterministic oracles (`code_fix`, `db_migration`). For task classes without deterministic oracles (`research_synthesis`, `refactor_audit`, `blog-post`, `ui-audit`, `ops-runbook`), promotion requires operator review — `mini-ork eval` produces a ranked candidate, `mini-ork promote --candidate <id>` is gated on a human signal.

Add to the same section a 2-row taxonomy table:

| Task class oracle | Promotion rule |
|---|---|
| Deterministic (typecheck, lint, migration apply, target test passes) | Auto-promote on oracle pass |
| LLM-judged only | Manual-promote-only; panel score is a recommendation rank |

**DoD:** `docs/positioning/why-mini-ork.md` includes the paragraph + table; `bin/mini-ork promote` emits a warning if invoked for a non-deterministic task class without `--operator-confirmed`.

**Citations:** Zenil 2026; Setlur 2025; DeVilling 2025.

---

### W1-B — ρ hard-block gate (~0.5d)

**Sub-epic:** Convert `panel_topology_telemetry` from observation-only to a hard-block in `bin/mini-ork-execute`.

**Spec:**

Current state: `lib/topology_metrics.sh:measure_topology` computes pairwise correlation ρ after each panel run and writes to `panel_topology_telemetry`. The values are visible but the synthesizer runs regardless.

New behavior: after `measure_topology`, if `rho >= 0.25` (Rajan 2025 submodularity precondition) OR if 2+ lenses route to the same model family per `config/agents.yaml`, abort the synthesizer node and emit run verdict `COALITION_ABORT`. The 4 lens reports are preserved (forensic value); the synthesis is not produced. Operator must either widen family diversity (configure new lenses) or accept the coalition-flagged outputs without synthesis.

**DoD:** `bin/mini-ork-execute` synthesizer step is gated on `rho < 0.25` AND `family_count == lens_count`; refactor-audit recipe regression test covers both abort paths.

**Citations:** Bertalanič 2026 (`2605.00914`) — empirical demonstration that homogeneous N-agent debate is WORSE than single-agent self-correction; Rajan 2025 (`2511.16708`) — submodularity precondition.

---

### W1-C — CW-POR diagnostic (~0.5d)

**Sub-epic:** Add Confidence-Weighted Persuasion Override Rate as a panel-health metric orthogonal to Krippendorff α.

**Spec:**

Implement `lib/cw_por.sh` exposing one function:

```bash
# mo_compute_cw_por <verdict_json>
#   Reads per-lens score variance + per-lens authority-signal extracted
#   from rationale text. Emits JSON:
#     {
#       "cw_por": <0.0-1.0>,
#       "threshold": 0.3,
#       "verdict": "panel_healthy" | "authority_capture_suspected",
#       "rationale": "<one-sentence summary>"
#     }
```

Wire into the existing reviewer-output pipeline. When `cw_por > 0.3` AND `krippendorff_alpha > 0.6` simultaneously (high agreement + high authority-capture signal), flag as `consensus_of_coalition` and escalate to operator review.

**DoD:** `lib/cw_por.sh` exists; unit test covers (a) low CW-POR clean panel, (b) high CW-POR with high α (the coalition signal), (c) malformed verdict JSON.

**Citations:** Agarwal & Khanna 2025 (`2504.00374`) — original CW-POR paper, 9 citations.

---

### W1-D — Selective-feedback conjunction in promotion gate (~1d)

**Sub-epic:** Extend `lib/promotion_gate.sh` so synthesis-class candidates must satisfy a quality conjunction, not a single panel score.

**Spec:**

Current behavior: `promotion_gate.sh` checks `utility_delta > 0` and forwards to LLM-judged eval. For non-deterministic-oracle task classes this is the single failure point this epic exists to mitigate.

New behavior for synthesis-class candidates: require ALL THREE of the following before auto-promote-eligible:

1. Panel rubric score ≥ threshold (default 80/100)
2. CW-POR ≤ 0.3 (no authority-capture signal)
3. ≥ 1 independent quality signal: citation density per lens, file-coverage delta, finding cardinality > N — at least one structural metric independent of the panel verdict

Conjunction beats single-signal because correlated failures across the panel are less likely to also fool the second structural signal. This is the Anti-Ouroboros pattern (Adapala 2025): the selective filter IS the oracle slice that makes recursive feedback resilient instead of degenerative.

**DoD:** `lib/promotion_gate.sh` rejects synthesis-class candidates failing any of the 3 conditions; deterministic-oracle task classes unchanged (still single-pass typecheck gate).

**Citations:** Adapala 2025 (`2509.10509`) — Anti-Ouroboros selective feedback.

---

## Wave 2 — Anchor + adaptive controls (~3-5 dev-days, parallelizable)

Two sub-epics that introduce external grounding signals into the loop.

### W2-A — Held-out anchor corpus (~3-5d)

**Sub-epic:** Author a small fixed corpus of human-graded reference findings per synthesis-class recipe. Every promoted candidate gets evaluated against the anchor; the anchor doesn't replace the panel — it constrains drift.

**Spec:**

For each of `refactor_audit`, `research_synthesis`, `blog-post`, `ui-audit` recipes:

1. Hand-author 10-20 reference findings on a fixed input corpus (chosen once per recipe, frozen).
2. Each reference finding has: `id`, `severity`, `evidence_anchor` (file:line), `expected_lens_pickup` (which lens(es) should surface this), `rationale`.
3. Add `bin/mini-ork-eval --anchor` flag: when set, eval mode runs the candidate against the anchor and emits a recall metric (what % of anchor findings did the candidate's panel surface) + a precision metric (what % of candidate-only findings would a human accept).
4. Surface `anchor_recall` and `anchor_precision` as columns in `task_runs` table for trend analysis.

**DoD:** 4 recipes × 10-20 anchor findings authored; `bin/mini-ork-eval --anchor` runs cleanly; `mini-ork metrics` surfaces anchor_recall + anchor_precision trend.

**Citations:** Wang et al 2026 (`2601.05184`) — Self-Consuming Performative Loop, held-out-anchor remedy.

---

### W2-B — Adaptive stability detection in debate (~1.5d)

**Sub-epic:** Replace fixed-N panel rounds with adaptive convergence detection.

**Spec:**

Today's recipes run fixed lens count + 1 synthesis pass. Replace with adaptive stop in `mo_run_panel_review`:

1. Dispatch lenses round-robin; after each lens completes, compute pairwise output similarity with already-completed lenses.
2. Three regimes:
   - **Gradual convergence** (similarity rises smoothly toward stability) — keep going, expected behavior
   - **Instant convergence within 1-2 lenses** — flag as `cheap_consensus_suspected` (sycophancy risk) and escalate
   - **No convergence after threshold lenses** — flag as `unreconcilable_disagreement` and escalate
3. Average panel cost drops 20-40% on easy cases (early stop); hard cases get full N. Implementation: extend `bin/mini-ork-execute` synthesizer step to read per-iter pairwise similarity from a small classifier trained on a few hundred prior debate trajectories.

**DoD:** `mo_run_panel_review` emits per-lens convergence trajectory; recipe regression test covers all 3 regimes; `mini-ork metrics` surfaces average lens-count-to-stability per recipe.

**Citations:** Hu et al 2025 (`2510.12697`) — Multi-Agent Debate for LLM Judges with Adaptive Stability Detection, 3 citations.

---

## Wave 3 — Mechanical verifier (recall-floor oracle, ~2-3 weeks, sequential)

The big lift. Builds the recall-floor oracle the blog post calls option (a) — turns `refactor_audit` from "LLM-judged both ends" into "recall-anchored + LLM-judged precision."

### W3 — Mechanical citation+coverage verifier (~2-3wk)

**Sub-epic:** Author `lib/citation-verifier-mechanical.sh` + integrate into `bin/mini-ork-execute` post-lens, pre-synthesis.

**Spec:**

For each lens finding of the form *"file X line range L1-L2 contains pattern P"*:

1. Mechanically re-read file X at lines L1-L2 and verify pattern P matches (regex / AST query / sed slice depending on pattern shape).
2. Findings that fail the mechanical check are filtered OUT before the synthesizer sees them — they don't propagate into the verdict.
3. Findings that pass become **evidence-anchored**; the synthesizer's rationale field for each surviving finding cites the mechanical-verify trace ID.
4. Persistence: write per-finding verify result to `mechanical_citation_log` table (new migration) for trend analysis + adversarial-bug-injection follow-up work.

This is RECALL-only — it filters fabrications (a lens claims pattern P at line N when P doesn't appear there) but doesn't detect missed bugs (lens overlooked a real bug elsewhere in the file). Precision (whether the LLM's interpretation of the verified evidence is correct) remains LLM-judged. Recall-floor oracle + LLM-judged precision is still better than today's pure-LLM both ends — it's the bridge between Wave 1's diagnostics and a real deterministic oracle.

**DoD:**

- `lib/citation-verifier-mechanical.sh` ships with regex / AST / sed engines; ≥ 80% finding-shape coverage tested against the run-1780604422-58608 lens outputs (real fixture data)
- `bin/mini-ork-execute` calls verifier post-lens, pre-synthesis; fabrication-class findings filtered out
- Migration `0016_mechanical_citation_log.sql` adds the per-finding verify log
- `mini-ork metrics` surfaces per-recipe `fabrication_rate` (findings filtered / findings produced)
- Regression test: inject a fabricated finding (file:line citation that doesn't match) into a fixture lens output; verify the synthesizer never sees it

**Citations:** Sistla et al 2025 (`2509.26546`) — Towards Verified Code Reasoning by LLMs; Ficek 2025 (`2502.13820`) — Scoring Verifiers: Evaluating Synthetic Verification.

---

## Recommended dispatch shape

### Wave 1 dispatch (~1 mini-ork-run for all 4 sub-epics)

Dispatch all 4 sub-epics in parallel via the `bdd-first-delivery` recipe with `workers: [glm, kimi, codex, minimax]` per sub-epic + `reviewer: opus`. Sub-epics are file-disjoint (W1-A touches docs/; W1-B touches bin/mini-ork-execute + lib/topology_metrics.sh; W1-C creates lib/cw_por.sh; W1-D touches lib/promotion_gate.sh) — zero merge-conflict surface. Estimated panel cost: 4 sub-epics × ~$0.90/sub-epic ≈ ~$3.60.

### Wave 2 dispatch (~1 mini-ork-run for 2 sub-epics)

W2-A held-out anchor corpus requires human-graded content for the anchor itself — author manually first, then dispatch the `bin/mini-ork-eval --anchor` wiring as a code sub-epic. W2-B adaptive stability is a clean panel target. Estimated panel cost ~$1.80 (just W2-B; W2-A is hand-authored).

### Wave 3 dispatch (multiple runs, sequential)

W3 mechanical citation+coverage verifier is heavy enough to merit its own decomposition pass. Suggest a sub-decomposition via `bin/mini-ork-classify + mini-ork-plan` to identify sub-sub-epics by engine (regex / AST / sed / migration / metrics) — likely 4-5 sub-sub-epics × ~2-3 days each.

---

## Phase A-M tracker delta after this epic completes

| Phase | Before this epic | After Wave 1 | After Wave 2 | After Wave 3 |
|---|---|---|---|---|
| **L** Recursive self-audit | ✅ proof shipped 2026-06-04 | unchanged | unchanged | enhanced — audit findings now mechanically verified |
| **M** Audit-recommendation-verification | ✅ (lens-completeness.sh runs) | enhanced — CW-POR + ρ hard-block in gate | enhanced — anchor recall/precision in trend | enhanced — fabrication_rate in trend |
| **N** *(new)* Promotion-class taxonomy enforced | — | ✅ (W1-A + W1-D) | ✅ + anchor gate | ✅ + mechanical verifier |
| **O** *(new)* Panel-failure detection | — | ✅ (CW-POR + ρ hard-block) | ✅ + adaptive stability | ✅ + fabrication filter |

This epic introduces Phases N + O — promotion-class-taxonomy and panel-failure-detection are the missing pieces between Phase M (verify the audit produced) and Phase E (LIVE benchmark_run, still deferred).

---

## Bibliography (9 papers from research brief)

Per-paper rationale + so-what mapping is documented in the downstream research brief at `docs/_meta/research/20260605-self-evolution-oracle-arxiv-summaries.md`.

1. Zenil, H. (2026). *On the Limits of Self-Improving in Large Language Models: The Singularity Is Not Near Without Symbolic Model Synthesis.* arXiv:2601.05280
2. DeVilling, B. (2025). *The Mirror Loop: Recursive Non-Convergence in Generative Reasoning Systems.* arXiv:2510.21861
3. Adapala, S. T. R. (2025). *The Anti-Ouroboros Effect: Emergent Resilience in Large Language Models from Recursive Selective Feedback.* arXiv:2509.10509
4. Wang, Y., Cai, Z., Bao, Y., Zhang, X., & Liu, Y. (2026). *Observations and Remedies for Large Language Model Bias in Self-Consuming Performative Loop.* arXiv:2601.05184
5. Hu, T., Tan, Z., Wang, S., Qu, H., & Chen, T. (2025). *Multi-Agent Debate for LLM Judges with Adaptive Stability Detection.* arXiv:2510.12697
6. Bertalanič, B., & Fortuna, C. (2026). *The Cost of Consensus: Isolated Self-Correction Prevails Over Unguided Homogeneous Multi-Agent Debate.* arXiv:2605.00914
7. Agarwal, M., & Khanna, D. (2025). *When Persuasion Overrides Truth in Multi-Agent LLM Debates: Introducing a Confidence-Weighted Persuasion Override Rate (CW-POR).* arXiv:2504.00374
8. Sistla, M., Balakrishnan, G., Rondon, P., Cambronero, J., Tufano, M., & Chandra, S. (2025). *Towards Verified Code Reasoning by LLMs.* arXiv:2509.26546
9. Setlur, A., Rajaraman, N., Levine, S., & Kumar, A. (2025). *Scaling Test-Time Compute Without Verification or RL is Suboptimal.* arXiv:2502.12118 (ICML 2025, 82 citations)
