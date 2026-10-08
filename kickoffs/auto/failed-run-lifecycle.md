# A failed run is handed to auto-repair right after execute, and post-run verify does not rebuild a rolled-back tree

## Why (live, 2026-10-08)

Run `ide-orca-f2b-flow-20261008172728` (code-fix on the Zed fork, `MINI_ORK_TEST_CMD =
script/mini-ork-build`):

1. The round-2 implementer timed out. The DAG skipped `test` and `typecheck` ("blocked by failed
   parent"), and rollback reverted the tree. `execute` ended the run `failed` at 18:44:37.
2. `mini_ork/cli/main.py` `_run_lifecycle_impl` then ran the post-run verify
   (`mini_ork.cli.verify` subprocess).
   - There was no DAG result to reuse, so verify re-ran `script/mini-ork-build`, a full Rust
     build, against the REVERTED tree.
   - That build blocked on a cargo lock held by an orphan of the dead implementer. Verify
     never finished.
3. The lifecycle process ended without its stdout being flushed. It never reached the
   auto-repair hook (~:1063-1074, after verify and retry_notify). So `auto_repair.maybe_repair`
   never ran for a run whose decision is clearly `infra` from `implementer`: `mini-ork repair
   <run> --dry-run` says so. No `repair.json` exists for ANY run since auto-repair shipped.

Two fixes:

- **Hand the failure to auto-repair as soon as execute has decided it.** Do not wait for the
  slow, fragile tail: rubric, verify, reflect.
- **Never re-run a verifier on a rolled-back tree.** When the DAG skipped a verifier and the
  run rolled back, there is nothing to verify.

## Files in scope (touch ONLY these)

- `mini_ork/cli/main.py`: ONLY the auto-repair hook placement in `_run_lifecycle_impl`
- `mini_ork/cli/verify.py`: ONLY the post-run reuse branch (~:401-445)
- `tests/unit/test_failed_run_lifecycle.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **`main.py`: move the hook.**
   - Move the `auto_repair.maybe_repair(Path(home), run_id, wait_for_exit=True)` call (with
     its try/except) to RIGHT AFTER `_write_execute_log(...)`, i.e. after execute returns and
     before the rubric pre-screen.
   - Keep the comment, updated: the spawned recover already waits for this lifecycle to exit
     (`MO_AUTO_REPAIR_WAIT_PID`), so handing off early is safe. If this process later dies in
     verify or reflect, the repair still happens.
   - Remove the old call site. There must be exactly ONE call.
   - The withheld-publish case still works: the publisher sets `status='failed'` during
     execute, so the status is already terminal when the hook runs.
2. **`verify.py` post-run reuse branch.** No DAG result for a verifier
   (`_dag_result(run_dir, name) is None`) AND the run was rolled back (`<run_dir>/rolled-back.json`
   exists) AND `MO_VERIFY_RERUN != "1"` → do NOT run it. Emit the row:

   ```
   {"verifier": name, "pass": None, "evidence_path": "", "reused": "skipped",
    "detail": "not run: the workflow skipped this verifier and the run was rolled back"}
   ```

   Print `[verify] <name>: not re-run on a rolled-back tree (MO_VERIFY_RERUN=1 to force)` to
   stderr. It counts as neither pass nor fail, exactly like `pass: null` today.

## Tests (`tests/unit/test_failed_run_lifecycle.py`)

- **`verify.py`:** a temp run dir with `rolled-back.json` and no `verifier_test.json` → the test
  verifier script is NOT executed (monkeypatch the runner, or point it at a script that writes a
  sentinel file and assert the sentinel is absent), and the result row has
  `"reused": "skipped"` and `pass` null.
  - With `MO_VERIFY_RERUN=1` it IS executed.
  - Without `rolled-back.json` it IS executed (unchanged).
- **`main.py`:** the source has exactly one `maybe_repair(` call, and it appears before the
  rubric pre-screen in `_run_lifecycle_impl`. Use `inspect.getsource` and string offsets.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_failed_run_lifecycle.py tests/unit/test_auto_repair.py tests/unit/test_post_run_verify_reuse.py; do [ -f "$f" ] || continue; env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/main.py mini_ork/cli/verify.py tests/unit/test_failed_run_lifecycle.py` → clean.
- `git diff --stat` touches only the files in scope.
