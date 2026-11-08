# RRL-3: `mini_ork/rsi/benchmarks.py` — a benchmark registry and the `verifier-replay` benchmark

## Goal

The research-driven RSI loop's impact gate (G12, RRL-4) needs to score a git tree on
a fixed labelled item set, per item, for $0. `scripts/verifier_replay.py` already
does the expensive part for the code-fix test verifier: for each stored held-out
patch it checks out the task's base commit into a scratch worktree, applies the patch,
runs the verifier, then grades the tree with the hidden tests. This kickoff wraps that
as a registered benchmark that:

- scores ANY tree's verifier (the tree under test — base or candidate — not the
  installed `mini_ork`),
- caches the hidden-test ground truth once (it does not depend on the verifier),
- caches a clean tree's results by tree content,
- runs items in parallel.

Nothing calls it yet (RRL-4 wires it). All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `scripts/verifier_replay.py` — three backwards-compatible additions

Existing behaviour with default arguments must stay byte-for-byte the same; the
existing `tests/unit/test_verifier_replay_py.py` must pass unchanged.

**a. `BOOTSTRAP`** — a new module constant:

```python
BOOTSTRAP = ("import runpy, sys; root, script = sys.argv[1], sys.argv[2]; "
             "sys.path[0:0] = [root]; sys.argv = [script]; "
             "runpy.run_path(script, run_name='__main__')")
```

**b. `run_verifier(verifier, scratch, python, timeout, *, import_root=None)`** — new
keyword-only `import_root: Path | None = None`. When it is `None` the command is the
existing `[python, str(verifier)]`. Otherwise the command is
`[python, "-c", BOOTSTRAP, str(import_root), str(verifier)]`, so the verifier's own
`import mini_ork…` resolves to `import_root` while the scratch tree's test
subprocesses (which inherit the environment, not `sys.path`) are untouched. The
environment is built exactly as today in both cases (do NOT add `PYTHONPATH`).

**c. `replay_one(source, task, *, repo, verifier, python, timeout, import_root=None, hidden=True)`**
— pass `import_root` through to `run_verifier`. When `hidden` is `False`, skip the
hidden-test grading: the row then has no `hidden_pass` / `failed_ids` keys.

**d. `grade_one(source, task, *, repo, python, timeout) -> dict`** — new. The same
scratch flow as `replay_one` (scratch dir under `repo.parent / f"{repo.name}-replay-scratch"`,
`git worktree add -q --detach <scratch> <base_sha>`, `git apply --whitespace=nowarn`,
`worktree remove --force` in `finally`) but it runs NO verifier: on a clean apply it
returns `{"run": source.name, "task": task["id"], "hidden_pass": graded["passed"],
"failed_ids": graded["failed_ids"]}` from `rh.grade(scratch, task, python, timeout)`;
on a failed apply `{"run": ..., "task": ..., "skipped": "patch does not apply to base: …"}`.

### 2. `mini_ork/rsi/benchmarks.py` (new)

Standard library plus a lazy import of `scripts/verifier_replay.py` (see below). No DB,
network or model access.

**Dataclasses** (all `frozen=True, slots=True`):

- `BenchContext`: `home: Path`, `repo: Path`, `workers: int = 4`,
  `timeout_s: int = 900`, `python: str = sys.executable`,
  `options: Mapping[str, str] = field(default_factory=dict)`.
- `BenchResult`: `benchmark: str`, `tree: str | None`,
  `scores: Mapping[str, float]`, `strata: Mapping[str, str]`,
  `guards: Mapping[str, float]`, `skipped: tuple[str, ...] = ()`,
  `meta: Mapping[str, Any] = field(default_factory=dict)`. Methods:
  `to_dict()` (plain JSON-able dict, `skipped` as a list) and classmethod
  `from_dict(d)` (inverse). `subset(items) -> BenchResult`: same result restricted to
  the given item ids (scores, strata; `guards` and `meta` copied as-is).
