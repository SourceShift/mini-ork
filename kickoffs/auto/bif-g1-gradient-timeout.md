# G1: the gradient extractor's 120 s cap kills a quarter of its calls just before they finish

## Goal

`_default_dispatch` in `mini_ork/learning/gradient_extractor.py` (~325-360)
calls `llm_dispatch` with a hard-coded `"--timeout", "120"`.

Measured on `llm_calls`, `feature_name = 'mini-ork:gradient-extract'`, 14 days
to 2026-10-27:

- 2,372 successes: p50 77 s, p75 93 s, p90 107 s, p95 113 s. 16% of successes
  took over 100 s. The distribution is crowded against the cap, so its real
  tail runs past it.
- 411 failures on glm, every one `timeout after 120.0s`: 21.9 hours, 45% of
  ALL failed-call time in the home (48.8 h). Per day, 20-35% of extractions
  time out.

Each timed-out extraction loses that node's gradient, the learning loop's
input. Make the cap configurable and raise its default to 300 s.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `mini_ork/learning/gradient_extractor.py`:

1. Add a module constant `_DEFAULT_TIMEOUT_S = 300` and a function
   `_timeout_s() -> int`:
   - read `MINI_ORK_GRADIENT_TIMEOUT_S`;
   - unset or empty → `_DEFAULT_TIMEOUT_S`;
   - a positive integer → that value;
   - anything else (non-numeric, zero, negative) → `_DEFAULT_TIMEOUT_S`, plus
     ONE stderr line `[gradient] MINI_ORK_GRADIENT_TIMEOUT_S=<value> is not a positive integer; using 300`.
2. `_default_dispatch` passes `"--timeout", str(_timeout_s())` instead of
   `"--timeout", "120"`. Nothing else in the argv changes.

## Files in scope (touch ONLY these)

- `mini_ork/learning/gradient_extractor.py`
- `tests/unit/test_gradient_extractor_py.py`

Do NOT modify `mini_ork/learning/pattern_induction.py` (it holds a process
lock for its whole dispatch, so a longer cap there would serialise every
worker behind one hung call), `mini_ork/dispatch/`, `docs/`, or any path in
`config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (append to `tests/unit/test_gradient_extractor_py.py`, hermetic, no LLM)

Monkeypatch `mini_ork.dispatch.llm_dispatch.llm_dispatch` with a fake that
records its `argv` and returns 0, then call `_default_dispatch` with a short
prompt:

1. `MINI_ORK_GRADIENT_TIMEOUT_S` unset → argv has `--timeout` followed by `300`.
2. `MINI_ORK_GRADIENT_TIMEOUT_S=450` → `450`.
3. `MINI_ORK_GRADIENT_TIMEOUT_S` set to `abc`, then `0`, then `-5` → `300`
   each time, and stderr carries the warning line (use `capsys`).
4. `--max-turns 5` and `--node-type gradient-extract` are unchanged.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_gradient_extractor_py.py tests/unit/test_pattern_induction_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_gradient_extractor_py.py --junitxml="${MINI_ORK_RUN_DIR}/bif-g1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-g1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/learning/gradient_extractor.py tests/unit/test_gradient_extractor_py.py` is clean.
- An unusable `MINI_ORK_GRADIENT_TIMEOUT_S` falls back to 300 with a warning, never to no cap.
