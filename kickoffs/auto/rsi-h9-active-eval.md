# H9 — spend the fuzzing budget where the failures are likely *and* diverse

## Goal

`mini_ork/gates/gate_fuzzer.py` (cycle G3) measures a gate's blind-spot rate over a
**fixed corpus**. That leaves a harder question untouched: which scenarios should the
next probe be? Today the answer is "whatever is already in the file". The corpus does not
grow toward the failures that matter, so the blind-spot rate it reports is a rate over a
corpus someone else chose, and the gate can be blind in a region no probe ever visits.

Coverage Aware Active Evaluation (arXiv 2608.13719) makes two claims that turn selection
into a module:

> Although cheaper proxies ... can be sampled extensively to find failures, proxy failures
> often do not transfer to the real world due to sim-to-real and system-to-system gaps.

> To find failures that are both likely and diverse, we combine this predictor with a
> support-aware mutual-information objective that favors realistic, well-supported regions
> while expanding coverage across failure modes.

The first claim is the anti-fabrication contract. In mini-ork the "cheap proxy" is the
frozen evaluator run against a scenario that never reaches a real lane — a proxy pass is
**not** evidence of a real pass, and a selector that ranks on proxy signal alone is
selecting on the unaudited thing. The correction the paper learns is a **control-variate
residual**: over the scenarios where both the proxy and the real target were observed,
how far off is the proxy? With no such pairs the residual is `None` and **every derived
risk is `None`** — the module must refuse to recommend, not fall back to the proxy.

The second claim is the acquisition objective: risk alone collapses selection onto one
failure mode. A candidate is worth testing only if it is *likely* to fail **and** it
covers a mode the already-selected set does not — gated by whether that region is
realistic at all (`support`).

This cycle builds the **selection half**. It measures which scenario to probe next; it
does not generate scenarios, run a lane, call a model, or edit the corpus. Wiring the
selector into `gate_fuzzer`'s corpus refresh is a separate cycle that must not be built
on an unaudited residual.

Reference: arXiv 2608.13719 (Coverage Aware Active Evaluation for Failure Discovery with
Paired Systems).

## What already exists (read it; do not rewrite it)

- `mini_ork/gates/gate_fuzzer.py` — the sibling this cycle guides. It is the house shape
  for a rate report: rates are `None` (never `0.0`) when their denominator is zero, an
  unknown evaluator verdict **raises** `ValueError` naming the offending case, and a
  `"defer"` earns no credit. Mirror the discipline; **do not import or edit it.** Note
  that `fuzz_gate` scores a corpus it is *given* — H9 is what decides what that corpus
  should contain next.
- `mini_ork/learning/collapse_detector.py` — the sibling that reports a **condition**
  rather than a hypothesis, with `MIN_STEPS = 4` and half-split means rather than a fitted
  slope. Reuse the `None`-over-empty rule; do **not** import or edit it.
- `mini_ork/learning/hack_probe.py` — H2, the closest sibling in shape: a pure monitor over
  a caller-assembled history, an exact two-sided sign test in `math.comb`, and `None` for
  every p-value and rate over an unmeasured quantity. Do **not** import it.
- `mini_ork/learning/harness_operator.py` — H1, the sibling typed-proposal module: module
  docstring explaining *why*, a fixed vocabulary, anti-fabrication. Do **not** import it.
- `mini_ork/cli/hack_probe.py` — the house shape for a report-only CLI: `main(argv=None)
  -> int`, `--json` puts the payload on stdout and every diagnostic on stderr, always
  exits 0. Mirror it; do not import it.
- `mini_ork/cli/main.py` — `_NATIVE_MODULE_SUBS`.

## Deliverable 1 — `mini_ork/learning/active_eval.py` (new)

Pure functions over a caller-assembled scenario history. No DB, no lane, no network, no
model, no numpy, no scipy — an exact binomial sign test computed with `math.comb` is the
only statistic needed. Module docstring explaining *why* a proxy failure is not a target
failure and *why* a selector with no paired observation must report `None` rather than
rank on the proxy.

### Constants (names are the contract)

```python
MIN_PAIRED = 4        # below this, the transfer residual is None
MIN_CANDIDATES = 4    # below this, the selection is insufficient
ALPHA = 0.05
SUPPORT_FLOOR = 0.1   # at or below this, the region is not well-supported
```

### A scenario observation