- `Benchmark`: `name: str`,
  `runner: Callable[[Path, tuple[str, ...] | None, BenchContext], BenchResult]`,
  `items: Callable[[BenchContext], Mapping[str, str]]` (item id → stratum),
  `surface: tuple[str, ...] = ()`, `lower_is_better: tuple[str, ...] = ()`.

**Registry**

- `register_benchmark(bench: Benchmark, *, replace: bool = False) -> None` — a
  duplicate name without `replace=True` raises `ValueError`.
- `get_benchmark(name) -> Benchmark` — unknown raises `KeyError(f"unknown benchmark {name!r}")`.
- `benchmark_names() -> tuple[str, ...]` — sorted.

**`tree_key(root: Path) -> str | None`** — `None` when `git -C root rev-parse HEAD`
fails. Otherwise the sha256 hex of: the HEAD sha, `"\n"`, the stdout of
`git -C root diff HEAD --binary`, `"\n"`, then for each path in
`sorted(git -C root ls-files --others --exclude-standard)` the line
`f"{path}:{sha256(file bytes)}\n"` (a path that cannot be read contributes
`f"{path}:unreadable\n"`). Every git call uses `subprocess.run(..., capture_output=True, text=False, check=False)`.

**Cache** — `_cache_path(ctx, name, key) = ctx.home / "rsi" / "bench" / name / f"{key}.json"`.

**`run_benchmark(name, root, *, ctx, items=None, use_cache=True) -> BenchResult`**

- `bench = get_benchmark(name)`; `wanted = tuple(sorted(set(items)))` when `items` is
  given, else `None`; `key = tree_key(Path(root))`.
- Cache hit: `use_cache` and `key` is not `None` and the cache file exists, parses,
  and its `scores ∪ skipped` covers every wanted id (or `wanted is None` and the cached
  `meta["complete"]` is true) → return `BenchResult.from_dict(cached)` restricted with
  `subset(wanted)` when `wanted` is given.
- Otherwise `result = bench.runner(Path(root), wanted, ctx)`. When `key` is not
  `None`, merge into the cache file: union the cached and new `scores`, `strata` and
  `skipped`; the new `guards` and `meta` win, except `meta["complete"]` is
  `cached_complete or wanted is None`. Write atomically (`tmp` file in the same dir +
  `os.replace`), creating parent dirs. Return `result`.

### 3. The built-in `verifier-replay` benchmark (in the same module)

**Constants**

- `VERIFIER_REPLAY = "verifier-replay"`
- `DEFAULT_PATCHES = "evals/heldout/results/2026-09-29-baseline-dev.patches"`
- `DEFAULT_MANIFEST = "evals/heldout/mined/manifest.json"`
- `DEFAULT_SEED = "evals/heldout/results/2026-09-30-replay-testverifier.jsonl"`
- `DEFAULT_VERIFIER = "recipes/code-fix/verifiers/test.py"`
- `VR_SURFACE = ("recipes/code-fix/verifiers", "mini_ork/certify", "mini_ork/verify", "mini_ork/gates/suite_adequacy.py", "mini_ork/gates/suite_strengthen.py")`
- `VR_GUARDS = ("false_accept", "false_reject", "no_verdict")`

**Loading `scripts/verifier_replay.py`** — `_replay_module(repo: Path)`: import it by
file path with `importlib.util.spec_from_file_location("mini_ork_verifier_replay", repo / "scripts" / "verifier_replay.py")`,
cached per resolved path. (It imports `run_heldout` via its own `sys.path` insert.)

**Option resolution** — `ctx.options` keys `patches_dir`, `manifest`, `seed_labels`;
each defaults to the constant above, and a relative value is resolved against `ctx.repo`.

**`_tasks_and_patches(ctx) -> dict[str, tuple[Path, dict]]`** — item id (the patch
stem, which is the task id) → `(patch path, task)`, from the module's
`stored_patches(patches_dir, {t["id"]: t for t in manifest})`.

