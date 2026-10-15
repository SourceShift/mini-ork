# LF-R5 harvest only code problems, with honest reasons, files and severities

## Goal

`mini_ork/learning/code_findings.py` stores, and context v2 then injects under
"Problems reviewers already found in these files. Do not repeat them", text
that is not a code problem:

- receipts and harness notes the `RECEIPT_RE` filter misses: "Diff inspected
  and in scope: …", "Root cause of the false is environmental, not the
  implementation …", "To clear the gate: resolve the suite-adequacy module
  availability …" (all injected into `run-1791930074-82118`);
- process complaints carried by STRUCTURED reviewer findings, which bypass the
  prose filter: row 3791 "This delivery was pushed to origin/main as commit
  43740937 … before the reviewer node ran …" (filed under
  `mini_ork/ide_pages/__init__.py:32`), row 2286 "No approve is possible:
  files_changed=[] with 0-byte review-diff.patch …";
- 63 `verifier:static-check` rows with severity `high` whose issue is the
  PASSING note "diff-apply-check-clean: diff applies cleanly to repo root",
  because `_verifier_reason` falls back to joined notes when no check is named;
- 227 rows filed under run artifacts (`verdict.json` 78, `implementer-summary.json`
  52, `verifier_test.json` 45, `lens-bottleneck.md` 30, …) or absolute paths;
- every unlabelled prose finding gets severity `medium`, so the default
  `MO_CONTEXT_V2_MIN_SEVERITY=medium` floor passes 83% of rows.

`prior_attempts` (`mini_ork/context_v2.py`) injects the top 3 earlier findings
with no floor and no filter. Decision record:
`docs/plans/2026-10-15-learning-loop-fixes.md` (R5). R6's `prune_duplicates`
is already on main.

All paths are relative to the repository root.

## Mechanism (exact)

1. `code_findings.classify_note(text) -> "code" | "process" | "harness" | "receipt"`:
   `receipt` = the existing `RECEIPT_RE` plus `diff inspected`, `in scope:`,
   `applies cleanly`, `diff-apply-check-clean`; `harness` = environment / gate /
   harness / lane infrastructure (`not the implementation`, `to clear the gate`,
   `suite-adequacy`, `module availability`, `harness`, `rc=124`, `quota`,
   `rate limit`, `usage limit`, `lane (died|timed out)`); `process` = delivery
   and review-procedure complaints (`pushed to origin/main`, `before the
   review`, `review-before-merge`, `no approve is possible`, `files_changed=[]`,
   `0-byte`, `skipped_steps`, `confidence 0.0`, `merged before`); else `code`.
   Order: receipt, harness, process, code. Keep the patterns in one module-level
   table with a comment per group.
2. Apply it to EVERY finding before insert — prose items AND structured
   `findings[]` / dict notes. Only `code` findings are stored.
3. `_verifier_reason`: when no failed check is named and every reasons/notes item
   classifies as `receipt` or `harness`, return None, and `_parse_verifier_raw`
   then records nothing for that verifier.
4. `normalize_file` returns None for run artifacts — basenames matching
   `verdict.json`, `review-verdict.json`, `panel-verdict.json`, `plan.json`,
   `run_profile.json`, `synthesis.md`, `implementer[-_]summary.json`,
   `verifier[-_]*.json`, `review-*.json`, `context-*.json`, `lens-*.md`,
   `framework-edit.diff`, `*.log` — and for paths still absolute after prefix
   stripping.
5. `normalize_severity`: a finding with no severity token gets `unknown`
   (instead of `medium`/`low`). `mini_ork/context_v2.py` `_SEVERITY_RANK` gains
   `"unknown": 0`, so the default floor excludes it.
6. `context_v2.prior_attempts` applies `min_severity()` and drops rows whose
   `classify_note(issue)` is not `code`.
7. `prune_receipts` deletes every row whose issue is not `code` under the new
   classifier. Add `clear_artifact_files(db=None) -> int` that sets `file = NULL`
   where `normalize_file(file)` is None. Add `reharvest(home, *, db=None) -> dict`:
   for every run dir that still exists under `home/runs`, delete that run's
   `code_findings` rows and harvest it again with the current parser (runs whose
   dirs are gone keep their rows). All three: cold-safe, never raise, return counts,
   exposed on the module CLI (`prune`, `clear-artifact-files`, `reharvest`).
   `mini_ork/cli/reflect.py` calls `prune_receipts` and `clear_artifact_files`
   right after `prune_duplicates` (fail-soft, print counts only when > 0).
   `reharvest` is NOT called automatically.

## Files in scope

- `mini_ork/learning/code_findings.py`
- `mini_ork/context_v2.py`
- `mini_ork/cli/reflect.py`
- `tests/unit/test_code_findings.py`
- `tests/unit/test_code_findings_hygiene.py` (new)

## Tests (`tests/unit/test_code_findings_hygiene.py`, hermetic)

- the three F12 strings, the texts of rows 3791 and 2286, and
  "diff-apply-check-clean: diff applies cleanly to repo root" each classify as
  non-`code`; "The log proxy leaks into the transcript lower bound whenever
  impl-<node>.log exists" and "Dead priority parse: _task_card never emits a
  prio segment" classify as `code`;
- a structured reviewer finding with `file`/`line` and process text (row 3791's)
  is not stored; a structured code finding is stored with its file;
- a failed verifier whose only note is the diff-apply-check-clean receipt
  produces no row; one with a named failed check produces one `high` row;
- `normalize_file` returns None for `verdict.json`, `verifier_test.json`,
  `lens-bottleneck.md`, `/Users/admin/.py` and keeps `mini_ork/context_v2.py`;
- unlabelled prose under a `fail` verdict gets `unknown`; a `BLOCKER …` prefix
  still gets `high`;
- `prior_attempts` drops `unknown` and non-code rows;
- `reharvest` on a temp home rewrites a run's rows with the current parser and
  leaves rows of a run whose dir is missing untouched;
- `prune_receipts` and `clear_artifact_files` are idempotent (second call 0).

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_hygiene.py tests/unit/test_code_findings.py tests/unit/test_code_findings_dedupe.py tests/unit/test_context_v2.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_code_findings_hygiene.py tests/unit/test_code_findings.py tests/unit/test_code_findings_dedupe.py tests/unit/test_context_v2.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r5-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r5-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `_fingerprint`, `parse_contract`,
`context_v2.build`, `mini_ork/ide_pages/`, any migration, and do not write to
the live DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/` (tests use temp
DBs only).
