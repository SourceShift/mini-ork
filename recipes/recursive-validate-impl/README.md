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
probes, checks hard-rule compliance, searches arxiv-libwit for current
techniques, and judges compliance with modern techniques for the feature class.
That keeps verification focused on both "does it work?" and "is this a modern,
defensible implementation?"
