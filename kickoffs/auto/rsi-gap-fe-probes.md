# RSI gap: a frozen probe set for framework-edit, and per-recipe probe budgets

## Goal

The apply loop promotes a learned prompt directive only on a real held-out
measurement: `mini_ork/learning/probe_scorer.py` runs the recipe on its frozen
probes with and without the directive. Only `recipes/code-fix/probes/` and
`recipes/obs-smoke/probes/` exist, so every framework-edit candidate (planner,
implementer, reviewer, both lenses — 118+ live gradients for the implementer
alone) is quarantined "probe scorer measured nothing" and no measured promotion
has ever landed. Two engine defects would also break a framework-edit probe set
on day one: the global defaults (`MO_APPLY_PROBE_BUDGET_USD` 2.0,
`MO_APPLY_PROBE_TIMEOUT_S` 600) cannot fit one framework-edit run ($3-12,
20-60 min), and `_launch_run` passes no `--placement`, so on a host with a
Daytona environment profile every probe arm is sent to Daytona.

Add the probe set and make the scorer able to run it.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/learning/probe_scorer.py`:
   - Per-recipe probe config: if `<recipe>/probes/probe-config.yaml` exists, read
     `budget_usd`, `timeout_s`, `control_n`, `max_tasks` from it as the DEFAULTS
     for that recipe; an explicitly set env var (`MO_APPLY_PROBE_BUDGET_USD`,
     `MO_APPLY_PROBE_TIMEOUT_S`, `MO_APPLY_PROBE_CONTROL_N`,
     `MO_APPLY_PROBE_MAX_TASKS`) still wins. A config file that exists but does
     not parse, or has a non-numeric value, raises `ValueError` naming the file
     (fail closed: never silently fall back to the $2 default). Apply this in
     every place those four env vars are read today (`_load_probes`,
     `_launch_run`, `probe_score`, `probe_score_code`).
   - `_launch_run` launches `run --placement local <recipe> <kickoff>` (probe
     arms run against a private fixture tmpdir on this host; remote placement
     is never correct for them).
2. New `recipes/framework-edit/probes/probe-config.yaml`:
   `budget_usd: 60`, `timeout_s: 3600`, `control_n: 2`, `max_tasks: 2`.
3. New frozen probes under `recipes/framework-edit/probes/`, each with the same
   FROZEN preamble style as `recipes/code-fix/probes/probe-1.md` (say it grades
   framework-edit directives, cite GRASP 2605.29668, and say never to edit it to
   fit a candidate), and each with these sections: `## Problem`,
   `## Files in scope`, `## Verification commands` (one bullet with the inline
   command shown), `## Rules` ("Edit files directly; do not emit unified diffs.").
   Each fixture is a plain directory of Python files under
   `recipes/framework-edit/probes/fixtures/<probe-stem>/` (no `__pycache__`):
   - `probe-1.md` (gate, one file). Fixture `ledger.py`:
     `balance(entries)` sums `(kind, amount)` tuples where `kind` is `"credit"`
     or `"refund"`; the bug: refunds are ADDED instead of subtracted.
     `test_ledger.py` (read-only) asserts `balance([]) == 0`,
     `balance([("credit", 10), ("refund", 3)]) == 7`,
     `balance([("refund", 5)]) == -5`, and that an unknown kind raises
     `ValueError`. Ship `ledger.py` with only the refund bug (unknown kinds
     already raise). Command: `pytest -q test_ledger.py`.
   - `probe-2.md` (gate, two files). Fixture `units.py` with
     `to_meters(value, unit)` supporting `"m"` and `"cm"` only (an unknown unit
     raises `KeyError`), and `route.py` with `total_distance(legs)` summing
     `to_meters` over `(value, unit)` legs. Task: add `"km"` to `units.py` AND
     make `total_distance` return the sum rounded to 3 decimals.
     `test_route.py` (read-only) asserts `to_meters(2, "km") == 2000`,
     `total_distance([(1, "km"), (50, "cm")]) == 1000.5`,
     `total_distance([(0.1, "m"), (0.2, "m")]) == 0.3`, and that
     `to_meters(1, "mi")` raises `KeyError`. Command: `pytest -q test_route.py`.
   - `probe-3.md` (anchor: the file starts with `---\nanchor: true\n---`).
     Fixture `greet.py` with `greet(name)` returning `f"Hello, {name}"`; task:
     return `f"Hello, {name}!"`. `test_greet.py` (read-only) asserts
     `greet("Ada") == "Hello, Ada!"`. Command: `pytest -q test_greet.py`.
   Each probe's `## Files in scope` names exactly the files the task may edit
   and marks the test file read-only.

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_scorer.py`
- `recipes/framework-edit/probes/probe-config.yaml` (new)
- `recipes/framework-edit/probes/probe-1.md`, `probe-2.md`, `probe-3.md` (new)
- `recipes/framework-edit/probes/fixtures/probe-1/ledger.py`, `test_ledger.py` (new)
- `recipes/framework-edit/probes/fixtures/probe-2/units.py`, `route.py`, `test_route.py` (new)
- `recipes/framework-edit/probes/fixtures/probe-3/greet.py`, `test_greet.py` (new)
- `tests/unit/test_probe_config.py` (new)

Do NOT modify any other file. Do not edit any file under `recipes/code-fix/` or
`recipes/obs-smoke/`.

## Tests (`tests/unit/test_probe_config.py`, hermetic)

- with no `probe-config.yaml` → the old global defaults (2.0 / 600 / 3 / 2);
- with the framework-edit config and no env → 60 / 3600 / 2 / 2;
- env `MO_APPLY_PROBE_BUDGET_USD=5` beats the config value;
- an unparseable config → `ValueError` naming the file;
- `_load_probes("framework_edit")` returns the two gate probes plus the anchor,
  with the anchor flagged;
- `_launch_run` builds a command containing `--placement local` before the
  recipe name (monkeypatch the subprocess call; never launch a real run);
- each framework-edit fixture is consistent with its probe: in a temp copy,
  the shipped fixture FAILS its test, and applying the fix described in the
  probe (done inside the test with plain string replacement on the temp copy)
  makes the test PASS. This proves every probe is solvable and not already
  solved.

## Verification commands

- `pytest -q tests/unit/test_probe_config.py tests/unit/test_probe_scorer_control_arm.py tests/unit/test_probe_validity.py tests/unit/test_probe_scorer_code_arm.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_probe_config.py --junitxml="${MINI_ORK_RUN_DIR}/fe-probes-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/fe-probes-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/probe_scorer.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
