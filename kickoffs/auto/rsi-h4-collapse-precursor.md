# H4 — silent-collapse precursors: early warning that fires before the metrics move

## Goal

`collapse_detector` (G7) fires when the score rises *while the anchor falls* — a
contradiction between two numbers the loop already watches. That is a late signal: by the
time the anchor has visibly dropped, the degradation is on the record.

Silent Collapse in Recursive Learning Systems (arXiv 2605.14588) identifies something
earlier and quieter: under recursive conditions, a system's **internal distributions
contract while the standard metrics stay flat or improving**. The paper names three
trajectory-level precursors that manifest *multiple generations before* any degradation in
standard validation metrics — (1) contraction of **anchor entropy**, (2) freezing of
**representation drift**, (3) erosion of **tail coverage** — and frames the point as a lead
time: the internal signal moves first, the visible metric moves later, and the gap between
them is the warning window.

This cycle builds the **detector half**: a pure function that measures the two precursors
mini-ork can actually observe, tests whether each is genuinely contracting rather than
noise, and reports the **lead time** — how many generations the precursor fired before the
visible score degraded. It measures; it does not act. The paper's MTR framework also
*regulates* the learning intensity off these signals; a regulator built on an unvalidated
detector is the higher-stakes cycle that must wait.

**The honest limit, stated up front.** The paper recurses on **weights**, so precursor (2),
freezing of representation drift, is a statement about a hidden-layer representation in a
neural network. mini-ork recurses on **harness and prompt**, and has no weights and no
hidden representation to drift. Precursor (2) therefore **does not map**, and this module
must say so in its vocabulary rather than inventing a placeholder that looks like a
measurement. Two of three precursors map; the module carries exactly those two and names
the third as unmapped. A detector that quietly covers two-thirds of a taxonomy and reports
it as the whole is the failure this file exists to avoid.

Reference: arXiv 2605.14588 (Silent Collapse in Recursive Learning Systems).

## What already exists (read it; do not rewrite it)

- `mini_ork/learning/collapse_detector.py` — G7, the sibling, and the thing this sharpens.
  `MIN_STEPS = 4`, half-split means (first-half mean vs second-half mean) rather than a
  fitted slope, because at small n a least-squares slope is determined almost entirely by
  the two endpoints. Reuse that discipline and the `None`-over-empty rule; do **not** import
  or edit it. **The distinction matters:** `collapse_detector` fires on
  `score_rise > 0 AND anchor_drop > 0` and returns `"halt"`. This module fires *earlier*, on
  a contraction of the anchor's **internal distribution entropy** and its **tail coverage**,
  while the visible score is still rising — the case `collapse_detector` deliberately does
  not flag.
- `mini_ork/gates/gate_fuzzer.py` — the house shape for a rate report: rates are `None`
  (never `0.0`) when their denominator is zero, and an unknown verdict **raises**
  `ValueError`. Mirror the discipline; do not import it.
- `mini_ork/learning/harness_operator.py` — H1, the sibling typed-proposal module: module
  docstring explaining *why*, a fixed vocabulary, `None`-over-empty, anti-fabrication. Do
  **not** import it.
- `mini_ork/cli/collapse.py` — G7's report-only CLI, the house shape for this file's CLI:
  `main(argv=None) -> int`, always exits 0, `--json` payload only on stdout, diagnostics on
  stderr. Mirror it; do not import it.
- `mini_ork/cli/main.py` — `_NATIVE_MODULE_SUBS`.

## Deliverable 1 — `mini_ork/learning/collapse_precursor.py` (new)

Pure functions over a caller-assembled generation history. No DB, no lane, no network, no
model, no numpy, no scipy — the only statistic needed is a Shannon entropy over a two-way
outcome count, which `math.log2` computes exactly, plus the same exact binomial sign test
the H2 monitor uses. Module docstring explaining *why* an internal-distribution signal can
precede a metric signal, and *why* the two-premature-of-three honesty matters more than the
coverage.

### Vocabulary (names are the contract)

```python
PRECURSORS = ("anchor_entropy_contraction", "tail_coverage_erosion")
UNMAPPED = ("representation_drift_freezing",)   # weights-only; mini-ork has no weights
MIN_GENERATIONS = 4
MIN_TESTS = 2
ALPHA = 0.05
```

