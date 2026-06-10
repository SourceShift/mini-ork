# recipe-creator — meta-recipe for authoring recipes

The framework's dogfood meta-recipe. Takes a natural-language epic
kickoff and produces a complete `recipes/<derived_name>/` directory
through a heterogeneous-panel + arbiter + verifier-smith pipeline.

## Why it exists

Recipe authoring is a small-N task — operators don't author recipes
daily, so historical per-agent performance data is too sparse to defensibly
pick "the best planner." Per Rajan 2025 / Shehata 2026, small-N tasks
are where the consensus paradox bites hardest. So instead of picking the
best agent, this recipe **runs the framework on itself**: three drafters
from three families propose independently, an opus arbiter scores them
against an 8-item rubric, a verifier-smith mechanizes the chosen draft's
verifier_contract into bash, and a hard-floor verifier blocks anything
that fails ≥3-distinct-families or structural-completeness.

## DAG

```
                 ┌── arxiv_lens ──────────┐
planner ──┬──────┤                       ├──┬── glm_drafter ──┐
          │      └── prior_art_lens ─────┘  ├── kimi_drafter ─┼── opus_arbiter ── verifier_smith ── recipe_validator ── publisher
          └─────────────────────────────────┴── codex_drafter ┘                                            │
                                                                                                          └── rollback
```

11 nodes, 5 families (planner=opus, arxiv_lens=codex, prior_art_lens=opus,
glm_drafter=glm, kimi_drafter=kimi, codex_drafter=codex, opus_arbiter=opus,
verifier_smith=codex, recipe_validator=deterministic, publisher / rollback).

Heterogeneity at draft time = **3 distinct families minimum** (one per
drafter); the chosen draft must also satisfy the **same ≥3 floor** as a
HARD verifier (`verifiers/recipe-validator.sh`).

## What it produces

A `recipes/<derived_name>/` directory containing:

| File | Source |
|---|---|
| `workflow.yaml` | chosen drafter, optionally merged |
| `task_class.yaml` | chosen drafter |
| `artifact_contract.yaml` | chosen drafter |
| `prompts/*.md` | chosen drafter |
| `verifiers/*.sh` | **verifier_smith overwrites stubs with real bash** |
| `example-kickoff.md` | chosen drafter |
| `README.md` | chosen drafter |

Plus, under the run dir (NOT committed):

- `lens-arxiv.md` — academic grounding (≥5 arxiv citations, ≥1 from 2024+)
- `lens-prior-art.md` — repo conventions inventory
- `drafts/{glm,kimi,codex}/<derived_name>/` — all three drafts preserved
  for inspection
- `chosen/<derived_name>/` — pre-publisher staging area
- `chosen-recipe.json` — arbiter's rubric scores + merge notes
- `verifier-smith.json` — smith's coverage report

## When to use it

Use `mini-ork-creator` when the existing recipes don't fit the work you
want to dispatch and adapting an existing recipe by hand would lose more
than it saves. For ad-hoc one-shot tasks, prefer `code-fix` or `docs`.
For systematic recurring work, author a recipe via this meta-recipe.

## Hard constraints

- The new recipe MUST satisfy `verifiers/recipe-validator.sh` to be
  published. The most common failure is the heterogeneity floor — if
  a drafter collapses to a single-family DAG, the arbiter must pick a
  different draft or escalate to rollback.
- Verifier scripts written by `verifier_smith` must be deterministic +
  hermetic + `bash -n` clean — the validator runs `bash -n` on each.
- Recipe names must be kebab-case ≤ 32 chars.

## Self-improvement loop

Per user direction (option 6): the recipe-creator IS promotable. Each
successful run writes a `self_improve_runs` row keyed by the meta-recipe
itself. Over generations the loop learns which drafter family produces
the highest rubric-pass-rate and adjusts (future epic — for now the
rubric scores are persisted but not yet auto-weighted).

## Run it

```bash
# Author the example db-migration-audit recipe
mini-ork classify recipes/recipe-creator/example-kickoff.md
# Then:
mini-ork plan
mini-ork run recipe-creator <generated_kickoff_path>
```

The publisher commits the new `recipes/<derived_name>/` dir under
`mini-ork@local`. Inspect the rubric in
`.mini-ork/runs/<run_id>/chosen-recipe.json` for the arbiter's
reasoning.
