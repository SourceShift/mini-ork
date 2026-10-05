# VT5 mutation-based suite adequacy audit for the code-fix test verifier (G03-T04)

## Goal

A green target-repo suite is only evidence if that suite can detect broken code, and nothing in mini-ork measures that today. `mini_ork/gates/mutation_adversary.py` validates mini-ork's OWN pipeline (LLM-proposed diffs, `git apply` in place) and has no reusable AST mutator. `recipes/code-fix/verifiers/test.py` accepts any green suite whose base replay overlaps.
Consensus priority #5 of the 2000-paper verification review (G03-T04): score the verifier's suite by fault-detection power (killed non-equivalent mutants), not by input coverage. Evidence: arXiv 2609.19844 (SecTB-RTL mutant kills 36/75/78), 2609.37322, 2608.24135, 2609.35841 (test-aware mutants). Caution from 2607.22880: mutation/coverage proxies may not transfer to LLM-written tests, so this ships opt-in.
Slice 1: generate deterministic AST mutants over the files the candidate changed. A green from a suite that cannot kill them is downgraded to UNVERIFIED.

## Mechanism (exact spec)

1. New stdlib-only module `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/gates/suite_adequacy.py`. Rules: no LLM, no network, no `mini_ork.dispatch` import, and nothing imported from `mutation_adversary.py`. It imports `scrubbed_test_env` from `mini_ork.verify.test_env`. Public names: `OPERATORS`, `MIN_VALID = 3`, `Mutant`, `enabled`, `settings`, `generate_mutants`, `audit_suite`.
2. Knobs:
   - `enabled(environ=None) -> bool` is True only when `MO_SUITE_ADEQUACY == "1"`. The default is OFF because each audit costs up to 2+N extra full suite runs and the downgrade changes code-fix verdicts. It stays opt-in until the live smoke and a false-pass measurement land.
   - `settings(environ=None) -> {"max_mutants": int, "min_score": float, "timeout_s": float}` reads `MO_SUITE_ADEQUACY_MAX_MUTANTS` (default 12, clamped 1..50), `MO_SUITE_ADEQUACY_MIN_SCORE` (default 0.6, clamped 0..1) and `MO_SUITE_ADEQUACY_TIMEOUT_S` (default 300, minimum 1). An unparsable value falls back to the default.
3. `OPERATORS = ("cmp_flip", "arith_swap", "bool_negate", "return_none", "cond_true", "cond_false", "const_off_by_one")`. Each node is one site; a `Compare` gets one site per op index.
   - cmp_flip: `<`↔`>=`, `<=`↔`>`, `==`↔`!=`, `is`↔`is not`, `in`↔`not in`.
   - arith_swap, on `BinOp` and `AugAssign`: `+`→`-`, `-`→`+`, `*`→`/`, `/`→`*`, `//`→`*`, `%`→`//`, `**`→`*`.
   - bool_negate: `and`↔`or`; `not x`→`x`.
   - return_none: `return X`→`return None`. Skip bare `return` and `return None`.
   - cond_true: `If.test` / `IfExp.test` → `True`.
   - cond_false: `If.test` / `IfExp.test` / `While.test` → `False`.
   - const_off_by_one: an int constant `n` → `n+1`. The check is `type(v) is int`, so bools are excluded.
   - Never mutate inside `if __name__ == "__main__":`, `if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:`. This exclusion covers the `If` node itself.
4. `generate_mutants(repo_dir, source_files, *, max_mutants=12, seed=0) -> list[Mutant]` reads files only.
   - Eligible files: repo-relative, `.py`, a regular file inside `repo_dir` (absolute paths and `..` escapes rejected), decodes as UTF-8, and `ast.parse` succeeds. Dedupe and sort the list.
   - The normalised original is `N(f) = ast.unparse(ast.parse(text))`.
   - Each mutant source is `ast.unparse` of a `copy.deepcopy` tree with exactly ONE site rewritten. Drop a source that equals `N(f)` or an earlier mutant of the same file.
   - Site key: `(file, lineno, col_offset, OPERATORS.index(op), op_index)`.
   - Selection, fully deterministic: bucket sites by operator in `OPERATORS` order and sort each bucket by key. Shuffle the buckets in that order with ONE `random.Random(seed)`. Take sites round-robin across buckets until `max_mutants`, re-sort by key, then assign ids `M01`, `M02`, ….
   - `Mutant` is a frozen dataclass `(id, file, line, col, operator, original, mutated, source)`. `original` / `mutated` are `ast.unparse` of the node before/after, at most 120 chars. `source` is never serialised.
