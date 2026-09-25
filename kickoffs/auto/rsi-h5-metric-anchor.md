# H5 — who grades the grader: anchor discipline for an evolved eval metric

## Goal

Every self-improving loop rests on a hidden assumption the paper names outright: *a
reliable evaluation metric already exists*. In mini-ork it often does not — the loop's
gate score, its rubric, its verifier verdict are themselves imperfect proxies, and the
loop that selects on them is selecting on a grader nobody graded.

Who Grades the Grader? (arXiv 2607.12790) makes three claims, and the third is the one
that turns into a monitor:

> Safety comes from **anchor discipline plus outer audits**: removing anchor guards
> collapses the metric into a **vacuous detector** while removing the lifecycle does not.

That is a sharp, testable asymmetry. A metric that has an anchor it never reads stays
honest; strip the anchor and it does not degrade gracefully — it collapses into something
that agrees with everything, and an always-agreeing detector is indistinguishable from no
detector at all. The paper's metric loop trains against a **ten-item anchored reference
set**, regularizes by consensus over unlabeled outputs, and — the load-bearing part —
**audits against a held-out anchor it never reads**.

This cycle builds the **audit half**: given a caller-assembled history of metric
generations, report whether the metric still has an anchor it never reads and whether its
discrimination has collapsed toward vacuity. It measures; it does not evolve, retrain, or
replace the metric. The metric loop that *searches* detector compositions is a separate,
model-spending cycle that must not be built on an unaudited anchor.

Reference: arXiv 2607.12790 (Who Grades the Grader?).

## What already exists (read it; do not rewrite it)

- `mini_ork/gates/gate_fuzzer.py` — the house shape for a rate report: rates are `None`
  (never `0.0`) when their denominator is zero, and an unknown verdict **raises**
  `ValueError` naming the offending case. Mirror the discipline; do not import it.
- `mini_ork/learning/collapse_detector.py` — the sibling that also reports a **condition**
  rather than a hypothesis, plus `MIN_STEPS = 4` and the half-split means (first-half mean
  vs second-half mean) rather than a fitted slope, because at small n a least-squares slope
  is determined almost entirely by the two endpoints. Reuse that discipline and the
  `None`-over-empty rule; do **not** import or edit it.
- `mini_ork/learning/harness_contrast.py` — the sibling whose `resolved` field encodes the
  same lesson this module needs: a contrast in which every pair agrees carries no
  information and must be reported as `None`, not as a resolved result. Do not import it.
- `mini_ork/learning/harness_operator.py` — H1, the sibling typed-proposal module: module
  docstring explaining *why*, a fixed vocabulary, `None`-over-empty, anti-fabrication. Do
  **not** import it.
- `mini_ork/cli/collapse.py` — the house shape for a report-only CLI: `main(argv=None) ->
  int`, `--json` puts the payload on stdout and every diagnostic on stderr, always exits 0.
  Mirror it; do not import it.
- `mini_ork/cli/main.py` — `_NATIVE_MODULE_SUBS`.

## Deliverable 1 — `mini_ork/learning/metric_anchor.py` (new)

Pure functions over a caller-assembled metric history. No DB, no lane, no network, no
model, no numpy, no scipy — an exact binomial sign test computed with `math.comb` is the
only statistic needed. Module docstring explaining *why* an anchor the metric never reads
is what keeps the metric honest, and *why* a metric that cannot be shown to have one must
report `intact is None` rather than `True`.

### Constants (names are the contract)

```python
MIN_GENERATIONS = 4      # below this, every test reports its n and a None p-value
ALPHA = 0.05
REFERENCE_SIZE = 10      # the paper's ten-item anchored reference set
DISCRIMINATION_FLOOR = 0.1   # at or below this, the metric is vacuous
```

### A metric generation

A mapping:

```python
{"gen": int,
 "scored": [str, ...],      # output ids the metric actually READ
 "held_out": [str, ...],    # anchor ids the metric declares it never reads
 "agreements": [bool, ...]} # per-anchor-item agreement with the anchored reference
```

A row missing `scored` or `held_out` carries no anchor measurement and is excluded from
every test that reads the anchor.

### Public surface

- `anchor_contaminated(row) -> bool | None` — `True` when `scored` and `held_out`
  **intersect** (the metric read its own anchor, so it is no longer held out); `False` when
  they are disjoint; `None` when either key is missing. A contaminated anchor is a fact
  about what was read, never an inference.
- `under_anchored(row) -> bool | None` — `True` when `len(held_out) < REFERENCE_SIZE`;
  `None` when `held_out` is missing.
- `agreement(row) -> float | None` — the mean of `agreements`; `None` when empty or
  missing. Never `0.0` for an unmeasured agreement.
- `discrimination(row) -> float | None` — `min(p, 1 - p)` where `p` is the fraction of
  `agreements` that are `True`. A metric that agreed with every anchor item has
  discrimination `0.0`: it has stopped distinguishing anything. `None` when empty.