### A generation row

A mapping:

```python
{"gen": int, "visible": float,
 "anchor_pass": int, "anchor_fail": int,
 "covered_tail": int, "covered_total": int}
```

- `visible` is the score the loop promoted on.
- `anchor_pass` / `anchor_fail` are the frozen anchor's outcome counts for that generation.
- `covered_tail` / `covered_total` are the eval's scenario coverage: how many of the
  exercised scenarios were tail scenarios, out of how many were exercised at all.

A row whose anchor total is `0` carries no anchor measurement and is excluded from every
test that reads the anchor; the same for `covered_total <= 0` and the coverage series.

### Measurements

- `anchor_entropy(row) -> float | None` — the Shannon entropy, **base 2**, of the
  pass/fail split: `-Σ p log2 p` over the non-zero proportions. A degenerate all-pass (or
  all-fail) anchor has entropy `0.0` — it has stopped discriminating. `None` when
  `anchor_pass + anchor_fail <= 0`, and `None` when either count is missing.
- `tail_coverage(row) -> float | None` — `covered_tail / covered_total`, or `None` when
  `covered_total <= 0`.

### Over a history (half-split means, exactly as `collapse_detector` does)

- `_sign_test_p(pos, neg) -> float | None` — the exact two-sided binomial sign-test p at
  `p = 0.5`, summed over the tail with `math.comb` (no approximation, no scipy). `None`
  when `pos + neg == 0` — a test in which every pair agrees carries no information.
- `precursor(history, name) -> dict` — for `name` in `PRECURSORS`. Take the half-split
  mean of the precursor's per-generation series (first half vs second half; odd `n` gives
  the extra row to the second half) and report the delta. Returns
  `{"name", "n", "first", "second", "delta", "contracting", "p"}` where:
  - `first` / `second` are the half-split means, `None` when fewer than
    `MIN_GENERATIONS` usable rows;
  - `delta = second - first`, `None` under the same condition;
  - `contracting` is `delta is not None and delta < 0` — **only** a fall is contraction;
    a flat series is not contraction;
  - `p` is `_sign_test_p` over the non-zero per-step diffs of the series (`None` when
    fewer than `MIN_GENERATIONS` rows contribute, or when every step agrees with itself).
  An unknown `name` **raises** `ValueError` naming it.
- `family_p(tests: Sequence[Mapping]) -> dict` — collect every non-`None` `p` and return
  `{"k", "p_family", "contributing": [names...]}` where `p_family = 1 - prod(1 - p_i)`
  (Šidák). `p_family` is `None` when fewer than `MIN_TESTS` tests contribute — one test is
  not a family.

### The lead time (the whole claim)

- `standard_degradation_index(history) -> int | None` — the `gen` of the **first**
  generation at which the standard (`visible`) metric degrades, defined as the first `gen`
  whose `visible` is strictly below the running maximum of `visible` over all earlier
  usable rows. `None` when the metric never degrades, or when there is no earlier row to
  compare against.
- `precursor_onset(history, name) -> int | None` — the **first** `gen` at which the
  precursor's own local trend turns negative: the first `gen` whose value is strictly below
  the value at the previous usable generation. `None` when it never turns negative.
- `lead_time(history) -> dict` — returns
  `{"precursor_gen", "standard_gen", "lead", "early_warning"}`:
  - `precursor_gen` is the earliest `gen` at which **any** member of `PRECURSORS` turns
    negative, `standard_gen` is `standard_degradation_index`;
  - `lead = standard_gen - precursor_gen`, **only** when both are not `None`. A **positive**
    lead means the precursor fired first — that is the paper's claim, and a negative lead
    means the precursor *trailed* the metric, which must be reported as the negative number
    it is and **never clamped to zero**;
  - `lead` is `None` when either index is `None` — an unmeasured lead is not a lead of `0`,
    and must never be read as "no gap";
  - `early_warning` is `lead is not None and lead > 0`.

### One call

