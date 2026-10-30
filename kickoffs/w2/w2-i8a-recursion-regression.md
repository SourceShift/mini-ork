# W2-I8a: a recursion loop must stop when a node that passed in an earlier round fails again

## Goal

SDD roadmap epic I8 (`kickoffs/sdd-mechanisms/roadmap.md`, id `sdd-i8-guarded-loop`), AC2:
*"An earlier-passing AC that regresses in a later iteration rolls back to the checkpoint."*

Since 72ecae59, a recipe that declares a `recursion:` block runs a real repair loop:
- `escalates_to` edges become `CompiledWorkflow.escalation_edges`;
- `max_iterations` caps the rounds through `_round_cap`;
- `budget_cap_total_usd` is checked before each round is granted (`mini_ork/cli/execute.py`,
  the `[revise] recursion budget cap reached` branch).

Nothing compares rounds, though. If round 2 makes a verifier fail that **passed** in round 1,
the loop grants round 3 from the regressed state, revising a slice that was already correct and
spending budget walking away from a known-good tree. Recipes affected: spec-driven-delivery,
goal-loop, recursive-validate-impl, prompt-graph-loop, doc-to-features-loop.

This kickoff is **I8a: detect and stop**. When a repair source that passed in an earlier wave
of this run fails now, the loop does not grant another round for that target. It says so
loudly and records it. (I8b, a later kickoff, will restore the checkpoint tree; I8a does not
touch the target tree.)

**Do not write a second loop.** Add the guard to the existing round-granting code in
`mini_ork/cli/execute.py`.

All paths are relative to the repository root. Read these first:
- `mini_ork/cli/execute.py`, around the `revise_groups` loop. Note the `outcomes` list of
  `(field, rc, finish_reason)`, where `field[0]` is the node id, plus `round_used`,
  `escalation_edges`, `recursion`, and the budget-cap `continue` branch. Mirror the
  budget-cap branch.
- `mini_ork/workflow/recursion.py` (`RecursionConfig`, `load_recursion_config`).
- `tests/unit/test_revise_loop.py`: `_run`, `_VERIFIER_SCRIPT`, `_escalation_workflow`, and
  the escalation-wiring tests near the bottom.

## Mechanism (exact spec)

### `mini_ork/workflow/recursion.py`: add a pure helper

```python
def regressed_sources(ever_passed, failed_node_ids) -> list[str]:
    """Node ids that failed this wave after passing in an EARLIER wave of the same run.

    Pure: no I/O. ``ever_passed`` is the set of node ids that finished rc 0 in any
    previous wave; ``failed_node_ids`` are this wave's failed repair sources.
    """
    return sorted({n for n in failed_node_ids if n in ever_passed})
```

### `mini_ork/cli/execute.py`

1. Next to `round_used` (initialised once per run, before the wave loop), add
   `ever_passed: set[str] = set()`.
2. In the wave's revise handling, immediately before `if revise_groups:`, compute and then update:
   ```python
   _regressed_now = set(regressed_sources(
       ever_passed, [f[0] for f, rc, _fr in outcomes if rc != 0]))
   ever_passed |= {f[0] for f, rc, _fr in outcomes if rc == 0}
   ```
   Compute `_regressed_now` against the set as it was BEFORE this wave's passes are added.
3. Inside `for target, failed_sources in revise_groups.items():`, after `_escalation_fired` is
   computed and before the budget-cap check, add:
   ```python
   if _escalation_fired and recursion is not None:
       _regressed = sorted(
           _src_field[0] for _src_field, _src_rc, _src_fr in failed_sources
           if _src_field[0] in _regressed_now)
       if _regressed:
           print(f"[revise] recursion regression for {target}: "
                 f"{', '.join(_regressed)} passed in an earlier round and now fail "
                 "— stopping the loop instead of revising a passing slice",
                 file=sys.stderr)
           _append_run_note(db, run_id,
                            f"recursion_regression: {target} ({', '.join(_regressed)})")
           continue
   ```
4. Import `regressed_sources` from `mini_ork.workflow.recursion`, where `load_recursion_config`
   is imported.

Recipes WITHOUT a `recursion:` block (code-fix, framework-edit, every `retries`-only loop) are
untouched: the guard runs only when `_escalation_fired and recursion is not None`.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute.py`
- `mini_ork/workflow/recursion.py`
- `tests/unit/test_recursion_regression_py.py` (new)
- `tests/unit/test_revise_loop.py` (only to add a `pass_first` behaviour to `_VERIFIER_SCRIPT`,
  meaning pass when `n == 0` and fail afterwards; no existing behaviour changes)

Do NOT modify any other file.

## Tests

`tests/unit/test_recursion_regression_py.py`:
- `regressed_sources` unit cases:
  - `({"test"}, ["test"]) == ["test"]`
  - `(set(), ["test"]) == []`
  - `({"typecheck"}, ["test"]) == []`
  - order-independent and de-duplicated: `({"a", "b"}, ["b", "a", "b"]) == ["a", "b"]`.
- Loop wiring, driven through `test_revise_loop._run` (import its helpers). Build a workflow
  from `_escalation_workflow(recursion={"max_iterations": 3, "budget_cap_total_usd": 100.0})`
  or `_base_workflow()` plus a `recursion:` block, whose repair source is the `test` verifier.
  Use `test="pass_first"` with a reviewer that fails round 1:
  - The run's stderr contains `recursion regression`.
  - The run note `recursion_regression:` is in `task_runs.notes`.
  - The implementer ran exactly once more than the round that regressed; it was **not**
    re-dispatched after the regression.
- Control 1: the same workflow WITHOUT a `recursion:` block (plain `retries`) behaves exactly as
  today. There is no regression message, and the round count equals the existing
  `test_revise_loop` expectation for that shape.
- Control 2: a recursion workflow where the verifier only ever fails (`test="fail"`) never
  prints `recursion regression` (it never passed).

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_recursion_regression_py.py tests/unit/test_revise_loop.py tests/unit/test_eco6_k2_revise_caps.py`
- `python3 -m pytest -q -p no:asyncio tests/langgraph_parity/test_conditional_edge_firing.py -k "revise or recursion or escalat"`
- `ruff check --select F,E9 mini_ork/cli/execute.py mini_ork/workflow/recursion.py tests/unit/test_recursion_regression_py.py tests/unit/test_revise_loop.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_recursion_regression_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-i8a-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-i8a-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- Without a `recursion:` block the loop is byte-for-byte today's behaviour.
- With one, a regression stops the loop for that target, prints the reason and writes the run note.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