- `anchor_audit(history) -> dict` — over rows that carry an anchor measurement:
  `{"name": "anchor", "n", "n_contaminated", "n_under_anchored", "rate_contaminated",
  "contaminated"}`, where `rate_contaminated = n_contaminated / n` (`None`, never `0.0`,
  when `n == 0`) and `contaminated` is `True` iff `n_contaminated > 0`. `contaminated` is
  `False` only when the anchor was actually measured and found clean — a history with no
  anchor measurements is not a clean anchor.
- `vacuity(history) -> dict` — the discrimination series, half-split. Returns
  `{"name": "vacuity", "n", "first", "second", "delta", "latest", "vacuous", "p"}` where
  `first`/`second` are the half-split means (odd `n` gives the extra row to the second
  half), `delta = second - first`, `latest` is the last usable discrimination,
  `vacuous` is `latest is not None and latest <= DISCRIMINATION_FLOOR`, and `p` is the
  exact two-sided sign test over the non-zero per-step diffs. With fewer than
  `MIN_GENERATIONS` usable rows, `first`, `second`, `delta`, `latest` and `p` are all
  `None` and `vacuous` is `False`.
- `intact(history) -> bool | None` — **the anti-fabrication contract.** Returns `None`
  (never `True`) whenever the history has fewer than `MIN_GENERATIONS` rows carrying an
  anchor measurement. Otherwise `True` iff the anchor was measured, not contaminated, not
  under-anchored, and not vacuous. A metric nobody could audit must never read as intact.
- `audit(history) -> dict` — one call returning
  `{"n_generations", "n_with_anchor", "anchor": {...}, "vacuity": {...},
   "intact": bool | None, "undecided": [reasons...]}`. `undecided` names every reason the
  audit could not conclude — `"insufficient_history"`, `"no_anchor_measured"`,
  `"under_anchored"` — and is `[]` only when `intact` is `True` or `False`. When `intact`
  is `None`, `undecided` must be non-empty.

**Every rate, mean, and p-value over an unmeasured quantity is `None`, never `0.0`.** An
unaudited metric must never read as an intact one — that is the whole point.

### Imports

`math` and `collections.abc` only. The sign test is an exact tail sum over `math.comb`:

```python
def _sign_test_p(pos, neg):
    n = pos + neg
    if n == 0:
        return None            # every pair agrees: no information
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)
```

## Deliverable 2 — `mini_ork/cli/metric_anchor.py` (new)

`main(argv=None) -> int` plus a `__main__` block. **Always exits 0** — it reports; it does
not retrain, replace, disable, or promote any metric.

- `mini-ork metric-anchor <history.json> [--json] [--help]`
- Input file is a JSON array of metric generations, or an object with a `"generations"` key.
- Human output: an anchor line with `n`, `n_contaminated` and `rate_contaminated` (a `None`
  renders as `-`); a vacuity line with `latest`, `delta` and `p`; then the intact line.
  When `intact is None`, print the `undecided` reasons instead of a verdict, and print
  `insufficient history` when the history is below `MIN_GENERATIONS` — never a traceback,
  and never "intact".
- **Diagnostics to stderr; under `--json` the payload is the only thing on stdout.**
- A missing/unparseable file prints a clear message to stderr and exits **0**.
- `mini_ork/cli/main.py` — add `"metric-anchor": "mini_ork.cli.metric_anchor"` to
  `_NATIVE_MODULE_SUBS`.
- `tests/unit/test_native_dispatch_py.py` — add `"metric-anchor"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively`. That exact-set assertion is a real
  contract; leaving it red is a failure.

## Tests — `tests/unit/test_metric_anchor_py.py` (new)

Hermetic: no DB, no lane, no network, no real run. The editable install resolves `mini_ork`
to **main**, so insert the repo root on `sys.path` before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `anchor_contaminated` is `True` when `scored` and `held_out` share an id, `False` when
   they are disjoint, and `None` when either key is missing.
2. `under_anchored` is `True` for a 9-item `held_out` and `False` for a 10-item one;
   `None` when `held_out` is missing.
3. `agreement` returns `None` for an empty `agreements` list and for a missing key — never
   `0.0`.
4. `discrimination` is `0.0` when every agreement is `True`, `0.5` on an even split, and
   `None` when `agreements` is empty.
5. `_sign_test_p(5, 0) == pytest.approx(2 * 0.5**5)`, `_sign_test_p(0, 0) is None`, and
   `_sign_test_p(2, 2) == pytest.approx(1.0)`.
6. `anchor_audit` on a history with no anchor-measurable rows returns `n == 0`,
   `rate_contaminated is None`, and `contaminated is False` — an unmeasured anchor is not
   reported as a contaminated one, and `rate_contaminated` is not `0.0`.