- `monitor(history) -> dict` — returns
  `{"n_generations", "tests": [...], "family": {...}, "lead": {...},
   "precursors_unmapped": [...]}`. `tests` holds `precursor(...)` for each name in
  `PRECURSORS`, in that fixed order. `precursors_unmapped` is exactly `list(UNMAPPED)`,
  carried in the payload so a consumer cannot mistake two-of-three coverage for the whole
  taxonomy.

**Every rate, mean, and p-value over an unmeasured quantity is `None`, never `0.0`.** An
empty or short history must never read as a healthy one, and a precursor that never fired
must never read as a precursor that fired early. That is the whole point.

## Deliverable 2 — `mini_ork/cli/collapse_precursor.py` (new)

`main(argv=None) -> int` plus a `__main__` block. **Always exits 0** — it reports; it does
not halt, block, revert, regulate, or promote anything.

- `mini-ork collapse-precursor <history.json> [--json] [--help]`
- Input file is a JSON array of generation rows, or an object with a `"generations"` key.
- Human output: one line per precursor with its `n`, `delta`, and `p` (a `None` renders as
  `-`), then the family line, then the lead-time line, then a final line naming the unmapped
  precursor. When `n_generations < MIN_GENERATIONS`, print
  `insufficient history (n < 4)` and exit 0 — never a traceback, and never a "healthy".
- **Diagnostics to stderr; under `--json` the payload is the only thing on stdout.**
- A missing/unparseable file prints a clear message to stderr and exits **0**.
- `mini_ork/cli/main.py` — add
  `"collapse-precursor": "mini_ork.cli.collapse_precursor"` to `_NATIVE_MODULE_SUBS`.
- `tests/unit/test_native_dispatch_py.py` — add `"collapse-precursor"` to the `expected`
  set in `test_all_former_exec_subs_registered_natively`. That exact-set assertion is a
  real contract; leaving it red is a failure.

## Tests — `tests/unit/test_collapse_precursor_py.py` (new)

Hermetic: no DB, no lane, no network, no real run. The editable install resolves `mini_ork`
to **main**, so insert the repo root on `sys.path` before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `PRECURSORS == ("anchor_entropy_contraction", "tail_coverage_erosion")` and
   `UNMAPPED == ("representation_drift_freezing",)` — the two-of-three coverage is a
   contract, not a comment.
2. `anchor_entropy` is `1.0` on an even pass/fail split, `0.0` on an all-pass anchor, and
   `None` on a row with `anchor_pass == anchor_fail == 0` and on a row missing `anchor_fail`.
3. `tail_coverage` returns `None` when `covered_total == 0` and the exact ratio otherwise —
   never `0.0` for an unmeasured coverage.
4. `precursor` returns `delta is None` and `contracting is False` with fewer than
   `MIN_GENERATIONS` usable rows, and `contracting is False` on a **flat** series.
5. `precursor` returns `contracting is True` on a series whose second-half mean is strictly
   below its first-half mean, with `delta == second - first`.
6. `_sign_test_p(5, 0) == pytest.approx(2 * 0.5**5)`, `_sign_test_p(0, 0) is None`, and
   `_sign_test_p(2, 2) == pytest.approx(1.0)`.
7. `precursor` **raises** `ValueError` naming an unknown precursor name.
8. `family_p` over a single non-`None` p returns `p_family is None`; over three tests whose
   p-values are `0.02`, `0.3`, `None` it returns `k == 2` and
   `p_family == pytest.approx(1 - (1 - 0.02) * (1 - 0.3))`.
9. `standard_degradation_index` returns the first `gen` whose `visible` falls below the
   running maximum, and `None` on a monotonically non-decreasing series.
10. `precursor_onset` returns the first `gen` whose value falls below the previous usable
    value, and `None` when the series never turns negative.
11. `lead_time` returns a **positive** `lead` with `early_warning is True` when a precursor
    turns negative at least one generation before the standard metric degrades, and a
    **negative** `lead` with `early_warning is False` when it turns negative after — the
    negative case must not be clamped.
12. `lead_time` returns `lead is None` and `early_warning is False` when the standard metric
    never degrades, and when no precursor ever turns negative — an unmeasured lead is not
    `0`.
