# RSI Tier-1 #3c: re-validate harvested probes in git, and harvest framework-edit's own failures

## Goal

Prerequisites: `rsi-t1-probe-harvest-a` and `rsi-t1-probe-harvest-b` are merged.

Two gaps remain after -b.

1. **The mined tasks were validated with an older interpreter.** They were
   checked as fail-on-base, pass-on-fix on 2026-09-29. The engine venv is now
   Python 3.13, and a probe that no longer validates measures nothing. Every
   candidate must be re-checked in git before it is written.
2. **framework-edit has no probe source.** In state.db, framework-edit has 153
   failed runs, 282 published and 80 superseded. 48 of the failed runs carry a
   `pre-impl-fixture` capture, and the base of 47 of those is reachable. The
   rule diagnoser clusters those 47 as:

   | Cluster | Runs |
   |---|---|
   | `change/R4/reviewer` | 14 |
   | `change/R5/test_verifier` | 13 |
   | `provider/R6` | 7 |
   | `unclassified` | 7 |
   | `change/R5/spec-tests` | 2 |
   | `change/R5/static_check_verifier` | 2 |
   | other | 2 |

   A capture holds no test, so it cannot grade anything. But 13 of the 32
   change-layer failures name a test file in their kickoff that later landed on
   `origin/main`, in 13 distinct landing commits. That landing commit is a
   ready-made oracle:
   - base = the landing commit's first parent;
   - fix = the landing commit;
   - hidden tests = the named test files it added.

   The salted holdout puts 6 of the 13 aside, which leaves at most 7 new gate
   probes, before validation. With the 2 existing gate probes, framework-edit
   reaches at most **9 gate probes, not 12**, from its own failure history.