A mapping:

```python
{"id": str,
 "proxy_failed": bool | None,    # did the cheap proxy report failure
 "target_failed": bool | None,   # did the real target fail (present only if tested)
 "support": float | None,        # realistic density at this region, 0..1
 "modes": [str, ...]}            # failure modes this scenario exercises
```

A row missing `proxy_failed` carries no proxy measurement; a row missing `target_failed`
has never been tested against the real target. Only a row carrying **both** is a paired
observation, and only paired observations can correct the proxy.

### Public surface

- `proxy_rate(rows) -> float | None` — the mean of `proxy_failed` over rows that measured
  it; `None` when none did. Never `0.0` for an unmeasured rate.
- `target_rate(rows) -> float | None` — the mean of `target_failed` over rows that measured
  it; `None` when none did.
- `paired(rows) -> list` — the rows where **both** `proxy_failed` and `target_failed` are
  present and non-`None`. This is the only evidence that can correct the proxy.
- `transfer_residual(rows) -> float | None` — the **control-variate correction**: the mean
  of `target_failed - proxy_failed` over the paired rows, where `True` is `1.0` and `False`
  is `0.0`. `None` when there are fewer than `MIN_PAIRED` paired rows. A **positive**
  residual means the proxy *under-reports* failure — proxy said pass while the target
  failed — which is the dangerous direction. A negative residual means the proxy
  over-reports. Never `None` rendered as `0.0`.
- `predicted_risk(row, residual) -> float | None` — `clamp01(proxy_failed + residual)` for
  a row that measured `proxy_failed`, with `True` as `1.0` and `False` as `0.0`. `None`
  when `residual is None` **or** the row's `proxy_failed` is missing — an uncorrected
  proxy risk is not a risk. The result is always clamped into `[0.0, 1.0]`.
- `coverage_gain(candidate, selected) -> float | None` — the count of modes in `candidate`
  that appear in **no** row of `selected`. `None` when the candidate's `modes` is missing
  or its `support` is missing. `0.0` when `support <= SUPPORT_FLOOR`, even if it covers new
  modes: the paper's objective "favors realistic, well-supported regions", so a mode never
  observed at a realistic density is not a mode worth expanding into.
- `acquisition(candidate, selected, residual) -> float | None` — the selection objective:
  `predicted_risk * coverage_gain`. `None` when **either** factor is `None`; a candidate
  with unknown risk or unknown coverage is not scored, never scored as `0.0`. A candidate
  with a known risk but zero coverage gain scores `0.0` — that is a measured zero, and it
  is how risk-only collapse is prevented.
- `rank(history, budget) -> dict` — greedy selection. Repeatedly take the unselected
  candidate with the highest `acquisition` against the current `selected` set (ties broken
  by `id` ascending, for determinism), append it, and recompute; stop early when no
  remaining candidate has an acquisition `> 0.0` or all are `None`. Returns
  `{"name": "rank", "n_candidates", "budget", "selected": [id, ...],
  "unmeasured": [id, ...], "undecided": [reasons...]}`:
  - `selected` holds **only** ids with a non-`None` acquisition, at most `budget`.
  - `unmeasured` holds the ids whose acquisition is `None` — scenarios the proxy cannot
    speak to. They are reported, never quietly ranked as safe.
  - `undecided` names every reason the ranking could not proceed —
    `"insufficient_paired"` (fewer than `MIN_PAIRED` paired rows),
    `"insufficient_candidates"` (fewer than `MIN_CANDIDATES` rows), `"no_coverage"` (no
    candidate had a positive coverage gain) — and is `[]` only when `selected` is non-empty.
    When `undecided` is non-empty, `selected` may still be empty; a non-empty `undecided`
    must never be accompanied by a claim that the ranking succeeded.
- `report(history, budget=1) -> dict` — one call returning
  `{"n_rows", "n_paired", "proxy": {...}, "target": {...},
   "transfer_residual", "rank": {...}, "undecided": [reasons...]}` where `proxy` and
  `target` are the rates plus their `n`, and `undecided` is the union of the rank's
  `undecided` and `"no_transfer_measured"` when the residual is `None`. `report` returns
  `undecided == []` **only** when the residual was measured and the rank selected a
  candidate.

**Every rate, mean and risk over an unmeasured quantity is `None`, never `0.0`.** A
selector that ranked on the proxy alone is exactly the sim-to-real failure the paper
names, and it must be impossible to express through this surface.

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

