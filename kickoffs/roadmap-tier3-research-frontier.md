# Tier 3 — Research frontier roadmap

These epics are **research-open** — the underlying problem is unsolved
in the field, not just in mini-ork. Solo engineering will not close
them. Ship them as **research collaborations** with Apollo Research,
Redwood Research, METR, UK AISI, or academic groups.

Mini-ork's role: provide the substrate, run the experiments, publish
the findings. Partners bring the theoretical grounding.

Source-of-truth grounding: docs/RSP.md:1 §§ 1-9 (capability axes +
tripwires + known gaps).

The autonomous scheduler should NOT walk these. Each requires an
external collaboration signed off by the maintainer before any code
lands.

## T3-A Formal sandbox verification (id: t3-a-formal-sandbox-verification)

**Problem.** Mini-ork's sandbox adapters at lib/sandbox/local.sh:1 and
lib/sandbox/omnigent-bridge.sh:68 are improvised. No formally verified
guarantee that a contained agent cannot exfiltrate. The field has no
verified agent sandbox either.

**Goal.** Specify a containment property in a formal language (TLA+ or
P) for one chosen sandbox backend, prove the property holds under a
stated threat model, embed the proof artifact in the repo, gate that
backend behind the verified-mode flag.

**Collaboration candidates.** Galois Inc, Oxford GovAI containment-
formalism track, MIT CSAIL formal-methods group.

**Out of scope for the kickoff.** Verifying ALL sandbox backends.
Pick one backend (local OR modal OR daytona). Pick one threat-model
(filesystem-escape OR network-escape).

**Done when.** TLA+ or P specification merged at
`formal/sandbox-<backend>.tla`, proof-check command wired into CI,
`MO_SANDBOX_REQUIRE_VERIFIED=1` env-var refuses to dispatch unless the
chosen backend matches a verified spec.

**Effort estimate.** 6-12 months with one academic collaborator.

## T3-B Compositional reliability bounds (id: t3-b-compositional-reliability)

**Problem.** Chain N agents; error compounds. 95% per-step × 20 steps =
36% end-to-end. The field has no theorem bounding compositional failure
rate from per-agent failure rate as a function of dependency topology.
Mini-ork's heterogeneous-family panel at lib/coalition_gate.sh:49 +
Krippendorff-α gate at lib/krippendorff_alpha_gate.sh:56 is a partial
empirical defense but lacks theoretical bound.

**Goal.** Derive (or adopt from existing literature on Bayesian network
reliability) a compositional reliability theorem giving a tight upper
bound on `P(workflow failure)` given:
- per-node `P(failure)` measured by mini-ork's eval suite
- dependency DAG of the recipe
- gate-strength of intermediate verifiers at lib/coalition_gate.sh:49

Embed the predictor as `lib/reliability_predict.sh` callable before
dispatch.

**Collaboration candidates.** Stanford CRFM, CMU SCS, Berkeley Sky
Computing Lab.

**Out of scope.** Optimizing the workflow to maximize predicted
reliability (separate epic). Predicting reliability of single-node
tasks (covered by per-node eval).

**Done when.** Theorem statement + proof sketch in
`docs/reliability-bound.md`, mechanical predictor in
`lib/reliability_predict.sh`, agreement with empirical failure rate
≥ 0.85 across 50 test workflows.

**Effort estimate.** 4-9 months with one PhD-student collaborator.

## T3-C Cross-agent trust protocol (id: t3-c-cross-agent-trust)

**Problem.** When does Agent A trust Agent B's verifier signal? Today
coalition gate at lib/coalition_gate.sh:49 is unweighted majority vote —
assumes equal trust. Real-world heterogeneous panels have differential
trust based on track record. The field has no agreed protocol for trust
composition across heterogeneous verifiers.

**Goal.** Specify a trust-protocol DSL where each verifier declares:
- training family + version
- track record (rolling F1 on labeled eval set)
- declared blind spots

Compose trust at gate evaluation using Beta-distribution Bayesian
update informed by track record. Embed as `lib/trust_compose.sh`.
Replace unweighted-majority coalition gate at lib/coalition_gate.sh:49
with track-record-weighted gate behind opt-in flag.

