# rev6 · R7: the spec-tests verifier recognises a broken spec test and stops the revise loop chasing it

## Goal

The blind spec suite (`spec_test_author` → `recipes/framework-edit/verifiers/spec-tests.py`)
is mini-ork's strongest check, and it can be unsatisfiable. On 2026-10-26 two
runs failed only on spec tests no implementation could pass:

- **rev6 R2a** (`rev6-r2a-prepush-gate-20261026-133644`): 6 of 49 tests called
  the pytest fixture `driver(tmp_path, monkeypatch)` as a plain function.
  pytest refuses with `Fixture "driver" called directly. Fixtures are not meant
  to be called directly`, printed directly under the test's `___ name ___`
  header with **no `E` line**. That is why
  `failure_exceptions` (which reads only `E` lines) recorded no reason. The run
  spent both revise rounds and rolled back.
- **rev6 R5a**: a helper re-created its own directory. The implementer filed
  `spec-test-defect.md`, and the existing RB-18 rule (`mini_ork/cli/execute.py`,
  "a filed spec defect ends the round budget", `_spec_defect_report`) stopped
  the rounds.

Teach the verifier the cases that are certainly harness bugs, the pytest
fixture-misuse errors. When every failure is one, the verifier files
`spec-test-defect.md` itself, so RB-18 stops the rounds and the run escalates
to a person instead of burning implementer rounds. Mixed failures are
labelled, so the implementer knows which ones not to chase. The
`spec-tests-pass` check stays gating: a run never passes on broken tests.

All paths are relative to the repository root.

## Mechanism (exact spec)

### `recipes/framework-edit/verifiers/spec-tests.py`

1. **Patterns.** Module constant `_HARNESS_DEFECT_RES`, compiled, in this order:
   - `Fixture "([^"]+)" called directly`
   - `fixture '([^']+)' not found`
   - `ScopeMismatch: .+`
2. **`harness_defects(output: str) -> dict[str, str]`**: walk pytest's output
   using the same `_TRACEBACK_HEADER_RE` header detection as
   `failure_exceptions`.
   - A header that names something starts a new block. For an
     `ERROR at setup of <name>` or `ERROR at teardown of <name>` header, strip
     that prefix before `_unit_key`.
   - Within the current block, the first line (any line, `E`-prefixed or not)
     matching a pattern records `{unit: line.strip()[:200]}`, and later lines
     in that block are ignored.
   - `=====` section lines (e.g. `short test summary info`) end the current
     block.
3. **In `evaluate`**, after the `failures` list is built:
   - **Mark.** Set `harness = harness_defects(output)`. For each failure whose
     `_unit_key(test)` is in `harness`, set `failure["harness_defect"]` to the
     message.
   - **When every failure is marked** (at least one failure exists):
     - if `<run_dir>/spec-test-defect.md` does not exist (never overwrite an
       implementer-filed one), write it:
       ```
       # Spec-test defect (detected by the spec-tests verifier)

       These spec tests fail with a pytest usage error inside the test code itself; no implementation can make them pass. A person should fix the spec tests and re-measure. They say nothing about the implementation.

       - `<test>` — <harness_defect message> — <kickoff quote>
       ```
       (one bullet per failure);
     - set `result["harness_defect_report"]` to that path.
     - Writing errors are ignored.
   - `result["harness_defects"]` is the sorted list of marked test ids, always
     present (`[]` when none).
4. **`_failure_clause`**: when `failure.get("harness_defect")`, add
   `[harness defect: <message>]` after the test id, in place of the
   `[error]` part. The kickoff quote stays.
5. Nothing else changes:
   - `spec-tests-pass` keeps `pass: rc == 0` and `gating: True`;
   - tamper detection, docstring rules, deselection and timeouts are unchanged.

## Files in scope (touch ONLY these)

- `recipes/framework-edit/verifiers/spec-tests.py`
- `tests/unit/test_spec_tests_harness_defects.py` (new)

Do NOT modify `mini_ork/cli/execute.py`, the spec_test_author prompt, any other
verifier, or any path in `config/kernel-frozen.yaml`. Every existing test must
pass unchanged.

## Tests (`tests/unit/test_spec_tests_harness_defects.py`, hermetic)

Load the verifier with `importlib.util.spec_from_file_location`, as
`tests/unit/test_framework_edit_spec_tests.py` does; reuse that file's
helpers' shape for an `evaluate` run dir (spec dir, author node end, `Kickoff:`
docstrings).

1. `harness_defects` on a captured pytest output where the
   `Fixture "driver" called directly` line sits under `___ test_a ___` with no
   `E` line → `{"test_a": 'Fixture "driver" called directly. …'}`.
2. An `ERROR at setup of test_b` block containing `fixture 'nope' not found` → `{"test_b": ...}`.
3. An ordinary `E   AssertionError: assert 1 == 2` block → not recorded.
4. A message in the `short test summary info` section only → not recorded.
5. `evaluate` end to end, with a real spec dir holding:
   - one test that calls a fixture directly;
   - one passing test;
   - both with `Kickoff:` docstrings.
   Expect:
   - `pass` False;
   - `harness_defects == [<that test>]`;
   - `<run_dir>/spec-test-defect.md` exists and names the test;
   - the `spec-tests-pass` actual contains `harness defect:`.
6. The same suite plus a third test that fails with a real `assert False` →
   `spec-test-defect.md` is NOT written; only the fixture test is marked.
7. A pre-existing `<run_dir>/spec-test-defect.md` (implementer-filed) is left
   byte-for-byte unchanged.
8. An all-passing suite → `harness_defects == []` and no file.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_spec_tests_harness_defects.py tests/unit/test_framework_edit_spec_tests.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_spec_tests_harness_defects.py --junitxml="${MINI_ORK_RUN_DIR}/rev6-r7-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rev6-r7-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 recipes/framework-edit/verifiers/spec-tests.py tests/unit/test_spec_tests_harness_defects.py` is clean.
- A run whose only spec failures are fixture misuse can no longer spend a
  revise round on them (RB-18 sees `spec-test-defect.md`).
- No failure outside the three patterns is ever classified as a harness defect.
