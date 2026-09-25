# G2 — give the goal-loop a null verdict ("nothing to fix")

## Goal

The goal-loop's hunt classifies a unit by **one** predicate run: `evaluate_units` in
`recipes/goal-loop/lib/goal_state.py` runs `predicate_cmd <unit_id>` once and reads
`proc.returncode`. A red that is **not reproducible** — a timeout, a port collision, a
race, a network blip, a fixture that another process touched — is therefore
indistinguishable from a real defect.

The loop then does the worst possible thing with it: it selects the unit
(`_select_units` picks every `pass=False`) and spends a `code-fix` child against a unit
that has nothing to fix. The child cannot find a defect that isn't there, so it either
fabricates a diff or no-ops; either way the wave pays, the signature does not move, and
the loop blames itself (`diverged`). The literature on this is unambiguous — arXiv
2609.10123 shows LLMs consistently claim to detect bugs in entirely bug-free programs,
and arXiv 2609.01345 ("Cheap Verifiers, Large Blind Spots") is the same failure from the
verifier side.

This cycle adds the missing third answer. A unit is pass, **confirmed fail**, or
**not-reproduced** — and a not-reproduced red is a **null verdict**: the honest output is
"nothing to fix", and the loop must stop saying so rather than dispatch a child.

Reference: arXiv 2609.10123 (bug-free programs) and 2609.01345 (cheap verifiers, large
blind spots).

## What already exists (read it; do not rewrite it)

- `recipes/goal-loop/lib/goal_state.py` — `list_units(target_cwd, units_cmd)`,
  `evaluate_units(target_cwd, predicate_cmd, units)` (the seam to widen),
  `harvest_evidence(...)`, `read_obligations(...)`. `evaluate_units` today writes
  `results[unit] = {"pass": proc.returncode == 0, "reason": reason_text}` — that dict is
  **the contract** the rest of the recipe reads.
- `recipes/goal-loop/lib/transforms.py` — `goal_state_eval` (line ~76) calls
  `evaluate_units` and writes `goal-state.json`; `_select_units` (line ~197) picks the
  `not state.get("pass", False)` units in numeric-aware order with a **quarantine
  starvation guard** ("if EVERY failing unit is quarantined the exclusion is dropped");
  `classify_failure` / `_operator_for` (lines ~155–194) name the operator class
  (shadow-only, `MO_GOAL_OPERATOR_TYPING`).
- `recipes/goal-loop/lib/drive.py` — the stop ladder in the driver loop: **1. goal_met**
  (wave verdict `"pass"`, line ~888) → **2. budget** (~915) → **3. diverged** (~941) →
  **4. all_quarantined** (~955) → `max_waves_reached` (~970). The wave verdict is the
  payload assembled around lines 340–434: it already folds `goal-state.json` into
  `payload["unit_reasons"]` (line ~362) and reads `sweep-result.json` / `sweep-plan.json`
  for `attempted`, `child_diagnostics`, `evidence`, `operators`.
- `tests/unit/test_goal_loop_driver.py` — the file-path import pattern to copy
  (`_load(name, path)` with `importlib.util`; `RECIPE_DIR = Path(__file__).resolve().parents[2] / "recipes" / "goal-loop"`).

## Deliverable 1 — a confirmation pass in `evaluate_units`

`recipes/goal-loop/lib/goal_state.py`:

```python
def evaluate_units(
    target_cwd: str,
    predicate_cmd: str,
    units: Iterable[str],
    *,
    confirm_runs: int = 1,
) -> dict[str, UnitState]:
```

Semantics — **the `pass` and `reason` values must not move**; the new keys are additive:

