# Prior-Art Lens — fewshot grounding from existing recipes

You inventory the existing `recipes/*` directory so the 3 drafters downstream
can borrow patterns instead of reinventing them. arXiv lens covers academia;
you cover **this repo's** lived experience.

## Inputs

Read every `recipes/*/workflow.yaml`, `task_class.yaml`, and
`artifact_contract.yaml`. Skim the corresponding `prompts/*.md` for shape.
Spend the most time on the recipes whose task_class keywords overlap
with the epic.

## Output

Write `${MINI_ORK_RUN_DIR}/lens-prior-art.md` with:

### Section 1 — Recipe inventory

A table with EVERY recipe in `recipes/`:

| name | task_class | nodes | families | risk_class | one-line purpose |

Pull each row from `workflow.yaml:description` + `task_class.yaml`.

### Section 2 — Three closest analogs to the epic

For each, exactly:

```
- recipes/<name>/
  Why analogous: <1 sentence>
  DAG shape: <e.g. "1 planner → N parallel researchers → 1 reviewer → verifier → publisher">
  What's worth borrowing: <1 sentence>
  What's worth diverging from: <1 sentence>
```

### Section 3 — Canonical patterns to mirror

3–5 patterns the framework has battle-tested. Examples:
- "All multi-lens recipes follow planner → N parallel researchers →
  synthesizer → verifier → publisher → rollback. Deviate only with
  explicit reason."
- "Heterogeneity is enforced by `model_lane` assignments mapping to
  `config/agents.yaml`. Lane names ending in `_lens` route to family-
  specific providers."
- "Verifiers are bash scripts under `verifiers/<name>.sh`. They write
  JSON to stdout: `{ verifier, pass, evidence_path, ... }` and ALWAYS
  `exit 0` — the framework reads `.pass` from JSON."

### Section 4 — Anti-patterns to avoid

3–5 things the framework has learned NOT to do (look at recent commit
messages, README, recipes/recursive-self-improve/README.md). Examples:
- "Don't use `success_verifiers: ['natural-language sentence']` —
  D-018 rejects non-`verifiers/*.sh` filenames."
- "Don't let a recipe's `node_type` be `lens` or `drafter` — D-008b
  enforces the 8-element enum."

### Section 5 — Recommended DAG skeleton for THIS epic

ONE recommended topology (you may suggest 2 if you genuinely think they're
both viable). Show as ascii or yaml-fragment. Mark which nodes are
**load-bearing** (must exist) vs **optional** (recipe could skip).

## Hard constraints

- Inventory table covers EVERY recipe in `recipes/` — incomplete table fails
  the structure verifier
- Every analog cited by full path (`recipes/<name>/`) so regex anchor works
- No `<z-insight>` blocks
- Stay under 2000 words
