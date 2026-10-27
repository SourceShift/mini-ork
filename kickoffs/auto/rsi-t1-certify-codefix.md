# RSI Tier-1 #8a: run the certify relations and differential vetoes inside the code-fix test verifier (shadow)

## Goal

`docs/architecture/verification-stack.md` says metamorphic relations (①) and the
differential check (②) are "ON by default". In fact they run only under
`mini-ork certify`: `mini_ork/cli/certify.py:main` builds a docker `Crucible`
and calls `mini_ork.certify.oracle.judge`, which reaches `relations.check` and
`differential.check` only after a PROVEN delta and invariant verdict. The
code-fix verifier, `recipes/code-fix/verifiers/test.py`, imports only
`replay_check` and `suite_adequacy`. So no code-fix run has ever been vetoed by
a relation or a differential divergence.

This kickoff runs both vetoes inside the code-fix test verifier, **in shadow**.
The verdict is recorded in the verifier payload (`verifier_test.json` key
`certify`) and in `certify_terms.json` in the run dir. It never changes the
verifier's `pass`, its exit code, or any other key. The gate that may later
withhold publish lands in a separate kickoff (`rsi-t1-certify-codefix-gate`),
which reads this block from the level vector.

**Why the verifier and not the publisher.** The verifier is the only place
that has all four inputs at once:

- the candidate tree (cwd);
- a base worktree (`_attach_git_worktree_base(_run_dir_base_ref())`);
- the replay's proven fail-to-pass test ids (`replay["strong_overlap"]`);
- the kickoff text (`_kickoff_text`).

`mini_ork/verify/levels.py` promises at import time "No `subprocess` … no
LLM", so it can only *read* a recorded verdict.

**Why not `oracle.judge`.** The code-fix replay has already proven the delta
gate, with real tests that fail on the base and pass on the candidate. So the
relations and differential terms are called directly in veto mode, with that
test as the PoC. Two other terms are left out on purpose:

- **Discrimination** needs a pool of known-correct and known-bad patches, and
  code-fix has none.
- **Hidden checks** guard a PoC that the implementer could not see, but the
  code-fix PoC is the implementer's own test.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/certify/codefix_terms.py` (new)

Allowed imports:

- stdlib;
- `from mini_ork.certify import relations, differential` (module objects:
  always call `relations.check(...)` and `differential.check(...)` through
  them, so tests can monkeypatch them);
- `from mini_ork.certify.context import code_context`;
- `from mini_ork.certify.verdict import REFUTED`;
- `from mini_ork.context import scoped_environ`;
- `from mini_ork.runtime.engine import Crucible, ExecOutcome`;
- `from mini_ork.verify.test_env import scrubbed_test_env`.

Import `mini_ork.certify.llm` lazily, inside the function that
`bounded_dispatch` returns.

- **`MODE_ENV = "MO_CODEFIX_CERTIFY"`.**
- **`mode(environ=None) -> str`.** Reads `MO_CODEFIX_CERTIFY` from `environ`,
  or from `os.environ` when `environ` is None. The value is `.strip().lower()`ed.
  - `"0"`, `"off"`, `"false"`, `"no"` → `"off"`;
  - `"1"`, `"gate"` → `"gate"`;
  - anything else, including unset, `""` and `"shadow"` → `"shadow"`.

  A typo never gates.
- **`poc_from_replay(replay: dict, candidate_dir: str, *, max_chars: int = 8000) -> tuple[str | None, str | None, str]`.**
  - Walk the ids in `replay.get("strong_overlap") or []`, then those in
    `replay.get("overlap") or []`.
  - For each id, `rel = id.split("::", 1)[0]`. Skip the id when:
    - it ends with `"::<suite load failure>"`;
    - `rel` does not end with `.py`;
    - `os.path.realpath(os.path.join(candidate_dir, rel))` is not inside
      `os.path.realpath(candidate_dir)`;
    - that path is not a regular file.
  - The first id that survives returns
    `(text[:max_chars], rel, "")`. The file is read as UTF-8 with
    `errors="replace"`.
  - When none survives, return
    `(None, None, "no python fail-to-pass test in replay overlap")`.
  - Never raises: a non-dict `replay` gives the same `None` triple.
