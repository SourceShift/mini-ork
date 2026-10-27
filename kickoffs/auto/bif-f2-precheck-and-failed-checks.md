# F2: the pre-lock review never inherits or fakes a precheck; repair reads check dicts from failed_checks (high tier)

## Goal

Pre-push reviews 140 (R2e) and 141 (S2) raised these on main:

1. **An ambient precheck skips the pre-lock review.** `_prereview`
   (`scripts/mini_ork_worktree.py`) runs the review gate with
   `env = os.environ.copy()`. If the operator's shell (or a parent process)
   carries `MO_REVIEW_PRECHECKED_PATCH` equal to the branch's patch-id, the
   gate skips the very review `_prereview` exists to run, and the in-lock hook
   then skips too.
2. **A review that could not run is handed off as reviewed.** The gate exits
   0 both after a real review and when it was "unavailable" in the simple tier
   (it proceeds). `_prereview` returns the patch-id on every exit 0, so the
   in-lock hook skips its own attempt even when no review happened. The gate
   writes `<home>/state/pre-push-review-cache.json` (`CACHE_REL` in
   `mini_ork/review/gate.py`) only for a terminal, non-degraded review, keyed
   by patch-id, with an int `at` epoch; a cache hit is valid for
   `CACHE_TTL_S = 7 * 86400` seconds.
3. **Repair feedback drops check dicts stored under `failed_checks`.** Since
   S2, `_failing_verifier_sections` (`mini_ork/recovery/auto_repair.py`)
   reads check dicts only from `checks` and treats `failed_checks` as names.
   Some verifier payloads put the failing check DICTS (with `log`) under
   `failed_checks` and have no `checks`; those rounds lose their
   `failing check:` lines and logs.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `scripts/mini_ork_worktree.py` — `_prereview`

1. After `env = os.environ.copy()`, remove `MO_REVIEW_PRECHECKED_PATCH` from
   `env` (`env.pop(..., None)`), before the gate subprocess runs.
2. Add `_review_cached(pid: str) -> bool`: read
   `<home>/state/pre-push-review-cache.json`, where `<home>` is
   `os.environ.get("MINI_ORK_HOME")` when set and non-empty, else
   `os.path.join(ROOT, ".mini-ork")`. Return True only when the JSON is an
   object whose `pid` entry is an object with an int `at` and
   `0 <= int(time.time()) - at <= 7 * 86400` (the gate's `CACHE_TTL_S`). Any read or parse error → False.
   Stdlib only; the driver does not import `mini_ork`.
3. On gate exit 0, compute `pid = _patch_id(wt)` and return it only when
   `pid` is non-empty and `_review_cached(pid)`; otherwise print one line
   `[mo-worktree] pre-push review did not complete a cached review; the push hook will run it`
   and return `""`. The rc 1 (block) and other non-zero paths are unchanged.

### 2. `mini_ork/recovery/auto_repair.py` — `_failing_verifier_sections`

Iterate check dicts from BOTH `data.get("checks")` and
`data.get("failed_checks")` (each only when it is a list; skip non-dict
entries), de-duplicated by `id`/`name` so a check present in both is listed
once. The failing test is unchanged: `pass is False`, status FAIL/REFUTED, or
its id/name in the string names of `failed_checks`. A dict found under
`failed_checks` counts as failing. Everything else in the function is
unchanged.

## Files in scope (touch ONLY these)

- `scripts/mini_ork_worktree.py`
- `mini_ork/recovery/auto_repair.py`
- `tests/unit/test_prereview_handoff.py` (new)
- `tests/unit/test_auto_repair_verifier_evidence.py`

Do not modify `mini_ork/review/gate.py`, `mini_ork/verify/check_log.py`, `tests/unit/test_review_outside_lock.py`, `mini_ork/cli/execute.py`, `docs/`, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (hermetic, no LLM)

`tests/unit/test_prereview_handoff.py` (new). Load the driver the way
`tests/unit/test_review_outside_lock.py` does, with a tmp worktree that has
`.githooks/pre-push`, and monkeypatch `subprocess.run` (the gate call) and
`_patch_id`:

1. With `MO_REVIEW_PRECHECKED_PATCH=abc` in `os.environ`, the env passed to
   the gate subprocess has no `MO_REVIEW_PRECHECKED_PATCH`.
2. Gate rc 0 and a cache file (under a tmp `MINI_ORK_HOME`) holding the
   patch-id with `at` = now → `_prereview` returns the patch-id.
3. Gate rc 0 and NO cache entry for the patch-id → returns `""` and prints
   `did not complete a cached review`.
4. Gate rc 0 and a cache entry older than 7 days → returns `""`.
5. A malformed cache file → returns `""`.

`tests/unit/test_auto_repair_verifier_evidence.py` (append):

6. A `verifier_static-check.json` with `pass: false`, no `checks`, and
   `failed_checks: [{"name": "lint", "pass": false, "log": "E501 line too long"}]`
   → `_failing_verifier_sections` contains `failing check: lint` and
   `E501 line too long`.
7. A check present in both `checks` and `failed_checks` is listed once.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_prereview_handoff.py tests/unit/test_review_outside_lock.py tests/unit/test_pre_push_gate.py tests/unit/test_worktree_script_py.py tests/unit/test_merge_risk.py tests/unit/test_auto_repair_verifier_evidence.py tests/unit/test_auto_repair.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_prereview_handoff.py --junitxml="${MINI_ORK_RUN_DIR}/bif-f2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-f2-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 scripts/mini_ork_worktree.py mini_ork/recovery/auto_repair.py tests/unit/test_prereview_handoff.py tests/unit/test_auto_repair_verifier_evidence.py` is clean.
- The in-lock hook skips the review only when a real, cached review of that exact patch exists.
