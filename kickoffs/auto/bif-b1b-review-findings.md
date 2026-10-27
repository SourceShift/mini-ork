# B1b: fix the pre-push review's findings on B1 (failure classes) and B3 (lane affinity)

## Goal

B1 (`mini_ork/dispatch/failure_class.py`, commit 437b2fa3) and B3
(`mini_ork/dispatch/lane_affinity.py`) are on main. The pre-push review of B1
(review 111, lenses glm + deepseek_flash) raised four real defects. Fix them,
each with a regression test.

1. **Model prose can fuse a healthy lane.** On a failed attempt whose error
   text alone classifies `unknown`, `llm_dispatch` appends the last 1,000
   chars of the model's stdout and classifies again
   (`mini_ork/dispatch/llm_dispatch.py` ~1113-1120). Stdout is model prose.
   An implementer working on auth code writes "the server returned 401" and
   the attempt becomes `credential` / `auth`, which is a fuse-terminal
   category, so the lane is fused for the run. RB-10 (same file, ~123 and
   ~468) is exactly this lesson.
2. **`model … does not exist` matches across lines.** `_MODEL_ACCESS_RE`
   (`failure_class.py` ~61-64) contains `(model|deployment)[\s\S]*does not
   exist`. Over a full multi-line error text, the word "model" anywhere and
   "does not exist" anywhere later (a file, a path) classify as
   `model_access`, which trips the 1-strike fuse.
3. **Affinity never applies to JSON-contract nodes.** The affinity reorder
   (`llm_dispatch.py` ~991-1005) runs while `model` is still the single
   resolution-time lane; the shape-contract expansion that adds the role's
   fallback tail runs later (~1058-1062). For the planner (the node with the
   most shape rejects), the lane that served an earlier round is never in
   the chain when affinity looks, so affinity does nothing.
4. **A transient failure ignores the provider's wait hint.**
   `failure_class.action("transient", text)` returns `wait_hint_s=None` even
   when `text` carries `Retry-After: N`.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. Stdout tail may only promote to provider-structured classes

In `llm_dispatch()`'s per-attempt failure path (~1102-1135):

- Classify the error text alone first: `cls, category = failure_class.classify(raw_err, result_rc)`.
- Only when that gives `unknown` AND the attempt is not a shape reject, also
  classify `raw_err + "\n" + <stdout tail>`. Use that result ONLY if its
  class is one of `quota`, `model_access`, `rate_limit` (phrases a provider
  emits in a structured rejection). Otherwise keep the error-only `unknown`
  result.
- `wait_hint_s` comes from the same text the kept class came from.

Add `TAIL_PROMOTABLE = frozenset({"quota", "model_access", "rate_limit"})` to
`failure_class.py` and use it here; do not hard-code the set in
`llm_dispatch.py`.

### 2. Bound the model-access phrase to one line

In `_MODEL_ACCESS_RE`, replace `(model|deployment)[\s\S]*does not exist` with
`(model|deployment)[^\n]{0,120}does not exist`. The other alternatives are
unchanged.

### 3. Affinity reorders the FINAL chain

Move the whole affinity block (`_aff_run_dir` … the `except Exception: pass`)
from before the first fuse pass to AFTER the shape-contract expansion and
BEFORE the second fuse pass (`# Second fuse pass` ~1064). Keep:

- `_aff_lead` is the routing lead: the first lane of `model` as resolved
  BEFORE the expansion. Bind it before the first fuse pass, as today, and
  keep it unconditionally bound (the success path records against it).
- The second fuse pass still runs after the reorder, so a fused affinity lane
  is dropped.
- The stderr line and the try/except are unchanged.

### 4. Transient failures carry the wait hint

`failure_class.action("transient", text)` returns
`Action(True, True, False, wait_hint(text))`. No other row of the table
changes.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/failure_class.py`
- `mini_ork/dispatch/llm_dispatch.py`
- `tests/unit/test_failure_class.py`
- `tests/unit/test_lane_affinity.py`

Do NOT modify `mini_ork/dispatch/providers.py`, `mini_ork/dispatch/core.py`,
`mini_ork/dispatch/lane_affinity.py`, `mini_ork/cli/execute_handlers.py`,
`docs/`, any migration, or any path in `config/kernel-frozen.yaml`. Every
existing test must pass unchanged.

## Tests (hermetic, no LLM)

In `tests/unit/test_failure_class.py`:

1. `classify("model: glm\nspawn helper: file does not exist", 1)[0] != "model_access"`.
2. `classify("The model `gpt-x` does not exist", 1)[0] == "model_access"`.
3. `action("transient", "Retry-After: 7").wait_hint_s == 7` and
   `action("transient", "").wait_hint_s is None`.
4. Through `llm_dispatch` with a fake dispatch whose attempt fails with an
   empty error, rc 1, and stdout `"I checked: the API returned 401 unauthorized."`
   → the `llm_calls` row's `error_category` is NOT `auth` and its
   `metadata_json.failure_class` is `unknown` (use the integration fixture
   already in this file, `hermetic`).
5. Same, with stdout carrying `"code":"model_not_found"` → `failure_class`
   is `model_access`.
6. Through `llm_dispatch` with `MO_DISPATCH_MAX_ATTEMPTS=3`: a single-lane
   chain whose fake fails with `timeout after 5.0s`, rc 124 → the fake is
   called exactly once (a non-retryable class does not re-run the chain).

In `tests/unit/test_lane_affinity.py`, following the harness of
`tests/unit/test_llm_dispatch_shape_fallback.py:28-75` (`--node-type planner`,
no model override, `resolve_lane_model` → `glm`):

7. With `.lane-affinity.json` recording `{"planner": {"lead": "glm",
   "served": <the chain's second lane>}}` in a tmp `MINI_ORK_RUN_DIR` and
   `MO_NODE_ID=planner`, the fake's FIRST call is that second lane.
8. Same with `MO_LANE_AFFINITY=0` → the first call is `glm`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_failure_class.py tests/unit/test_lane_affinity.py tests/unit/test_llm_dispatch_py.py tests/unit/test_llm_dispatch_shape_fallback.py tests/unit/test_dispatch_fallback_py.py tests/unit/test_lane_fuse_and_chain_py.py tests/unit/test_fuse_placement.py tests/unit/test_dispatch_crash_classify.py tests/unit/test_fallback_attempt_spend.py tests/unit/test_dispatch_retry_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_failure_class.py tests/unit/test_lane_affinity.py --junitxml="${MINI_ORK_RUN_DIR}/bif-b1b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-b1b-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/failure_class.py mini_ork/dispatch/llm_dispatch.py tests/unit/test_failure_class.py tests/unit/test_lane_affinity.py` is clean.
- Model prose on stdout can never produce a fuse-terminal category.