5. `audit_suite(repo_dir, test_cmd, source_files, *, max_mutants=None, min_score=None, timeout_s=None, report_path=None) -> dict`. Any `None` argument comes from `settings()`.
   a. Copy, never touch the live tree:
      - `tmp = tempfile.mkdtemp(prefix="mo-suite-adequacy-")`, then `shutil.copytree(repo_dir, tmp/repo, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "venv", "node_modules", ".mini-ork"))`.
      - All work runs inside `try/finally: shutil.rmtree(tmp, ignore_errors=True)`. Nothing under `repo_dir` is EVER opened for writing.
      - Write `N(f)` into the copy for every eligible file. The baseline is therefore the normalised original, so the baseline and each mutant differ by exactly one node, and unparse artefacts show up as a red baseline (UNVERIFIED), never as a fake kill.
   b. Child env and process handling:
      - Env = `scrubbed_test_env()` + `PYTHONDONTWRITEBYTECODE=1` + the copy root prepended to `PYTHONPATH`. The bytecode flag matters: pyc staleness is checked by mtime+size, so a same-size rewrite in the same second would load the old bytecode and fake a survivor.
      - Run every command as `subprocess.Popen(test_cmd, shell=True, cwd=<copy root>, env=…, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)`.
      - On timeout, call `os.killpg(os.getpgid(p.pid), SIGKILL)` and then `p.wait()` (pattern: `probe_scorer._kill_process_group`). `OSError` → rc `None`.
   c. Baseline and canary:
      - Baseline run (timeout `timeout_s`): rc≠0 → UNVERIFIED `baseline-red: rc=<rc>`; a timeout → `baseline-timeout`.
      - Canary: replace every eligible file in the copy with `raise ImportError("mini-ork suite-adequacy canary")\n`, run, then restore `N(f)`. rc==0 → UNVERIFIED `canary-undetected`: the suite never loads in-scope code from the copy (for example, an editable install pointing at the live tree).
      - Per-mutant timeout = `min(timeout_s, max(10.0, 5 * baseline_seconds))`.
   d. Per mutant: write its source, run, and restore `N(f)` in `finally`.
      - A failed `compile()`, a timeout, or rc `None` → `invalid`.
      - If `"pytest" in test_cmd`: rc 0 → `survived`; rc 1 → `killed`; any other rc → `invalid` (2 = collection/import error, also 3, 4, 5).
      - Other runners: rc 0 → `survived`, anything else → `killed`.
      - Equivalent mutants cannot be detected, so they count as survivors. This biases toward abstaining, never toward a false pass.
   e. Score and verdict:
      - `score = round(killed / (killed + survived), 3)`, or `None` when there is no valid mutant.
      - UNVERIFIED: `no-sources`, `no-sites`, the baseline/canary reasons above, or `killed + survived < MIN_VALID` (`too-few-valid: <n>`).
      - Otherwise: `score >= min_score` → ADEQUATE (`score <s> >= <m>`); else INADEQUATE (`score <s> < <m>`).
      - An unexpected exception → UNVERIFIED `harness-error: <exc>`. The function never raises.
   f. Return exactly these keys: `{"verdict", "reason", "score", "killed", "survived", "invalid", "total", "min_score", "min_valid", "max_mutants", "files", "baseline_rc", "canary_detected", "mutant_timeout_s", "mutants": [{"id","file","line","col","operator","original","mutated","outcome","rc"}], "survivors": [{"id","file","line","operator","original","mutated"}]}`.
   g. If `report_path` is given, also write this dict there as JSON (indent=2, mkdir parents, OSError swallowed).
