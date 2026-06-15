# recursive-validate-impl

`recursive-validate-impl` is a generic recipe for feature work that needs a
closed implementation loop: plan, implement, validate, reflect on failures,
replan, and retry until the Definition of Done probes pass or a budget/loop
guard stops the run. Use it when a kickoff describes one or more technical
features with machine-checkable success commands, hard rules, and enough scope
for deterministic verifiers to inspect the touched files.

The recipe uses a five-tier quality stack inspired by the canonical recursive
verification design: compile/typecheck, scoped unit tests, property or mutation
checks when available, a heterogeneous tier-4 panel, and final DoD probe review.
Cheap deterministic checks run first; the LLM panel only runs after lower tiers
are green. The reflector and replanner convert failures into the next plan
mutation, while `divergence_kill` prevents repeating the same failed iteration.

Tier 4 is quality-aware, not just completion-aware. Each panel lens reruns DoD
probes, checks hard-rule compliance, searches arxiv-search-tool for current
techniques, and judges compliance with modern techniques for the feature class.
That keeps verification focused on both "does it work?" and "is this a modern,
defensible implementation?"

## Fail-fast quorum (added 2026-06-14)

Between the 4 parallel tier-4 lens reports and the tier-4 synthesizer sits
a `tier4_quorum` verifier node. It counts how many of `tier4-{glm,kimi,
codex,minimax}.md` exist and are non-empty (size > 100 bytes). If the
count is less than `MO_TIER4_QUORUM` (default **3 of 4**), the gate
emits `pass=false` and the recipe edge escalates straight to `reflector`
instead of letting `tier4_synth` hang waiting for missing data.

This closes a stall pattern observed in two earlier runs where 2 of 4
lens reports were silently absent and the synthesizer either looped on
partial input or waited indefinitely. The quorum allows 1 lens failure
(rate-limit, network blip, single-provider outage) without blocking the
loop; 2+ failures escalate to reflector for replan or operator review.

Tune via env:
- `MO_TIER4_QUORUM` — minimum non-empty lens reports required (default 3)
- `MO_TIER4_LENS_MIN_BYTES` — size threshold for "non-empty" (default 100)