**Labels** — `labels_path(ctx) = ctx.home / "rsi" / "bench" / VERIFIER_REPLAY / "labels.json"`,
a JSON object `{item_id: {"patch_sha256": str, "hidden_pass": bool}}`.

`ensure_labels(ctx, ids) -> dict[str, bool]`:
1. Load the file (missing/unparseable → `{}`). A label counts only when its
   `patch_sha256` equals the sha256 of the current patch bytes.
2. For ids still unlabelled, seed from the `seed_labels` JSONL when that file exists:
   a row with `"run" == f"{id}.patch"` and a boolean `hidden_pass` labels `id` (record
   the current patch sha).
3. For ids still unlabelled, call the module's `grade_one(patch, task, repo=ctx.repo,
   python=ctx.python, timeout=ctx.timeout_s)` in a `ThreadPoolExecutor(ctx.workers)`;
   a row with a boolean `hidden_pass` labels the id; anything else leaves it unlabelled.
4. Write the merged file atomically when anything changed. Return `{id: hidden_pass}`
   for the labelled ids among `ids`.

**`_vr_items(ctx) -> dict[str, str]`** — for every id in `_tasks_and_patches(ctx)`
with a label from `ensure_labels`: `"hard_negative"` when `hidden_pass` is `False`,
else `"correct"`.

**`_vr_run(root, items, ctx) -> BenchResult`**

- `verifier = root / DEFAULT_VERIFIER`; missing → `FileNotFoundError`.
- `pairs = _tasks_and_patches(ctx)` restricted to `items` when given (an unknown id goes
  to `skipped`); `labels = ensure_labels(ctx, ids)`.
- In a `ThreadPoolExecutor(ctx.workers)` call the module's
  `replay_one(patch, task, repo=ctx.repo, verifier=verifier, python=ctx.python,
  timeout=ctx.timeout_s, import_root=root, hidden=False)`. An exception from one item
  is retried once; a second exception puts the id in `skipped`.
- Per item: a `skipped` row or no label → `skipped`. `verifier_pass is None` → score
  `0.0` and `no_verdict += 1`. Otherwise score `1.0` when `verifier_pass == label`
  else `0.0`; `verifier_pass and not label` → `false_accept += 1`;
  `not verifier_pass and label` → `false_reject += 1`.
- `strata[id]` = `"hard_negative"` / `"correct"` from the label.
- `guards = {"false_accept": float(fa), "false_reject": float(fr), "no_verdict": float(nv)}`.
- `meta = {"verifier": DEFAULT_VERIFIER, "n": len(scores),
  "accuracy": round(mean(scores), 6) if scores else None,
  "reasons": {id: (row.get("verifier_reason") or "")[:200]}}`.
- `tree = tree_key(root)`.

Register at import time:
`register_benchmark(Benchmark(VERIFIER_REPLAY, _vr_run, _vr_items, VR_SURFACE, VR_GUARDS))`.

### 4. A small CLI (`python -m mini_ork.rsi.benchmarks`)

`main(argv=None) -> int` with subcommands:

- `list` → prints each registered name and its surface, one per line; returns 0.
- `run <name> --root <path> [--home <path>] [--repo <path>] [--workers N] [--limit N] [--no-cache] [--out <file>]`
  — `home` defaults to `$MINI_ORK_HOME` else `<root>/.mini-ork`; `repo` defaults to
  `--root`. `--limit` keeps the first N sorted ids from `items(ctx)`. Prints a JSON
  summary `{"benchmark", "tree", "n", "accuracy", "guards", "skipped"}`; with
  `--out`, also writes the full `to_dict()` there. Returns 0, or 2 on an unknown
  benchmark (message on stderr).

`if __name__ == "__main__": sys.exit(main())`.

## Files in scope (touch ONLY these)

- `mini_ork/rsi/benchmarks.py` (new)
- `scripts/verifier_replay.py`
- `tests/unit/test_rsi_benchmarks_py.py` (new)

Do NOT modify any other file, including `scripts/run_heldout.py`,
`recipes/code-fix/verifiers/test.py` and anything under `evals/`.

## Tests (`tests/unit/test_rsi_benchmarks_py.py`: hermetic, `tmp_path`, no network, no real replay)

- **Registry:** `"verifier-replay" in benchmark_names()`; its `surface == VR_SURFACE`;
  registering a duplicate name raises; `replace=True` works (restore the original in a
  `finally`); `get_benchmark("nope")` raises `KeyError`.
- **tree_key:** in a fresh `git init` repo with one commit: stable across two calls;
  changes after editing a tracked file; changes after adding an untracked file;
  `None` for a non-git dir.
- **run_benchmark caching** with a fake registered benchmark (`replace=True`, cleaned
  up in `finally`) whose runner counts calls and returns scores `{a:1, b:0}`:
  - first call on a clean git tree runs the runner and writes
    `home/rsi/bench/<name>/<key>.json`; a second call with `items=["a"]` returns
    `{a: 1.0}` without calling the runner;
  - `use_cache=False` calls it again; a non-git root never writes a cache file;
  - asking for an id the cache lacks calls the runner and the cache then holds the
    union.
- **BenchResult round trip:** `from_dict(to_dict(r)) == r`; `subset(["a"])` keeps only `a`.
- **BOOTSTRAP / import_root:** write `tmp/root/mini_ork_probe_mod.py` containing
  `WHERE = "root"`, and a script `tmp/v.py` that does
  `import mini_ork_probe_mod, json; print(json.dumps({"pass": mini_ork_probe_mod.WHERE == "root", "reason": "ok"}))`;
  `verifier_replay.run_verifier(Path("tmp/v.py"), scratch=tmp, python=sys.executable, timeout=60, import_root=tmp/"root")`
  returns `(True, "ok")`; without `import_root` it returns `(None, …)` (the import
  fails, no verdict).
- **ensure_labels and _vr_run** with a fake replay module (monkeypatch
  `benchmarks._replay_module` to return a `SimpleNamespace` with `stored_patches`,
  `replay_one`, `grade_one`):
  - three patch files `t1.patch`, `t2.patch`, `t3.patch`; a seed JSONL labels `t1`
    True and `t2` False; `grade_one` labels `t3` True and is called once, only for
    `t3`; the labels file is written; a second `ensure_labels` calls nothing;
  - editing `t1.patch` invalidates its label (it is re-graded);
  - `replay_one` returning `verifier_pass=True` for all → `scores == {t1: 1, t2: 0, t3: 1}`,
    `guards == {false_accept: 1, false_reject: 0, no_verdict: 0}`,
    `strata[t2] == "hard_negative"`; it was called with `import_root=<root>` and
    `hidden=False`;
  - `verifier_pass=None` for one item → score 0 and `no_verdict == 1`;
  - a `replay_one` that raises twice for `t3` → `"t3" in skipped`; raising once then
    succeeding → scored;
  - `_vr_items` returns the strata mapping;
  - a root without `recipes/code-fix/verifiers/test.py` raises `FileNotFoundError`.
- **CLI:** `main(["list"])` returns 0 and prints `verifier-replay`; `main(["run",
  "nope", "--root", str(tmp_path)])` returns 2.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_rsi_benchmarks_py.py tests/unit/test_verifier_replay_py.py`
- `ruff check --select F,E9 mini_ork/rsi/benchmarks.py scripts/verifier_replay.py tests/unit/test_rsi_benchmarks_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_rsi_benchmarks_py.py --junitxml="${MINI_ORK_RUN_DIR}/rrl-3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rrl-3-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `python -m mini_ork.rsi.benchmarks list` prints `verifier-replay`.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