**Collaboration candidates.** Anthropic alignment team, OpenAI
preparedness team, Redwood Research.

**Out of scope.** Adversarial-robustness against Sybil verifiers
(separate research). Trust composition across recipes (initially
within one recipe only).

**Done when.** Trust DSL spec at `docs/trust-protocol.md`,
`lib/trust_compose.sh` wired into lib/coalition_gate.sh:49 behind
`MO_TRUST_WEIGHTED=1`, empirical study shows weighted gate has lower
false-pass rate on adversarial test set.

**Effort estimate.** 3-6 months with one industry-research
collaborator.

## T3-D Interpretability hooks (id: t3-d-interpretability-hooks)

**Problem.** Mini-ork dispatches opaque LLM calls via
lib/llm-dispatch.sh:1162. No visibility into what the model was
attending to when it produced its output. Interpretability has
progressed (sparse autoencoders, circuit tracing) but explains <10%
of model behavior as of Apr 2024 Anthropic paper.

**Goal.** Specify a hooks API at the dispatch boundary at
lib/llm-dispatch.sh:1162 that — when the dispatch target is a model
where SAE activations are available — captures per-call SAE activations
into `state.db` for later analysis. Research substrate, not a runtime
safety mechanism (today's SAEs not yet reliable enough to gate on).

**Collaboration candidates.** Goodfire, Neuronpedia, Apollo Research,
Anthropic interp team.

**Out of scope.** Gating dispatch decisions on SAE activations
(premature). Building our own SAE training pipeline (use existing
providers).

**Done when.** Hooks API documented at `docs/interp-hooks.md`,
`lib/interp_capture.sh` writes SAE activations to
`state.db:interp_traces` when configured, one notebook in `notebooks/`
demonstrates analysis.

**Effort estimate.** 4-8 months with one academic collaborator.

## T3-E Negative-trajectory curation taxonomy (id: t3-e-negative-trajectory-taxonomy)

**Problem.** Tier 2 ships the negative-trajectory training step (model
produces deliberately harmful attempt, critiques, revises) — see
kickoffs/roadmap-tier4-ecosystem-launch.md:1. Which harmful trajectories
teach which lessons? Curating the harm-set is a research problem — the
field has no agreed taxonomy.

**Goal.** Build a taxonomy + curation pipeline mapping harmful
trajectory type → safety capability gained. Axes (placeholder):
- Goal misgeneralization (RL-style)
- Specification gaming (reward-hacking)
- Deceptive alignment (Apollo's research line)
- Multi-step manipulation
- Capability sandbagging

For each axis, generate fixture set of contained harmful trajectories
under the safety_events emit contract at lib/safety_events.sh:1,
measure capability gain when added to recursive-self-improve training
set, publish taxonomy + fixtures.

**Collaboration candidates.** Apollo Research, Redwood Research, METR,
Owain Evans group at Oxford.

**Out of scope.** Building harms beyond the academic-research category.
No biosecurity uplift, cyber offense, or CSAM-adjacent trajectories
regardless of academic justification. Pre-screen with maintainer.

**Done when.** Taxonomy at `docs/negative-trajectory-taxonomy.md`,
fixtures at `fixtures/negative-trajectories/`, integration with Tier 2
Item 5 shows measurable capability gain on the corresponding axis.

**Effort estimate.** 6-12 months with one alignment-research
collaborator.

---

## Common preconditions for ALL Tier 3 epics

1. Tier 2 fully shipped (docs/RSP.md:1 § 9 known-gaps cleared).
2. Public RSP commitment at docs/RSP.md:1 in place.
3. Maintainer signs off on each collaboration individually.
4. Each epic defines a kill-criterion before research starts.

## Why these stay in Tier 3 not Tier 2

Tier 2 is "things one engineer can ship in 8 weeks." These five items
are each 3-12 months elapsed time WITH a research collaborator. Solo
eng work will not produce useful output because the underlying problems
are unsolved in the field.
