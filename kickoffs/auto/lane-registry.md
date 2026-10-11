# Commit the audit panel's third judge lane into the tracked lane registry

## Goal

`recipes/audit-judge-panel-3l/workflow.yaml` line 12 declares
`model_lane: laguna_lens`, and `recipes/audit-judge-panel-3l/task_class.yaml`
documents that node as running on the free OpenRouter lane
`poolside/laguna-s-2.1:free`. But the alias `laguna_lens` is defined in NO
tracked lane registry — it exists only in the per-user, gitignored overlay
`.mini-ork/config/agents.local.yaml`. A fresh clone therefore cannot resolve the
third judge lane, and any run of the recipe on a fresh checkout dies at lane
resolution before it starts.

The lane TARGET is already tracked: `.mini-ork/config/providers.yaml` defines
the `laguna_free` provider. Only the alias is missing from the registries.

## Acceptance

- AC1 — add exactly one lane alias, `laguna_lens: laguna_free`, to the LENS
  block of the tracked home registry `.mini-ork/config/agents.yaml`, alongside
  the existing `minimax_lens` / `glm_lens` / `kimi_lens` / `codex_lens` /
  `opus_lens` / `deepseek_lens` entries (under the
  "heterogeneous lens lanes" comment). Leave the file's comments, the producer
  and gate-keeper blocks, and the `budget:` block untouched.
- AC2 — add the same alias `laguna_lens: laguna_free` to the LENS block of the
  tracked repository template `config/agents.yaml`, alongside `glm_lens` /
  `kimi_lens` / `codex_lens` / `opus_lens` / `minimax_lens`. This is the file
  `mini-ork init` copies into a new home, so it must resolve there too.
- AC3 — add a regression test `tests/unit/test_recipe_lane_registry.py` that
  prevents this class of drift from recurring. It must: (a) collect every
  `model_lane:` value referenced by every committed recipe workflow, by scanning
  `recipes/*/workflow.yaml` for `model_lane:` tokens with a simple line scan
  (do NOT depend on a full YAML parse — flow-mapping text is enough and a parse
  would be brittle); (b) build the set of defined lane aliases from the tracked
  registries `.mini-ork/config/agents.yaml` and `config/agents.yaml` (these two
  ARE plain `lanes:` mappings and may be parsed with `yaml.safe_load`); and
  (c) assert every referenced lane is defined, naming the offending
  recipe + lane in the assertion message. The test must FAIL on the pre-fix tree
  (where `laguna_lens` is undefined) and PASS once AC1+AC2 are applied.
- AC4 — additive only. Do NOT change, rename, or remove any EXISTING lane alias
  or its value in either registry — `reviewer`, `implementer`, `researcher`,
  `kimi_lens: kimi`, `codex_lens: codex`, `opus_lens: opus`, the `budget:`
  numbers, and every other line must be byte-identical. Add the two new lines;
  change nothing else.
- AC5 — do NOT touch `.mini-ork/config/agents.local.yaml` (the untracked
  operator overlay) and do NOT add `laguna_lens` to
  `config/agents.recursive-self-improve.yaml` (the audit panel is not part of
  the RSI recipe family).

## Files in scope

- `.mini-ork/config/agents.yaml`
- `config/agents.yaml`
- `tests/unit/test_recipe_lane_registry.py`   (new)

## Out of scope

- Any module under `mini_ork/`. Any other recipe, verifier, or prompt file.
  `.mini-ork/config/agents.local.yaml` (untracked). `config/agents.recursive-self-improve.yaml`.

## Verification command

`python3 -m pytest tests/unit/test_recipe_lane_registry.py -q`
