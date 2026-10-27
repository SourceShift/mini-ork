# B1: typed failure classes and one action table for retry, fallback walk and fuse

## Goal

`mini_ork/dispatch/llm_dispatch.py` `classify_error` (58-97) is a regex chain
over the error text. Of 2,675 failed `llm_calls` rows, **1,881 are `unknown`**
(no retry, no fuse, no diagnosis). Most are recognisable:
- 488 timeouts (`timeout after 120.0s …`, rc 124);
- 128 spawn failures (`spawn failed: [Errno 2] No such file or directory`,
  `dispatcher crashed: [Errno 2] …`, rc 127);
- 93 cwd-confusion guards (`… likely cwd-confusion …`, `… refusing codex dispatch …`);
- 64 shape rejects (`dispatch result failed shape check …`);
- 7 `empty output`.

Two more defects:
- The per-attempt row classifies the HEAD of the error (`raw_err[:200]`),
  while the final row uses the tail. Codex attempts therefore read
  `[cl_codex] codex exec failed with rc=1 — see stderr …` and land in `unknown`.
- `400 … "code":"model_not_found"` / "model is not supported" classifies as
  `request`.

The fallback walk (`providers.py` `dispatch_with_fallback` ~2547-2651) moves
to the next lane on ANY failure, and nothing parses `Retry-After` or
"try again in/at …".

`llm_calls.error_category` has a CHECK constraint
(`db/migrations/0067_llm_calls_thinking_tokens_nullable.sql:49-52`):
`auth, quota, capacity, request, safety, network, stream, provider, config, unknown`.
Keep storing only those. Put the new typed class in `metadata_json`. No
migration.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/dispatch/failure_class.py` (new)

```
CLASSES = ("transient", "rate_limit", "credential", "quota", "model_access",
           "caller_fault", "timeout", "environment", "shape", "unknown")

@dataclass(frozen=True)
class Action:
    retry_chain: bool      # the outer loop may re-run the chain (with backoff)
    next_lane: bool        # the fallback walk continues to the next lane
    stop_walk: bool        # the walk stops here: no other lane can help
    wait_hint_s: float | None

def classify(text: str, rc: int | str = "") -> tuple[str, str]   # (failure_class, db_category)
def wait_hint(text: str, *, now: datetime | None = None) -> float | None
def action(failure_class: str, text: str = "", env: Mapping[str, str] | None = None) -> Action
```

1. **`classify(text, rc)`** checks the FULL text, case-insensitively. The
   first match wins:
   - `rc in (124,)` or `timeout after \d` or `timed out after` → (`timeout`, `network`)
   - `rc in (127,)` or `spawn failed` or `dispatcher crashed: \[errno 2\]` or
     `likely cwd-confusion` or `refusing codex dispatch` → (`environment`, `config`)
   - `failed shape check` or `shape reject` → (`shape`, `request`)
   - `model_not_found` or `model is not supported` or
     `(model|deployment) .*does not exist` or
     `do(es)? not have access to (the )?model` → (`model_access`, `config`)
   - otherwise call the existing `llm_dispatch.classify_error(text, rc)` and map:
     - `quota` → (`quota`, `quota`)
     - `auth` → (`credential`, `auth`)
     - `capacity` where the text has `429` and `rate` → (`rate_limit`, `capacity`); other `capacity` → (`transient`, `capacity`)
     - `network` / `stream` / `provider` → (`transient`, same)
     - `request` / `safety` → (`caller_fault`, same)
     - `config` → (`environment`, `config`)
     - `unknown` → (`unknown`, `unknown`)
   - An `empty output` text with rc 0 → (`transient`, `provider`).
2. **`wait_hint(text)`**:
   - `retry-after[:=\s]+(\d+)` → seconds;
   - `try again in (\d+)\s*(s|sec|seconds|m|min|minutes)` → seconds;
   - `try again at (\d{1,2}):(\d{2})\s*(am|pm)` → seconds until that local time
     today, or tomorrow when it has passed;
   - otherwise `None`.
3. **`action(cls, text, env)`**:
   - `transient` → `Action(True, True, False, None)`;
   - `rate_limit` → `Action(True, True, False, wait_hint(text))`;
   - `timeout` → `Action(False, True, False, None)`;
   - `credential`, `quota`, `model_access` → `Action(False, True, False, None)`;
   - `environment` → `Action(False, False, True, None)`: a missing cwd or a
     confused target affects every lane alike;
   - `shape` → `Action(True, True, False, None)` (today's shape-reject retry);
   - `caller_fault` → `Action(False, True, False, None)`, except when
     `MO_FALLBACK_STOP_ON_CALLER_FAULT == "1"`, which gives
     `Action(False, False, True, None)`. The default walks on, because
     "prompt too long" on one lane can fit another;
   - `unknown` → `Action(False, True, False, None)` (today's behaviour).

### 2. `mini_ork/dispatch/llm_dispatch.py`

1. **`_attempt_sink`** (per-attempt row, ~1079) and the final failed row
   (~1244) both call `failure_class.classify(<full error text>, rc)`, where the
   full text is `res.error` plus the stdout tail when available. They store
   `db_category` in `error_category`, and `{"failure_class": cls}` in
   `metadata_json` (merged with the existing keys), plus `"wait_hint_s"` when
   one was found. `error_retryable` is computed from the class:
   - `"1"` for transient, rate_limit and shape;
   - `"0"` otherwise.
   The stored `error_message` keeps today's slice for each writer.
2. **The outer retry loop** (~1111-1183) decides with
   `action(classify(probe, rc)[0], probe)`:
   - **retry** when `retry_chain` is true and `attempt < max_attempts`. Keep
     the glm fair-usage and shape-reject rules as they are; they are covered
     by `rate_limit` / `shape` too.
   - **sleep** `min(wait_hint_s, MO_DISPATCH_RETRY_MAX_SLEEP_S)` when a hint
     exists and is not above the cap; otherwise `backoff_seconds(attempt)` as
     today.
   - When the hint is above the cap, do NOT retry the same chain. Log
     `[dispatch] <lane> asks to wait <n>s (above the <cap>s cap); not retrying`.
3. **The lane fuse** keeps its categories. The new `config` rows
   (model_access, environment) now trip the terminal 1-strike fuse as `config`
   already does.

### 3. `mini_ork/dispatch/providers.py` — `dispatch_with_fallback`

After a lane fails (~2630), compute
`act = failure_class.action(failure_class.classify(result.error or "", result.rc)[0], result.error or "")`.
When `act.stop_walk` is true, log
`lane <lane> failed with <class>; no other lane can help — stopping the walk`
and return that result immediately. Otherwise behave as today.

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/failure_class.py` (new)
- `mini_ork/dispatch/llm_dispatch.py`
- `mini_ork/dispatch/providers.py`
- `tests/unit/test_failure_class.py` (new)

