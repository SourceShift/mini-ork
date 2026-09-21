# G3 — a fuzzer that attacks the gate, not the artifact

## Goal

Every RSI cycle so far hardens the **artifact**; none attacks the **gate**. arXiv 2606.08960
audits 1,968 agent-benchmark tasks and finds 323 (16%) hackable **given only the task
description** — a verifier that checks a proxy for success rather than success itself can be
satisfied without solving the task, and a self-improving loop then optimizes the cheap path
the gate rewards.

mini-ork has the same exposure and it is provable, not hypothetical.
`mini_ork/gates/artifact_contract.py::validate_artifact` makes exactly three checks: the file
exists, its extension matches `expected_artifact`, and every command in `success_verifiers`
exits 0. The shipped default contract has `success_verifiers: []` (`_default_contract`), so
**any existing file of the right extension passes**. A submission that is *wrong* but
*well-shaped* is accepted — the definition of the failure the paper measures.

This cycle builds the **measurement** half of the paper's hacker-fixer loop: a hermetic gate
fuzzer that runs a corpus of probes against a gate evaluator and reports the **blind-spot
rate** (probes that should have been rejected but were accepted) alongside the
**over-block rate** (legitimate probes the gate rejected) that the paper's solver-confirmation
step exists to guard. The LLM hacker and fixer agents are **explicitly out of scope** — this
cycle is the deterministic audit that says how big the hole is, the same way G1's
`calibration_backtest` measures the router's blind spot before anyone tries to shrink it.

Reference: arXiv 2606.08960 (adversarial hacker-fixer loop for verifier hardening).

## What already exists (read it; do not rewrite it)

- `mini_ork/gates/artifact_contract.py` — the gate under measurement.
  `validate_artifact(contract: dict, artifact_path: str, mini_ork_root: str | None = None) -> dict`
  returns a payload whose `payload["verdict"]` is `"pass"` or `"fail"` (it returns rc 0 for
  both — the verdict is the payload, not the exit code). `_TYPE_TO_EXTS` maps
  `expected_artifact` → allowed extensions. `_default_contract` returns
  `success_verifiers: []`. The verifier runner is
  `_run_verifier(verifier, artifact_path, mini_ork_root)` which builds
  `f"{verifier} '{artifact_path}'"` and runs it through `bash -c` (60 s timeout).
- `mini_ork/gates/mutation_adversary.py` — the **sibling** technique: it attacks the produced
  artifact against the test suite (`run_campaign`, `compute_validation_results`,
  `threshold_pass`, `_THRESHOLD = 0.8`). Read its report shape
  (`kill_rate`/`total`/`killed`/`results`) and mirror the **spirit** (`an overall rate
  computed from a per-case list`) — but do not import or modify it.
- `mini_ork/cli/main.py` — `SUBCOMMAND_REGISTRY: dict` built in `_build_default_registry()`;
  the line to copy is `"calibrate": "mini_ork.cli.calibrate",` (line ~67).
  `register_subcommand(name, handler)` also exists; use the **literal dict line**.
- `tests/unit/test_artifact_contract_py.py` — the gate's existing tests. Leave green.
- `tests/unit/test_goal_loop_driver.py` — the `_load(name, path)` file-path import pattern
  (`importlib.util`) to copy if you want to import by path; a normal `from mini_ork.gates
  import gate_fuzzer` is fine here because these modules live in the installed package.
- `tests/unit/test_mutation_adversary_gate_py.py` — the house style for a gate-adjacent unit
  test (hermetic, tmp_path, no network, no lane).

## Deliverable 1 — `mini_ork/gates/gate_fuzzer.py` (new)

A gate-agnostic, hermetic fuzzer. It must not import a model, a lane, or the network.

```python
def fuzz_gate(
    evaluate: Callable[[dict], str],
    cases: Sequence[dict],
) -> dict:
```

Semantics:

- `evaluate(case) -> str` returns `"pass"`, `"fail"`, or `"defer"`. Any other return value is
  an error, not a verdict — raise `ValueError` naming the offending case id and value.
- Each case is a dict with at least `"id"` (str, unique), and `"expect"` (`"pass"` or
  `"fail"`). Anything else in the case is opaque — passed to `evaluate` untouched.
- An `"expect"` value other than `"pass"` / `"fail"` raises `ValueError`. An unknown
  expectation must never be silently scored.
- The report:

```python
{
  "n": int,                    # len(cases)
  "n_expect_pass": int,
  "n_expect_fail": int,
  "blind_spots": int,          # expect "fail", got "pass"
  "over_blocks": int,          # expect "pass", got "fail"
  "defers": int,               # got "defer" (counted regardless of expectation)
  "blind_spot_rate": float | None,   # blind_spots / n_expect_fail; None when n_expect_fail == 0
  "over_block_rate": float | None,   # over_blocks / n_expect_pass; None when n_expect_pass == 0
  "results": [
      {"id": str, "expect": str, "got": str, "ok": bool, "detail": str},
      ...
  ],
}
```

- `ok` is `True` when the case behaved as the fuzzer demands, and `False` for a blind spot or
  an over-block. **A `defer` is not `ok` and not a blind spot** — it is an unmeasured case and
  is counted only in `defers`, so a gate that answers nothing scores no credit. (This mirrors
  `mutation_adversary.gate_verdict`, which refuses to score `skipped`/`zero` as a pass.)
- Rates are `float` in `[0.0, 1.0]`, or `None` when the denominator is zero. A rate that is
  `0/0` is `None`, never `0.0` — reporting "no blind spots" for a corpus with no rejectable
  cases is the exact false reassurance this fuzzer exists to prevent.
- `results` is ordered as the `cases` were supplied.

```python
def load_corpus(path: str) -> list[dict]:
```

Reads a JSON array of cases. Raises `ValueError` (with the path) when the file is not a JSON
array, when an entry is not an object, when `"id"` is missing/empty/not a string, when an `id`
repeats, or when `"expect"` is not `"pass"`/`"fail"`. Validation lives here so `fuzz_gate` can
assume well-formed cases from this loader.

```python
def artifact_contract_evaluator(workdir: str) -> Callable[[dict], str]:
```

The adapter that makes the shipped corpus runnable. Returns a closure that, per case:

1. writes `case["artifact"]["content"]` to `os.path.join(workdir, case["artifact"]["name"])`
   (creating parent dirs; `content` is a `str`; a `None` content means: create **no** file at
   that path, for the not-found probe),
2. calls `artifact_contract.validate_artifact(case["contract"], path)` with the module's real
   function,
3. returns `payload.get("verdict", "defer")` (a missing/unexpected verdict is `"defer"`, never
   a silent pass).

Import the gate lazily **inside** this factory (module-level `from ... import validate_artifact`
is fine too, but do not copy the function — call the real one so the fuzzer measures the
shipped gate).

```python
def summarize(report: dict) -> str:
```

One human line, e.g.
`gate-fuzz: n=9 blind_spots=4/5 (0.800) over_blocks=1/4 (0.250) defers=0`.
`None` rates print as `-` (e.g. `blind_spots=0/0 (-)`).

## Deliverable 2 — the starter corpus `mini_ork/gates/probes/artifact_contract_probes.json`

A JSON array of **at least 8** cases with **unique ids**. Every case carries `"id"`,
`"expect"`, `"artifact": {"name": str, "content": str | null}`, and `"contract": {...}`. The
artifact `name` is a **relative path** (the evaluator writes it under a temp workdir); use
only names that are legal on macOS and Linux.

The corpus must contain, at minimum, these named cases (ids exactly as written):

- `no-verifiers-accepts-garbage` — `expect: "fail"`; contract `success_verifiers: []`,
  `expected_artifact: "data"`; artifact `out.json` with genuinely invalid content
  (`"not json at all"`). The shipped gate **passes** it. **Proves the default contract cannot
  reject anything.**
- `weak-verifier-file-exists` — `expect: "fail"`; contract verifier `test -e` (checks
  presence, not content); artifact `out.json` containing `"{}"` — present but semantically
  empty. The gate **passes** it. **A proxy that is not the property.**
- `weak-verifier-nonempty` — `expect: "fail"`; contract verifier `test -s`; artifact
  `out.json` whose content is a single space. The gate **passes** it.
- `strong-verifier-rejects-wrong-content` — `expect: "fail"`; contract verifier
  `grep -q '"ok": true'`; artifact `out.json` containing `{"ok": false}`. The gate **correctly
  fails** it — this case must be a **non**-blind-spot (proves the fuzzer is not counting
  everything).
- `strong-verifier-admits-good-content` — `expect: "pass"`; same verifier; artifact
  `out.json` containing `{"ok": true}`. The gate passes it. A **non**-over-block.
- `extension-mismatch` — `expect: "fail"`; contract `expected_artifact: "patch"`; artifact
  `notes.md`. The gate correctly fails it (extension check).
- `missing-artifact` — `expect: "fail"`; `"content": null` so the file is never written;
  contract `expected_artifact: "data"`. The gate correctly fails it (`artifact not found`).