7. `anchor_audit` reports `contaminated is True` and `n_contaminated == 1` when exactly one
   of three measured rows read its held-out set.
8. `vacuity` reports `vacuous is True` when the latest discrimination is at or below
   `DISCRIMINATION_FLOOR`, and `False` when it is comfortably above.
9. `vacuity` returns `delta is None`, `latest is None` and `p is None` with fewer than
   `MIN_GENERATIONS` usable rows.
10. `intact` returns `None` — not `True` — on an empty history, and on a history with
    `MIN_GENERATIONS` rows **none of which carry an anchor measurement**.
11. `intact` returns `True` only for a history with enough measured rows, a clean anchor of
    at least `REFERENCE_SIZE` items, and discrimination above the floor; and `False` when
    any one of those fails.
12. `audit([])` returns `intact is None` with `"insufficient_history"` in `undecided`, and
    `audit` on a fully clean history returns `intact is True` with `undecided == []`.
13. The CLI runs: writing a fixture JSON to a temp file and invoking the module's `main`
    with `["<path>", "--json"]` returns `0` and the captured stdout parses as JSON with an
    `"intact"` key. Invoking with a genuinely missing path returns `0` and writes nothing to
    stdout.

## Files in scope

- `mini_ork/learning/metric_anchor.py` (new)
- `mini_ork/cli/metric_anchor.py` (new)
- `mini_ork/cli/main.py` — one line in `_NATIVE_MODULE_SUBS`
- `tests/unit/test_metric_anchor_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — one name in `expected`

Do **not** touch `mini_ork/learning/collapse_detector.py`,
`mini_ork/learning/harness_contrast.py`, `mini_ork/learning/harness_operator.py`,
`mini_ork/learning/harness_integrity.py`, `mini_ork/learning/metamorphic.py`,
`mini_ork/gates/**`, any recipe or prompt under `recipes/**`, `web/**`,
`mini_ork/dispatch/**`, or any migration. Do not retrain, replace, disable, or promote any
metric. Do not add a routing policy or any SQL.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_metric_anchor_py.py -q
python3.11 -m pytest tests/unit/test_native_dispatch_py.py -q
python3.11 -m py_compile mini_ork/learning/metric_anchor.py mini_ork/cli/metric_anchor.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'metric-anchor' in R, sorted(R); print('metric-anchor registered')"
python3.11 -c "import mini_ork.cli.metric_anchor" | wc -c    # expect 0 — stdout is data
```

## Self-application measurement

Zero model spend. Run the real CLI against a **real** metric history assembled read-only
from a live source. `/tmp/mo-rsi-home` holds the runs from prior cycles; each run directory
carries a run-level verdict and a rubric, and `/tmp/mo-rsi-home/state.db` carries
`execution_traces`. Read what is actually there and materialize one metric generation per
observation you can defend — the rubric's agreement with the run's own verdict is an
agreement you can measure; the anchor the rubric never read is the part you will not find.

```bash
ls /tmp/mo-rsi-home/runs/ | wc -l
python3.11 -m mini_ork.cli.metric_anchor /tmp/h5-history.json --json
```

**The honest expected result is that no run records a held-out anchor at all**, so
`n_with_anchor` is `0`, `intact` is `None`, and `undecided` names `insufficient_history`.
Report the number you actually observed. An `intact is None` from an unauditable metric is
**not** a clean bill of health and must not be described as one — say "undecided", give the
count, and stop. Do not invent a `held_out` list to make a row measurable, and do not pool
unrelated task classes to manufacture four generations. Never write to
`/tmp/mo-rsi-home/state.db` or any run directory under `/tmp/mo-rsi-home/runs/`.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-h5-metric-anchor.json`:

```json
{"unit_tests_green": true, "native_dispatch_test_green": true,
 "cli_import_stdout_bytes": 0, "metric_anchor_registered": true,
 "live_generations_observed": <int>, "live_with_anchor": 0,
 "live_intact": null, "undecided_names_a_reason": true,
 "intact_none_never_true_on_short_history": true,
 "rates_none_not_zero": true, "sign_test_is_exact_not_approx": true,
 "live_state_db_untouched": true}
```

## Done When

- `tests/unit/test_metric_anchor_py.py` is green and `test_native_dispatch_py.py` is green
  with `"metric-anchor"` in its expected set.
- `python3.11 -c "import mini_ork.cli.metric_anchor" | wc -c` prints `0`.
- `intact` returns `None` — never `True` — for a history it could not audit, and `undecided`
  names a reason whenever `intact` is `None` — verifiable by reading the module.
- `_sign_test_p` uses `math.comb` and an exact tail sum — no `scipy`, no `numpy` —
  verifiable by reading the imports.
- `${MINI_ORK_RUN_DIR}/rsi-h5-metric-anchor.json` exists with the fields above, and
  `live_generations_observed` is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
