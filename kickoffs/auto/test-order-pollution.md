# Three tests fail only when their whole file runs: find the leaking test and isolate it

## Why (2026-10-08)

Three tests pass when run alone but fail when their whole file runs, on `main` and in every
worktree:

| test | full-file result | alone |
|---|---|---|
| `tests/unit/test_retry_hint.py::test_run_page_retry_section_for_case_4` | FAILED | passed |
| `tests/unit/test_node_overview.py::test_resolve_session_path_falls_back_to_home_projects_transcript` | FAILED | passed |
| `tests/unit/test_board_cmd.py::test_diffstat_not_written_when_compute_fails` | FAILED (`assert False` at ~:805; the stub raises `RuntimeError("git show forbidden")`) | passed |

Every merge today had to re-run each of them alone to tell a real failure from this noise. A
red that is "known noise" hides the next real red.

**Cause class.** An earlier test in the same file leaves process state behind that
`tests/conftest.py` `_isolate_process_state` does not restore. The fixture restores only
`os.environ` and the cwd. Candidates:
- a module-level cache (`functools.lru_cache`, a `_CACHE` dict, a memoised `recipes_catalog`
  list, `retry_hint`'s hint cache, `fleet`/`node` caches);
- a monkeypatched module attribute set without `monkeypatch`;
- a file written outside `tmp_path` (`~/.claude/projects`, `/tmp`);
- a contextvar binding.

## Files in scope (touch ONLY these)

- `tests/unit/test_retry_hint.py`
- `tests/unit/test_node_overview.py`
- `tests/unit/test_board_cmd.py`
- `tests/conftest.py`: ONLY if the leak is a cache every test should reset (then reset it in
  `_isolate_process_state`, guarded by `sys.modules` so no module is imported just to reset it)

Do NOT touch any file under `mini_ork/`.
- A production cache that needs a public reset hook → do NOT add it. Isolate the test instead,
  with a fixture that clears or monkeypatches the cache.
- Note the finding in the final summary.

## Method (per failing test)

1. **Bisect the polluter.** Run `pytest <file>::<earlier_test> <file>::<failing_test>` for the
   earlier tests in file order, halving the prefix each time, until one earlier test alone makes
   the failing test fail.
2. **Name the leaked state exactly:** the module, attribute and value.
3. **Fix the isolation in the narrowest place:**
   - the polluting test cleans up after itself (`monkeypatch`, `tmp_path`); or
   - the failing test resets what it depends on.

   Do not weaken an assertion, and do not skip or xfail a test.

## Verification command

The command that proves this run succeeded (each file in chunks, because the host kills one
CPU-bound process that runs longer than 30 s):

```bash
for f in tests/unit/test_retry_hint.py tests/unit/test_node_overview.py tests/unit/test_board_cmd.py; do grep -n "^def test" "$f" | sed 's/.*def \(test[a-z0-9_]*\).*/\1/' | sed "s#^#$f::#" > /tmp/ids.$$; total=$(wc -l < /tmp/ids.$$); env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio $(cat /tmp/ids.$$) || exit 1; sleep 3; done   # must exit 0; each file's full ordered list in ONE process
```

If one file's full list in one process exceeds the 30 s CPU guard, split it into two
consecutive halves, but keep the polluter and the failing test in the SAME half, and say which
split you used.

## Done when

- The verification command → 0 failed, with each of the three tests running AFTER its polluter
  in one process. Paste the summary lines.
- For each test, one line: polluter test → leaked state → fix.
- `uvx ruff check` on the touched files → clean.
- `git diff --stat` touches only the files in scope.
