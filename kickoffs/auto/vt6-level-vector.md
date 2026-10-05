# VT6 non-nested correctness level vector on run verdicts + publish gate (G08-T05)

## Goal

Consensus priority #6 of the 2000-paper verification review (G08-T05): correctness levels are empirically NON-NESTED, so score each one independently. The levels are applies, executes, target (red→green), preserve (no regressions) and contract. Passing a shallow level does not imply a deeper one. Evidence: arXiv 2609.26830 (16 of 150 simulated-valid circuits failed a deeper level); cluster 2609.22767, 2609.37470, 2610.02814. `ExecOutcome` (`mini_ork/runtime/engine.py:57`) is the shipped multi-status seed.

Two measured gaps this slice closes:
1. **An exit-0 abstention gets published.** `recipes/code-fix/verifiers/test.py` `emit_unverified` (l.303) prints `pass:false` and exits 0, flagged `replay_unverified` or, via `_green_pass` (l.328), `adequacy_unverified`. `_run_verifier_ref` (`mini_ork/cli/execute.py:1356-1398`) cannot `json.load` evidence that starts with `[test] running: …` stderr lines, so it returns the script rc 0. `_handle_verifier` (`execute_handlers.py:1121`) then succeeds, the reviewer runs, and `publisher_node` (`publisher.py:139`) commits on reviewer APPROVE and sets `published`. Only the LLM reviewer ever sees `pass:false`; no deterministic code reads it.
2. **"Verifier pass" stands in for "target fixed".** A read-only audit of the live home (2026-10-05) covered 236 code-fix task_runs; 61 are `published` with a parseable test verdict. 0 of those 61 had a replay overlap (target PROVEN). 39 were published with a RED post-patch suite via the `pre-existing failure` pass branch (e.g. `heldout-mo-0914e66e83-1790746058`: `post_rc=2, base_rc="2", pass:true`). 17 were green with no replay.

Slice 1, opt-in: derive a fixed 5-level vector from evidence the run already writes, stamp it into the run-level `verdict.json`, and publish only when every REQUIRED level is PROVEN.

## Mechanism (exact spec)

1. **New module** `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/verify/levels.py`. Imports: stdlib, `mini_ork.context.context_env`, and `PROVEN, REFUTED, UNVERIFIED` from `mini_ork.verify.behavioral`. Nothing else: no `subprocess`, no `mini_ork.cli` / `mini_ork.dispatch`, no LLM, no network. It never raises; an error makes that level UNVERIFIED. Public names: `LEVELS, NA, VALUES, REQUIRED_LEVELS, enabled, required_levels, read_verifier_payload, derive_levels, publish_decision, all_levels_ok, level_report`.
2. **Constants.** `LEVELS = ("applies", "executes", "target", "preserve", "contract")`; `NA = "n/a"`; `VALUES = (PROVEN, REFUTED, UNVERIFIED, NA)`. `REQUIRED_LEVELS = {"code_fix": ("applies", "executes", "target", "preserve")}` is the ONLY declaration point. `required_levels(task_class) -> tuple` returns that entry, or `()` for any other class (the gate passes; the vector is still recorded). `contract` stays unrequired until a contract producer is wired into the code-fix workflow.
3. **Knob `MO_LEVEL_VECTOR`, default OFF.** `enabled(environ=None) -> bool` is True only for `"1"`. It reads `environ` when given, else `context_env("MO_LEVEL_VECTOR", "0")`. Why off: knob off must be byte-identical, and turning it on today would withhold all 61 audited published runs. Measure first (smoke + live), as `MO_SUITE_ADEQUACY` did. `derive_levels` / `level_report` never read the knob. Document it in the module docstring.
4. **`read_verifier_payload(path, verifier) -> dict | None`**: the LAST JSON object with `"verifier" == verifier`. Scan bottom-up; for each line whose `lstrip()` starts with `{`, try `json.loads("\n".join(lines[i:]))`, then `json.loads(lines[i])`. This covers test.py's stderr-prefixed one-line payload and `BehavioralVerdict.to_json`'s `indent=2` payload.
5. **`derive_levels(run_dir) -> tuple[dict, dict]`** returns `(vector, reasons)`, keyed in `LEVELS` order. Each reason is a short string prefixed by its source file name.
   - Inputs: `S` = `<run_dir>/implementer-summary.json` (dict); `T` = `read_verifier_payload(<run_dir>/verifier_test.json, "test")`; `B` = the same for `verifier_behavioral.json` / `"behavioral"`.
   - Derived values: `rc` = `T["post_rc"]` if int, else `None`; `base_green` = `str(T.get("base_rc","")) == "0"`; `ov` = `T["replay"]["overlap"]` if it is a list, else `None`; `ru` / `au` = `T.get("replay_unverified") is True` / `T.get("adequacy_unverified") is True`.
   - Rules: first match wins; anything not listed is UNVERIFIED.

   | level | producer | PROVEN | REFUTED |
   |---|---|---|---|
   | applies | `implementer-summary.json` ← `_write_implementer_summary` (execute.py:1853, git-derived, called at execute_handlers.py:981) | `status=="implemented"` and `files_changed` is a non-empty list of str | `status=="no_changes"` |
   | executes | `verifier_test.json` ← `_handle_verifier` copy (execute_handlers.py:1182) of test.py `emit` / `emit_unverified` | `rc in (0, 1)`: the runner reached test outcomes | `rc` is an int outside {0,1} and `base_green` |
   | target | same | not `ru`/`au`, `rc==0`, `T["pass"] is True`, `ov` non-empty | not `ru`/`au`, `rc==0`, `T["pass"] is False`, `ov == []` |
   | preserve | same | not `au` and `rc==0` | `rc not in (None, 0)` and `base_green` (regression) |
   | contract | `verifier_behavioral.json` ← same persistence, from `BehavioralVerdict.to_json()` | `B["status"]=="PROVEN"` | `B["status"]=="REFUTED"` |

   - contract: file ABSENT → `"n/a"`; present but unparsable, or any other status → UNVERIFIED.
   - `au` also downgrades preserve: the suite is the only witness for "no regression", and it was measured unable to detect faults in the changed files. `ru` does not affect preserve, because `rc==0` already shows every test passes.
   - NEVER read `review-verdict.json`, `panel-verdict.json`, `eval.json`, `rubric.json` or any LLM output.
