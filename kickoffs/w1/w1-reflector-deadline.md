# W1-a: the reflector workflow node must honour `--deadline`

## Goal

`--deadline N` is the operator's cap on the whole run. The post-verdict reflect
already caps its timeout to the remaining deadline: `mini_ork/cli/main.py`
(around line 2234) calls `_cap_reflect_to_deadline`. The reflector *workflow
node* does not. `_handle_reflector_early` in `mini_ork/cli/execute_handlers.py`
runs `python -m mini_ork.cli.reflect` with `timeout=_reflect_timeout_seconds()`,
a history-derived bound that can be hours, and never looks at the deadline. A
run started with `--deadline 1800` can therefore sit in an early reflect for the
whole derived bound (run-1792151796-92942 was silent for 8978 s).

Fix: bound the reflector node's subprocess with the same rule the post-verdict
reflect uses.

All paths are relative to the repository root. Read `mini_ork/cli/execute_handlers.py`
(`_deadline_state`, `_handle_reflector_early`) and `mini_ork/cli/main.py`
(`_reflect_timeout`, `_cap_reflect_to_deadline`, `_REFLECT_DEADLINE_GRACE`) first.

## Mechanism (exact spec)

### `mini_ork/cli/execute_handlers.py`

1. Extend the existing import `from mini_ork.cli.main import _module_env, _reflect_timeout_seconds`
   so it also imports `_reflect_timeout` and `_cap_reflect_to_deadline`. Keep
   `_reflect_timeout_seconds` in the import.
2. Add this helper directly above `_handle_reflector_early`:

```python
def _early_reflect_bound() -> tuple[int, str]:
    """``(seconds, why)`` for the reflector node: the derived bound, capped to an armed ``--deadline``."""
    bound, why = _reflect_timeout()
    armed, remaining = _deadline_state()
    if not armed:
        return bound, why
    # ``_deadline_state`` already established that a deadline is armed; any
    # positive ``deadline_seconds`` selects the capping branch.
    return _cap_reflect_to_deadline(
        bound, why, deadline_seconds=1, remaining_seconds=remaining, hit=remaining <= 0,
    )
```

3. In `_handle_reflector_early`, compute `bound, why = _early_reflect_bound()` before
   `subprocess.run`, and pass `timeout=bound` in place of `timeout=_reflect_timeout_seconds()`.
4. In the existing `subprocess.TimeoutExpired` branch, change the message's opening from
   `"  [reflect] timed out — "` to `f"  [reflect] timed out after {bound}s ({why}) — "`. Keep
   the rest of the text exactly as it is.

Nothing else changes. Reflection stays a side channel that never fails the node:
every return path still returns `0, "done"`.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute_handlers.py`
- `tests/unit/test_reflector_early_deadline_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_reflector_early_deadline_py.py`, hermetic)

Monkeypatch `mini_ork.cli.execute_handlers._reflect_timeout` to
`lambda *a, **k: (21600, "derived")`, monkeypatch `_deadline_state` per case, and
monkeypatch `execute_handlers._module_env` to `lambda root: {}`. Import
`_REFLECT_DEADLINE_GRACE` from `mini_ork.cli.main`.

- Unarmed `(False, 0)`: `_early_reflect_bound() == (21600, "derived")`.
- Armed `(True, 300)`: the seconds equal `300 + _REFLECT_DEADLINE_GRACE`, and the
  reason contains `"capped to the --deadline remainder"` and ends with `"derived"`.
- Armed `(True, 0)`: the seconds equal `_REFLECT_DEADLINE_GRACE`, and the reason
  contains `"deadline already hit"`.
- Armed with plenty of room `(True, 100000)`: `(21600, "derived")`.
- Handler wiring: monkeypatch `execute_handlers.subprocess.run` with a fake that records
  `kwargs["timeout"]` and returns `types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")`.
  With `(True, 300)`, `_handle_reflector_early(str(tmp_path))` returns `(0, "done")` and
  the recorded timeout is `300 + _REFLECT_DEADLINE_GRACE`.
- Timeout path: the fake raises `subprocess.TimeoutExpired(cmd="reflect", timeout=1)`.
  The handler returns `(0, "done")`, and captured stderr contains
  `f"timed out after {300 + _REFLECT_DEADLINE_GRACE}s"`.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_reflector_early_deadline_py.py tests/unit/test_reflect_deadline_cap.py tests/unit/test_reflect_deadline.py`
- `ruff check --select F,E9 mini_ork/cli/execute_handlers.py tests/unit/test_reflector_early_deadline_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_reflector_early_deadline_py.py --junitxml="${MINI_ORK_RUN_DIR}/w1-reflector-deadline-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w1-reflector-deadline-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- With no `--deadline` armed, the reflector node's timeout is exactly today's.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