**Default off.** Nothing here changes a promotion or apply decision. The
harvester only writes git-oracle probes, which the apply loop loads only with
`MO_APPLY_PROBE_GIT_ORACLE=1`. The CLI validates by default. The library
function `harvest` still validates only when it is given a `validate_fn`, so
the tests from -b stay valid.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/probe_harvest.py`

- **Constants.**
  - Change `SUPPORTED_RECIPES` to `("code-fix", "framework-edit")`.
  - Add `MAX_SRC_FILES = 12`.
  - Add `DEFAULT_LANDING_REF = "origin/main"`.
  - Add `KICKOFF_TEST_RE = re.compile(r"(tests/(?:[\w-]+/)*test_\w+\.py)")`.
- **New `validate_candidate(candidate: dict, *, root: str, timeout: float = 600.0) -> tuple[bool, str, dict]`.**
  It uses `mini_ork.learning.probe_scorer` (imported inside the function). It
  removes every tree it creates in a `finally` block. The steps run in order,
  and the first failure returns its reason:
  1. **The base tree.** Call
     `base = probe_scorer.materialize_git_tree(candidate["base_sha"], root=root)`.
     A `RuntimeError` gives `(False, "archive-failed", candidate)`.
  2. **The hidden tests.** Overlay each entry of `test_files` from
     `git -C <root> show <fix_sha>:<path>`, writing to `<base>/<path>`. A
     non-zero return code gives `(False, "fix-missing-test", candidate)`.
  3. **The base run.** Call
     `b = probe_scorer.run_pytest_junit(base, test_files, timeout=timeout)`.
     `None` gives `(False, "base-no-report", candidate)`.
  4. **The fix tree.** Call
     `fix = probe_scorer.materialize_git_tree(candidate["fix_sha"], root=root)`.
     A `RuntimeError` gives `(False, "archive-failed", candidate)`.
  5. **The fix run.** Call `g = run_pytest_junit(fix, test_files, timeout=timeout)`.
     `None` gives `(False, "fix-no-report", candidate)`.
  6. **The fix must be green.** Let `broken` be the sorted ids where `g` says
     `"failed"` or `"error"`. When it is non-empty, return
     `(False, f"fix-not-green:{len(broken)}", candidate)`.
  7. **Some test must flip.** Let
     `f2p = sorted(t for t, o in g.items() if o == "passed" and b.get(t) != "passed")`.
     When it is empty, return `(False, "no-fail-to-pass", candidate)`.
  8. **The tests that pass on both.** Let
     `p2p = sorted(t for t, o in g.items() if o == "passed" and b.get(t) == "passed")`.
  9. Return `(True, "", {**candidate, "fail_to_pass": f2p, "pass_to_pass": p2p})`.
- **New `failed_run_candidates(home: str, root: str, *, landing_ref: str = DEFAULT_LANDING_REF) -> tuple[list[dict], list[dict]]`.**
  - **The query.** Open `<home>/state.db` read-only and run
    `SELECT id, kickoff_path, created_at FROM task_runs WHERE recipe = 'framework-edit' AND status = 'failed' ORDER BY created_at, id`.
  - **Rejections.** Rejections are `{"task_id": <run id>, "reason": R}`. The
    rules run in this order, and the first one that fires gives the reason:
    1. `"no-pre-impl-fixture"`: `<home>/runs/<id>/pre-impl-fixture/MANIFEST.json`
       is missing or unparseable.
    2. `"baseline-unreachable"`: the manifest has no `baseline_ref`, or
       `git -C <root> cat-file -e <baseline_ref>^{commit}` fails.
    3. `f"layer:{layer}"`: `_dg.diagnose(Path(home), id, write=False)["layer"] != "change"`.
       The diagnose import is local, as in -b.
    4. `"kickoff-unrecoverable"`: no kickoff text, where the text is:
       - `kickoff_path` read from disk, when it is an existing file;
       - otherwise, when `kickoff_path` matches `(?:^|/)(kickoffs/.*\.md)$`, the
         output of `git -C <root> show <baseline_ref>:<that group>`.
    5. `"no-test-named"`: `KICKOFF_TEST_RE.findall(text)` is empty. Keep the
       matches sorted and unique.
    6. `"test-not-landed"`: no named test landed. A named test `tf` has landed
       when `git cat-file -e <baseline_ref>:<tf>` fails AND
       `git cat-file -e <landing_ref>:<tf>` succeeds. Its add commit is the LAST
       line of `git log --diff-filter=A --format=%H <landing_ref> -- <tf>`
       (the oldest add).
    7. `"no-parent"`: `git rev-parse <fix_sha>^1` fails.
    8. `"landing-too-broad"`: the run has more than `MAX_SRC_FILES` source files.
       These are the entries of `git diff --name-only <base_sha> <fix_sha>` that
       do not match `(^|/)tests/`.
  - **The landing fields.**
    - `fix_sha` is the add commit of the first landed test, in sorted order.
    - `test_files` is the sorted landed tests whose add commit is `fix_sha`.
    - `base_sha` is `git rev-parse <fix_sha>^1`.
    - `src_files` is the sorted list from step 8.
  - **The kickoff text that landed wins.** When the kickoff path is a repo path
    (the `kickoffs/...` group) and `git show <fix_sha>:<group>` succeeds, use
    that version instead of the text from step 4.
  - **The candidate.** Strip any leading YAML frontmatter block from the text.
    The candidate is:
    - `task_id`: `f"fe-{fix_sha[:10]}"`
    - `source`: `"failed-run"`
    - `cluster`: `cluster_key(diagnosis)`
    - `baseline_outcome`: `0.0`
    - `baseline_source`: `"task_runs.status=failed"`
    - `holdout`: `is_holdout(fix_sha)`
    - `run_ids`: `[id]`
    - `problem_statement`: the text
    - `fail_to_pass`: `[]`
    - `pass_to_pass`: `[]`

    Validation fills `fail_to_pass` and `pass_to_pass`.
  - **Merging.** Runs that resolve to the same `fix_sha` merge into ONE
    candidate:
    - `run_ids` is the sorted union of their run ids;
    - `cluster` and `problem_statement` come from the newest run (the last in
      query order).
  - **Order.** Candidates are returned in order of first appearance.
- **`render_probe` for `source == "failed-run"`.**
  - The frontmatter, title and FROZEN paragraph are the same as for mined
    candidates.
  - The body is then the scrubbed kickoff text verbatim, with NO
  `## Problem`, `## Files in scope` or `## Definition of Done` sections added.
  - The scrub replaces only `task_id`, `fix_sha`, `base_sha` and their 10-character
    prefixes. Test paths are NOT scrubbed: a framework-edit kickoff names the
    test file its spec-test author must write, and the grader overlays the
    hidden test on that path anyway.
  - The rendering for `heldout-mined` candidates does not change.
