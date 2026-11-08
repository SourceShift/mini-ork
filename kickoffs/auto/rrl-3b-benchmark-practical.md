# RRL-3b: make the `verifier-replay` benchmark practical — scoped tests, resolved paths, surface-keyed cache

## Goal

`mini_ork/rsi/benchmarks.py` (RRL-3) scores a tree's code-fix verifier on 39 stored
held-out patches. A live smoke on 2026-10-29 found three problems that make it unusable
by gate G12 and the canary:

1. **Every item is skipped from the CLI.** `--repo .` stays relative; `replay_one`
   builds the scratch dir as `repo.parent / f"{repo.name}-replay-scratch"`, and with
   `repo == Path(".")` that is `-replay-scratch/…`, which `git worktree add` parses as
   an option.
2. **Items take 300 s+ and time out.** With no profile in the replay, the verifier
   auto-detects the whole suite of a months-old checkout. A calibration that instead
   handed the verifier `MINI_ORK_TEST_CMD` = pytest over the task's test files that
   exist at base ∪ the test files the stored patch adds or edits ran 36 of 39 items in
   73 s on average (max 240 s) with a real signal: 14 true rejects, 3 true accepts,
   14 false rejects, 0 false accepts. That is the item procedure this kickoff adopts.
3. **The cache key is the whole tree,** so every commit on `main` (by anyone) forces a
   full re-measure of the base. The benchmark only depends on its declared surface.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `scripts/verifier_replay.py`

- `run_verifier(verifier, scratch, python, timeout, *, import_root=None, extra_env=None)`
  — new keyword `extra_env: Mapping[str, str] | None = None`. The child environment is
  `{**os.environ, **(extra_env or {}), <the existing fixed keys>}` (fixed keys still
  win). Default behaviour unchanged. (Per-call env — never mutate `os.environ`; items
  run in threads.)
- New `TEST_SUFFIX = ".py"` helper `is_test_path(path: str) -> bool`: true when
  `path.endswith(".py")` and (`path.startswith("tests/")` or `"/tests/" in path` or the
  basename starts with `"test_"`).
- New `patch_files(patch: Path, cwd: Path) -> list[str]`: the third tab-separated column
  of each non-empty line of `git apply --numstat <patch>` run in `cwd`
  (`check=False`; a failure → `[]`).
- `replay_one(..., import_root=None, hidden=True, scoped_tests=False)` — new keyword.
  When `scoped_tests` is true, after a clean apply and before the verifier:
  - `files = sorted({f for f in task.get("test_files") or [] if (scratch / f).is_file()}
    | {f for f in patch_files(patch, scratch) if is_test_path(f) and (scratch / f).is_file()})`;
  - empty → `row["skipped"] = "no runnable tests for a scoped verifier run"`, return;
  - otherwise `row["scoped_tests"] = files` and call
    `run_verifier(..., import_root=import_root, extra_env={"MINI_ORK_TEST_CMD": cmd})`
    with `cmd = " ".join([shlex.quote(python), "-m", "pytest", "-q", "-p", "no:asyncio", *map(shlex.quote, files)])`.
  With `scoped_tests=False` everything is byte-for-byte as today.

### 2. `mini_ork/rsi/benchmarks.py`

- **Resolved paths.** `_vr_run`, `_tasks_and_patches`, `ensure_labels` and
  `_option_paths` use `Path(ctx.repo).resolve()`; `_vr_run` uses `Path(root).resolve()`
  for the verifier path and `import_root`. `main()` resolves `--root`, `--home` and
  `--repo` before building the context.
- **Scoped items.** `_vr_run` passes `scoped_tests=` to `replay_one`: true unless
  `ctx.options.get("scoped_tests", "1") == "0"`. A row with `skipped` stays skipped.
- **Surface key.** New constant `CACHE_VERSION = "2"` and
  `surface_key(root: Path, surface: Sequence[str], *, salt: str = "") -> str` — sha256 hex
  over: `CACHE_VERSION`, `salt`, then for each entry of `sorted(surface)`: when
  `root/entry` is a file, the line `f"{entry}:{sha256(bytes)}\n"`; when it is a
  directory, one such line per file under it (recursive, sorted relative paths,
  skipping any path containing `__pycache__` or ending in `.pyc`); when missing,
  `f"{entry}:missing\n"`.