Do NOT modify `classify_error`'s existing return values for texts it already
classifies (several tests pin them), any migration, `docs/`, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (`tests/unit/test_failure_class.py`, hermetic)

Use the real failure texts quoted in the Goal and below. For the integration
cases, use the `mock` provider kind (`docs/operator/mock-provider.md`,
B6) where it fits, or the fake `dispatch_fn` style of
`tests/unit/test_dispatch_retry_py.py`. Set `MO_DISPATCH_RETRY_BASE_S=0`.

1. **Classification table** (text, rc → class, db_category):
   - `timeout after 120.0s (spawned→kill 120s, kill→reaped 0s)`, 124 → timeout, network
   - `spawn failed: [Errno 2] No such file or directory: '/x'`, 127 → environment, config
   - `… likely cwd-confusion (a target-repo lane landing in mini-ork) …`, 1 → environment, config
   - `dispatch result failed shape check (looks_like_json)`, 1 → shape, request
   - `"status":400,…"message":"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account.","code":"model_not_found"`, 4 → model_access, config
   - `HTTP 429: API Error: Request rejected (429) · Token Plan usage limit reached: Upgrade your Token Plan …`, 1 → quota, quota
   - `[openai_chat] HTTP 429 from https://openrouter.ai/api/v1: … temporarily rate-limited upstream`, 4 → rate_limit, capacity
   - `lane preflight failed: kimi: API key rejected (HTTP 401) …`, 2 → credential, auth
   - `HTTP 500 Internal Server Error`, 1 → transient, provider
   - `400 Bad Request: invalid request`, 1 → caller_fault, request
   - `something odd`, 1 → unknown, unknown
2. **`wait_hint`:** `Retry-After: 30` → 30; `try again in 2 minutes` → 120;
   `try again at 10:50 AM` with `now` = 10:40 → 600.
3. **`action`:**
   - environment stops the walk;
   - caller_fault walks on by default and stops with
     `MO_FALLBACK_STOP_ON_CALLER_FAULT=1`;
   - rate_limit carries `wait_hint_s`.
4. **The walk stops.** `dispatch_with_fallback` over two lanes where the first
   returns rc 127 `spawn failed` → the second lane is never called.
5. **The walk continues.** The first lane returns `model_not_found` → the
   second lane serves.
6. **Classified on the full text.** A failed attempt whose error starts with
   `[cl_codex] codex exec failed with rc=1 — see stderr for cause` and ends
   with the quota text → the per-attempt `llm_calls` row has
   `error_category='quota'` and `metadata_json.failure_class == 'quota'`.
   Use a tmp DB from `mini_ork.stores.migrate.init_db`.
7. **Rate-limit hint under the cap.** `Retry-After: 2` →
   the loop sleeps about 2 s (monkeypatch `time.sleep` to record) and retries.
8. **Hint above the cap.** `try again in 10 minutes` with the cap at 45 → no
   retry of the chain, and the log line is printed.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_failure_class.py tests/unit/test_llm_dispatch_py.py tests/unit/test_dispatch_retry_py.py tests/unit/test_dispatch_fallback_py.py tests/unit/test_lane_fuse_and_chain_py.py tests/unit/test_fuse_placement.py tests/unit/test_dispatch_crash_classify.py tests/unit/test_dispatch_failure_detail.py tests/unit/test_fallback_attempt_spend.py tests/unit/test_llm_dispatch_shape_fallback.py tests/unit/test_lane_repair_hint.py tests/unit/test_llm_calls_ledger_py.py`

(Drop any listed test file that does not exist; do not create it.)

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_failure_class.py --junitxml="${MINI_ORK_RUN_DIR}/bif-b1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-b1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/dispatch/failure_class.py mini_ork/dispatch/llm_dispatch.py mini_ork/dispatch/providers.py tests/unit/test_failure_class.py` is clean.
- Re-classifying the 1,881 `unknown` rows' `error_message` text with
  `failure_class.classify` (a read-only script, not shipped) leaves fewer than
  900 `unknown`. Report the count in the implementer summary.
