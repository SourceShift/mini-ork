# H1 — turn a batch of run failures into a typed harness-edit proposal, scored by realized outcome

## Goal

mini-ork's self-improvement loop has exactly **one operator**: dispatch a `code-fix`
child and see what comes back. When the defect is not in a file but in the *harness* —
a stage that runs in the wrong order, a prompt contract that invites a malformed answer,
a retry policy that cannot survive a turn cap — the loop has no way to say so. It can
only re-dispatch the same child against the same harness and hope.

Harness-R1 (arXiv 2608.02276) makes harness editing itself a learned capability, and the
part it is careful about is the **reward**: an edit is scored by the *realized task success
it produces on a fresh rerun of the same failure batch*, **not** by the editor's own
judgement of its own proposal. The paper's engineer is a post-trained 9B model; that half
is out of scope here and always will be. What is in scope is the shape that half must
conform to, and the measurement that makes it falsifiable:

1. **propose** — a batch of failure receipts → a *typed, structured* harness-edit proposal
   (which surface, which kind of edit, supported by how many observations). Deterministic,
   no model. A group of one failure is not a pattern and must not be proposed.
2. **score** — the proposal → its *realized* downstream success over the same failure
   batch, with the proposer's own claimed direction reported **beside** the realized one
   and never used to compute it. A proposer that says "this will improve things" is a
   claim, and the gap between the claim and the outcome is the finding.

This is the **measurement half**. Nothing here applies an edit to any recipe, prompt, or
workflow. Wiring a proposal into an actual harness change is a separate, higher-stakes
cycle — a proposer that also acts cannot be validated without acting.

## What already exists (read it; do not rewrite it)

- `mini_ork/learning/failure_classifier.py` — `classify(*, reason, exit_code, signal,
  stderr, max_turns_hit, provider_status) -> str`, plus `ALL_CLASSES`,
  `INFRA_INTERRUPT`, `PROVIDER_LIMIT`, `OUTPUT_INVALID`, `INPUT_REQUIRED`, `TERMINAL`,
  `recovery_policy()`, `is_terminal()`, `auto_recoverable()`. **Call `classify()`; do not
  re-derive its needles or its precedence.** An unknown class is `terminal` by its
  fail-closed default and you inherit that for free.
- `mini_ork/learning/harness_contrast.py` — `attribute(rows) -> dict` over rows shaped
  `{"probe", "lane", "a", "b"}`. It already returns `{"n", "lane", "a_rate", "b_rate",
  "delta", "resolved", "discordant", "a_only", "b_only", "rows"}` where
  `delta = (b_pass - a_pass) / n`, rates/delta are `None` when `n == 0`, and it **raises
  `ValueError`** on an empty/missing lane or a mixed-lane set. `score()` in this cycle is a
  **thin wrapper** over it: delegate the arithmetic, then attach the proposer-claim
  comparison. Do **not** re-implement the delta, the contamination check, or the
  `None`-over-empty rule.
- `mini_ork/learning/collapse_detector.py` — the house style for a detector half: a pure,
  hermetic function over caller-assembled rows, no DB, no lane, no model. Match it.
- `mini_ork/cli/collapse.py` — the house style for a report-only CLI: `main(argv) -> int`,
  `--json` puts the payload on stdout and diagnostics on stderr, and it **always exits 0**.
- `mini_ork/cli/main.py` — `_NATIVE_MODULE_SUBS`, the dict that maps a subcommand name to
  its module.

## Deliverable 1 — `mini_ork/learning/harness_operator.py` (new)

Pure functions over caller-supplied mappings. No DB, no file I/O, no network, no lane, no
model. Module docstring explaining *why* the split is propose-then-score and why the
proposer's own claim is recorded but never trusted.

Public surface (names are the contract):

- `HARNESS_SURFACES = ("prompt", "stage_order", "verifier", "routing", "recovery")` —
  a fixed vocabulary of harness surfaces. A proposal's `target` is always one of these.
- `MIN_SUPPORT = 2` — the default minimum observations before a group is a pattern.
- `failure_signature(receipt: Mapping) -> str` — canonical key for one receipt.
  Classify it by calling `failure_classifier.classify(...)` with the receipt's
  `reason`, `exit_code`, `signal`, `stderr`, `max_turns_hit`, `provider_status`
  (each `.get()`, absent left as the default). Node comes from `receipt["node"]`,
  normalized: `str(...).strip()` or `"-"` when missing/empty. Return
  `f"{failure_class}:{node}"`. Deterministic and pure.
- `group_failures(receipts: Iterable[Mapping]) -> list[dict]` — group by
  `failure_signature`. Each entry:
  `{"signature", "failure_class", "node", "n", "runs": [...], "reasons": [...]}` where
  `runs` is the sorted, de-duplicated list of non-empty `receipt["run_id"]` values and
  `reasons` is the first up-to-3 distinct non-empty `receipt["reason"]` values in input
  order. Sorted by `(-n, signature)`. Empty input → `[]`.