- **`harvest`.**
  - It gains a keyword parameter `landing_ref: str = DEFAULT_LANDING_REF`.
  - For `recipe == "framework-edit"`, the candidates come from
    `failed_run_candidates(home, root, landing_ref=landing_ref)`.
  - `diagnoses_available` is `True` when `<home>/state.db` exists.
  - Everything else is unchanged, including the validation hook, select, write
    and the report keys.
- **`main`.**
  - New flags: `--landing-ref REF` (default `DEFAULT_LANDING_REF`),
    `--no-validate` and `--grade-timeout S` (default 600).
  - Unless `--no-validate` is given, it passes
    `validate_fn=lambda c: validate_candidate(c, root=root, timeout=S)`.

## Files in scope (touch ONLY these)

- `mini_ork/learning/probe_harvest.py`
- `tests/unit/test_probe_harvest_git.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_probe_harvest_git.py`: hermetic temp git repo and temp home, no network, no real diagnose)

**Setup in every test.**
- `monkeypatch.setenv("MO_APPLY_PROBE_SCRATCH_DIR", str(tmp_path / "scratch"))`.
- Monkeypatch `mini_ork.recovery.diagnose.diagnose` with a fake that takes
  `(home, run_id, *, write=True, allow_model=False)`, asserts `write is False`,
  and returns the layer scripted per run id (`"change"` by default) with
  `rule_id` `"R5"` and `node` `"test_verifier"`.

**The fixture** `repo(tmp_path)` returns `(root, c1, c2, c3)`:

- `root = tmp_path / "fw"`. Run `git init` and set user.email and user.name.
- **c0, "init".** `README` is `x\n`.
- **c1, "base".** `calc.py` is `def add(a, b):\n    return a - b\n`.
- **c2, "landing", on the main branch.** `calc.py` is fixed (`return a + b`) and
  `tests/test_calc.py` is added. Its content is identical to the test file in
  `rsi-t1-probe-harvest-a`'s fixture, with `test_add` and `test_add_zero`.
- **c3, "side".** On a branch `side` created from c2: `calc.py` becomes
  `return a * b`. Afterwards check `main` out again, so `HEAD == c2`.

**The home helper** `_home(tmp_path, runs)`:
- It creates `tmp_path/home/state.db` with
  `CREATE TABLE task_runs (id TEXT PRIMARY KEY, recipe TEXT, status TEXT, kickoff_path TEXT, created_at INTEGER)`
  and inserts the given rows.
- For each row flagged `fixture=True`, it writes
  `home/runs/<id>/pre-impl-fixture/MANIFEST.json` as
  `{"run_id": id, "baseline_ref": <ref>}`.

**Tests.**

- **Validation passes.** `validate_candidate({base_sha: c1, fix_sha: c2, test_files: ["tests/test_calc.py"], ...}, root=root)`
  returns `ok` True, `fail_to_pass == ["tests.test_calc::test_add"]` and
  `pass_to_pass == ["tests.test_calc::test_add_zero"]`.
- **Validation reasons.**
  - base c2 with fix c2 gives `(False, "no-fail-to-pass", _)`;
  - base c1 with fix c3 gives `(False, "fix-not-green:1", _)`;
  - base `"0" * 40` gives `(False, "archive-failed", _)`;
  - `test_files` `["tests/test_missing.py"]` gives
    `(False, "fix-missing-test", _)`;
  - afterwards `tmp_path / "scratch"` is empty every time.
- **A failed run becomes a candidate.** One failed framework-edit run
  `fe-ok-1`:
  - it has a fixture whose `baseline_ref` is c1;
  - its kickoff file on disk is `tmp_path/k1.md` =
    `"Fix add.\n\n## Files in scope\n- calc.py\n- tests/test_calc.py (new)\n"`;
  - call `failed_run_candidates(home, root, landing_ref="HEAD")`.

  It returns exactly one candidate:
  - `source == "failed-run"`;
  - `task_id == "fe-" + c2[:10]`;
  - `fix_sha == c2` and `base_sha == c1`;
  - `test_files == ["tests/test_calc.py"]`;
  - `src_files == ["calc.py"]`;
  - `cluster == "change/R5/test_verifier"`;
  - `run_ids == ["fe-ok-1"]`;
  - `baseline_outcome == 0.0`;
  - `problem_statement` is the kickoff text.

  There are no rejections.
