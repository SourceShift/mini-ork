# RSI Tier-1 #3b: harvest frozen code-fix probes from mini-ork's own mined failures

## Goal

Prerequisite: `rsi-t1-probe-harvest-a` is merged. It provides the git-oracle
probe kind, the `probes/oracles/<stem>.json` sidecar, and the
`MO_APPLY_PROBE_GIT_ORACLE` flag.

The code-fix probe set has 2 gate probes. The apply gate therefore decides on
n ≤ 2, where the best possible McNemar p is 0.25 (TOP-20 #13: "any accept
decision taken on n=2 is theater"). The data to grow it is already in the
repo:

- **The mined tasks.** `evals/heldout/mined/manifest.json` holds 92 tasks mined
  from mini-ork's own `fix:` commits: dev 40, test 52, 24 `weak_signal`. Each
  task fails on its base and passes on its fix, with hidden tests from
  `fix_sha`.
- **The baseline grades.** `evals/heldout/results/2026-09-29-baseline-dev.json`
  grades the code-fix recipe on all 40 dev tasks against those hidden tests:
  22 solved and 18 failed.
- **The failed runs.** state.db holds 37 failed `heldout-mo-*` code-fix runs
  across 26 dev tasks. 32 of the 47 failed code-fix runs that carry a
  `pre-impl-fixture` capture are these runs. The rule diagnoser
  (`diagnose(home, run_id, write=False)`) clusters the 39 failed code-fix
  captures with a reachable base as `change/R5/test` 20, `change/R4/verdict` 18
  and `change/R1b/publisher` 1.

A simulation of the selection rules below over that data gives this:

- **Kept as candidates:** 30 (52 are rejected as test split and 10 as weak
  signal).
- **Holdout:** 7 of the 30 fall in the salted holdout bucket.
- **Admitted as gate probes:** 12 of the remaining 23. That is 7 baseline-failed
  probes (`change/R4/verdict` 5, `unmeasured-failure` 2) and 5 baseline-solved
  probes.
- **Gate total:** 14, counting the 2 existing gate probes. This is before the
  git re-validation that kickoff -c adds.

**Default off.** This kickoff writes files only when asked (`write=True` or
`--write`). Every probe it writes is a git-oracle probe, which the apply loop
ignores unless `MO_APPLY_PROBE_GIT_ORACLE=1`. No promotion or apply decision
changes.

**The hold-out and anchor discipline.**
- A task from the mined `test` split is never harvested; that split is the
  held-out set.
- A salted hash puts 25% of harvested candidates in a holdout bucket. Those are
  written with `holdout: true`, and the apply loop never loads them.
- The harvester never writes `anchor: true`, so the anchors stay the
  hand-written `probe-3` files.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/probe_harvest.py` (new)

- **Module constants (exact names and values).**
  - `HARVESTER = "probe_harvest@1"`
  - `SUPPORTED_RECIPES = ("code-fix",)`
  - `DEFAULT_MAX_NEW = 12`
  - `MAX_PER_CLUSTER = 6`
  - `HOLDOUT_FRACTION = 0.25`
  - `HOLDOUT_SALT = "probe-holdout:"`
  - `PROBE_STEM_PREFIX = "probe-h"`
  - `MAX_STEM_INDEX = 99`
  - `SOLVED = "solved"`
  - `UNMEASURED = "unmeasured-failure"`
  - `DEFAULT_MANIFEST = "evals/heldout/mined/manifest.json"`
  - `DEFAULT_BASELINE = "evals/heldout/results/2026-09-29-baseline-dev.json"`
  - `PYTEST_FILE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")`
- **The candidate dict.** A candidate has exactly these keys: `task_id`,
  `source`, `base_sha`, `fix_sha`, `test_files`, `fail_to_pass`,
  `pass_to_pass`, `src_files`, `problem_statement`, `cluster`,
  `baseline_outcome`, `baseline_source`, `holdout`, `run_ids`.
- **`is_holdout(fix_sha: str) -> bool`.** It returns
  `int(hashlib.sha256((HOLDOUT_SALT + fix_sha).encode()).hexdigest()[:8], 16) / 0x100000000 < HOLDOUT_FRACTION`.
  The salt matters: without it the bucket is the same hash as the mined
  dev/test split, so it selects no dev task at all.
- **`cluster_key(diagnosis: dict | None) -> str`.**
  - `None` gives `UNMEASURED`.
  - Otherwise it gives `f"{L}/{R}/{N}"`, where each part is
    `str(diagnosis.get(k) or "-")` for `k` in `layer`, `rule_id`, `node`.
- **`mined_candidates(manifest: list[dict], baseline: dict[str, dict], diagnoses: dict[str, dict], *, baseline_source: str = DEFAULT_BASELINE) -> tuple[list[dict], list[dict]]`.**
  It walks `manifest` in order. Each task is either rejected with
  `{"task_id": t["id"], "reason": R}` or kept as a candidate. The first rule
  that fires gives the reason:
  1. `"heldout-test-split"` when `t.get("split") != "dev"`;
  2. `"weak-signal"` when `t.get("weak_signal")` is truthy;
  3. `"non-pytest-oracle"` when `test_files` is empty, or any of its entries
     fails `PYTEST_FILE.search`;
  4. `"no-fail-to-pass"` when `fail_to_pass` is empty;
  5. `"no-baseline-measurement"` when `t["id"]` is not in `baseline`, or
     `baseline[t["id"]].get("passed")` is not a `bool`.

  A kept task becomes a candidate with these values:
  - `source`: `"heldout-mined"`;
  - `cluster`: `SOLVED` when the baseline `passed` is true, else
    `cluster_key(diagnoses.get(t["id"]))`;
  - `baseline_outcome`: `1.0` when it passed, else `0.0`;
  - `baseline_source`: the argument;
  - `holdout`: `is_holdout(t["fix_sha"])`;
  - `run_ids`: `list(diagnoses.get(t["id"], {}).get("run_ids", []))`;
  - every other key copied from the manifest task (`task_id` comes from
    `t["id"]`).
- **`select(candidates: list[dict], *, existing_fix_shas: set[str], max_new: int = DEFAULT_MAX_NEW, max_per_cluster: int = MAX_PER_CLUSTER) -> tuple[list[dict], list[dict]]`.**
  - **Dedup.** A candidate is rejected `"duplicate"` when its `fix_sha` is in
    `existing_fix_shas`, or an earlier candidate in the list has the same
    `fix_sha`.
  - **Holdout candidates.** Every holdout candidate that survives dedup is
    admitted. They do not count toward any cap.
  - **Gate candidates.**
    - Group them by `cluster`. The cluster order is all non-`SOLVED` keys in
      sorted order, then `SOLVED`.
    - Within a cluster, sort by `task_id`.
    - Round-robin: pass over the clusters in order, taking each cluster's next
      candidate while that cluster has taken fewer than `max_per_cluster`.
      Stop once `max_new` gate candidates are taken, or when no cluster can
      give one.
    - A gate candidate left over is rejected `"cluster-cap"` when its cluster
      took `max_per_cluster`, and `"max-new"` otherwise.
  - **Return order.** Admitted is the gate candidates in the order taken, then
    the holdout candidates in input order. Rejections are
    `{"task_id", "reason"}`.
- **`existing_fix_shas(probes_dir: str) -> set[str]`.** The `fix_sha` values in
  `<probes_dir>/oracles/*.json`. A file that does not parse is ignored. A
  missing directory gives `set()`.
- **`next_stems(probes_dir: str, count: int) -> list[str]`.**
  - It finds the existing stems among `*.md` files that match
    `^probe-h(\d{2})$`, and starts one past their maximum (at 1 when there are
    none).
  - It returns `count` stems in the form `probe-h%02d`.
  - It raises `ValueError` when the last index would exceed `MAX_STEM_INDEX`.
  - These names sort after `probe-1`, `probe-2` and `probe-3`, so with the
    default cap of 2 the gate keeps scoring today's two probes.
- **`render_probe(candidate: dict, *, recipe: str, stem: str, harvested_at: str) -> tuple[str, dict]`.**
  It returns `(markdown, oracle)`.
  - **The markdown, in this order.**
    1. The frontmatter: `---\noracle: git\n` plus `holdout: true\n` when the
       candidate is holdout, then `---\n`.
    2. `# Frozen probe {stem} — {recipe} harvested task\n\n`.
    3. A fixed paragraph:
       `FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.\n\n`.
    4. `## Problem\n\n{scrubbed problem_statement}\n\n`.
    5. `## Files in scope\n\n` with one `- {path}\n` line per `src_files` entry,
       then `\n`.
    6. `## Definition of Done\n\nFix the behaviour described above so the repository state at HEAD satisfies the spec.\n`.
  - **The scrub.** Replace every occurrence of each of these strings with
    `[hidden]`, longest first: `task_id`, `fix_sha`, `base_sha`, `fix_sha[:10]`,
    `base_sha[:10]`, and each `test_files` path.
  - **What the markdown never contains.** `fix_sha`, `base_sha`, `task_id`, any
    `test_files` path, or any `fail_to_pass` / `pass_to_pass` id.
  - **The oracle dict.**

    `{"schema": 1, "base_sha", "fix_sha", "test_files", "fail_to_pass", "pass_to_pass", "provenance": {"source", "task_id", "run_ids", "cluster", "baseline_outcome", "baseline_source", "recipe", "harvested_at", "harvester": HARVESTER}}`

    Each value comes from the candidate or the arguments.
- **`write_probes(admitted: list[dict], probes_dir: str, *, recipe: str, harvested_at: str) -> list[str]`.**
  1. Let `stems = next_stems(probes_dir, len(admitted))`.
  2. Check EVERY target path first: `<probes_dir>/<stem>.md` and
     `<probes_dir>/oracles/<stem>.json`. If any already exists, raise
     `FileExistsError` and write nothing.
  3. Write each `.md`, and each `.json` with `json.dump(indent=2, sort_keys=True)`
     plus a trailing newline. Create `oracles/` as needed.
  4. Return `stems`.
- **`load_code_fix_inputs(root: str, home: str) -> tuple[list[dict], dict[str, dict], dict[str, dict]]`.**
  - It reads `manifest` (a list) from `<root>/DEFAULT_MANIFEST` and `baseline`
    (a dict) from `<root>/DEFAULT_BASELINE`. A missing file raises
    `FileNotFoundError`.
  - **Diagnoses.** When `<home>/state.db` exists:
    - Open it read-only (`file:<path>?mode=ro`, `uri=True`) and run
      `SELECT id FROM task_runs WHERE id LIKE 'heldout-mo-%' AND status = 'failed' ORDER BY created_at, id`.
    - For each id matching `^heldout-(mo-[0-9a-f]{10})-`, record the run id
      under that task id.
    - `diagnoses[task_id]` is
      `{**_dg.diagnose(Path(home), newest_run_id, write=False), "run_id": newest_run_id, "run_ids": [all of that task's run ids in query order]}`.
    - The import is `from mini_ork.recovery import diagnose as _dg`, done
      inside the function, so a test can monkeypatch
      `mini_ork.recovery.diagnose.diagnose`.
    - It never calls `diagnose` with `write=True`.
  - Without a state.db, `diagnoses` is `{}`.
- **`harvest(recipe: str, *, root: str, home: str, max_new: int = DEFAULT_MAX_NEW, write: bool = False, now: str | None = None, validate_fn=None) -> dict`.**
  1. When `recipe not in SUPPORTED_RECIPES`, raise
     `ValueError(f"unsupported recipe {recipe!r}")`.
  2. Let `probes_dir = <root>/recipes/<recipe>/probes`.
  3. Load the inputs, then build the candidates with `mined_candidates`.
  4. When `validate_fn` is not `None`, call it on each candidate. Its signature
     is `(candidate) -> (ok: bool, reason: str, updated: dict)`.
     - On `ok`, the candidate is replaced by `updated`.
     - Otherwise it is rejected with reason `f"validate:{reason}"`.
     - The library default (`None`) never validates.
  5. Call `select(..., existing_fix_shas=existing_fix_shas(probes_dir), max_new=max_new)`.
  6. When `write` is true, call `write_probes` with
     `harvested_at = now or <UTC now, "%Y-%m-%dT%H:%M:%SZ">`.
  7. Return the report, with exactly these keys:
     - `recipe`
     - `harvester`
     - `n_existing_probes`: the count of `*.md` in `probes_dir` before writing
     - `n_candidates`
     - `admitted`: a list of
       `{"task_id", "cluster", "holdout", "baseline_outcome", "stem"}`, where
       `stem` is `None` when not writing
     - `rejected`: every rejection from every stage, in stage order
     - `n_admitted_gate`
     - `n_admitted_holdout`
     - `diagnoses_available`: a bool
     - `written`: a bool
- **`main(argv: list[str] | None = None) -> int`.**
  - The command line is
    `python -m mini_ork.learning.probe_harvest --recipe R [--max-new N] [--write] [--root P] [--home P] [--report FILE]`.
  - `--root` defaults to `MINI_ORK_ROOT` or the cwd. `--home` defaults to
    `MINI_ORK_HOME` or `<root>/.mini-ork`.
  - It prints the report as one JSON line on stdout. With `--report`, it also
    writes the same JSON to that file.
  - It returns 0.
  - The module ends with an `if __name__ == "__main__": sys.exit(main())` guard.
- **Imports.** The module imports nothing from `mini_ork.cli`. It is a library
  with no callers yet.

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_harvest.py` (new)
- `tests/unit/test_probe_harvest.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_probe_harvest.py`: pure inputs plus temp dirs, no network, no git, no real diagnose)

Use synthetic 40-hex shas, such as `"a" * 40` and `"b" * 40`, and test paths
under `tests/`, such as `tests/unit/test_x.py`.

- **`is_holdout` vectors (exact).**
  - `"a" * 40` gives `True` (bucket 0.15188764198683202).
  - `"0123456789abcdef0123456789abcdef01234567"` gives `False` (0.5020780412014574).
  - `"f487f5014286ff2a7d8c9fa8d129ac45e371720b"` gives `True` (0.15689958119764924).
- **`cluster_key`.**
  - `{"layer": "change", "rule_id": "R4", "node": "verdict"}` gives
    `"change/R4/verdict"`.
  - `{"layer": "provider", "rule_id": "R6", "node": None}` gives
    `"provider/R6/-"`.
  - `None` gives `"unmeasured-failure"`.
- **`mined_candidates` rejection order and fields.** Build a manifest of seven
  tasks, one per case: a test-split task, a weak task, a task with
  `test_files=["src/x.py"]`, a task with an empty `fail_to_pass`, a task missing
  from the baseline, a dev task the baseline failed (with a diagnosis), and a
  dev task the baseline solved.
  - The rejections are exactly the five reasons, in manifest order.
  - The failed task's candidate has the `change/R4/verdict` cluster,
    `baseline_outcome` 0.0, and the diagnosis's `run_ids`.
  - The solved task's candidate has cluster `"solved"` and `baseline_outcome`
    1.0.
  - Both have `source` `"heldout-mined"`, and exactly the 14 candidate keys.
- **`select` round-robin, cap binds.** Seven gate candidates (`holdout` False):
  - cluster `change/R4/verdict` with task ids `t-a1`, `t-a2`, `t-a3`;
  - cluster `change/R5/test` with `t-b1`;
  - cluster `solved` with `t-s1`, `t-s2`, `t-s3`.

  With `max_new=5` and `max_per_cluster=2`:
  - the admitted ids are `["t-a1", "t-b1", "t-s1", "t-a2", "t-s2"]`;
  - `t-a3` and `t-s3` are rejected `"cluster-cap"`.
- **`select` round-robin, max_new binds.** The same candidates with `max_new=5`
  and `max_per_cluster=3` give the same admitted ids, and `t-a3` and `t-s3` are
  rejected `"max-new"`.
- **`select` dedup and holdout.**
  - A candidate whose `fix_sha` is in `existing_fix_shas` is rejected
    `"duplicate"`.
  - A second candidate with the same `fix_sha` as an earlier one is rejected
    `"duplicate"`.
  - With `max_new=1`, two holdout candidates are both admitted, after the one
    gate candidate.
- **`next_stems`.**
  - An empty directory gives `["probe-h01", "probe-h02"]` for 2.
  - A directory holding `probe-1.md`, `probe-h01.md` and `probe-h07.md` gives
    `["probe-h08", "probe-h09"]`.
  - A directory holding `probe-h98.md` gives `["probe-h99"]` for 1, and raises
    `ValueError` for 2.
- **`render_probe`.**
  - The markdown of a gate candidate starts with `"---\noracle: git\n---\n"`.
    The markdown of a holdout candidate starts with
    `"---\noracle: git\nholdout: true\n---\n"`.
  - Give a candidate the `problem_statement` `"see tests/unit/test_x.py and mo-aaaaaaaaaa"`,
    `task_id` `"mo-aaaaaaaaaa"` and `fix_sha` `"a" * 40`. Its markdown:
    - contains `"[hidden]"`;
    - contains none of `"tests/unit/test_x.py"`, `"mo-aaaaaaaaaa"`, `"a" * 40`
      or `"a" * 10`;
    - contains `"## Files in scope"` and one `"- <src>"` line per `src_files`
      entry.
  - The oracle dict equals the expected dict exactly, with
    `provenance.harvester == "probe_harvest@1"`.
- **`write_probes`.**
  - It writes `.md` and `oracles/<stem>.json` files whose JSON round-trips to
    the oracle from `render_probe`.
  - With a pre-existing `oracles/probe-h01.json` (and no `probe-h01.md`), it
    raises `FileExistsError`, and no new file exists afterwards.
- **The written probes load in the scorer (the round trip with kickoff -a).**
  1. Write two gate candidates and one holdout candidate into
     `<tmp root>/recipes/code-fix/probes`, with `recipes/code-fix/` existing.
  2. Set `monkeypatch.setattr(probe_scorer, "_ROOT", str(root))`,
     `MO_APPLY_PROBE_GIT_ORACLE=1` and `MO_APPLY_PROBE_MAX_TASKS=20`.
  3. The basenames of `probe_scorer._load_probes("code-fix")` are exactly the
     two gate stems plus `.md`, in order.
  4. `probe_scorer._probe_oracle(path)["fix_sha"]` equals each candidate's
     `fix_sha`.
- **`harvest` end to end.**
  - **The setup.**
    - Write a 3-task manifest and a baseline under the temp root at the
      `DEFAULT_*` paths: one dev failed, one dev solved, one test split.
    - Give the temp home a `state.db` holding
      `CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, created_at INTEGER)`,
      with two failed rows `heldout-<failed task id>-1` (created_at 1) and
      `heldout-<failed task id>-2` (created_at 2).
    - Monkeypatch `mini_ork.recovery.diagnose.diagnose` with a fake. It
      records its arguments and returns
      `{"layer": "change", "rule_id": "R4", "node": "verdict"}`.
  - **A dry run** (`write=False`):
    - `n_candidates` is 2, and the test-split task is in `rejected`;
    - every `stem` is `None`, `written` is `False`, and no file was created;
    - the fake was called only with `write=False`, for the `-2` run;
    - the failed task's admitted entry has cluster `change/R4/verdict`.
  - **A write** (`write=True`, `now="2026-10-27T00:00:00Z"`) creates the files.
    Each sidecar's `provenance.harvested_at` is `"2026-10-27T00:00:00Z"`, and
    the run ids are `["heldout-<id>-1", "heldout-<id>-2"]`.
  - **A second write** admits nothing, and both candidates are rejected
    `"duplicate"`.
- **An unsupported recipe.** `harvest("obs-smoke", ...)` raises `ValueError`.
- **The `validate_fn` hook.** A fake that returns `(False, "nope", c)` for one
  task gets that task rejected with `"validate:nope"`.
- **The CLI.** `main(["--recipe", "code-fix", "--root", R, "--home", H, "--report", P])`
  returns 0. The JSON in the file equals the JSON line printed on stdout.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_probe_harvest.py`
- `pytest -q -p no:asyncio tests/unit/test_probe_scorer_git_oracle.py tests/unit/test_probe_config.py tests/unit/test_probe_scorer_control_arm.py`
- `pytest -q -p no:asyncio tests/unit/test_mine_heldout_tasks_py.py tests/unit/test_run_heldout_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_probe_harvest.py --junitxml="${MINI_ORK_RUN_DIR}/probe-harvest-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/probe-harvest-junit.xml`

## Success criteria

- All three verification commands pass, and every pre-existing test in them is
  unchanged. The pre-existing tests that must stay green are:
  - `test_probe_scorer_git_oracle.py`, from kickoff -a
  - `test_probe_config.py`
  - `test_probe_scorer_control_arm.py`
  - `test_mine_heldout_tasks_py.py`
  - `test_run_heldout_py.py`
- Nothing is written under `recipes/` by this kickoff. The probe files are
  generated later by the operator step in `rsi-t1-probe-harvest-d`.
- `ruff check --select F,E9 mini_ork/learning/probe_harvest.py tests/unit/test_probe_harvest.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
