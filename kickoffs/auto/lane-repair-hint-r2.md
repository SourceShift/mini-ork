# Lane repair (A) — revision 2: pick the run's own failed node, not a reflect-time call

WIP commit 12fc7002 holds revision 1. Opus passed it: 93 tests, ruff clean. The live proof, which
nobody had run, shows the hint pointing at the WRONG failure. Fix only that. `mini-ork recover
--lane` is merged on main now (lane-repair-resume), so the hint's command must be one recover
accepts.

## Live output of revision 1 (run `learn-memory-tab-r2-20261007142114`)

```
kind: lane | failed_node: None | retryable: True
summary: minimax is out of quota
detail: timeout after 120.0s
nodes: []
suggestions: [glm (225 ok), opus (52 ok), deepseek (19 ok)]
command: mini-ork recover learn-memory-tab-r2-20261007142114 --lane gradient-extract=glm
```

Why it's wrong:
- `gradient-extract` is a reflect-time call (`feature_name`/`actor` of the gradient extractor,
  run after the workflow ended). It timed out on minimax. It is not a workflow node's lane alias,
  so recover rejects it, `nodes` is empty, and `failed_node` is None.
- The real failure: node `prior_art_lens`, whose `node_start` payload has `model_lane:
  "codex_lens"`, got `API Error: Request rejected (429) · Token Plan usage limit reached: Upgrade
  your Token Plan or purchase Credits for more usage. (2056)`, found in
  `agent-prior_art_lens.live.jsonl` (`"is_error": true, "api_error_status": 429, "result": …`).
- `codex_lens` is shared with `implementer` in framework-edit, so it's a code alias and glm must
  never be suggested. With the wrong alias, `code=False` and glm ranked first.

## Files in scope

- `mini_ork/recovery/retry_hint.py`: ONLY `_case_lane_unavailable` and helpers it calls
- `tests/unit/test_lane_repair_hint.py`

Do NOT modify any other file.

## Fix (exact)

1. **Only workflow aliases count.** Build `alias → [node ids]` from this run's `run_events`
   `node_start` payloads (`node_id`, `model_lane`), not from the recipe file alone. Only failed
   `llm_calls` rows whose alias (`actor`, or `feature_name` after `mini-ork:`) is in that map are
   candidates. Reflect-time aliases (`gradient-extract`, `pattern-induct`, `rubric`, anything not
   in the map) are ignored.
2. **Failed node.** Among the candidate rows, choose the node whose `node_end.finish_reason !=
   "done"`, or whose `node_end` is missing, and whose alias matches. Prefer `error_category in
   ("quota", "auth")`. `failed_node` and `from_node` = that node id.
3. **Legacy path** (rows with NULL `error_category`): scan `agent-<node>.live.jsonl` and
   `impl-<node>.log` for the quota/auth wording. The node comes from the file name, the alias from
   that node's `node_start.model_lane`, and `detail` from the stream's `result` text (≤ 400 chars),
   never from an unrelated `llm_calls.error_message`.
4. **Suggestions.** `nodes` = every node using the alias (from step 1 plus the workflow file).
   `code` = any of them is a code role. Suggestions come from `lane_suggest.suggest(...)` with
   that flag, so glm is excluded for `codex_lens`.
5. **No candidate.** If no workflow-alias failure exists, this case returns None and the existing
   cases decide.

## Tests (add to `tests/unit/test_lane_repair_hint.py`)

- Seed the live shape: a `node_start` for `code_impact_lens` (`minimax_lens`) and `prior_art_lens`
  (`codex_lens`); `node_end` prior_art_lens `finish_reason: error`; a failed `llm_calls` row
  `actor=codex_lens` with NULL category and stderr-chatter `error_message`; a LATER failed row
  `actor=gradient-extract`, `model_id=minimax`, `error_message="timeout after 120.0s"`;
  `agent-prior_art_lens.live.jsonl` with the 429 result line. Expect:
  - `failed_node == "prior_art_lens"`, `alias == "codex_lens"`;
  - `"Token Plan usage limit" in detail`;
  - `nodes ⊇ {"prior_art_lens", "implementer"}`;
  - no `glm` in suggestions;
  - `command == f"mini-ork recover {run} --lane codex_lens={first suggestion}"`.
- With ONLY the gradient-extract failure (no node failure) → this case returns None.

## Verification command

The command that proves this run succeeded:

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_repair_hint.py tests/unit/test_retry_hint.py tests/unit/test_retry_notify.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/recovery/retry_hint.py tests/unit/test_lane_repair_hint.py` → clean.
- Live proof (read-only):
  `env -u MINI_ORK_RUN_ID python3.11 -c 'import json; from pathlib import Path; from mini_ork.recovery import retry_hint, retry_notify; h=retry_hint.compute(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn-memory-tab-r2-20261007142114"); print(json.dumps(h, indent=1)); print(retry_notify.fix_steps(h))'`.
  It must show `failed_node: prior_art_lens`, `alias: codex_lens`, the Token Plan 429 detail, and
  a non-glm suggestion. Paste it.
- `git diff 12fc7002 --stat` touches only the files in scope.
