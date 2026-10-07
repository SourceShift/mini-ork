# Lane repair (A) — classify a dead-lane failure, suggest a working lane, tell the owner what to do

## Why (live evidence, 2026-10-07)

MiniMax ran out of quota. Every `minimax` call now returns
`API Error: Request rejected (429) · Token Plan usage limit reached: Upgrade your Token Plan or
purchase Credits for more usage. (2056)`. Run `learn-memory-tab-r2-20261007142114` (framework-edit)
failed at `prior_art_lens` (lane alias `codex_lens` → `minimax`), skipped the implementer, and
rolled back. The operator learned nothing useful. Every layer lost the cause:

1. **`llm_calls.error_message`** for this claude-code-wrapped (anthropic-compat) lane holds the
   CLI's stderr chatter ("another auth source is set…",
   `[claude-code:unrecognized_model] {"model":"MiniMax-M3"}`), not the provider error. The 429
   is in the stdout JSON result: `{"type": "result", "is_error": true, "api_error_status": 429,
   "result": "API Error: Request rejected (429) · Token Plan usage limit reached: …"}`.
   `error_category` is NULL on all 3 failed rows.
2. **`classify_error`** (`mini_ork/dispatch/llm_dispatch.py:31-62`) only says `quota` when "429"
   comes with `monthly|tokens-per-day|billing|quota|insufficient credits|credit limit`.
   "Token Plan usage limit reached … purchase Credits" matches none of them, falls through the
   `capacity` test as well, and ends up `unknown` (not retryable).
3. **`retry_hint.compute`** (`mini_ork/recovery/retry_hint.py`) for this run returns case 5:
   `{"kind": "unknown", "summary": "Failed at ?; the cause was not classified"}`,
   `retryable: false`, `failed_node: null`. `node_attempts` rows are written only on success, so
   no `failure_class` exists, and there's no `impl-*.log` because the implementer never ran.
   Even case 4 (provider trouble) returns `needs_change: None`, which `retry_notify.notify`
   ignores (`retry_notify.py:683-689`).
4. **`retry_notify.fix_steps`** therefore says only "Re-run with MO_VERBOSE=1".
   `_lane_from_attempt_row` (`retry_notify.py:202-238`) queries a `lane` column that
   `node_attempts` doesn't have, so it always returns "".

The data needed is all there:
- `llm_calls` (`run_id`, `feature_name = "mini-ork:<lane alias>"`, `actor = <alias>`,
  `provider`, `model_id = <lane>`, `status`, `error_message`, `error_category`, `ts`)
- `run_events` (`node_start` payload `{"node_id", "node_type", "model_lane": <alias>}`,
  `node_end` payload `finish_reason`)

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/llm_dispatch.py`: ONLY `classify_error` and the failure path that fills
  `error_message` / `error_category` / `retryable` in the `llm_calls` writer (~:282-330)
- `mini_ork/recovery/lane_suggest.py` (new)
- `mini_ork/recovery/retry_hint.py`: a new case function + its call in `compute`
- `mini_ork/recovery/retry_notify.py`: `fix_steps` (new `lane` branch), `_lane_from_attempt_row`
- `tests/unit/test_lane_repair_hint.py` (new); plus existing `tests/unit/test_retry_hint.py` /
  `tests/unit/test_retry_notify.py` only if an assertion there must change

Do NOT modify any other file.

## Changes (exact)

1. **Capture the real provider error.** On a failed call, if the provider's stdout parses as a
   claude-code `result` object with `is_error: true` (or contains such a line in a JSONL stream),
   set `error_message` = `f"HTTP {api_error_status}: {result}"`[:1000] (the `HTTP` part only
   when present) and classify THAT text. Otherwise keep today's behaviour. Always fill
   `error_category` (never NULL on a failed row) and `retryable`.
2. **Quota wording.** In `classify_error`, a 429 (or any text) matching
   `usage limit|token plan|purchase credits|out of credits|credit balance|insufficient
   balance|exceeded your current quota|billing` is `quota`. Keep existing categories and their
   precedence otherwise. `quota` stays non-retryable.
3. **`lane_suggest.py`**:
   - `CODE_ROLES = {"implementer", "worker", "publisher", "rollback", "bdd_runner", "healer"}`.
     A node is a code node if its `node_type` is in `CODE_ROLES`, or its alias is shared with a
     code node in the same workflow. In framework-edit, `prior_art_lens` shares `codex_lens` with
     the implementer, so switching that alias moves the implementer too.
   - `code_lanes()` = `MO_CODE_LANES` (comma list), default `minimax,codex,deepseek,opus`. `glm`
     is never allowed for code (house policy: glm is analysis-only).
   - `known_lanes(home)` = lane names from the effective `providers.yaml` (the same resolution
     `llm_dispatch` uses; `MINI_ORK_PROVIDERS` first).
   - `lane_health(db, lane, hours=6)` → `{"ok": n_success, "quota": n, "auth": n, "other_fail":
     n, "last_error": str, "last_ts": iso}` from `llm_calls` (`model_id = lane` OR `provider =
     lane`).
   - `suggest(home, *, failed_lane, alias, node_types, db=None, limit=3) -> list[dict]`:
     - candidates = `known_lanes` minus `failed_lane`, restricted to `code_lanes()` when any of
       `node_types` is a code node;
     - drop lanes with any quota/auth failure in the window;
     - rank by `ok` desc, then fewer `other_fail`, then name;
     - return `[{"lane", "reason"}]`, where reason is e.g. `"14 successful calls in the last
       6h"` or `"no recent calls (untested)"` for unseen lanes, which rank last.
   Pure reads, no network.