- **Cache keying.** `run_benchmark` computes `key = surface_key(root, bench.surface, salt=<salt>)`
  when `bench.surface` is non-empty, else `tree_key(root)` as today, where
  `salt = json.dumps({"name": name, "options": dict(sorted(ctx.options.items()))}, sort_keys=True)`.
  The returned `BenchResult.tree` is that key (the runner's own `tree` value is
  replaced when merging/returning). Everything else in `run_benchmark` is unchanged.
  (A change outside the surface reuses the cached result — it can only read as "no
  gain", never as a false gain or a false regression.)

## Files in scope (touch ONLY these)

- `scripts/verifier_replay.py`
- `mini_ork/rsi/benchmarks.py`
- `tests/unit/test_rsi_benchmarks_py.py` — append tests; update an existing test only
  where it pins the cache key being `tree_key` for a benchmark that has a surface
  (state the change in a comment)
- `tests/unit/test_verifier_replay_py.py` — append tests only

Do NOT modify any other file (not `recipes/code-fix/verifiers/test.py`, not `evals/`).

## Tests (hermetic; no real replay)

- `run_verifier(..., extra_env={"X_RRL": "1"})` on a tiny script printing
  `{"pass": os.environ.get("X_RRL") == "1", "reason": "env"}` → `(True, "env")`; the
  fixed keys still win (`extra_env={"MINI_ORK_RUN_ID": "x"}` → the child sees
  `verifier-replay`); `os.environ` has no `X_RRL` afterwards.
- `is_test_path`: `tests/unit/test_a.py` True, `pkg/tests/x.py` True, `test_b.py` True,
  `mini_ork/a.py` False, `tests/data.json` False.
- `replay_one(..., scoped_tests=True)` in a temp git repo with one commit (as the task's
  `base_sha`), a patch adding `tests/test_new.py` and editing `src.py`, and a task
  `test_files=["tests/test_old.py", "tests/test_missing.py"]` where only
  `tests/test_old.py` exists: the verifier stub (monkeypatch `run_verifier`) receives
  `extra_env["MINI_ORK_TEST_CMD"]` naming exactly `tests/test_new.py` and
  `tests/test_old.py` (sorted) and not `test_missing.py`; `row["scoped_tests"]` lists
  them; a patch touching no test, with no base test files → `row["skipped"]` starts
  with `no runnable tests`.
- `scoped_tests=False` → `run_verifier` called without `extra_env` (or with `None`).
- `surface_key`: stable across calls; changes when a surface file changes; unchanged
  when a file OUTSIDE the surface changes; a `__pycache__/*.pyc` file under a surface
  dir does not change it; a missing entry is hashed as missing; different `salt` →
  different key.
- `run_benchmark` with a fake registered benchmark that HAS a surface: two git trees
  differing only outside the surface → the runner is called once (second call hits the
  cache) and both results carry the same `tree`; changing a surface file → runner
  called again.
- `_vr_run` with the fake replay module (existing pattern) and `ctx.repo=Path(".")`
  relative → the fake `replay_one` receives an absolute `repo` and
  `scoped_tests=True`; with `options={"scoped_tests": "0"}` → `scoped_tests=False`.
- CLI `main(["run", "verifier-replay", "--root", ".", ...])` with `_vr_run`
  monkeypatched → the runner receives an absolute root.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_benchmarks_py.py tests/unit/test_verifier_replay_py.py`
- `pytest -q -p no:asyncio tests/unit/test_rsi_impact_gate_py.py tests/unit/test_rsi_monitor_py.py`
- `ruff check --select F,E9 scripts/verifier_replay.py mini_ork/rsi/benchmarks.py tests/unit/test_rsi_benchmarks_py.py tests/unit/test_verifier_replay_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_benchmarks_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-3b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-3b-junit.xml`

## Success criteria

- All verification commands pass with every pre-existing test unchanged (apart from a
  cache-key pin, if any, as stated above).
- Default `replay_one` / `run_verifier` behaviour is byte-for-byte unchanged.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
