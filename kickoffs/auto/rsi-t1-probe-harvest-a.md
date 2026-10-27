# RSI Tier-1 #3a: a git-oracle probe kind for the probe scorer (default off)

## Goal

The apply gate scores a candidate on the frozen probe set in
`recipes/<recipe>/probes/*.md`. That set is too small to support a decision,
and the probe outcome is too weak to grow it:

- **The sets are tiny.** code-fix and framework-edit each ship 3 probes: 2 gate
  probes and the anchor `probe-3`. obs-smoke ships 2. `MO_APPLY_PROBE_MAX_TASKS`
  defaults to 2, so every measured decision has n ≤ 2. Only 2 of the 43
  `apply_attempts` rows were probe-scored, and both had n=2.
- **The outcome is publish status.** `_run_outcome` returns 1.0 when
  `task_runs.status == 'published'`. The 2026-09-29 held-out replay showed the
  code-fix test verifier false-accepting a wrong fix, so "published" alone is
  a weak oracle.
- **The failure history has no grader.** The 519 `pre-impl-fixture/` captures
  hold only the pre-edit bytes of the files that changed. None of them holds a
  test. For 81 of the 95 failed code-fix and framework-edit captures, the target
  repo no longer exists. But the base commit of 86 of the 95 is still reachable
  in this repo (763 `refs/mo/pre-impl/*` refs are pinned). Every mined held-out
  task in `evals/heldout/mined/manifest.json` (92 tasks) is a
  `(base_sha, fix_sha, test_files)` triple in this repo.

This kickoff adds a second kind of probe: the **git-oracle probe**.

- **Its target** is a history-free snapshot of `base_sha` taken from this repo.
- **Its outcome** is 1.0 only when the run published AND the hidden tests from
  `fix_sha` pass on the result.

Kickoffs -b and -c write probes of this kind. Nothing writes them yet, so
merging this kickoff changes no decision.

**Default off.**
- The new behaviour sits behind `MO_APPLY_PROBE_GIT_ORACLE=1`.
- With the flag unset, `_load_probes` skips every git-oracle probe, and
  `probe_score` and `probe_score_code` behave exactly as they do today.