- `propose(receipts: Iterable[Mapping], *, min_support: int = MIN_SUPPORT) -> list[dict]`
  — one typed proposal per group whose `n >= min_support`. A group below the threshold is
  **omitted entirely**: not emitted with a weak/`low_confidence` flag. Each entry:
  `{"signature", "target", "kind", "support", "rationale", "evidence"}`, where `support`
  is the group's `n`, `evidence` is the group's `runs`, and `rationale` is one short
  sentence naming the failure class and the surface. Sorted by `(-support, signature)`.
- `score(proposal: Mapping, rows: Iterable[Mapping]) -> dict` — the realized outcome.
  Materialize `rows`, call `harness_contrast.attribute(rows)`, and return that report's
  keys **plus**: `proposer_direction` (the proposal's `expected_direction`, or `None`),
  and `agrees` (`True`/`False`/`None`). `rows` may be empty — `attribute` already returns
  `delta is None` there. `agrees is None` when `delta is None` **or** when the proposal
  carries no `expected_direction`; otherwise `agrees` is `(delta > 0) == (proposer_direction
  == "improve")` for a non-`"none"` claim. Contamination must propagate: do not catch the
  `ValueError` `attribute` raises.
- `summarize(receipts, *, rows=None, min_support=MIN_SUPPORT) -> dict` — one call returning
  `{"n_receipts", "n_groups", "n_proposals", "proposals": [...], "scores": [...]}`.
  `scores` is `[score(p, rows) for p in proposals]` when `rows` is not `None`, else `[]`.
  A thin composition, no extra logic.

**The typed mapping is the contract** — `failure_class` → `(target, kind)`:

| failure_class | target | kind |
|---|---|---|
| `output_invalid` | `prompt` | `prompt_edit` |
| `provider_limit` | `recovery` | `retry_policy` |
| `infra_interrupt` | `recovery` | `retry_policy` |
| `input_required` | `stage_order` | `reorder` |
| `terminal` | `verifier` | `gate_edit` |

An unclassifiable receipt lands on `terminal` via `classify`'s fail-closed default and so
is proposed as a `verifier`/`gate_edit` — the gate could not see the cause, which is a
diagnostic-surface defect. That is the intended behaviour, not a bug.

## Deliverable 2 — `mini_ork/cli/harness_edit.py` (new)

`main(argv=None) -> int` plus a `__main__` block. Always exits 0 — it reports; it applies
nothing.

- `mini-ork harness-edit <receipts.json> [--json] [--help]`
- Input file is either a JSON array of receipts, or an object with a `"receipts"` key and
  an optional `"rows"` key (the paired outcome rows for `score`).
- Human output: the proposals, then (when rows are present) each score with its
  `delta`, `resolved`, and — when the proposal carried a claim — whether the proposer and
  the outcome agree. When there are no proposals, print a clear
  `no failures met the support threshold (min_support=2)` line and exit 0.
- **Diagnostics to stderr; under `--json` the payload is the only thing on stdout.**
- A missing/unparseable file prints a clear message to stderr and exits **0**, not with a
  traceback.
- `mini_ork/cli/main.py` — add `"harness-edit": "mini_ork.cli.harness_edit"` to
  `_NATIVE_MODULE_SUBS`.
- `tests/unit/test_native_dispatch_py.py` — add `"harness-edit"` to the `expected` set in
  `test_all_former_exec_subs_registered_natively`. That exact-set assertion is a real
  contract; leaving it red is a failure.

## Tests — `tests/unit/test_harness_operator_py.py` (new)

Hermetic: no DB, no lane, no network, no real run. The editable install resolves
`mini_ork` to **main**, so insert the repo root on `sys.path` before importing:

```python
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
```

**These assertions are the contract** — implement all of them; do not reword, weaken, or
drop one to match what you built:

1. `failure_signature` separates classes: `{"node": "w3", "exit_code": 137}` →
   `"infra_interrupt:w3"`; `{"node": "w3", "max_turns_hit": True}` →
   `"provider_limit:w3"`; `{"node": "w3", "reason": "fatal: unrecoverable"}` →
   `"terminal:w3"`. A receipt with no `node` keys to `"-"`.
2. `group_failures` counts and de-duplicates: three receipts for the same signature with
   `run_id`s `["r2", "r1", "r2"]` yield one group with `n == 3` and `runs == ["r1", "r2"]`.
3. `group_failures([])` returns `[]` and `propose([])` returns `[]` — an empty batch is an
   absence of evidence, not a crash.
4. A singleton group is **omitted**: two signatures with counts 2 and 1 produce exactly one
   proposal, whose `signature` is the count-2 one; the count-1 signature appears nowhere in
   the output.
5. `support` equals the group's `n`, and proposals are sorted by `(-support, signature)` —
   give three groups with supports 2, 5, 3 and assert the emitted order.
6. The typed mapping holds exactly: build one receipt per `failure_class` (via
   `exit_code=137` → infra, `max_turns_hit=True` → provider_limit, `reason="invalid json"`
   → output_invalid, `reason="needs_answers"` → input_required, `reason="fatal"` →
   terminal), and assert each proposal's `target`/`kind` pair against the table above.
7. `score` agrees with the primitive: for any non-empty `rows`, `score(...)["delta"] ==
   harness_contrast.attribute(rows)["delta"]` **exactly**, and `score(...)["n"] ==
   attribute(rows)["n"]`.
8. `score` over empty `rows` returns `delta is None` and `agrees is None` — never `0.0`,
   never `False`.
9. `agrees` is `None` when the proposal carries no `expected_direction`; `True` when
   `expected_direction == "improve"` and `delta > 0`; `False` when `expected_direction ==
   "improve"` and `delta < 0`; and `None` for `expected_direction == "none"`.
10. `score` **raises** `ValueError` on rows that mix lanes — the contamination control
    must not be swallowed.
11. `summarize` over an empty receipts list returns `n_receipts == 0`, `n_proposals == 0`,
    `proposals == []`, and `scores == []` when `rows` is `None`.
12. The CLI runs: writing a fixture JSON to a temp file and invoking the module's `main`
    with `["<path>", "--json"]` returns `0` and the captured stdout parses as JSON with a
    `"proposals"` key. Invoking with a genuinely missing path returns `0` and writes
    nothing to stdout.

## Files in scope

- `mini_ork/learning/harness_operator.py` (new)
- `mini_ork/cli/harness_edit.py` (new)
- `mini_ork/cli/main.py` — one line in `_NATIVE_MODULE_SUBS`
- `tests/unit/test_harness_operator_py.py` (new)
- `tests/unit/test_native_dispatch_py.py` — one name in `expected`

Do **not** touch `mini_ork/learning/failure_classifier.py` (call it, do not edit it),
`mini_ork/learning/harness_contrast.py` (call it, do not edit it),
`mini_ork/learning/collapse_detector.py`, any recipe or prompt under `recipes/**`, `web/**`,
`mini_ork/dispatch/**`, `observability/node_events.py`, or any migration. Do not add a
new routing policy, a second escalation path, or any SQL at all. Do not apply, write, or
patch a recipe, prompt, or workflow — this cycle only reports.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_harness_operator_py.py -q
python3.11 -m pytest tests/unit/test_native_dispatch_py.py -q
python3.11 -m py_compile mini_ork/learning/harness_operator.py mini_ork/cli/harness_edit.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY as R; assert 'harness-edit' in R, sorted(R); print('harness-edit registered')"
python3.11 -c "import mini_ork.cli.harness_edit" | wc -c    # expect 0 — stdout is data
```

## Self-application measurement

Zero model spend. Build a receipts file from a **real** run's failure receipts, then run
the real CLI against it. A framework-edit run keeps `$RUN_DIR/run-verdict.json`
(`{"verdict","failed_nodes","dispatched","source"}`) and per-node failure lines in
`$RUN_DIR/execute.log`; `/tmp/mo-rsi-home` holds the runs from prior cycles. Read what is
actually there and materialize one receipt per observed failing node.

```bash
# read-only against the live scratch home — never write to it
ls /tmp/mo-rsi-home/runs/ | tail -5
python3.11 -m mini_ork.cli.harness_edit /tmp/h1-receipts.json --json
```

**The honest expected result is a small `n_receipts` and possibly `n_proposals == 0`** —
the runs on disk record a run-level verdict, not per-node classified receipts, so most
signatures will be singletons and be omitted by `min_support`. Report the number you
actually observed. Do not manufacture extra receipts to make the proposal list non-empty,
and do not claim a score you did not compute. Never mutate `/tmp/mo-rsi-home/state.db` or
any run directory under `/tmp/mo-rsi-home/runs/`.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-h1-harness-operator.json`:

```json
{"unit_tests_green": true, "native_dispatch_test_green": true,
 "cli_import_stdout_bytes": 0, "harness_edit_registered": true,
 "live_receipts_observed": <int>, "live_proposals_emitted": <int>,
 "min_support_omits_singletons": true, "score_delegates_to_contrast": true,
 "live_state_db_untouched": true}
```

## Done When

- `tests/unit/test_harness_operator_py.py` is green and `test_native_dispatch_py.py` is
  green with `"harness-edit"` in its expected set.
- `python3.11 -c "import mini_ork.cli.harness_edit" | wc -c` prints `0`.
- `failure_signature` calls `failure_classifier.classify` — verifiable by reading the
  module, not by reading a summary.
- `score`'s delta is `harness_contrast.attribute`'s delta — verifiable by reading the
  module and by the equality test.
- `${MINI_ORK_RUN_DIR}/rsi-h1-harness-operator.json` exists with the fields above, and
  `live_receipts_observed` is the number you actually observed.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does
not survive `git diff`.