- **`class LocalTreeRunner`.** The duck-typed runner that `relations.check` and
  `differential.check` consume, with `.up` and `.run_test`.
  - `__init__(self, base_dir: str, candidate_dir: str, *, python: str | None = None, timeout_s: float = 120.0)`.
    `python` defaults to `sys.executable`.
  - `up` (property): `os.path.isdir(base_dir) and os.path.isdir(candidate_dir)`.
  - `run_test(self, test_src: str, patch: str = "") -> ExecOutcome`. Returns
    `self.run_in(self.candidate_dir if patch.strip() else self.base_dir, test_src)`.
    The patch text is never applied: the candidate tree already contains it.
  - `run_in(self, tree_dir: str, test_src: str) -> ExecOutcome`:
    - Write `test_src` to `<tmp>/crucible_probe.py`, where
      `tmp = tempfile.mkdtemp(prefix="mo-codefix-probe-")`.
    - Run
      `[python, "-m", "pytest", <probe>, "-q", "-p", "no:cacheprovider", "--rootdir", tmp]`
      with `cwd=tree_dir`, `stdout=PIPE`, `stderr=STDOUT`, `text=True` and
      `timeout=timeout_s`.
    - The env is `scrubbed_test_env()`, then these changes:
      - drop every key that starts with `MO_`;
      - drop `PYTEST_ADDOPTS`;
      - set `PYTHONDONTWRITEBYTECODE=1`;
      - set `PYTHONPATH` to `tree_dir`, plus `os.pathsep` and the previous
        value when there was one.
    - Return `Crucible._classify(stdout + f"\nCRUCIBLE_RC={rc}\n", backend="local-tree")`.
    - On `subprocess.TimeoutExpired`, return
      `ExecOutcome(status="error", ran=False, output="timeout", backend="local-tree", exc="Timeout")`.
    - On `OSError`, return
      `ExecOutcome(status="no_run", output=str(e), backend="local-tree")`.
    - Always `shutil.rmtree(tmp, ignore_errors=True)`.
    - Never write inside `tree_dir`.
- **`bounded_dispatch() -> Callable[[str], str]`.** Returns `fn(prompt)`, which
  calls `mini_ork.certify.llm.default_dispatch(prompt)`. Look the function up
  as a module attribute at call time. The call runs inside
  `scoped_environ({"MO_CERTIFY_LANE_RETRIES": "0", "MO_CERTIFY_LANE_TIMEOUT_S": os.environ.get("MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S", "90")})`.
  `fn` returns `""` on any exception.
- **`run_terms(*, issue: str, patch: str, poc: str, runner, dispatch, context=None) -> dict`.**
  - **Relations.** When `relations.enabled()`, the slot is
    `relations.check(poc, issue, patch, runner=runner, dispatch=dispatch, context=context, k=relations.k_from_env())`,
    slimmed: a copy whose `records` entries have no `src` and no `detail` key.
    Otherwise the slot is `None`.
  - **Differential.** When `differential.enabled()`, the slot is
    `differential.check(poc, issue, patch, runner=runner, dispatch=dispatch, context=context, n=differential.n_from_env())`,
    slimmed: a copy without the top-level `src`. Otherwise the slot is `None`.
  - **Errors.** A term that raises gets the slot
    `{"error": f"{type(e).__name__}: {e}"}`. `run_terms` never raises.
  - **Return** exactly these keys:
    - `relations`, `differential`: the two slots;
    - `vetoed_by`: in that order, the terms whose slot is a dict with
      `verdict == REFUTED`;
    - `verdict`: `REFUTED` when `vetoed_by` is non-empty, else `None`;
    - `reason`: the first vetoing term's `reason`, else `"no veto"`.
- **`evaluate(*, issue: str, poc: str, poc_file: str | None, base_dir: str, candidate_dir: str, base_ref: str = "", mode: str = "shadow", dispatch=None, python: str | None = None, timeout_s: float = 120.0) -> dict`.**
  - **Return shape.** Always exactly these 9 keys: `mode`, `status`, `reason`,
    `poc_file`, `relations`, `differential`, `verdict`, `vetoed_by`,
    `would_block`. `status` is one of `"ran"`, `"skipped"`, `"error"`.
    Non-ran records have `relations=None`, `differential=None`,
    `verdict=None`, `vetoed_by=[]` and `would_block=False`.
  - **Steps:**
    1. Compute the patch with
       `git -C <candidate_dir> diff --no-color <base_ref or "HEAD">`. A non-zero
       rc or empty output gives `"skipped"` with reason
       `"empty candidate diff"`. No dispatch happens.
    2. Build `runner = LocalTreeRunner(base_dir, candidate_dir, python=python, timeout_s=timeout_s)`.
       When `not runner.up`, return `"skipped"` with reason
       `"runner unavailable"`.
    3. Set `context = code_context(Path(candidate_dir), base_ref or "HEAD", patch)`,
       or `None` on any exception.
    4. Call `run_terms(...)` with `dispatch or bounded_dispatch()`.
    5. Return `"ran"`, with `reason`, `relations`, `differential`,
       `verdict` and `vetoed_by` taken from `run_terms`, and
       `would_block = verdict == REFUTED`.
  - Any unexpected exception gives `"error"` with reason
    `f"{type(e).__name__}: {e}"`.