- Run the predicate **once**. `rc == 0` → `pass=True`, `reproduced=True`, `attempts=1`,
  `reason` = the first stdout/stderr line (exactly today's text).
- A failing first run with `confirm_runs > 1` → **re-run** until either some attempt exits
  `0` or `confirm_runs` attempts are exhausted.
  - a re-run exits 0 ⇒ the red **evaporated**: `reproduced=False`, `attempts=<n used>`,
    and `reason` is prefixed `"flake: did not reproduce (<n> attempts): "` + the **first**
    attempt's reason text.
  - every attempt failed ⇒ `reproduced=True`, `attempts=<n used>`, `reason` is the first
    attempt's text, unchanged.
- `confirm_runs <= 1` ⇒ exactly one run, `reproduced=True`, `attempts=1`. This is the
  default and it is **byte-identical to today** apart from the two new keys.
- Clamp defensively: `confirm_runs = max(1, int(confirm_runs))` — a bad value must not
  raise out of the hunt.

Every returned dict gains exactly two keys: `"reproduced": bool` and `"attempts": int`.

## Deliverable 2 — a null verdict must not be dispatched

`recipes/goal-loop/lib/transforms.py`:

1. `goal_state_eval` reads `MO_GOAL_CONFIRM_RUNS` (default `"1"`, int-parsed with the same
   defensive fallback `goal_sweep_plan` uses for `MO_GOAL_MAX_CHILDREN_PER_WAVE`) and
   passes it to `evaluate_units(..., confirm_runs=...)`.

2. `_select_units` excludes the null verdicts:

   ```python
   failing = sorted(
       (uid for uid, st in goal_state.items()
        if not st.get("pass", False) and st.get("reproduced", True)),
       key=_unit_sort_key,
   )
   ```

   The `state.get("reproduced", True)` default is what keeps a **legacy** `goal-state.json`
   (written before this change, no `reproduced` key) selecting exactly as it does today.

3. **The deliberate asymmetry — state it in the docstring.** The quarantine exclusion has a
   starvation guard (drop the exclusion if it would empty the selection, so the wave still
   dispatches and `all_quarantined` ends the loop cleanly). The null-verdict exclusion must
   have **no such guard**: if every remaining red is not reproducible, `_select_units`
   returns `[]`, and an empty selection **is** the honest verdict. Dispatching a child
   anyway is the behaviour this cycle exists to remove.

## Deliverable 3 — a `nothing_to_fix` stop, so the loop says the true thing

`recipes/goal-loop/lib/drive.py`. Without this the fix is half-done: an empty selection
produces no children, the signature repeats, and the loop stops with **`diverged`** — which
blames the loop for a defect that does not exist. The truth is "nothing to fix".

1. In the wave-payload builder, next to the `unit_reasons` block (~line 362), read the same
   `goal-state.json` and add:

   ```python
   payload["unit_reproduced"] = {
       str(uid): bool(v.get("reproduced", True))
       for uid, v in gs.items() if isinstance(v, dict)
   }
   ```

2. In the driver loop, read it beside `unit_reasons` (~line 786) with the same shape guard:

   ```python
   raw_reproduced = verdict_dict.get("unit_reproduced")
   unit_reproduced = (
       {str(k): bool(v) for k, v in raw_reproduced.items()}
       if isinstance(raw_reproduced, dict) else None
   )
   ```

3. Add stop **1b** between `goal_met` and `budget` (a null verdict outranks budget —
   there is nothing to spend on; do NOT put it after `budget`, or a null verdict with a
   nearly-exhausted budget reports `budget` instead of the truth):

   ```python
   # 1b. nothing_to_fix — every still-failing unit is a confirmed-fail we could NOT
   # reproduce. There is no defect to patch, so no wave can move this.
   if failing_after and unit_reproduced is not None and all(
       not unit_reproduced.get(u, True) for u in failing_after
   ):
       payload = {
           "stop": "nothing_to_fix",
           "waves": wave_no,
           "failing_units": failing_after,
           "quarantined_units": sorted(quarantined),
       }
       save_state(state, resolved_state_dir)
       _write_final_verdict(resolved_state_dir, payload)
       return payload
   ```

   `unit_reproduced.get(u, True)` defaults to **True** so a unit with no recorded flag
   (legacy artifact) can never trigger the stop — it is conservative in the direction that
   preserves today's behaviour.

## Tests — `tests/unit/test_goal_loop_null_verdict.py` (new)

Hermetic: no network, no lane, no `bin/mini-ork`. Load `goal_state.py`, `transforms.py`
and `drive.py` by file path with the `_load` pattern from `test_goal_loop_driver.py`.
Drive the flaky predicate with a **counter file**: a small `predicate.py` in `tmp_path`
that appends a line to `count.txt` and exits `1` the first time, `0` thereafter.

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `evaluate_units(..., confirm_runs=2)` against the fails-then-passes predicate yields
   `pass is False`, `reproduced is False`, `attempts == 2`, and a `reason` starting
   `"flake: did not reproduce"`.
2. `evaluate_units(..., confirm_runs=3)` against an always-failing predicate yields
   `pass is False`, `reproduced is True`, `attempts == 3`, and a `reason` with **no**
   `"flake:"` prefix.
3. `evaluate_units(...)` (default) against an always-failing predicate returns exactly one
   run's worth — `attempts == 1`, `reproduced is True` — and the counter file shows the
   predicate ran **once**. A passing predicate likewise returns `reproduced is True`.
4. `_select_units` **skips** a unit whose state is `{"pass": False, "reproduced": False}`
   while still selecting `{"pass": False, "reproduced": True}`, and never selects a
   `{"pass": True}` unit.
5. `_select_units` on a **legacy** `goal-state.json` dict (no `reproduced` key at all)
   selects exactly as it does today — assert the selection equals the one from an
   equivalent all-reproduced dict.
6. The asymmetry: `_select_units` returns `[]` when **every** failing unit is
   `reproduced=False` — it must NOT fall back to dispatching them, unlike the quarantine
   guard. Assert the quarantine guard still works in the same test (a quarantined-but-
   reproduced failing unit still yields a selection when it is the only one left).
7. The driver stop: run `drive(...)` (the same fakes `test_goal_loop_driver.py` uses) with
   a wave whose verdict reports `failing_after=["u1"]` and
   `unit_reproduced={"u1": False}` → stop == `"nothing_to_fix"`; and with
   `unit_reproduced={"u1": True}` → the loop does **not** stop with `nothing_to_fix`
   (assert `!=`, not the specific next stop).
8. A wave verdict with **no** `unit_reproduced` key at all does not stop with
   `nothing_to_fix` — the legacy/conservative path.

## Files in scope

- `recipes/goal-loop/lib/goal_state.py` — `evaluate_units` confirmation pass
- `recipes/goal-loop/lib/transforms.py` — `MO_GOAL_CONFIRM_RUNS` + `_select_units` exclusion
- `recipes/goal-loop/lib/drive.py` — `unit_reproduced` threading + the `nothing_to_fix` stop
- `tests/unit/test_goal_loop_null_verdict.py` (new)
- `tests/unit/test_goal_loop_driver.py` — nothing required; leave it green
- `tests/unit/test_goal_loop_recipe.py` — nothing required; leave it green

Do **not** touch `recipes/goal-loop/lib/loop_state.py`, `assurance.py`, `loop_ledger.py`,
any `workflow.yaml` / `task_class.yaml`, `mini_ork/**`, `web/**`, or any other recipe. Do
not add a new operator to the taxonomy, do not change `classify_failure`, and do not make
`nothing_to_fix` reachable when `MO_GOAL_CONFIRM_RUNS` is unset.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_goal_loop_null_verdict.py -q
python3.11 -m pytest tests/unit/test_goal_loop_driver.py tests/unit/test_goal_loop_recipe.py -q
python3.11 -m py_compile recipes/goal-loop/lib/goal_state.py recipes/goal-loop/lib/transforms.py recipes/goal-loop/lib/drive.py
```

## Self-application measurement

Zero model spend, and it is the point of the cycle: prove the null verdict actually fires
on a real flaky predicate rather than only in a test double.

```bash
python3.11 - <<'PY'
import importlib.util, pathlib, subprocess, tempfile, os, json
spec = importlib.util.spec_from_file_location("gs", "recipes/goal-loop/lib/goal_state.py")
gs = importlib.util.module_from_spec(spec); spec.loader.exec_module(gs)
d = tempfile.mkdtemp()
open(os.path.join(d, "count.txt"), "w").close()
open(os.path.join(d, "pred.py"), "w").write(
    "import sys,os\n"
    "p=os.path.join(os.path.dirname(__file__),'count.txt')\n"
    "open(p,'a').write('x')\n"
    "n=len(open(p).read())\n"
    "print('flaky red' if n==1 else 'clean')\n"
    "sys.exit(1 if n==1 else 0)\n")
r = gs.evaluate_units(d, "python3 pred.py", ["u1"], confirm_runs=2)
print(json.dumps(r, indent=2))
assert r["u1"]["reproduced"] is False and r["u1"]["pass"] is False, r
print("NULL VERDICT CONFIRMED")
PY
```

Report the printed dict verbatim. If the null verdict does **not** fire, that is the finding
— say so with the output rather than adjusting the claim.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-g2-null-verdict.json`:

```json
{"confirm_pass_implemented": true, "null_verdict_observed": true,
 "attempts_observed": <int>, "selection_skips_null": true,
 "legacy_state_unchanged": true, "nothing_to_fix_stop_reachable": true,
 "stop_absent_when_flag_unset": true, "driver_tests_green": true}
```

## Done When

- `tests/unit/test_goal_loop_null_verdict.py` is green, and both pre-existing goal-loop test
  files are still green.
- `evaluate_units` with `confirm_runs=1` produces a dict whose `pass` / `reason` are
  identical to the previous implementation — verifiable by reading the diff, not a summary.
- `_select_units` returns `[]` for an all-null-verdict goal state, and is unchanged for a
  legacy state dict with no `reproduced` key.
- The driver stops with `stop == "nothing_to_fix"` only when every still-failing unit is
  recorded `reproduced=False`; with `MO_GOAL_CONFIRM_RUNS` unset it is unreachable.
- `${MINI_ORK_RUN_DIR}/rsi-g2-null-verdict.json` exists with the fields above, and
  `attempts_observed` is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
