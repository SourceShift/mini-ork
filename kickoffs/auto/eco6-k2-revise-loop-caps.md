# ECOSYSTEM §6 · K2: revise loop — remove CoBa, honor declared recursion caps

## Goal

Two things in the revise loop of `mini_ork/cli/execute.py` are wired but never
change a run:

1. **CoBa revise-value stop** (`mini_ork/learning/revise_value.py`). It stops a
   round only when the measured pass rate of that round is below 0.05, after 20
   settled samples. On disk, 150 runs ever revised and framework_edit runs that
   reached round 2 published 19 of 41 times (46%). It can never fire, and its
   default is "no change". Remove it.
2. **Declared `recursion:` caps are overridden.** For the four recipes whose
   `recursion:` block turns `escalates_to` edges into repair edges
   (`doc-to-features-loop` 8, `prompt-graph-loop` 5, `recursive-validate-impl` 5,
   `spec-driven-delivery` 6), `compiler.py` sets the edge's `max_rounds` to
   `recursion.max_iterations`, but the executor caps every round at
   `min(edge.max_rounds, MO_REVISE_ROUNDS)` and `MO_REVISE_ROUNDS` defaults to
   2. The declared caps never apply, and neither does the declared
   `budget_cap_total_usd`. Make both live for recursion edges only.

`goal-loop` declares a `recursion:` block but has no `escalates_to` edges, so it
is unaffected. `retries` edges keep today's behaviour exactly.

All paths are relative to the repository root.

## Mechanism (exact spec)

### A. Remove CoBa

In `mini_ork/cli/execute.py`:
- delete `from mini_ork.learning import revise_value`;
- delete the `open_round_settles` dict and every use of it;
- delete the "CoBa: settle the revise round" block (the
  `for field, rc, _finish_reason in outcomes:` loop that calls
  `revise_value.settle_round`);
- delete both `revise_value.should_stop(...)` checks (rc channel and condition
  channel) and their `print("[revise] coba stop ...")` lines;
- delete the `revise_value.record_round(...)` call and the loop that fills
  `open_round_settles` when a round is granted.

Delete `mini_ork/learning/revise_value.py` and `tests/unit/test_revise_value.py`.
Leave migration `0081` and the `revise_rounds` table in place (no new
migration).

### B. Recursion edges use their declared cap and budget

1. Keep `escalation_edges` (already compiled at the top of the function) and
   load the recursion config once next to it:
   `recursion = load_recursion_config(workflow)` when the workflow file exists
   (the existing export block already does this; reuse that value, do not load
   twice).
2. Add a helper in `execute.py`:
   ```python
   def _round_cap(source: str, edge, escalation_edges, revise_rounds_cap: int) -> int:
   ```
   - If `revise_rounds_cap <= 0`: return `0` (the revise loop stays disabled
     for every edge, as today).
   - If `source in escalation_edges` **and** the environment variable
     `MO_REVISE_ROUNDS` is **not set** (read through `context_env`, treat an
     empty string as not set): return `edge.max_rounds`.
   - Otherwise: return `min(edge.max_rounds, revise_rounds_cap)` (today's rule).
3. Use `_round_cap(field[0], edge, escalation_edges, revise_rounds_cap)` in
   place of both `min(..., revise_rounds_cap)` expressions (rc channel and
   condition channel).
4. Budget: when a round is about to be granted for a source in
   `escalation_edges` and `recursion` is not `None`, read the run's spend with
   `mini_ork.cost_ledger.run_cost(db, run_id)`. If it returns a number
   `>= recursion.budget_cap_total_usd`, do not grant the round and print
   `[revise] recursion budget cap reached for <target>: spent $<x.xx> of $<cap.xx>`
   to stderr. A `None` spend (unreadable ledger) grants the round (fail open, as
   the orchestrator budget gate does). Check once per target per wave, in the
   grant loop, before `round_used[target]` is incremented.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute.py`
- `mini_ork/learning/revise_value.py` (delete)
- `tests/unit/test_revise_value.py` (delete)
- `tests/unit/test_eco6_k2_revise_caps.py` (new)
- `tests/unit/test_revise_loop.py` (only if an existing case pins the old
  `min(max_iterations, 2)` cap for a recursion edge; change that case's
  expectation and say why in a comment)

Do NOT modify `mini_ork/workflow/compiler.py`, `mini_ork/workflow/recursion.py`,
any recipe, the `MO_RECURSION_*` export block, or any other file.

## Tests (`tests/unit/test_eco6_k2_revise_caps.py`, hermetic)

- `importlib.util.find_spec("mini_ork.learning.revise_value") is None`, and the
  text of `mini_ork/cli/execute.py` contains neither `revise_value` nor
  `coba stop`.
- `_round_cap`:
  - retries edge (`source` not in `escalation_edges`), `max_rounds=2`,
    `revise_rounds_cap=2` → 2; `max_rounds=5` → 2.
  - recursion edge, `max_rounds=6`, `MO_REVISE_ROUNDS` unset → 6.
  - recursion edge, `max_rounds=6`, `MO_REVISE_ROUNDS=3` → 3.
  - any edge with `revise_rounds_cap=0` → 0.
- Budget: drive the revise loop the way `tests/unit/test_revise_loop.py` does,
  with a two-node workflow whose `recursion:` block sets `max_iterations: 4` and
  `budget_cap_total_usd: 1.00`, an `escalates_to` edge from a failing reviewer
  back to the implementer, and `cost_ledger.run_cost` monkeypatched to return
  `1.50`. No revise round is granted and stderr contains
  `recursion budget cap reached`. With `run_cost` returning `0.10`, round 1 is
  granted, and with `MO_REVISE_ROUNDS` unset the loop can grant more than 2
  rounds (assert round 3 is reached when the reviewer keeps failing).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k2_revise_caps.py tests/unit/test_revise_loop.py tests/langgraph_parity/test_conditional_edge_firing.py tests/unit/test_replay_antigaming.py tests/unit/test_workflow_recursion_config_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k2_revise_caps.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k2-junit.xml`

## Success criteria

- The verification command passes; no pre-existing test changes except a
  `test_revise_loop.py` case that pinned the overridden recursion cap.
- `ruff check --select F,E9 mini_ork/cli/execute.py` is clean.
- Recipes without a `recursion:` block (code-fix, framework-edit, …) behave
  byte-for-byte as before: their caps still come from
  `min(edge.max_rounds, MO_REVISE_ROUNDS)`.