- A probe with `holdout: true` in its frontmatter is never loaded by the apply
  loop, whether or not the flag is set.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/probe_scorer.py`

- **New module constants.**
  - `GIT_ORACLE_FLAG = "MO_APPLY_PROBE_GIT_ORACLE"`
  - `ORACLE_DIR = "oracles"`
  - `_SHA_RE = re.compile(r"^[0-9a-f]{40}$")`
  - `_PYTEST_FILE_RE = re.compile(r"(^|/)tests/(.*/)?test_[^/]*\.py$")`. This
    is the same pattern as `PYTEST_FILE` in `scripts/mine_heldout_tasks.py`.
- **The sidecar format.** A git-oracle probe is a probe `.md` whose YAML
  frontmatter has `oracle: git`. Beside it sits a sidecar file
  `probes/oracles/<stem>.json`, where `<stem>` is the probe's file name without
  `.md`. The sidecar is a JSON object with these keys:
  - `base_sha`: 40 lowercase hex characters;
  - `fix_sha`: 40 lowercase hex characters;
  - `test_files`: a non-empty list of repo-relative paths, each matching
    `_PYTEST_FILE_RE`;
  - `fail_to_pass`: a non-empty list of strings of the form
    `<junit classname>::<name>`;
  - `pass_to_pass`: a list of strings in the same form. It may be empty.

  Other keys (`schema`, `provenance`) are allowed and ignored. The oracle data
  lives ONLY in the sidecar, never in the `.md`, because the `.md` is the
  kickoff the solver reads.
- **New function `_probe_oracle(probe_path: str) -> dict | None`.**
  - It returns `None` when
    `_parse_probe_frontmatter(probe_path).get("oracle") != "git"`.
  - Otherwise it reads
    `os.path.join(os.path.dirname(probe_path), ORACLE_DIR, <stem> + ".json")`.
  - It returns `None` when that file is missing or unreadable, is not JSON or
    not a JSON object, or has any field that breaks its rule above.
  - Otherwise it returns the parsed dict.
- **`_load_probes`.** Read the frontmatter once per probe file (`fm`) and apply
  these rules in order:
  1. When `bool(fm.get("holdout"))` is true, skip the probe. Holdout probes are
     never scored by apply.
  2. When `fm.get("oracle") == "git"`, include the probe only when both hold:
     `os.environ.get(GIT_ORACLE_FLAG, "") == "1"`, and
     `_probe_oracle(path) is not None`. Otherwise skip it.
  3. When `fm.get("oracle")` is any other non-empty value, skip the probe.
  4. Everything else is unchanged: `is_anchor = bool(fm.get("anchor"))`, the
     cap on gate probes, anchors always kept, and the return value
     `gate + anchors`.
- **New function `probe_scratch_parent(root: str | None = None) -> str`.**
  - Let `r = os.path.abspath(root or _root())`.
  - It returns `os.environ["MO_APPLY_PROBE_SCRATCH_DIR"]` when that variable is
    set and non-empty.
  - Otherwise it returns
    `os.path.join(os.path.dirname(r), os.path.basename(r) + "-probe-scratch")`.
  - Either way it creates the directory with `os.makedirs(..., exist_ok=True)`.
  - The scratch lives beside the repo because colima's docker mount cannot
    see `$TMPDIR` (the same rule as `scripts/run_heldout.py:_run_task`).
- **New function `materialize_git_tree(sha: str, *, root: str | None = None) -> str`.**
  1. Let `r = root or _root()`, and
     `dst = tempfile.mkdtemp(prefix="mo-probe-git-", dir=probe_scratch_parent(r))`.
  2. Run `subprocess.run(["git", "-C", r, "archive", "--format=tar", sha], capture_output=True)`.
     On a non-zero return code, call `shutil.rmtree(dst, ignore_errors=True)`
     and raise `RuntimeError(f"git archive {sha!r} failed: ...")`, ending the
     message with the last 300 characters of stderr.
  3. Extract the tar bytes into `dst` with `tarfile`, using `filter="data"`.
  4. Call `_make_target_repo(dst)` (the existing helper). The target becomes a
     one-commit repo with no history, so no object from `fix_sha` (or from any
     later commit) is reachable from it.
  5. Return `dst`.
- **`_materialize_target(probe_path)`.** Its first statement becomes
  `oracle = _probe_oracle(probe_path)`. When that is not `None`, it returns
  `materialize_git_tree(oracle["base_sha"])`. Otherwise it is unchanged.
- **New function `run_pytest_junit(target: str, test_files: list[str], *, timeout: float) -> dict[str, str] | None`.**
  1. Let `tmp = tempfile.mkdtemp(prefix="mo-probe-grade-")`; the report path is
     `<tmp>/junit.xml`.
  2. Build the environment from `os.environ`, with `PYTHONSAFEPATH` removed and
     these values set:
     - `MINI_ORK_ROOT=<target>`
     - `MINI_ORK_HOME=<tmp>/home`
     - `MINI_ORK_DB=<tmp>/home/state.db`
     - `PYTHONDONTWRITEBYTECODE=1`
  3. Run
     `[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *test_files]`
     with `cwd=target`, `capture_output=True` and `timeout=timeout`.
  4. Return `None` on `subprocess.TimeoutExpired`, or when the report file does
     not exist.
  5. Otherwise return `{f"{classname}::{name}": outcome}` for every `testcase`
     element. The outcome is:
     - `"failed"` when the element has a `failure` child;
     - `"error"` when it has an `error` child;
     - `"skipped"` when it has a `skipped` child;
     - `"passed"` otherwise.

     This is the same id format as `scripts/mine_heldout_tasks.py:_run_pytest`.
  6. Always remove `tmp`.
- **New function `_grade_git_oracle(target: str, oracle: dict, *, timeout: float | None = None) -> bool`.**
  1. When `timeout` is `None`, use
     `float(os.environ.get("MO_APPLY_PROBE_GRADE_TIMEOUT_S", "600"))`.
  2. For each path in `oracle["test_files"]`, run
     `git -C _root() show <fix_sha>:<path>` and read the bytes.
     - On a non-zero return code, return `False`.
     - Otherwise write the bytes to `os.path.join(target, path)`, creating
       parent directories and overwriting any existing file. This overlay
       throws away whatever the solver wrote at that path.
  3. Let `results = run_pytest_junit(target, oracle["test_files"], timeout=timeout)`.
     When it is `None`, return `False`.
  4. Return
     `all(results.get(t) == "passed" for t in set(oracle["fail_to_pass"]) | set(oracle["pass_to_pass"]))`.
- **New function `_probe_outcome(probe_path: str, run_id: str, target: str | None, db: str | None = None) -> tuple[float, bool | None]`.**
  1. Let `published = _run_outcome(run_id) if db is None else _run_outcome(run_id, db=db)`.
     When `db is None` it must pass exactly one positional argument, because
     `tests/unit/test_cli_apply_py.py:658` monkeypatches `_run_outcome` with a
     one-argument fake.
  2. Let `oracle = _probe_oracle(probe_path)`. When it is `None`, return
     `(published, None)`.
  3. For a git oracle: when `published < 1.0` or `target` is falsy, return
     `(0.0, False)` WITHOUT calling `_grade_git_oracle`.
  4. Otherwise let `hidden = _grade_git_oracle(target, oracle)` and return
     `(1.0 if hidden else 0.0, hidden)`.
- **The four call sites.** `probe_score` and `probe_score_code` each have two
  `outcome = _run_outcome(...)` lines, one for the baseline arm and one for the
  candidate arm. Replace each with
  `outcome, hidden = _probe_outcome(probe, run_id, target, db=<what that line passes today, or None>)`.
  In the `runs.append({...})` dict that follows each one, add
  `"hidden_pass": hidden` ONLY when `hidden is not None`. Non-git probes keep
  exactly today's dict.
- **Nothing else in this file changes.** That includes the budget pre-flight,
  `control_n`, the anchor split, `_materialize_arm`, `_launch_run` and the
  cleanup. The existing `shutil.rmtree` of every temp target already removes a
  git scratch, because it is a plain directory and not a registered worktree.

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_scorer.py`
- `tests/unit/test_probe_scorer_git_oracle.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_probe_scorer_git_oracle.py`: hermetic temp git repo, no network, no real lane)