- `apostrophe-path-overblock` — `expect: "pass"`; the artifact `name` contains a single quote
  (e.g. `o'ut.json`), content `{"ok": true}`, contract verifier `grep -q '"ok": true'`. The
  shipped gate **fails** a legitimate submission here, because `_run_verifier` interpolates
  the path into `bash -c "{verifier} '{path}'"` and the quote breaks the command. **Record
  this as an over-block finding; do not fix the gate in this cycle.**

Add whatever further cases make the audit honest, but every expected-fail case must be one a
*strict* reader would agree should be rejected, and every expected-pass case must be a genuine
legitimate submission. Do not pad the corpus with trivially-correct cases to lower the rate.

The result on today's gate is **expected to be a non-zero blind-spot rate** — that is the
finding, not a bug in the fuzzer. If it comes out zero, say so with the report rather than
adjusting a case's expectation.

## Deliverable 3 — `mini_ork/cli/gate_fuzz.py` + registry line

```python
def main(argv: list[str] | None = None) -> int:
```

- `--corpus PATH` — default: the shipped `mini_ork/gates/probes/artifact_contract_probes.json`
  resolved relative to the package (`Path(__file__).resolve().parents[1] / "gates" /
  "probes" / "artifact_contract_probes.json"`).
- `--json` — print the report as `json.dumps(report, indent=2, sort_keys=True)`.
- default (no `--json`) — print `summarize(report)`, then one indented line per **failing**
  case (`blind spot` / `over block`), naming its id and the verdict it got.
- **Always exit 0.** This is a measurement, not a gate; a red exit would make it unusable in
  the very loop it is meant to feed. (The fixer that acts on a finding is out of scope.)
- It must be runnable as `python3.11 -m mini_ork.cli.gate_fuzz --json` **and** as
  `mini-ork gate-fuzz --json`.

Then add exactly one line to `SUBCOMMAND_REGISTRY` in `mini_ork/cli/main.py`, beside the
`"calibrate"` entry:

```python
    "gate-fuzz": "mini_ork.cli.gate_fuzz",
```

## Tests — `tests/unit/test_gate_fuzzer_py.py` (new)

Hermetic: no network, no lane, no `bin/mini-ork`, no DB. Use `tmp_path` for artifacts.

**These assertions are the contract** — implement all of them; do not reword, weaken, or drop
one to match what you built:

1. **Rate math is exact.** A synthetic `evaluate` over a hand-built case list with a known
   mix (e.g. 2 expected-fail of which 1 is accepted, 3 expected-pass of which 1 is rejected,
   1 defer) yields `blind_spots == 1`, `over_blocks == 1`, `defers == 1`,
   `blind_spot_rate == 0.5`, `over_block_rate == 1/3`, and `results` in input order.
2. **`0/0` is `None`.** A corpus with **no** expected-fail case has `blind_spot_rate is None`;
   with no expected-pass case, `over_block_rate is None`. Assert it is `None`, not `0.0`.
3. **A defer is not `ok` and not a blind spot.** A case `expect: "fail"` whose evaluator
   returns `"defer"` leaves `blind_spots == 0`, increments `defers`, and has `ok is False`.
4. **Bad input raises.** An unknown `expect` raises `ValueError`; an evaluator returning
   `"maybe"` raises `ValueError`; `load_corpus` on a missing file, on a non-array JSON, and on
   a corpus with a duplicate `id` each raise `ValueError`.
5. **The shipped corpus is well-formed.** `load_corpus(<shipped path>)` returns ≥ 8 cases, all
   ids unique, every case having `id`/`expect`/`artifact`/`contract`, and at least one case
   with each expectation.
6. **The blind spot is real, end to end.** Run `fuzz_gate(artifact_contract_evaluator(tmp_path),
   load_corpus(<shipped>))` and assert `report["n"] == len(corpus)` and
   `report["blind_spots"] >= 1` — i.e. against the **real** shipped gate, at least one
   submission that should have been rejected was accepted.
7. **The fuzzer discriminates.** In that same report, assert the id
   `strong-verifier-rejects-wrong-content` has `ok is True` (got `"fail"`) and
   `strong-verifier-admits-good-content` has `ok is True` (got `"pass"`) — so the blind-spot
   count above is not an artifact of the fuzzer calling everything a blind spot.
8. **The quoting over-block is observed.** Assert the id `apostrophe-path-overblock` has
   `got == "fail"` and `ok is False`, i.e. a legitimate submission is rejected because of the
   path interpolation. This documents the hazard; it is not fixed here.
