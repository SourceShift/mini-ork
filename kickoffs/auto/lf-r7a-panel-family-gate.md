# LF-R7a judge panels fail when two judges are the same model family

## Goal

A "multi-family" judge panel can silently be one or two models: the operator
overlay maps `opus_lens: glm`, and a free lane that fails falls back to another
lane. Panel run `run-1791725633-31802` (`audit-judge-panel-3l`) had `opus_lens`
served by glm and `laguna_lens` falling back to minimax six times, so its three
judges were two families — and it published. Only `audit-judge-panel-4l`
checks served models, and it compares raw lane strings, so `opus` and `opus55`
(one family) count as different. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (R7a; decision Q6: fail, do not
warn, and leave the operator overlay alone).

All paths are relative to the repository root.

## Mechanism (exact)

1. New module `mini_ork/verify/panel_heterogeneity.py`:
   - `lane_model(lane) -> str`: the model a provider lane really calls — the
     registry entry's `model:`; else its `extra_env.ANTHROPIC_MODEL`; else, for an
     `anthropic-native` lane, `claude-<lane>`; else the lane name. Read the
     registry with the existing loader in `mini_ork/dispatch/providers.py`
     (do not modify that file).
   - `model_family(model) -> str`: vendor family from the model string,
     case-insensitive: claude/opus/sonnet/haiku/fable → `anthropic`; glm → `zai`;
     minimax → `minimax`; deepseek → `deepseek`; kimi/moonshot → `moonshot`;
     gpt/codex/o1/o3/o4 → `openai`; gemini/gemma → `google`; laguna/poolside →
     `poolside`; qwen → `alibaba`; anything else → the lowercased string.
   - `check_panel(run_id, aliases, *, db=None, expect=None) -> dict` with keys
     `ok` (bool), `problems` (list of str), `served` (alias → sorted lanes),
     `families` (alias → sorted families), `skipped` (str or None). It reads
     `llm_calls` read-only (`file:…?mode=ro`) for `run_id` and the given actor
     aliases. Problems:
     - `same_family:<a>,<b>:<family>` when two aliases' successful calls share a
       family;
     - `fallback:<alias>:served=<lane>` when an alias has a FAILED call and its
       successful call came from a different lane (the fallback tail served it);
     - `expect:<alias>:served=<lane>` when `expect` (alias → allowed lanes) is
       given and a served lane is not allowed;
     - `no_calls:<alias>` when the run has `llm_calls` rows but none for that alias.
     When the DB, the table, or every row for the run is missing, return
     `ok=True, skipped=<reason>` (hermetic tests and dry runs have no rows).
2. `recipes/audit-judge-panel-4l/verifiers/panel-completeness.py`: replace
   `check_served_models`' raw-string comparison with `check_panel` (keep reading
   `MO_PANEL_EXPECT_MODELS` into `expect`); every problem is a gate failure
   prefixed `served_models:`. The verifier imports the module via the repo root on
   `sys.path` (`MINI_ORK_ROOT`, else the verifier's `../../..`).
3. `recipes/audit-judge-panel-3l/verifiers/panel-completeness.py` and
   `recipes/audit-judge-panel/verifiers/panel-completeness.py`: add the same call
   for their judge aliases (3l: `opus_lens`, `minimax_lens`, `laguna_lens`; 2l:
   `opus_lens`, `minimax_lens`); problems fail the gate.

## Files in scope

- `mini_ork/verify/panel_heterogeneity.py` (new)
- `recipes/audit-judge-panel-4l/verifiers/panel-completeness.py`
- `recipes/audit-judge-panel-3l/verifiers/panel-completeness.py`
- `recipes/audit-judge-panel/verifiers/panel-completeness.py`
- `tests/unit/test_panel_heterogeneity.py` (new)
- `tests/unit/test_audit_judge_panel_4l.py`

## Tests (`tests/unit/test_panel_heterogeneity.py`, hermetic temp DB with an `llm_calls` table)

- served `{opus_lens: glm, minimax_lens: minimax, laguna_lens: minimax}` → not
  ok, with `same_family:minimax_lens,laguna_lens:minimax`;
- served `{opus_lens: opus55, kimi_lens: opus}` where `opus55` has
  `extra_env.ANTHROPIC_MODEL: claude-opus-5-5` → `same_family:…:anthropic`;
- served `{opus_lens: opus55, glm_lens: glm, deepseek_lens: deepseek_flash,
  minimax_lens: minimax}` → ok;
- `laguna_lens` with a failed `laguna_free` call then a successful `minimax`
  call → `fallback:laguna_lens:served=minimax`;
- no rows for the run → `ok=True` with `skipped` set;
- `model_family` maps `GLM-5.3`, `MiniMax-M3`, `deepseek-v4-flash`,
  `claude-opus-5-5`, `opus` to `zai`, `minimax`, `deepseek`, `anthropic`, `anthropic`.
- `tests/unit/test_audit_judge_panel_4l.py`: the "two judges served by one model"
  case still fails (update the expected message to the new form), and two
  anthropic lanes (`opus`, `opus55`) on two judges now fail too.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_panel_heterogeneity.py tests/unit/test_audit_judge_panel_4l.py tests/unit/test_recipe_lane_registry.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_panel_heterogeneity.py tests/unit/test_audit_judge_panel_4l.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r7a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r7a-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not modify `mini_ork/dispatch/providers.py`,
`mini_ork/dispatch/agents_config.py`, any `workflow.yaml`, or
`.mini-ork/config/*` (the overlay is operator-owned), and do not write to the
live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
