# Kickoff: PRM vs ORM for multi-step LLM agent training

## Topic

For training and routing **multi-step** LLM agents (planner → tool-use →
verify → review pipelines like mini-ork's own), is a **process reward model**
(PRM — per-step rewards) actually superior to an **outcome reward model**
(ORM — single end-of-trajectory reward), or does the advantage collapse once
you account for reward-hacking, annotation cost, and credit-assignment noise?

Anchor the synthesis on the 2023-2026 literature and shipped systems:
- PRM800K / "Let's Verify Step by Step" (Lightman 2023) and its successors
- GRPO / group-relative advantage (DeepSeekMath 2024) as an ORM-with-baseline
- process-reward critiques: reward hacking, the "first-error" vs "soft" PRM
  debate, and whether step-labels generalise across task families
- real agent frameworks (SWE-agent, OpenHands, Devin-likes) and which reward
  shape they actually train/route on

This is deliberately a **contested** question: the four lenses should *disagree*
on the bottom line, so the synthesis must report dispute honestly rather than
average it away. That contestedness is the point — it makes per-lens quality
differences sharp enough to be measurable.

## Definition of Done

The recipe produces, under `${MINI_ORK_RUN_DIR}/`:

1. Four lens reports, each meeting its citation floor:
   - `lens-glm.md` — 10-25 recent web/vendor/news sources, each with
     date + author + TL;DR + a confidence tag
   - `lens-kimi.md` — 8-15 arxiv papers with methodology + reported effect
     size + replication/contradiction status + citation chain
   - `lens-codex.md` — 8-15 public implementations with `file:line` evidence
     of which reward shape they actually train/route on
   - `lens-opus.md` — 1500-2500 word essay in 6 sections: history,
     conventional wisdom, the dissent, edge cases (reward hacking /
     annotation cost), open questions, numbered falsifiable recommendations
2. `synthesis.md` with:
   - TL;DR (≤5 bullets)
   - Consensus findings tagged ★ / ★★ / ★★★ by lens count
   - **Disputed findings reported as disputes** (no vote-rule averaging)
   - Cross-lens gaps (what no lens could source)
   - Numbered, falsifiable recommendations
   - A source manifest deduplicated across lenses
3. `verifiers/source-completeness.sh` passes: every lens meets its minimum
   citation count and the synthesis names all four lenses.

## Scope

Read-only research synthesis. No code mutation. No external services beyond the
provider-wrapped `claude --print` lanes (glm / kimi / codex / opus).

## Success Criteria

- All four `lens-*.md` files exist and each clears its citation floor.
- `synthesis.md` contains at least one explicitly **disputed** finding
  (proving it did not collapse disagreement into a false consensus).
- `synthesis.md` references all four lens names.
- `verifiers/source-completeness.sh` exits 0.

## Why this kickoff exists (learning-loop validation)

This is the fixed, repeatable task for `scripts/learning-loop-live-validate.sh`.
Running it **N times against the live `state.db`** lets us watch the learning
loop move: each run writes four lane-stamped researcher traces under
`task_class=research_synthesis`, PRM scores them, GRPO ranks the lanes, and the
router's `learning_governed` policy should eventually override the static lane
toward the lane that consistently wins. Keep the topic stable across runs so the
samples accumulate under one task class.

## Model Preference

Per-lens lanes are fixed by the recipe (glm/kimi/codex/opus). Do not pin a
single model — the whole point is to let the four lanes compete so the learning
loop can rank them.