6. Hook in `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/recipes/code-fix/verifiers/test.py`:
   a. Late import `from mini_ork.gates import suite_adequacy as _suite_adequacy` inside try/except, falling back to `None`. This mirrors the existing `replay_check` import.
   b. `_changed_source_files(candidate_cwd) -> list[str]`:
      - Parse the same `git status --porcelain --untracked-files=all` output that `_overlay_candidate_tests` reads. Renames → the new path. Skip `!!`, any status containing `D`, and quoted paths.
      - Keep `.py` paths where `not _is_test_path(p)`. Return them sorted and deduped; return `[]` on a git error.
   c. Change the signatures to `emit(passed, reason, post_rc, replay=None, adequacy=None)` and `emit_unverified(post_rc, reason, replay=None, adequacy=None, flag="replay_unverified")`.
      - `payload[flag] = True` replaces the hard-coded `replay_unverified` key.
      - When `adequacy is not None`, add `payload["suite_adequacy"] = adequacy` as the LAST key.
      - The defaults reproduce today's bytes exactly.
   d. `_green_pass(reason, post_rc, replay=None)` replaces the three `emit(True, …)` calls inside `if post_rc == 0:` (replay opt-out, replay skipped, replay passed).
      - Knob off (`os.environ.get("MO_SUITE_ADEQUACY", "0") != "1"`): return exactly `emit(True, reason, post_rc, replay=replay)`.
      - Knob on: `a = _suite_adequacy.audit_suite(os.getcwd(), CMD, _changed_source_files(os.getcwd()), report_path=os.path.join(LOG_DIR, "suite_adequacy.json"))`. If the module is `None`, `a` is UNVERIFIED with reason `module-unavailable`.
      - ADEQUATE → `emit(True, f"{reason}; suite adequacy ADEQUATE (score {a['score']:.3f})", post_rc, replay=replay, adequacy=a)`.
      - INADEQUATE or UNVERIFIED → `emit_unverified(post_rc, f"suite-{a['verdict'].lower()}: {a['reason']}", replay=replay, adequacy=a, flag="adequacy_unverified")`.
   e. Decision: YES, downgrade. A green from a suite that cannot kill mutants is not evidence, so pass becomes UNVERIFIED (`pass: false`, exit 0). This is the existing abstention contract: no rollback, no certify.
      - The red-suite, replay-fail and replay-unverified paths stay byte-identical even with the knob on. Measuring adequacy there is out of scope for slice 1.
      - Add the four knobs to the verifier's header env-var list.

## Files in scope (touch ONLY these)

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/gates/suite_adequacy.py` (new): the deterministic mutant generator and auditor.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/recipes/code-fix/verifiers/test.py`: the production hook (`_changed_source_files`, `_green_pass`, the emit kwargs).
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/tests/unit/test_suite_adequacy.py` (new): hermetic module tests AND the real-verifier-script tests, in one file.

Read-only references (do NOT edit):
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/verify/test_env.py`: `scrubbed_test_env`.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/gates/mutation_adversary.py`: no AST mutator to reuse; leave untouched.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/learning/probe_scorer.py`: the `_kill_process_group` pattern.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/certify/oracle.py`: `replay_check` result shape.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/mini_ork/cli/execute.py`: `_run_verifier_ref`. Its evidence starts with stderr lines, so the script's exit code decides, and exit 0 means abstain.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy/tests/unit/test_codefix_replay.py`: the harness to copy. It must keep passing UNMODIFIED.

Do NOT modify any other file.

## Tests