9. **`summarize` renders `None` as `-`.** A report with `blind_spot_rate is None` produces a
   string containing `0/0 (-)`.

## Files in scope

- `mini_ork/gates/gate_fuzzer.py` (new)
- `mini_ork/gates/probes/artifact_contract_probes.json` (new)
- `mini_ork/cli/gate_fuzz.py` (new)
- `mini_ork/cli/main.py` — one added registry line
- `tests/unit/test_gate_fuzzer_py.py` (new)

Do **not** modify `mini_ork/gates/artifact_contract.py` (this cycle measures the gate; it does
not fix it), `mutation_adversary.py`, `native_gates.py`, `gate_registry.py`, any
`workflow.yaml` / `task_class.yaml` / `artifact_contract.yaml`, `recipes/**`, `web/**`, or any
other recipe. Do not add a model lane, a network call, or a DB dependency. Do not make
`gate-fuzz` exit non-zero.

## Verification commands

Run from the worktree root with `python3.11` explicitly — the ambient `python3` is 3.9 and
dies at collection.

```bash
python3.11 -m pytest tests/unit/test_gate_fuzzer_py.py -q
python3.11 -m pytest tests/unit/test_artifact_contract_py.py -q
python3.11 -m py_compile mini_ork/gates/gate_fuzzer.py mini_ork/cli/gate_fuzz.py
python3.11 -c "from mini_ork.cli.main import SUBCOMMAND_REGISTRY; assert 'gate-fuzz' in SUBCOMMAND_REGISTRY, 'gate-fuzz not registered'; print('gate-fuzz registered')"
python3.11 -m mini_ork.cli.gate_fuzz --json
```

## Self-application measurement

Zero model spend, and it is the point of the cycle: prove the gate's blind spot is real by
running the fuzzer against the shipped gate on the shipped corpus.

```bash
python3.11 - <<'PY'
import json, tempfile
from mini_ork.gates import gate_fuzzer as gf
corpus = gf.load_corpus(gf.DEFAULT_CORPUS)
with tempfile.TemporaryDirectory() as d:
    rep = gf.fuzz_gate(gf.artifact_contract_evaluator(d), corpus)
print(gf.summarize(rep))
print(json.dumps({k: rep[k] for k in
      ("n","n_expect_pass","n_expect_fail","blind_spots","over_blocks","defers",
       "blind_spot_rate","over_block_rate")}, indent=2))
assert rep["blind_spots"] >= 1, rep
assert rep["blind_spot_rate"] and rep["blind_spot_rate"] > 0, rep
print("GATE BLIND SPOT CONFIRMED")
PY
```

(Export `DEFAULT_CORPUS` from `gate_fuzzer` as the resolved shipped-corpus path so the CLI and
this measurement agree on one source.) Report the printed report verbatim. If the blind spot
does **not** fire, that is the finding — say so with the output rather than adjusting the
claim.

Evidence artifact `${MINI_ORK_RUN_DIR}/rsi-g3-gate-fuzzer.json`:

```json
{"fuzzer_implemented": true, "corpus_size": <int>, "blind_spots_observed": <int>,
 "blind_spot_rate_observed": <float>, "over_blocks_observed": <int>,
 "cli_registered": true, "artifact_contract_tests_green": true,
 "gate_unchanged": true, "quoting_hazard_recorded": true,
 "strict_case_not_a_blind_spot": true}
```

`blind_spots_observed`, `blind_spot_rate_observed` and `over_blocks_observed` are the numbers
you actually observed, not the ones the corpus "should" produce.

## Done When

- `tests/unit/test_gate_fuzzer_py.py` is green and `tests/unit/test_artifact_contract_py.py`
  is still green.
- `mini-ork gate-fuzz --json` prints a report with `n` equal to the corpus size and a
  non-`None` `blind_spot_rate`.
- `fuzz_gate` returns `None` (not `0.0`) for a rate whose denominator is zero, and counts a
  `defer` as neither a pass nor a blind spot — verifiable by reading the tests.
- `strong-verifier-rejects-wrong-content` is **not** counted as a blind spot, proving the
  fuzzer discriminates rather than counting every case.
- `git diff` shows `mini_ork/gates/artifact_contract.py` **unchanged** — this cycle measures.
- `${MINI_ORK_RUN_DIR}/rsi-g3-gate-fuzzer.json` exists with the fields above.

## If you cannot finish

Say so — with the failing command and its output — rather than narrowing the tests or
reporting a partial build as done. A precise partial result beats a green claim that does not
survive `git diff`.