**Seams.**
- Point the scorer at the temp repo with
  `monkeypatch.setattr(ps, "_ROOT", str(root))`, as
  `tests/unit/test_probe_config.py:61` does.
- Stub `_launch_run` and `_run_outcome` the way the `scripted_arm` fixture in
  `tests/unit/test_probe_scorer_control_arm.py` does. The fake launch has the
  signature `(recipe, kickoff, target_cwd=None, root=None)` and returns
  `(stdout, run_id, cost)`.
- Call `monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)`.
- Call `monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))`
  in every test except the default-parent test.

**The fixture** `git_root(tmp_path)` returns `(root, base_sha, fix_sha)`:

- `root = tmp_path / "fw"`. Run `git init` and set `user.email` and `user.name`.
- **Commit 1, "base".** `calc.py` is `def add(a, b):\n    return a - b\n`.
  `base_sha` is `git rev-parse HEAD`.
- **Commit 2, "fix".** `calc.py` is `def add(a, b):\n    return a + b\n`, and
  `tests/test_calc.py` is:

  ```
  import os, sys
  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  from calc import add

  def test_add():
      assert add(2, 3) == 5

  def test_add_zero():
      assert add(0, 0) == 0
  ```

  `fix_sha` is `git rev-parse HEAD`.
- **Uncommitted working-tree files.** `recipes/code-fix/prompts/implementer.md`
  is `base\n`. `recipes/code-fix/probes/` holds:
  - `probe-1.md`: `# plain probe\n`
  - `probe-h01.md`: `---\noracle: git\n---\n# git probe\n`
  - `probe-h02.md`: `---\noracle: git\nholdout: true\n---\n# held out\n`
  - `oracles/probe-h01.json` and `oracles/probe-h02.json`, both:
    `{"schema": 1, "base_sha": <base_sha>, "fix_sha": <fix_sha>, "test_files": ["tests/test_calc.py"], "fail_to_pass": ["tests.test_calc::test_add"], "pass_to_pass": ["tests.test_calc::test_add_zero"]}`

**Tests.**

- **The flag off skips git and holdout probes.** The basenames from
  `ps._load_probes("code-fix")` are `["probe-1.md"]`.
- **The flag on loads the git probe but never the holdout.** With
  `MO_APPLY_PROBE_GIT_ORACLE=1` and `MO_APPLY_PROBE_MAX_TASKS=5`, the basenames
  are `["probe-1.md", "probe-h01.md"]`, and both have the anchor flag `False`.
- **An invalid sidecar is skipped.** With the flag on, each of these gives
  basenames `["probe-1.md"]`:
  - `oracles/probe-h01.json` rewritten with `fix_sha` `"abc"`;
  - the sidecar file deleted;
  - `test_files` set to `["calc.py"]`.
- **`_probe_oracle`.** For `probe-h01.md` it returns a dict whose `base_sha` is
  the fixture's `base_sha`. For `probe-1.md` it returns `None`.
- **The snapshot has no history.** With `t = ps.materialize_git_tree(base_sha)`:
  - `os.path.dirname(t) == str(tmp_path / "scratch")`;
  - `calc.py` contains `a - b`;
  - `tests/test_calc.py` does not exist;
  - `git -C t rev-list --count HEAD` prints `1`;
  - `git -C t cat-file -e <fix_sha>` exits non-zero.