### 2. `recipes/code-fix/verifiers/test.py`

- **Import seam.** Add one next to the existing ones:
  `try: from mini_ork.certify import codefix_terms as _codefix_terms` /
  `except Exception: _codefix_terms = None`.
- **Module globals.** Next to `TARGET_WAIVER`, add `CERTIFY = None` and
  `CERTIFY_REPORT = os.path.join(LOG_DIR, "certify_terms.json")`.
- **Payload.**
  - In `emit`: insert `if CERTIFY is not None: payload["certify"] = CERTIFY`
    after the `suite_adequacy` block and before `target_waiver`.
  - In `emit_unverified`: insert the same line after the `suite_adequacy` block
    and before `payload["status"]`.
  - No other payload key changes.
- **New helper `_git_toplevel(cwd: str) -> str`.** Returns the stripped stdout
  of `git -C <cwd> rev-parse --show-toplevel`, or `cwd` on failure.
- **New helper `_record_certify(replay) -> None`.** It never raises. In this
  order:
  1. Return when `_codefix_terms is None` or `replay` is not a dict.
  2. Set `mode = _codefix_terms.mode()`. Return when it is `"off"`.
  3. Set `issue = _kickoff_text(_git_toplevel(os.getcwd()))`. When it is empty,
     write `[test] certify: skipped (no kickoff text)\n` to stderr and return.
  4. Set `poc, poc_file, why = _codefix_terms.poc_from_replay(replay, os.getcwd())`.
     When `poc is None`, write `[test] certify: skipped ({why})\n` and return.
  5. Set `base_ref = _run_dir_base_ref()` and
     `wt = _attach_git_worktree_base(base_ref)`. When `wt is None`, write
     `[test] certify: skipped (no base worktree)\n` and return.
  6. Call:

     ```
     rec = _codefix_terms.evaluate(issue=issue, poc=poc, poc_file=poc_file,
         base_dir=wt, candidate_dir=os.getcwd(), base_ref=base_ref, mode=mode,
         python=os.environ.get("MO_CODEFIX_CERTIFY_PYTHON") or sys.executable)
     ```

     An exception here gives the 9-key record with `status="error"`, reason
     `f"{type(exc).__name__}: {exc}"`, `poc_file=poc_file`, and the non-ran
     defaults. Call `_detach_git_worktree(wt)` in `finally`.
  7. Write `rec` to `CERTIFY_REPORT` (`json.dumps(rec, indent=2)`; swallow
     `OSError`).
  8. Set the global `CERTIFY = rec` unless `rec.get("status") == "skipped"`.
- **Call site.** The first statement of `_green_pass` is
  `if replay is not None: _record_certify(replay)`. Nothing else in
  `_green_pass` or `main` changes. The verifier's exit code and `pass` are the
  same in all three modes.
