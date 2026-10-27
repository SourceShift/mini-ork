# S2: revise and repair feedback shows the failing check's own lines

## Goal

Run `bif-r5c-gaming-is-high-20261027-123156` failed static-check on ONE
check, `no-test-gaming`. Its evidence log has the answer in that check's
section:

```
[no-test-gaming] diff adds no test-harness detection or cross-module patching to production code
  mini_ork/cli/execute_handlers.py:1715: pytest-env-probe: 'that detects a test run (PYTEST_CURRENT_TEST, ...'
  FAIL
```

Neither feedback builder showed it, so three revise/repair rounds never fixed
a one-line problem:

1. `_revise_failure_section` (`mini_ork/cli/execute.py` ~5279-5350, in-run
   revise rounds) writes the JSON summary ("no-test-gaming failed; see …log")
   and then the first error-shaped lines of the log (`_error_lines`). Those
   were `error: patch failed: …` lines from `diff-apply-check-clean`, a check
   that PASSED (the forward apply-check fails by design when the edit is
   already applied; the reverse check succeeds).
2. `_failing_verifier_sections` (`mini_ork/recovery/auto_repair.py` ~961-1030,
   auto-repair rounds) reads `data.get("failed_checks") or data.get("checks")`.
   `failed_checks` is a list of check NAMES (strings), so it shadows the
   `checks` dicts, every entry is skipped as a non-dict, and no
   `failing check:` line is written. It then falls back to the same
   error-shaped noise.

Add one parser for the static-check log's section format, and have both
builders lead with the failing sections.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/verify/check_log.py` (new; stdlib only)

```
SECTION_RE = re.compile(r"^\[([A-Za-z0-9_.:-]+)\]\s")
def failing_sections(log_path: str, names: list[str] | None = None,
                     *, max_lines_per_check: int = 20, max_lines: int = 60) -> list[str]
```

- The log is a sequence of sections. A section starts at a line matching
  `SECTION_RE` (`[<check-id>] <description>`) and runs until the next such
  line or the end of the file.
- A section FAILED when one of its lines, stripped, equals `FAIL`.
- Return, for each failed section in file order, its header line followed by
  its body lines (without the `FAIL` marker line), each line `rstrip`ped,
  capped at `max_lines_per_check` lines per section and `max_lines` in total.
- When `names` is given, keep only sections whose check id is in `names`.
- A missing or unreadable file, or a log with no failed section, → `[]`.

### 2. `mini_ork/cli/execute.py` — `_revise_failure_section`

Before the existing `_error_lines` loop over `_verifier_log_candidates`, try
`failing_sections(candidate, names)` on each candidate, where `names` is the
artifact's `failed_checks` when it is a list of strings, else `None`. On the
first non-empty result, append `failing checks (<basename>):` and those
lines, and SKIP the `_error_lines` / tail fallback for this verifier. With an
empty result everywhere, behaviour is unchanged.

### 3. `mini_ork/recovery/auto_repair.py` — `_failing_verifier_sections`

1. Read check dicts from `data.get("checks")` only. Treat
   `data.get("failed_checks")` as a list of names: a dict check is failing
   when `pass is False`, its status is FAIL/REFUTED, OR its name/id is in
   that list.
2. Before the `_real_error_lines` loop, try `failing_sections(candidate, names)`
   on each log candidate (declared evidence path first, as today). On the
   first non-empty result, append `failing checks (<basename>):` and those
   lines and skip the `_real_error_lines` / tail fallback.

## Files in scope (touch ONLY these)

- `mini_ork/verify/check_log.py` (new)
- `mini_ork/cli/execute.py`
- `mini_ork/recovery/auto_repair.py`
- `tests/unit/test_check_log.py` (new)
- `tests/unit/test_revise_error_lines.py`
- `tests/unit/test_auto_repair_verifier_evidence.py`

Do not modify `recipes/`, the static-check verifier, `docs/`, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (hermetic, no LLM)

`tests/unit/test_check_log.py`, with a tmp log containing three sections:
an `ok` one, a `FAIL` one with two body lines, and a section whose body has
`error: patch failed: x` but ends `ok`:

1. `failing_sections(log)` returns exactly the FAIL section's header and two
   body lines; no `patch failed` line appears.
2. `names=["other"]` → `[]`; `names=["no-test-gaming"]` → the section.
3. Caps: a FAIL section with 50 body lines → 20 lines (header included in the
   cap); two such sections with `max_lines=30` → 30 lines.
4. Missing file → `[]`.

`tests/unit/test_revise_error_lines.py` (append):

5. A run dir whose `verifier_static-check.json` has `pass: false`,
   `failed_checks: ["no-test-gaming"]`, `evidence_path` → that log, and the log
   above → `_revise_failure_section` output contains
   `failing checks (verifier-static-check.log):` and the FAIL section's body
   line, and NOT `patch failed`.

`tests/unit/test_auto_repair_verifier_evidence.py` (append):

6. Same run dir → `_failing_verifier_sections(run_dir)` contains
   `failing check: no-test-gaming`, the FAIL section's body line, and not
   `patch failed`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_check_log.py tests/unit/test_revise_error_lines.py tests/unit/test_auto_repair_verifier_evidence.py tests/unit/test_framework_edit_spec_tests.py tests/unit/test_review_converge.py tests/unit/test_mini_ork_execute_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_check_log.py --junitxml="${MINI_ORK_RUN_DIR}/bif-s2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-s2-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 mini_ork/verify/check_log.py mini_ork/cli/execute.py mini_ork/recovery/auto_repair.py tests/unit/test_check_log.py tests/unit/test_revise_error_lines.py tests/unit/test_auto_repair_verifier_evidence.py` is clean.
- A failed static check's own lines reach the implementer; lines from passing checks never do.
