# Post-run verify reuses the DAG's verifier results — no duplicate suite runs, no clobbered evidence

## Why (live, 2026-10-08)

After `execute`, the run flow (`mini_ork/cli/main.py` ~:1014-1024) runs `python -m
mini_ork.cli.verify <artifact>`. `verify.main` (`mini_ork/cli/verify.py` ~:303-380) re-runs
EVERY `artifact_contract.success_verifiers` script unconditionally, including scripts whose
workflow verifier node already ran inside this run (e.g. code-fix `verifiers/test.py`, which
the `test` node runs).

On run `ide-orca-b2b-story-20261008113340` (code-fix on the Zed fork,
`MINI_ORK_TEST_CMD=script/mini-ork-build`):
- the `test` node failed (cargo rc 101), the revise loop ran out, and rollback reverted the tree;
- then post-run verify re-ran `test.py` at 12:00. That started a second full cargo build of the
  ROLLED-BACK tree (minutes of CPU, measuring nothing about the candidate) and truncated
  `<run_dir>/verifier_test.log`, the only copy of the compiler errors. The run's failure
  evidence was destroyed.

Even on a green run, the second invocation duplicates the suite on an unchanged tree. For
slow suites that is minutes to an hour.

## Files in scope (touch ONLY these)

- `mini_ork/cli/verify.py`: ONLY the per-verifier loop in `main` + one helper
- `tests/unit/test_post_run_verify_reuse.py` (new)

## Changes (exact)

1. **`_dag_result(run_dir, name) -> dict | None`** (new):
   - When `run_dir` (env `MINI_ORK_RUN_DIR`) holds `verifier_<stem>.json`, where `<stem>` =
     `_verifier_stem(name)`, and it parses to a dict with a `pass` key, return it.
   - Otherwise None.
   - Also accept `verifier_<stem>.json` written as one JSON line. Some verifiers print a single
     line; the file holds it.
2. **In the loop, before running a script:**
   - When `os.environ.get("MO_VERIFY_RERUN") != "1"` and `_dag_result(...)` returns a dict:
     - do NOT run the script;
     - append to `results` the DAG result as a compact JSON line, adding
       `"reused": "dag"` and keeping its `evidence_path`;
     - count pass/fail from its `pass`: `True` → pass; anything else → fail. An `unverified` /
       abstain status counts the way the current code would count that same JSON.
   - Print `[verify] <name>: reused DAG result (MO_VERIFY_RERUN=1 to re-run)` to stderr.
3. **No DAG result** → today's behaviour, unchanged. When re-running is forced
   (`MO_VERIFY_RERUN=1`), today's behaviour is unchanged too.
4. **Gates and the verdict computation** (`gate_registry.gate_run_all`, min-evidence checks) are
   unchanged. A reused result must satisfy the same minimum-evidence assertion as a fresh one:
   - its `evidence_path` file exists and is non-empty, OR
   - the JSON has `error_summary`.

   Otherwise treat it as missing and fall back to running.

## Tests (`tests/unit/test_post_run_verify_reuse.py`; reuse fixtures from `tests/unit/test_mini_ork_verify_py.py`)

- **A run dir with `verifier_test.json` `{"verifier":"test","pass":false,...}`** plus a plan
  whose `success_verifiers` is `["verifiers/test.py"]`, where the verifier script is a stub that
  would create a marker file if run:
  - `verify.main` does not run it (no marker);
  - the verdict counts one fail;
  - stdout has `"reused":"dag"`.
- **The same with `pass: true` and non-empty evidence** → reused as a pass.
- **`MO_VERIFY_RERUN=1`** → the stub runs (marker present).
- **No `verifier_test.json`** → the stub runs (unchanged behaviour).
- **A reused result whose evidence file is missing and has no `error_summary`** → falls back to
  running.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_post_run_verify_reuse.py tests/unit/test_mini_ork_verify_py.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/cli/verify.py tests/unit/test_post_run_verify_reuse.py` → clean.
- `git diff --stat` touches only the files in scope.