`tests/unit/test_suite_adequacy.py` is hermetic. Each test builds a throwaway package in `tmp_path` (`calc.py` + `tests/test_calc.py` + an empty `conftest.py`) and uses `CMD = f"{sys.executable} -m pytest -q -p no:cacheprovider"`. No LLM, no network.
1. Strong suite (asserts every function's outputs) → `ADEQUATE`, `score >= 0.6`, `killed + survived >= 3`.
2. Weak suite (calls the functions, asserts nothing) → `INADEQUATE`. `survivors` is non-empty, and each entry has `file == "calc.py"`, an int `line >= 1` and `operator in OPERATORS`.
3. A module-level guard `if _LIMIT < 0: raise ImportError(...)`, audited with `max_mutants=50`:
   - the `cmp_flip` and `cond_true` mutants on that line are `invalid`;
   - `total == killed + survived + invalid`;
   - `score == round(killed / (killed + survived), 3)`.
4. Suite red on the unmutated code → `UNVERIFIED`, reason starts with `baseline-red`, `mutants == []`.
5. Suite that never imports `calc` → `UNVERIFIED`, reason starts with `canary-undetected`.
6. Live tree untouched:
   - a `{relpath: bytes}` snapshot of every file under the package is equal before and after the audit;
   - no `__pycache__` is created there;
   - the audit's temp dir (captured by monkeypatching `tempfile.mkdtemp`) no longer exists afterwards.
7. Determinism and cap:
   - `generate_mutants` called twice → identical `(id, file, line, col, operator, source)` lists;
   - `max_mutants=4` → exactly 4 mutants, drawn from at least 3 distinct operators.
8. Knobs:
   - `enabled({})` is False and `enabled({"MO_SUITE_ADEQUACY": "1"})` is True;
   - `settings({})` returns the defaults;
   - out-of-range values clamp and garbage values fall back to the defaults.

A second section of the same `tests/unit/test_suite_adequacy.py` drives the REAL production entrypoint `recipes/code-fix/verifiers/test.py` as a subprocess.
- Copy `_make_repo`, `_run_verifier` and `_snap_worktree` from `test_codefix_replay.py`.
- HEAD's `mod.py` has `add` returning `a - b`, plus at least two more small functions. The uncommitted fix returns `a + b`.
- `MINI_ORK_TEST_CMD = f"{sys.executable} -m pytest -p no:cacheprovider"`. Do not add `-q`: the replay parses per-test lines.

9. Knob unset vs `MO_SUITE_ADEQUACY=0` (same run id):
   - the last stdout lines are byte-identical;
   - keys are exactly `{verifier, pass, evidence_path, error_summary, post_rc, base_rc, replay}`;
   - `error_summary == "post-patch suite green; replay: tests exercise the change"`.
10. Knob on + strong suite + an extra untracked `tests/test_extra.py`:
    - `pass is True`, `suite_adequacy.verdict == "ADEQUATE"`, `suite_adequacy.files == ["mod.py"]`;
    - `<MINI_ORK_HOME>/runs/<run_id>/suite_adequacy.json` exists;
    - the candidate files and `git status --porcelain` are byte-identical before and after.
11. Knob on + weak suite (only `test_add`, which exercises the fix; the other functions are untested):
    - process rc 0, `pass is False`, `adequacy_unverified is True`, `"replay_unverified" not in out`;
    - `error_summary` starts with `unverified: suite-inadequate`;
    - `replay.overlap` is non-empty (today's verifier would have passed it);
    - `suite_adequacy.survivors` is non-empty.

## Success criteria

- The verification command below exits 0. `test_codefix_replay.py` and `test_verifier_test_env.py` pass without edits.
- With `MO_SUITE_ADEQUACY` unset, the code-fix verifier output is unchanged (test 9). No new required env var and no new `mini-ork` subcommand.
- `git status` shows changes only to the three in-scope paths. `mutation_adversary.py` is unchanged.

## Verification command

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt5-mutation-adequacy && python3.11 -m pytest -q -p no:cacheprovider tests/unit/test_suite_adequacy.py tests/unit/test_codefix_replay.py tests/unit/test_verifier_test_env.py tests/unit/test_mutation_adversary_py.py tests/unit/test_mutation_adversary_gate_py.py
```

## Review bar

- Every new input has a named production producer:
  - `repo_dir` ← the verifier cwd, which is the executor's `roots.target` / `MO_TARGET_CWD` (`_run_verifier_ref`);
  - `test_cmd` ← the verifier's `CMD` (`detect_test_cmd` / `MINI_ORK_TEST_CMD`);
  - `source_files` ← `_changed_source_files`, the same `git status` the overlay already reads, which lists the implementer's uncommitted edits;
  - `report_path` ← the existing `LOG_DIR`;
  - knobs ← env, with module defaults.
  
  No parameter exists only for tests.
- Tests 10 and 11 go through the real verifier script, not an in-process `audit_suite` call.
- Reject the change if any of these appear:
  - any write under `repo_dir`;
  - any LLM or network import;
  - `shell=True` without `start_new_session=True`;
  - an INADEQUATE or UNVERIFIED result reaching `pass: true`;
  - an edit to `mutation_adversary.py`;
  - knob-off output drift.

## Rules

Edit files directly; do not emit unified diffs. Do not touch `execute.py`, `execute_handlers.py`, `mutation_adversary.py`, `probe_scorer.py` or any other recipe.