The residual's own significance uses this over the sign of the paired differences
(`target_failed - proxy_failed`): `pos` counts positive residuals, `neg` counts negative
ones, and a zero difference is dropped. Expose it as `residual_p(rows) -> float | None`
returning the exact two-sided p; `None` when there are no non-zero differences. Do not
gate the residual on this p — report it, and let the caller decide.

## Deliverable 2 — `mini_ork/cli/active_eval.py` (new)

`main(argv=None) -> int` plus a `__main__` block. **Always exits 0** — it reports; it does
not probe, test a target, label a scenario, or edit any corpus.

- `mini-ork active-eval <history.json> [--budget N] [--json] [--help]`
- Input file is a JSON array of scenario rows, or an object with a `"scenarios"` key.
- Human output: a proxy/target line with both rates and their `n` (a `None` renders as
  `-`); a transfer-residual line with the residual and its `p`; then the selection line
  listing the chosen ids. When the ranking is undecided, print the `undecided` reasons
  instead of a selection, and print `insufficient paired observations` when the history is
  below `MIN_PAIRED` — never a traceback, and never a recommendation.
- **Diagnostics to stderr; under `--json` the payload is the only thing on stdout.**
- A missing/unparseable file prints a clear message to stderr and exits **0**.
- `mini_ork/cli/main.py` — add `"active-eval": "mini_ork.cli.active_eval"` to
  `_NATIVE_MODULE_SUBS`.
- `tests/unit/test_native_dispatch_py.py` — add `"active-eval"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively`. That exact-set assertion is a real
  contract; leaving it red is a failure.

## Tests — `tests/unit/test_active_eval_py.py` (new)

Hermetic: no DB, no lane, no network, no real run. The editable install resolves `mini_ork`
to **main**, so insert the repo root on `sys.path` before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `proxy_rate` returns `None` for an empty history and for a history whose rows all lack
   `proxy_failed` — never `0.0`; and the correct mean when some rows measured it.
2. `target_rate` returns `None` when no row was tested against the target.
3. `paired` returns exactly the rows carrying **both** `proxy_failed` and `target_failed`,
   and excludes rows where either is `None`.
4. `transfer_residual` is `None` with fewer than `MIN_PAIRED` paired rows, and equals the
   mean of `target_failed - proxy_failed` (as `1.0`/`0.0`) once `MIN_PAIRED` are present.
5. `transfer_residual` is **positive** when the proxy says pass while the target fails.
6. `predicted_risk` is `None` when `residual is None`, `None` when the row lacks
   `proxy_failed`, and is clamped into `[0.0, 1.0]` when a large positive residual would
   otherwise push it above `1.0`.
7. `coverage_gain` is `None` when `modes` is missing and `None` when `support` is missing;
   `0.0` when `support <= SUPPORT_FLOOR` even with entirely new modes; and the count of
   new modes when well-supported.
8. `coverage_gain` counts only modes absent from **every** selected row, not merely the
   last one.
9. `acquisition` is `None` when either factor is `None`, and `0.0` — not `None` — when the
   risk is known but the coverage gain is `0.0`.
10. `_sign_test_p(5, 0) == pytest.approx(2 * 0.5**5)`, `_sign_test_p(0, 0) is None`, and
    `_sign_test_p(2, 2) == pytest.approx(1.0)`.
11. `rank` refuses to select — `selected == []` and `"insufficient_paired" in undecided` —
    on a history with proxy measurements but fewer than `MIN_PAIRED` paired rows. This is
    the anti-fabrication contract: no target evidence, no recommendation.
12. `rank` selects, and covers more distinct modes than a risk-only ordering would, on a
    paired history with two equally risky candidates from different modes — greedy must
    prefer the one contributing an unseen mode.
13. `rank`'s `unmeasured` list contains exactly the candidate ids whose acquisition is
    `None`, and `selected` contains none of them.
14. `report` returns `undecided == []` only when the residual was measured and a candidate
    was selected; on a proxy-only history it returns `"no_transfer_measured"` in
    `undecided` and a residual of `None`.
15. The CLI runs: writing a fixture JSON to a temp file and invoking the module's `main`
    with `["<path>", "--json"]` returns `0` and the captured stdout parses as JSON with a
    `"rank"` key. Invoking with a genuinely missing path returns `0` and writes nothing to
    stdout.