6. **`publish_decision(vector, *, required) -> str`** returns `"refute"` if any required level is REFUTED; else `"abstain"` if any required level is not PROVEN (UNVERIFIED, `n/a` or missing); else `"publish"`. Empty `required` → `"publish"`. `all_levels_ok(vector, *, required) -> bool` is `publish_decision(...) == "publish"`. `level_report(run_dir, task_class) -> dict` returns exactly these keys, in order: `levels`, `levels_reasons`, `levels_required` (list), `levels_ok` (bool), `levels_decision`.
7. **Hook A, run-level verdict:** `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/cli/execute.py` `_emit_run_verdict` (l.317) gains the kwarg `task_class=""`; the live call at l.985 passes `task_class=task_class`.
   - Knob on and not `dry_run`: write `json.dumps({"verdict":…, "failed_nodes":…, "dispatched":…, "source":"execute@run-level", **level_report(run_dir, task_class)}, separators=(",", ":"), ensure_ascii=False) + "\n"` and append ` levels_ok=<true|false>` to the print line. The first four keys keep today's bytes. This also applies to `run-verdict.json` when a recipe owns `verdict.json`.
   - Knob off or `dry_run`: today's `%`-format write, untouched.
8. **Hook B, publish gate:** `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/cli/publisher.py` `publisher_node`, after the recursive-validate-impl block (ends l.183) and before `# ── artifact contract` (l.184). Use a module-level `from mini_ork.verify import levels as _levels`. If `_levels.enabled()`: `rep = _levels.level_report(run_dir, task_class)`, printing to stdout (it lands in `execute.log`):
   - `refute` → print `  [BLOCK] publisher: level vector REFUTED (<k>=<v> …)` and `return 1, "levels_refuted"`. A demonstrated failure fails the run; the rollback it triggers is the right compensation, and is a no-op when applies is REFUTED.
   - `abstain` → print `  [ABSTAIN] publisher: level vector not proven (<k>=<v> …) — publish withheld`, `set_status(db, run_id, "failed")` (the module's late-binding wrapper), and `return 0, "levels_unverified"`. Nothing is committed.
   - `publish` → print `  [ok] publisher: level gate pass (required=<list|none>)` and fall through unchanged.
9. **Why the publisher, not the verifier rc.** A non-zero verifier rc fails the node and blocks the reviewer. The `test → rollback` `escalates_to` edge (`recipes/code-fix/workflow.yaml:90`; `main()` l.980-982) then runs `revert_branch`, which DESTROYS the abstained edit and breaks test.py's contract (abstention = "no roll-back, no certify"). Editing `_run_verifier_ref` would also change node outcomes for every recipe. The publisher is the only code that commits (`_publisher_try_commit_files`) and sets `published`. Abstain uses rc 0 because rc≠0 bumps `fail_count`, which triggers that same rollback. `failed` is the only non-success terminal status the `task_runs` CHECK allows; `executing` would look like a hung run.

## Files in scope (touch ONLY these)

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/verify/levels.py` (new): the derivation and the predicate.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/tests/unit/test_verify_levels.py` (new): pure tests and real-entrypoint tests.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/cli/execute.py`: `_emit_run_verdict` and its l.985 call only (Hook A).
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/mini_ork/cli/publisher.py`: the import and the `publisher_node` gate only (Hook B).

Read-only references, all under `/Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector/`. Do NOT edit them:
- `recipes/code-fix/verifiers/test.py`: payload shapes.
- `mini_ork/cli/execute_handlers.py`: the `_handle_implementer` / `_handle_verifier` / `_handle_publisher` / `_handle_rollback` handlers.
- `mini_ork/certify/oracle.py`: `replay_check`.
- `mini_ork/verify/behavioral.py`: `BehavioralVerdict`.
- `tests/unit/test_mini_ork_execute_py.py`: COPY, do not import, `_seed_db`, `_sql`, `_seed_task_run`, `_fake` and the `test_main_live_run_wired` env pattern.
- `tests/unit/test_codefix_replay.py` and `tests/unit/test_suite_adequacy.py`: fixture shapes.

Do NOT modify any other file.

## Tests

All tests go in `tests/unit/test_verify_levels.py`. They are hermetic: no LLM, no network, everything under `tmp_path`.

**Pure tests.** Each writes run-dir files in the producer's shape; `verifier_test.json` is a `[test] running: x` line plus a one-line payload.
1. applies: `implemented` + 1 file → PROVEN; the real `ex._write_implementer_summary` on an untouched repo (`no_changes`) → REFUTED; file missing → UNVERIFIED; `implemented` + `[]` → UNVERIFIED.
2. (executes, preserve): rc 0 → (PROVEN, PROVEN); rc 1 with base `"0"` → (PROVEN, REFUTED), the non-nested case; rc 2 with base `"0"` → (REFUTED, REFUTED); rc 2 with base `"2"` → (UNVERIFIED, UNVERIFIED); no `post_rc` → (UNVERIFIED, UNVERIFIED).
3. target: overlap `["t::a"]` + pass → PROVEN; overlap `[]` + `pass:false` → REFUTED; `replay_unverified` → UNVERIFIED; `adequacy_unverified` + overlap → target AND preserve UNVERIFIED; rc 0 + pass + no `replay` → UNVERIFIED; rc 1 → UNVERIFIED.
4. contract: absent → `"n/a"`; the real multi-line `BehavioralVerdict(status=REFUTED, surface="api").to_json()` → REFUTED, and its PROVEN twin → PROVEN; a `garbage` file → UNVERIFIED.
5. Empty run_dir: the four required levels are UNVERIFIED, contract is `"n/a"`, nothing is PROVEN, and the `code_fix` decision is `"abstain"`.
6. Predicate: target PROVEN + preserve REFUTED → `"refute"` and `all_levels_ok` False; with preserve UNVERIFIED → `"abstain"`; four PROVEN + contract `n/a` → `"publish"`; `required=("contract",)` with `n/a` → `"abstain"`; `required_levels("docs") == ()` → `"publish"`.
7. Knob and parser: `enabled` is False for `{}`, `"0"` and `"true"`, True for `"1"`; `read_verifier_payload` skips a trailing non-JSON line and an object with another `verifier`.
8. `ex._emit_run_verdict` on a run dir holding evidence: knob unset and `"0"` → bytes `== b'{"verdict":"pass","failed_nodes":0,"dispatched":3,"source":"execute@run-level"}\n'`; `"1"` → the same four pairs plus the five report keys; `dry_run=True` with the knob on → no `levels` key.

**Real-entrypoint tests** drive `ex.main([], root=str(REPO), dispatch_fn=_fake(""))`.

Setup:
- Workflow: `dispatch_mode: serial`, nodes `{name: test, type: verifier, description: t, verifier_ref: verifiers/test.py}` then `{name: publisher, type: publisher, description: p}`.
- Env: `MINI_ORK_RECIPE=code-fix` (so the REAL `recipes/code-fix/verifiers/test.py` runs through `_handle_verifier`), `MINI_ORK_HOME=<tmp>/home`, `MINI_ORK_RUN_DIR=<home>/runs/<rid>`, `MINI_ORK_RUN_ID=<rid>`, `MINI_ORK_DB` (via `db/init.sh`), `MINI_ORK_WORKFLOW`, `MINI_ORK_PLAN_PATH`, `MO_TARGET_CWD=<repo>`, `MINI_ORK_TEST_CMD`, `PYTHONPATH=REPO`, `PYTHONDONTWRITEBYTECODE=1`, `MO_ORACLE_GATES_AUTO=0`. `delenv` `MINI_ORK_RECIPE_ROOT`, `MO_CODEFIX_REPLAY`, `MO_TEST_BASELINE`.
- `plan.json` = `{"objective":"o","task_class":"code_fix","artifact_contract":{"outputs":[]}}`.
- Before `main`: run the real `ex._write_implementer_summary(rd, repo, "impl.log")`; write `review-verdict.json` `{"verdict": "pass"}` (stands in for the reviewer; the vector never reads it); seed the task_run row.
- Repo: HEAD commits `mod.py` (`add` returns `a - b`) and `test_mod.py` (a `unittest.TestCase` `test_add` asserting `add(2,3)==5`). The uncommitted fix is `a + b`.
- Commands: pytest = `f"{sys.executable} -m pytest -p no:cacheprovider"` (no `-q`); unittest = `f"{sys.executable} -m unittest"` (assert `"pytest"` is not in it).

Cases:
9. Strong, knob 1, pytest: rc 0; `levels` = four PROVEN + contract `n/a`, `levels_ok` true; a new HEAD commit `mini-ork(code-fix): …` with exactly `mod.py`; status `published`.
10. Replay abstention, knob 1, unittest: payload has `replay_unverified` true, `pass` false; `main` rc 0 and `failed_nodes` 0: the node still succeeds; `levels.target` UNVERIFIED, `levels_ok` false, decision `"abstain"`; HEAD unchanged and `mod.py` still modified (no rollback); status `failed`.
11. Test 10 with the knob unset: a new commit, status `published`, and `verdict.json` equal to the old literal bytes. Today's gap is unchanged.
12. Pre-existing red, knob 1, pytest; HEAD also commits `test_env.py` (`assert False`): payload has `pass` true, `post_rc` 1, `base_rc` `"1"`; executes PROVEN, target UNVERIFIED, preserve UNVERIFIED; no commit; status `failed`.
13. Adequacy, knob 1 + `MO_SUITE_ADEQUACY=1`, pytest; `mod.py` also has the untested vt5 `clamp` and `is_even`: `adequacy_unverified` true, `suite_adequacy.verdict == "INADEQUATE"`; decision `"abstain"`; no commit.
14. Refute: `ex.publisher_node(str(REPO), rd, "", "r", "code-fix", "code_fix")` with knob 1, a `no_changes` summary from the real writer, and the test-9 evidence: returns `(1, "levels_refuted")`; the monkeypatched `ex.set_status` never receives `published`; HEAD is unchanged.

## Success criteria

- The verification command exits 0, with every pre-existing test file in it UNMODIFIED.
- Knob unset: `verdict.json` bytes and publisher outcomes are unchanged (tests 8, 11).
- Knob on: an abstention or a pre-existing-red pass never commits and never reaches `published`, and the edit is not rolled back (tests 10, 12, 13).
- Only the four in-scope paths change. No new subcommand and no new required env var.
- Measurement: `jq -c 'select(.levels)|[.levels.target,.levels.preserve,.levels_ok]' <home>/runs/*/verdict.json` counts target-green runs that fail deeper levels.

## Verification command

```bash
cd /Volumes/docker-ssd/ps/mini-ork-worktrees/vt6-level-vector && python3.11 -m pytest -q -p no:cacheprovider tests/unit/test_verify_levels.py tests/unit/test_mini_ork_execute_py.py tests/unit/test_codefix_replay.py tests/unit/test_suite_adequacy.py tests/unit/test_codefix_no_change_py.py tests/unit/test_dryrun_artifact_shadowing_py.py tests/unit/test_workflow_artifacts_py.py
```

Baseline at a7516a5b, without the new file: 138 passed, about 150 s. Do NOT run the full suite; it hangs.

## Review bar

- Every input has a named production producer: applies ← `_write_implementer_summary`; executes / target / preserve ← `verifier_test.json` from `_handle_verifier`; contract ← `verifier_behavioral.json` from `BehavioralVerdict.to_json`; task_class ← `plan.json` in `main()` and the publisher argument; knob ← env. No parameter exists only for tests.
- Tests 9–13 run the real verifier inside `ex.main`. Reject stubbed `_run_verifier_ref` / `_handle_verifier` or a seeded `verifier_test.json` there.
- Reject any of these: a level read from a reviewer, panel, eval or rubric file; PROVEN from missing evidence; rc≠0 or a rollback on abstain; an edit to `_run_verifier_ref`, `_handle_verifier`, `test.py` or a recipe yaml; knob-off byte drift; `levels.py` importing `subprocess`, `mini_ork.cli` or `mini_ork.dispatch`.

## Rules

Edit files directly; do not emit unified diffs. Touch only the four in-scope files. Keep each hook a few lines; all logic lives in `levels.py`.