13. `monitor([])` returns `n_generations == 0`, `lead["lead"] is None`,
    `lead["early_warning"] is False`, and every test entry with `p is None` — an empty
    history is reported as neither collapsed nor healthy.
14. `monitor` on a synthesized history in which the anchor entropy contracts for four
    generations before the visible score drops returns `early_warning is True` with
    `lead["lead"] >= 1`, and `monitor(...)["precursors_unmapped"] == ["representation_drift_freezing"]`.
15. The CLI runs: writing a fixture JSON to a temp file and invoking the module's `main`
    with `["<path>", "--json"]` returns `0` and the captured stdout parses as JSON with a
    `"family"` key. Invoking with a genuinely missing path returns `0` and writes nothing to
    stdout.

## Files in scope

- `mini_ork/learning/collapse_precursor.py` (new)
- `mini_ork/cli/collapse_precursor.py` (new)
- `mini_ork/cli/main.py` — one line in `_NATIVE_MODULE_SUBS`
- `tests/unit/test_collapse_precursor_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — one name in `expected`

Do **not** touch `mini_ork/learning/collapse_detector.py`,
`mini_ork/learning/harness_contrast.py`, `mini_ork/learning/harness_operator.py`,
`mini_ork/learning/metamorphic.py`, `mini_ork/gates/**`, any recipe or prompt under
`recipes/**`, `web/**`, `mini_ork/dispatch/**`, or any migration. Do not halt, revert,
regulate, block, or promote anything. Do not add a routing policy or any SQL.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_collapse_precursor_py.py -q
python3.11 -m pytest tests/unit/test_native_dispatch_py.py -q
python3.11 -m py_compile mini_ork/learning/collapse_precursor.py mini_ork/cli/collapse_precursor.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'collapse-precursor' in R, sorted(R); print('collapse-precursor registered')"
python3.11 -c "import mini_ork.cli.collapse_precursor" | wc -c    # expect 0 — stdout is data
```

## Self-application measurement

Zero model spend. Run the real CLI against a **real** promotion history assembled read-only
from a live source. `/tmp/mo-rsi-home` holds the runs from prior cycles; each run directory
carries a run-level verdict, and `/tmp/mo-rsi-home/state.db` carries `execution_traces`. Read
what is actually there and materialize one generation row per observation you can defend.

```bash
ls /tmp/mo-rsi-home/runs/ | wc -l
python3.11 -m mini_ork.cli.collapse_precursor /tmp/h4-history.json --json
```

**The honest expected result is an `n_generations` below `MIN_GENERATIONS = 4` per task
class, so `family.p_family` is `None` and `lead` is `None`.** Report the number you actually
observed. A `None` lead from an insufficient history is **not** evidence that the precursors
work and must not be described as one — say "insufficient history", give the count, and stop.
Do not pool unrelated task classes to manufacture four generations, and do not back-fill an
`anchor_fail` count you cannot read. Never write to `/tmp/mo-rsi-home/state.db` or any run
directory under `/tmp/mo-rsi-home/runs/`.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-h4-collapse-precursor.json`:

```json
{"unit_tests_green": true, "native_dispatch_test_green": true,
 "cli_import_stdout_bytes": 0, "collapse_precursor_registered": true,
 "live_generations_observed": <int>, "live_family_p": null, "live_lead": null,
 "rates_none_not_zero": true, "two_of_three_precursors_named": true,
 "lead_never_clamped": true, "sign_test_is_exact_not_approx": true,
 "live_state_db_untouched": true}
```

## Done When

- `tests/unit/test_collapse_precursor_py.py` is green and `test_native_dispatch_py.py` is
  green with `"collapse-precursor"` in its expected set.
- `python3.11 -c "import mini_ork.cli.collapse_precursor" | wc -c` prints `0`.
- `PRECURSORS` carries exactly the two precursors that map and `UNMAPPED` names the third —
  verifiable by reading the module.
- `_sign_test_p` uses `math.comb` and an exact tail sum, and entropy uses `math.log2` — no
  numeric library is imported for a statistic — verifiable by reading the imports.
- `${MINI_ORK_RUN_DIR}/rsi-h4-collapse-precursor.json` exists with the fields above, and
  `live_generations_observed` is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