- **Two runs with the same landing merge.** Add `fe-ok-2`, created later, with
  the same kickoff. The result is one candidate with
  `run_ids == ["fe-ok-1", "fe-ok-2"]`.
- **The rejection reasons, in query order.** Use one run for each of these:
  - no fixture → `"no-pre-impl-fixture"`;
  - `baseline_ref` `"0" * 40` → `"baseline-unreachable"`;
  - the fake diagnose layer `"provider"` → `"layer:provider"`;
  - `kickoff_path` `"/nonexistent/k.md"` → `"kickoff-unrecoverable"`;
  - a kickoff with no `tests/...py` path → `"no-test-named"`;
  - a kickoff naming `tests/unit/test_never.py` → `"test-not-landed"`;
  - `MAX_SRC_FILES` monkeypatched to 0 on an otherwise good run →
    `"landing-too-broad"`.

  The rows below are ignored, appearing neither as a candidate nor as a
  rejection:
  - a `published` framework-edit run;
  - a `failed` code-fix run.
- **The kickoff that landed wins.** Use a second repo variant:
  - c1 also commits `kickoffs/auto/k.md` = `"v1 tests/test_calc.py\n"`;
  - c2 also changes it to `"v2 tests/test_calc.py\n"`;
  - the run's `kickoff_path` is `"/gone/kickoffs/auto/k.md"`, which does not
    exist on disk.

  The candidate's `problem_statement` is `"v2 tests/test_calc.py\n"`, and its
  `src_files == ["calc.py", "kickoffs/auto/k.md"]`.
- **Rendering a failed-run candidate.**
  - The markdown contains the kickoff text, including `tests/test_calc.py`.
  - It contains neither `c2` nor `c1` nor `"fe-" + c2[:10]`.
  - It contains neither `"## Problem"` nor `"## Definition of Done"`.
- **`harvest` for framework-edit.** Call
  `harvest("framework-edit", root=root, home=home, write=True, landing_ref="HEAD", validate_fn=lambda c: (True, "", {**c, "fail_to_pass": ["x::y"]}), now="2026-10-27T00:00:00Z")`.
  - It writes `recipes/framework-edit/probes/probe-h01.md` and
    `oracles/probe-h01.json`, creating the directories.
  - The sidecar has `provenance.source == "failed-run"`.
- **The CLI validates by default.**
  - Write a one-task manifest under `root/evals/heldout/mined/manifest.json`. It
    is a dev task with these fields:
    - `id`: `"mo-" + c2[:10]`
    - `split`: `"dev"`
    - `weak_signal`: `false`
    - `base_sha`: c1
    - `fix_sha`: c2
    - `test_files`: `["tests/test_calc.py"]`
    - `fail_to_pass`: `["stale::id"]`
    - `pass_to_pass`: `[]`
    - `src_files`: `["calc.py"]`
    - `problem_statement`: `"Fix add."`
  - Write a baseline under `root/evals/heldout/results/2026-09-29-baseline-dev.json`
    marking it failed.
  - Run `main(["--recipe", "code-fix", "--root", root, "--home", home, "--write"])`.
  - It returns 0, and the written sidecar's `fail_to_pass` is
    `["tests.test_calc::test_add"]`: re-validation replaced the stale id.
  - The same command with `--no-validate` on a fresh copy keeps
    `["stale::id"]`.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_probe_harvest_git.py`
- `pytest -q -p no:asyncio tests/unit/test_probe_harvest.py tests/unit/test_probe_scorer_git_oracle.py`
- `pytest -q -p no:asyncio tests/unit/test_probe_config.py tests/unit/test_mine_heldout_tasks_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_probe_harvest_git.py --junitxml="${MINI_ORK_RUN_DIR}/probe-harvest-git-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/probe-harvest-git-junit.xml`

## Success criteria

- All three verification commands pass, and every pre-existing test in them is
  unchanged. The pre-existing tests that must stay green are:
  - `test_probe_harvest.py`, from kickoff -b
  - `test_probe_scorer_git_oracle.py`, from kickoff -a
  - `test_probe_config.py`
  - `test_mine_heldout_tasks_py.py`
- `ruff check --select F,E9 mini_ork/learning/probe_harvest.py tests/unit/test_probe_harvest_git.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
