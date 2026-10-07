# "Your code" tab — revision 2 (Opus review of run eng-your-code-tab-20261007163940)

WIP commit 239eb23f holds revision 1: 66 tests pass, ruff clean, tab order and default `code`
right, page read-only, reflect hook with opt-out. Opus returned `needs_revision`. Fix ONLY the
list below.

## Files in scope

- `mini_ork/ide_pages/learn/code.py`
- `mini_ork/cli/reflect.py`: ONLY the code_findings block
- `mini_ork/learning/code_findings.py`: ONLY `parse_review` / its note handling, plus a new
  `prune_receipts`
- `tests/unit/test_ide_pages_learn_code.py`, `tests/unit/test_code_findings.py`

Do NOT modify any other file.

## Fixes (exact)

1. **BLOCKING: summary and recurring problems must use ALL of the area's findings.** At
   `code.py:232/250-262`, the summary line and `recurring()` read the 50-capped findings list.
   Live: node.py's table says 147 findings / 24 runs, the detail says "50 findings in 8 runs", and
   a probe showed "60, high" vs "50 findings, worst low". Read the full set for the summary and the
   clusters, and cap only the Findings TABLE at 50. Add a regression test with more than 50
   findings that checks the summary count, runs and worst severity.
2. **BLOCKING: the exact-file branch.** At `code.py:227`, `LIMIT 50` is applied before filtering
   `file == base`, so prefix siblings (`bin/mini-ork-apply`, `-bugs`, `-epics` …) crowd out
   `bin/mini-ork`. Filter in SQL (`file = ?`) before the limit. Add a test.
3. **The reflect hook import goes inside the try** (`reflect.py:475`). A failing import must be
   fail-soft like the call.
4. **The detail honors the `days` window**, like the areas table (`code.py:250`).
5. **Receipts are not findings** (`code_findings.parse_review`). Reviewer notes that report
   checks rather than problems are harvested today because they name a file. Live examples:
   "Checked fine: kickoff gate 66 passed; ruff clean", "static-check diff-apply-check-clean FAIL
   is a verifier artifact", "OK: --reason is required by memory_lifecycle.py:39", "PASS: …",
   "VERIFIED: …", "FIXED BLOCKER: …", "NOT A DEFECT: …", "Scope OK: …".
   - A string note or reason is skipped when it matches the module-level `RECEIPT_RE`:
     `^\W*(pass|ok|verified|checked|fixed|accepted|not a defect|scope ok|resolved|confirmed)\b`
     (case-insensitive), OR contains `\b\d+ passed\b`, `ruff (clean|check)`,
     `false (negative|positive)`, `verifier artifact`, `reverse[- ]?apply`,
     `already (applied|exists)`.
   - When the review verdict is a pass (`pass` / `approve` / `APPROVE`), ALL string notes are
     receipts. Structured `findings[]` dicts are always kept.
   - New `prune_receipts(db=None) -> int` deletes existing `code_findings` rows whose `issue`
     matches the receipt rule. Expose it as `python -m mini_ork.learning.code_findings prune`.
6. **Test fixtures:** reuse the live strings above verbatim as receipts (skipped). Keep 2 real
   problem notes from the same reviews (kept).

## Verification command

The command that proves this run succeeded (run per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn_code.py && sleep 3 && env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_code_findings.py && sleep 3 && env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_learn.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/learn/code.py mini_ork/cli/reflect.py mini_ork/learning/code_findings.py tests/unit/test_ide_pages_learn_code.py tests/unit/test_code_findings.py` → clean.
- Live proof on a BACKUP of the live DB (`sqlite3` backup into a temp file):
  - run `prune_receipts`, then render `learn/code` and `learn/code area=mini_ork/ide_pages`
    with `MINI_ORK_DB=` the temp file;
  - the node.py summary count must equal the areas-table count;
  - the recurring problems must be real code problems, not receipts.
  Paste them and delete the temp file.
- `git diff 239eb23f --stat` touches only the files in scope.
