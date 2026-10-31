# W2-I8a-gaps: the recursion regression guard covers `retries` repairs and survives `recover`

## Goal

I8a (ff35acff, `kickoffs/w2/w2-i8a-recursion-regression.md`) stops a recursion loop when a
repair source that passed in an earlier wave fails now. Pre-push review 193 found two holes,
both confirmed in `mini_ork/cli/execute.py`:

1. **Only escalation repairs are guarded.** The guard runs under
   `if _escalation_fired and recursion is not None:`. A recipe that declares `recursion:` but
   repairs a node through a `retries` edge, which is legal and capped by `MO_REVISE_ROUNDS`, is
   never checked. A regression there is revised again from the regressed state.
2. **The memory is process-local.** `ever_passed` is a fresh `set()` in every process. A run
   resumed with `mini-ork recover` (after a cost pause, a review pause, or a crash) forgets which
   nodes passed before the restart, so a post-recover regression goes undetected.

Fix both, inside the existing loop. **Do not write a second loop.**

Read `mini_ork/cli/execute.py` first:
- `ever_passed` (initialised next to `round_used`)
- the `_regressed_now` computation before `if revise_groups:`
- the two `if _escalation_fired and recursion is not None:` blocks (regression guard, then
  budget cap)
- `live_run_dir`, and `_archive_revise_round`, which writes `revise/round-<n>/`

Then read `mini_ork/workflow/recursion.py` (`regressed_sources`) and
`tests/unit/test_recursion_regression_py.py`.

## Mechanism (exact spec)

### `mini_ork/workflow/recursion.py`: add two pure-ish helpers

```python
EVER_PASSED_FILE = "ever-passed.json"   # under <run_dir>/revise/


def load_ever_passed(run_dir) -> set[str]:
    """Node ids recorded as passed in earlier waves of this run (``set()`` when none).

    Reads ``<run_dir>/revise/ever-passed.json`` (a JSON list of strings). A missing file is
    ``set()``. An unreadable or malformed file raises ``ValueError``: a corrupted record must
    not silently disable the guard.
    """


def save_ever_passed(run_dir, ever_passed) -> None:
    """Atomically write ``sorted(ever_passed)`` to ``<run_dir>/revise/ever-passed.json``."""
```

Use a same-dir temp file plus `os.replace`, and create `revise/` when needed.

### `mini_ork/cli/execute.py`

1. Where `ever_passed` is initialised, load it with `load_ever_passed(live_run_dir)` instead of
   `set()`. On `ValueError`, print `execute: revise/ever-passed.json: <error>` to stderr and
   return 2. Use the same fatal-setup return style the function already uses.
2. Right after `ever_passed |= {...}` (each wave), call `save_ever_passed(live_run_dir, ever_passed)`.
   Wrap it in `try/except OSError`, printing `  [warn] ever-passed record not saved: <exc>` to
   stderr. Saving is enrichment, so a write failure warns and must not fail the run.
3. Change the **regression-guard** condition from `if _escalation_fired and recursion is not None:`
   to `if recursion is not None:`. The budget-cap block below it keeps
   `_escalation_fired and recursion is not None` exactly as it is. Update the guard's comment:
   in a recipe that declares `recursion:`, any repair round (`retries` or `escalates_to`) is
   guarded.

Recipes WITHOUT a `recursion:` block stay byte-for-byte unchanged. They never reach the guard.
`ever-passed.json` is only read and written when `live_run_dir` exists, and it is harmless
otherwise; keep the existing behaviour of every non-recursion test.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute.py`
- `mini_ork/workflow/recursion.py`
- `tests/unit/test_recursion_regression_py.py` (append tests; change no existing test)

Do NOT modify any other file.

## Tests (append to `tests/unit/test_recursion_regression_py.py`)

`load_ever_passed` / `save_ever_passed`:
- Round-trip in `tmp_path`.
- A missing file gives `set()`.
- A malformed file (`"{not json"`) raises `ValueError`.
- Saving creates `revise/`.
- The file content is a sorted JSON list.

Loop wiring, reusing the module's existing `_run`-based helpers:
- **Retries-repair recursion recipe:** a workflow with a `recursion:` block whose `test`
  verifier repairs through a `retries` edge, `test="pass_first"`. It prints `recursion regression`
  and writes the `recursion_regression:` run note.
- **Control:** the same `retries` workflow WITHOUT a `recursion:` block prints no regression
  message, exactly as today.
- **Persistence:** after a run in which `test` passed, `<run_dir>/revise/ever-passed.json`
  contains `"test"`.
- **Resume:** seed `<run_dir>/revise/ever-passed.json` with `["test"]` before a recursion run
  where `test="fail"`. The guard fires on the first failing wave, proving the record is loaded.
- **Corrupted record:** with a corrupted record the run returns rc 2 and stderr names
  `ever-passed.json`.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_recursion_regression_py.py tests/unit/test_revise_loop.py tests/unit/test_eco6_k2_revise_caps.py tests/unit/test_review_after_nodes_decl_py.py`
- `python3 -m pytest -q -p no:asyncio tests/langgraph_parity/test_conditional_edge_firing.py -k "revise or recursion or escalat"`
- `ruff check --select F,E9 mini_ork/cli/execute.py mini_ork/workflow/recursion.py tests/unit/test_recursion_regression_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_recursion_regression_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-i8a-gaps-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-i8a-gaps-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- No `recursion:` block means no behaviour change.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