- **The default scratch parent.** With `MO_APPLY_PROBE_SCRATCH_DIR` unset,
  `ps.probe_scratch_parent()` returns `str(tmp_path / "fw-probe-scratch")`, and
  that directory exists.
- **An unknown sha fails loud and leaves nothing behind.**
  `ps.materialize_git_tree("0" * 40)` raises `RuntimeError`, and afterwards
  `tmp_path / "scratch"` has no entries.
- **Grading.** On the snapshot `t`:
  - `ps._grade_git_oracle(t, oracle)` returns `False`;
  - afterwards `t/tests/test_calc.py` matches the fix commit's content;
  - after the fixed `calc.py` is written into `t`, `_grade_git_oracle` returns
    `True`.
- **The overlay beats a tampered test.** In a fresh snapshot, keep the buggy
  `calc.py` and write `tests/test_calc.py` as
  `def test_add():\n    assert True\n`. `_grade_git_oracle` returns `False`.
- **`run_pytest_junit` ids.** After the overlay on a fixed tree it returns
  exactly
  `{"tests.test_calc::test_add": "passed", "tests.test_calc::test_add_zero": "passed"}`.
- **`_probe_outcome` contracts.**
  - `probe-1.md` with `db=None`, and `_run_outcome` patched to
    `lambda run_id: 1.0` (one parameter): returns `(1.0, None)`.
  - `probe-h01.md` with `_run_outcome` returning `0.0` and `_grade_git_oracle`
    patched to raise `AssertionError`: returns `(0.0, False)`, so the grader was
    not called.
  - `probe-h01.md` with `_run_outcome` returning `1.0`: when
    `_grade_git_oracle` returns `True` the result is `(1.0, True)`; when it
    returns `False` the result is `(0.0, False)`.
  - With `db="x.db"`, the `_run_outcome` fake receives `db="x.db"`.
- **End to end through `probe_score`.**
  - Set the flag on, `MO_APPLY_PROBE_MAX_TASKS=5`, `MO_APPLY_PROBE_CONTROL_N=1`
    and `MO_APPLY_PROBE_BUDGET_USD=100`.
  - The fake `_launch_run`: when `"__probe_" in recipe` and
    `recipe.endswith("_1")` (the candidate arm, which is `_materialize_arm`
    index 1) and `target_cwd` is not `None`, it writes the fixed `calc.py` into
    `target_cwd`. Every launch returns a fresh run id and cost 0.0.
  - The `_run_outcome` fake returns 1.0.
  - Call `res = ps.probe_score("code-fix", "prompts/implementer.md", "a directive")`.
    Then:
    - `json.loads(res["pertask_json"])` equals
      `{"before": [1, 0], "after": [1, 1], "ids": ["probe-1.md", "probe-h01.md"]}`;
    - the `runs` entries for `probe-h01.md` carry `hidden_pass`: `False` for the
      baseline arm and `True` for the candidate arm;
    - no `probe-1.md` entry has a `hidden_pass` key;
    - afterwards `tmp_path / "scratch"` has no entries.
- **The flag off, end to end.** The same setup without the flag gives ids
  `["probe-1.md"]`, and no run entry has a `hidden_pass` key.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_probe_scorer_git_oracle.py`
- `pytest -q -p no:asyncio tests/unit/test_probe_scorer_control_arm.py tests/unit/test_probe_scorer_code_arm.py tests/unit/test_probe_config.py`
- `pytest -q -p no:asyncio tests/unit/test_cli_apply_py.py tests/unit/test_apply_harness_targets.py tests/unit/test_collapse_history_writer.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_probe_scorer_git_oracle.py --junitxml="${MINI_ORK_RUN_DIR}/git-oracle-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/git-oracle-junit.xml`

## Success criteria

- All three verification commands pass, and every pre-existing test in them is
  unchanged. The pre-existing tests that must stay green are:
  - `test_probe_scorer_control_arm.py`
  - `test_probe_scorer_code_arm.py`
  - `test_probe_config.py`
  - `test_cli_apply_py.py`
  - `test_apply_harness_targets.py`
  - `test_collapse_history_writer.py`
- With `MO_APPLY_PROBE_GIT_ORACLE` unset, `probe_score` and `probe_score_code`
  return the same results as before for any probe directory without `oracle:`
  or `holdout:` frontmatter.
- `ruff check --select F,E9 mini_ork/learning/probe_scorer.py tests/unit/test_probe_scorer_git_oracle.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