- **Header comment.** Add the knob lines to the env-var list:
  - `MO_CODEFIX_CERTIFY` (shadow|0|gate, default shadow);
  - `MO_CODEFIX_CERTIFY_LANE_TIMEOUT_S` (default 90);
  - `MO_CODEFIX_CERTIFY_PYTHON` (default: the verifier's interpreter).

### 3. `docs/architecture/verification-stack.md`

- Change the intro claim. ① and ② are ON in `mini-ork certify`. In
  `mini-ork run code-fix` they run in shadow.
- Add a section `## ⑮ Certify terms in code-fix (shadow)`. It names:
  - the trigger: a green replay whose overlap contains a Python test file, plus
    a kickoff;
  - the three knobs and the `MO_ASSAY_*` knobs it respects;
  - the evidence: `verifier_test.json` key `certify` and `certify_terms.json`;
  - the cost: at most 4 lane calls of 90 s each, plus 2k + 2N single-file
    pytest runs;
  - the statement that `gate` is recorded but inert until the level-vector
    consumer lands.

## Files in scope (touch ONLY these)

- `mini_ork/certify/codefix_terms.py` (new)
- `recipes/code-fix/verifiers/test.py`
- `docs/architecture/verification-stack.md`
- `tests/unit/test_codefix_certify_terms.py` (new)
- `tests/unit/test_codefix_certify_verifier.py` (new)

Do NOT modify any other file.

## Tests (hermetic: temp dirs and repos, fake dispatch only, no network, no real lane call)

Every test module imports what it uses (`import pytest`, `import subprocess`,
`import sys` …) explicitly.

### `tests/unit/test_codefix_certify_terms.py`

Fixture texts, written into plain temp dirs:

- `BASE_MED`:

  ```
  def median(xs):
      s = sorted(xs)
      return s[len(s) // 2]
  ```

- `CHEAT_MED`:

  ```
  def median(xs):
      if xs == [1, 2, 3, 4]:
          return 2.5
      s = sorted(xs)
      return s[len(s) // 2]
  ```

- `AVG_MED` (always averages the two middle values):

  ```
  def median(xs):
      s = sorted(xs)
      n = len(s)
      return (s[n // 2 - 1] + s[n // 2]) / 2
  ```

- `GOOD_MED`:

  ```
  def median(xs):
      s = sorted(xs)
      n = len(s)
      if n % 2 == 0:
          return (s[n // 2 - 1] + s[n // 2]) / 2
      return s[n // 2]
  ```

- `POC`:

  ```
  from med import median
  def test_even():
      assert median([1, 2, 3, 4]) == 2.5
  ```

- `REL`:

  ```
  from med import median
  SOURCE = [1, 2, 3, 4]
  FOLLOWUP = [4, 3, 2, 1]
  TRANSFORM = "reverse the list"
  RELATION = "median is unchanged"
  def test_relation():
      assert median(SOURCE) == median(FOLLOWUP)
  ```

- `SUITE`:

  ```
  from med import median
  CASES = [
      ("bug_domain", [1, 2, 3, 4]),
      ("preserve", [1, 2, 3]),
      ("preserve", [5]),
      ("preserve", [3, 1, 2]),
  ]
  def observe(x):
      return median(x)
  ```

The fake dispatch:

- returns `"```python\n" + REL + "```"` when the prompt contains
  `METAMORPHIC RELATIONS`;
- returns `"```python\n" + SUITE + "```"` when it contains
  `DIFFERENTIAL INPUT SUITE`;
- returns `""` otherwise.

For `evaluate` tests, make a git repo the way
`tests/unit/test_codefix_replay.py::_make_repo` does (copy `_git` and
`_make_repo`):

- commit `med.py = BASE_MED` and `test_med.py = POC`;
- overwrite `med.py` in the working tree with the candidate text, and leave it
  uncommitted;
- `base_dir` is a separate plain dir that holds only `med.py = BASE_MED`.

Tests:

- **`mode`.** It parses as specified: unset, `""`, `"shadow"` and `"bogus"`
  give shadow; `"0"`, `"off"` and `"OFF"` give off; `"1"` and `"gate"` give
  gate.
- **`poc_from_replay`.**
  - It prefers `strong_overlap`.
  - It skips `x.py::<suite load failure>`, `src/a.test.ts::t` and
    `../outside.py::t`.
  - It returns the `None` triple with the exact reason when nothing survives.
  - It clips a 10 000-char file to 8000.
- **`LocalTreeRunner`.**
  - `POC` on base is `failed` with exc `AssertionError`; on the candidate
    (`patch="x"`, `GOOD_MED`) it is `passed`.
  - A test using an undefined name is `test_defect`.
  - `up` is False when a dir is missing.
  - A `time.sleep(5)` test with `timeout_s=1` is `error`.
  - The candidate dir's file listing is identical before and after: no
    `crucible_probe.py`, `__pycache__` or `.pytest_cache`.
- **`run_terms` with monkeypatched `relations.check` / `differential.check`.**
  - The canned records carry `src` / `detail`, and the result has none.
  - A REFUTED relations record gives `vetoed_by == ["relations"]`,
    `verdict == "REFUTED"` and its reason.
  - A raising differential gives a slot with an `error` key, and no raise.
  - `MO_ASSAY_RELATIONS=0` means relations is not called and its slot is
    `None`.
- **Relations veto end to end.**
  - Set `MO_ASSAY_RELATIONS_K=1` and `MO_ASSAY_DIFFERENTIAL=0`.
  - With the `CHEAT_MED` candidate, `evaluate(..., dispatch=fake, python=sys.executable)`
    gives `status == "ran"`, `verdict == "REFUTED"`,
    `vetoed_by == ["relations"]`, `would_block is True`,
    `relations["counts"]["violated"] == 1`, and no `src` in any relations
    record.
  - With `GOOD_MED` it gives `verdict is None`, `would_block is False` and
    `relations["counts"]["held"] == 1`.
- **Differential veto end to end.**
  - Set `MO_ASSAY_RELATIONS=0` and `MO_ASSAY_DIFFERENTIAL_N=4`.
  - `AVG_MED` gives `vetoed_by == ["differential"]`.
  - `GOOD_MED` gives `verdict is None` and `differential["state"] == "equivalent"`.
- **Empty diff.** A clean working tree gives `status == "skipped"` and reason
  `"empty candidate diff"`, and the fake is never called.
- **`bounded_dispatch`.** With `mini_ork.certify.llm.default_dispatch`
  monkeypatched to record `os.environ["MO_CERTIFY_LANE_RETRIES"]` and
  `os.environ["MO_CERTIFY_LANE_TIMEOUT_S"]`:
  - one call sees `"0"` and `"90"`;
  - neither key is left in `os.environ` afterwards when it was absent before;
  - a raising fake returns `""`.

### `tests/unit/test_codefix_certify_verifier.py`

This is the in-process verifier. Copy `_load_verifier` and `_run_main` from
`tests/unit/test_replay_antigaming.py`. Use a strong replay, like that file's
`_WEAK_REPLAY` but with `"weak": False` and
`"strong_overlap": ["test_mod.py::test_target"]`, and the `_ADEQUATE` audit.

Stubs:

- `mod._kickoff_text` → `lambda toplevel: "median bug"`;
- `mod._attach_git_worktree_base` → `lambda ref="": str(tmp_path / "base")`;
- `mod._detach_git_worktree` → a no-op;
- `codefix_terms.poc_from_replay` → returns `("def test_x():\n    pass\n", "test_mod.py", "")`;
- `codefix_terms.evaluate` → a fake that records its kwargs and returns
  `dict(CANNED, mode=kw["mode"])`, where `CANNED` is a 9-key `ran` record with
  `verdict "REFUTED"`, `vetoed_by ["relations"]` and `would_block True`.

Tests:

- **Off.** `MO_CODEFIX_CERTIFY=0` gives no `certify` key, `evaluate` is never
  called, and `pass` is True with rc 0.
- **No kickoff.** A shadow run whose kickoff stub returns `""` gives no key,
  and `evaluate` is not called.
- **Shadow.**
  - `out["certify"] == CANNED`, with `pass` True and rc 0;
  - the key directly follows `suite_adequacy`;
  - `certify_terms.json` in `mod.LOG_DIR` equals `CANNED`;
  - the fake received `mode="shadow"`, `base_dir=str(tmp_path / "base")`,
    `candidate_dir=os.getcwd()` and `issue="median bug"`.
- **Gate.** `MO_CODEFIX_CERTIFY=gate` gives `out["certify"]["mode"] == "gate"`,
  and `pass` and rc are still True and 0.
- **Error.** A raising `evaluate` gives `out["certify"]["status"] == "error"`
  with the exception text in `reason`, `would_block` False, and `pass` and rc
  unchanged.
- **Skipped.** A fake that returns `status "skipped"` gives no `certify` key,
  and the report file is still written.

## Verification commands

- `pytest -q -p no:asyncio tests/unit/test_codefix_certify_terms.py tests/unit/test_codefix_certify_verifier.py tests/unit/test_certify_relations.py tests/unit/test_certify_differential.py`
- `pytest -q -p no:asyncio tests/unit/test_codefix_replay.py tests/unit/test_replay_antigaming.py tests/unit/test_codefix_abstention.py tests/unit/test_codefix_red_base.py tests/unit/test_codefix_tamper_guard.py tests/unit/test_suite_adequacy.py`
- `pytest -q -p no:asyncio tests/unit/test_replay_early_applicability.py tests/unit/test_replay_structured_runners.py tests/unit/test_verifier_replay_py.py tests/unit/test_codefix_base_dependency_overlay.py tests/unit/test_codefix_plan_verification_command.py tests/unit/test_codefix_verify_cmd_from_profile.py tests/unit/test_codefix_no_change_py.py`
- `pytest -q -p no:asyncio tests/unit/test_certify_oracle.py tests/unit/test_certify_context.py tests/unit/test_certify_cli.py tests/unit/test_verify_levels.py tests/unit/test_levels_framework_edit.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_codefix_certify_terms.py tests/unit/test_codefix_certify_verifier.py --junitxml="${MINI_ORK_RUN_DIR}/certify-codefix-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/certify-codefix-junit.xml`

## Success criteria

- All four verification commands pass, and every pre-existing test is
  unchanged. Every pre-existing file named above was green on main on
  2026-10-27.
- With `MO_CODEFIX_CERTIFY=0`, every verifier payload is byte-identical to
  today's.
- With the shadow default, a run without a kickoff or without a Python
  fail-to-pass test is byte-identical to today's.
- `ruff check --select F,E9 mini_ork/certify/codefix_terms.py recipes/code-fix/verifiers/test.py`
  is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