4. **New retry_hint case `_case_lane_unavailable`**, evaluated BEFORE cases 4 and 5.
   - It fires when the run has a failed `llm_calls` row with `error_category` in `("quota",
     "auth")`, OR when (for legacy rows with NULL category) an `agent-*.live.jsonl` /
     `impl-*.log` in the run dir contains `"api_error_status":429` or `(429)` together with the
     quota wording above.
   - Failed node = the node whose `node_start.model_lane` equals that row's alias (`actor`, or
     `feature_name` after `mini-ork:`) and whose `node_end.finish_reason != "done"`. If none,
     use the first `node_start` with that alias.
   - Returns:
     ```
     {"version", "run_id", "failed_node": <node>, "retryable": True, "strategy": "resume",
      "from_node": <node>,
      "needs_change": {"kind": "lane", "summary": f"{lane} is out of quota" | f"{lane} rejected the credentials",
                       "detail": <provider error, ≤400 chars>, "lane": <lane>, "alias": <alias>,
                       "provider": <provider>, "error_kind": "quota"|"auth",
                       "nodes": [<every node in the workflow using this alias>],
                       "suggestions": suggest(...)},
      "notes": [], "command": f"mini-ork recover {run_id} --lane {alias}={suggestions[0]['lane']}"
                              (or "" when there is no suggestion),
      "computed_at": …}
     ```
     Read the workflow's nodes with the same helper `retry_hint` already uses
     (`_recipe_workflow`).
5. **`fix_steps` for `kind == "lane"`** (deterministic strings):
   1. `f"{provider} lane '{lane}' (used by {alias}: {', '.join(nodes)}) failed: {detail}"`
   2. With suggestions: `f"Switch {alias} to '{s[0].lane}' ({s[0].reason}) and resume from
      {from_node}: {command}"`, then one line per other suggestion (`f"Or '{lane}' ({reason})"`).
      Without: `f"No other {'code ' if code else ''}lane looks healthy — top up {provider}
      credits or add a lane to providers.yaml, then: mini-ork recover {run_id}"`.
   3. `"Nothing was changed in your code; the run stopped before the implementer."` only when the
      run dir has no `framework-edit.diff` / implementer artifact.
6. **`_lane_from_attempt_row`** reads the lane from `llm_calls` (latest failed row's `model_id`
   for the run), not from a non-existent `node_attempts.lane`.

## Tests (`tests/unit/test_lane_repair_hint.py`, temp home/DB, no network)

- `classify_error` on the exact MiniMax text → `quota`; an existing `rate limit … 429` →
  `capacity`; 401 → `auth`.
- The failure path: a fake provider result JSON with `is_error/api_error_status 429` → the
  stored `error_message` starts with `HTTP 429:` and contains "Token Plan usage limit";
  `error_category = quota`.
- `suggest`: `minimax` failed with quota, `deepseek` 5 ok, `glm` 9 ok, `opus` 0 calls → for a
  code alias returns `deepseek` first, then `opus`, never `glm`; for an analysis-only alias
  `glm` ranks first.
- `compute` on a seeded run that copies the live shape (lens 429, implementer skipped, events as
  in the Why section) → `kind == "lane"`, `failed_node == "prior_art_lens"`, `nodes` include
  `implementer`, `command == "mini-ork recover <run> --lane codex_lens=deepseek"`,
  `retryable is True`.
- A legacy run with NULL `error_category` but the 429 in `agent-prior_art_lens.live.jsonl` →
  same hint.
- `fix_steps` for that hint: 3 lines with the texts above. `notify()` (existing) now enqueues
  the inbox row for it (retryable).

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_lane_repair_hint.py tests/unit/test_retry_hint.py tests/unit/test_retry_notify.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/dispatch/llm_dispatch.py mini_ork/recovery/lane_suggest.py mini_ork/recovery/retry_hint.py mini_ork/recovery/retry_notify.py tests/unit/test_lane_repair_hint.py` → clean.
- Read-only proof on the live run: `retry_hint.load_or_compute(Path("/Volumes/docker-ssd/ps/mini-ork/.mini-ork"), "learn-memory-tab-r2-20261007142114", write=False)`
  and `retry_notify.fix_steps(hint)`. It must say `kind: lane`, minimax out of quota, and suggest a
  non-glm lane. Paste both.
- `git diff --stat` touches only the files in scope.