## Files in scope

- `mini_ork/learning/active_eval.py` (new)
- `mini_ork/cli/active_eval.py` (new)
- `mini_ork/cli/main.py` — one line in `_NATIVE_MODULE_SUBS`
- `tests/unit/test_active_eval_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — one name in `expected`

Do **not** touch `mini_ork/gates/**` (including `gate_fuzzer.py`), `mini_ork/learning/
collapse_detector.py`, `mini_ork/learning/hack_probe.py`, `mini_ork/learning/
harness_operator.py`, `mini_ork/learning/harness_integrity.py`, any recipe or prompt under
`recipes/**`, `web/**`, `mini_ork/dispatch/**`, or any migration. Do not generate scenario
probes, run a lane, call a model, or edit the fuzzer corpus. Do not add a routing policy or
any SQL.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_active_eval_py.py -q
python3.11 -m pytest tests/unit/test_native_dispatch_py.py -q
python3.11 -m py_compile mini_ork/learning/active_eval.py mini_ork/cli/active_eval.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'active-eval' in R, sorted(R); print('active-eval registered')"
python3.11 -c "import mini_ork.cli.active_eval" | wc -c    # expect 0 — stdout is data
```

## Self-application measurement

**Reading the live source is in scope and expected.** Files under `/tmp/mo-rsi-home/` and the
in-repo probe corpus are **read-only inputs to this measurement**; opening them does not
touch any file in scope and must not be refused by a scope guard. Write your derived history
to `/tmp/` (e.g. `/tmp/h9-history.json`), never into the repo. If a guard blocks the read,
say so in the evidence artifact rather than reporting `live_*` as `0` without having surveyed.

Zero model spend. Run the real CLI against a **real** scenario history assembled read-only
from a live source. `/tmp/mo-rsi-home` holds the runs from prior cycles, and the G3 probe
corpus that `gate_fuzzer.py` scores ships in-repo under
`mini_ork/gates/probes/artifact_contract_probes.json`. Read what is actually there and
materialize one scenario row per probe you can defend — the probe's declared expectation is
a **proxy** label you can record, while the `target_failed` of a probe is the part you will
not find, because no probe in that corpus was ever run against a real lane.

```bash
python3.11 -c "import json,sys; d=json.load(open('mini_ork/gates/probes/artifact_contract_probes.json')); print(len(d) if isinstance(d,list) else sorted(d))"
python3.11 -m mini_ork.cli.active_eval /tmp/h9-history.json --json
```

**The honest expected result is that no probe records a target outcome at all**, so
`n_paired` is `0`, `transfer_residual` is `None`, `selected` is `[]`, and `undecided` names
`insufficient_paired`. Report the number you actually observed. A selection of `[]` from a
history with no paired evidence is **not** a finding that no scenario is worth probing and
must not be described as one — say "undecided", give the count, and stop. Do not invent a
`target_failed` to make a row paired, and do not pool unrelated probe families to
manufacture four paired observations. Never write to `/tmp/mo-rsi-home/state.db`, any run
directory under `/tmp/mo-rsi-home/runs/`, or `mini_ork/gates/probes/`.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-h9-active-eval.json`:

```json
{"unit_tests_green": true, "native_dispatch_test_green": true,
 "cli_import_stdout_bytes": 0, "active_eval_registered": true,
 "live_rows_observed": <int>, "live_rows_paired": 0,
 "live_residual": null, "live_selected_empty": true,
 "undecided_names_a_reason": true,
 "proxy_only_history_never_ranks": true,
 "rates_none_not_zero": true, "sign_test_is_exact_not_approx": true,
 "live_state_db_untouched": true}
```

## Done When

- `tests/unit/test_active_eval_py.py` is green and `test_native_dispatch_py.py` is green
  with `"active-eval"` in its expected set.
- `python3.11 -c "import mini_ork.cli.active_eval" | wc -c` prints `0`.
- `rank` selects nothing — and `undecided` names a reason — for a history with no paired
  observations, verifiable by reading the module.
- `_sign_test_p` uses `math.comb` and an exact tail sum — no `scipy`, no `numpy` —
  verifiable by reading the imports.
- `${MINI_ORK_RUN_DIR}/rsi-h9-active-eval.json` exists with the fields above, and
  `live_rows_observed` is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
