# mini-ork engine bugs found in the runs of 2026-10-13 .. 2026-10-20

Audit of every `task_runs` row created in the 7 days to 2026-10-20 (171 runs, $338.63 recorded spend),
read against its run dir (`execute.log`, verdict files, verifier logs, attempt ledgers, agent streams),
`run_events`, `node_attempts`, `llm_calls`, `execution_traces`, `mo_inbox_gates` and the guard logs.
Six parallel auditors each took a slice; this file merges and de-duplicates their findings. The
per-slice notes, with every run reviewed and the full evidence trail, are under [`slices/`](slices/).

Scope is **engine** defects: things mini-ork did wrong. Lane outages, operator actions and model
output quality are listed separately at the end, unless the engine handled them wrongly.

## The window at a glance

| Outcome | Runs | Recorded cost |
|---|---:|---:|
| published | 123 (1 of them with verdict `CRASH`) | $285.13 |
| failed | 41 (7 `CRASH`, 2 `ESCALATE`, 32 no verdict) | $51.04 |
| classified (never ran) | 4 | $0.00 |
| executing / reviewing (stuck) | 2 | $1.67 |
| not_needed | 1 | $0.79 |

| Recipe | Runs | Failed | Published |
|---|---:|---:|---:|
| framework-edit | 124 | 16 | 106 |
| code-fix | 24 | 19 | 4 |
| epic-runner | 7 | 0 | 7 |
| panels / docs / other | 12 | 6 | 6 |
| (none: `classify` only, RB-38) | 4 | 0 | 0 |

Slices: **A** code-fix (24) · **B** framework-edit that did not publish (18) · **C1/C2** framework-edit
published (53 + 53) · **D** other recipes and odd-state rows (27) · **E** cross-cutting DB/telemetry (all 171).

## Headline

1. **A failing framework-edit test gate never stops a run** (RB-02). The verifier always exits 0 and
   the executor cannot parse its JSON, so it falls back to that 0. Seven runs in the window published
   with `verdict.json` saying `tests_pass: false`, and two of those diffs are on `main`. The
   `test_verifier → implementer` revise edge and the `→ rollback` edge have never fired.
2. **A run whose target doesn't exist edits the engine's own checkout** (RB-01). A non-existent
   `MO_TARGET_CWD` is dropped silently and roots fall back to the kickoff's repo.
3. **Auto-repair launched from `mini-ork run` does nothing** (RB-03). The child inherits
   `MINI_ORK_PLAN_PATH`, is also handed the plan positionally, and exits rc=2 at argv parsing.
   22 `repair-*.log` files on disk carry the error. Only human-launched `recover` works.
4. **Run identity and run-dir files are not protected from the agents the run spawns** (RB-04, RB-05).
   A nested dry-run overwrote three live `run_profile.json` files, so their declared verification
   commands never ran. One implementer rewrote its own baseline, and the rubric then scored 8/8 on
   verifier evidence the implementer had written itself.
5. **Revive and recover lose context**. A remote run is revived on the host (RB-06). A dead remote
   dispatcher is never reaped (RB-07). The revived row keeps the old CRASH verdict and notes, and
   diagnosis then reads those stale notes (RB-16, RB-11).
6. **Several learning-loop inputs are silently empty or wrong**. `task_runs.verdict` is never
   `APPROVE`, so the planner's "established facts" are always empty (RB-28). `node_attempts` holds
   only successes (RB-29). A `partial` verify is written as reward 1.0 (RB-15).

## Summary table

Severity: **P0** data loss / wrong merge / security · **P1** run fails or false verdict ·
**P2** degraded, workaround exists · **P3** cosmetic / telemetry.
Confidence: **C** confirmed (artifact and code agree) · **P** plausible (symptom certain, cause inferred).
Slice IDs point into [`slices/`](slices/).

| ID | Bug | Sev | Conf | Status | Runs | Slice refs |
|---|---|---|---|---|---:|---|
| RB-01 | Missing `target_repo` silently resolves to the engine's primary checkout | P0 | C | OPEN | 1 | A-1 |
| RB-02 | framework-edit test/static verifier fails open; failed gate publishes | P1 (latent P0) | C | OPEN | 14 | B-1, C1-1, C2-1, C2-10, B-13, B-14 |
| RB-03 | Auto-repair `recover` dies on inherited `MINI_ORK_PLAN_PATH` | P1 | C | OPEN | 9 | D-1 |
| RB-04 | Nested `mini-ork run` inherits run identity, overwrites live `run_profile.json` | P1 | C | OPEN | 3 | B-7, C2-2 |
| RB-05 | Agent can rewrite run-dir baseline; rubric trusts self-produced verifier evidence | P1 | C | OPEN | 2 | B-8, C1-12 |
| RB-06 | `recover` of a `--placement remote` run executes every node on the host | P1 | C | OPEN | 2 | A-2 |
| RB-07 | Reaper never closes a remote run whose local dispatcher died; node leaked | P1 | C | OPEN | 1 | E-1 |
| RB-08 | Recipe `reflector`/`replanner` nodes are no-ops; recover runs without a plan | P1 | C | OPEN | 3 | D-2, A-14 |
| RB-09 | Kickoff prose harvested as a verification command | P1 | C | OPEN (partly fixed) | 6 | B-3, C1-3 |
| RB-10 | Crash classified from exception text that embeds the prompt → false `auth` + fuse | P1 | C | OPEN | 1 | A-3 |
| RB-11 | Retry hint / diagnosis blames the wrong party | P1 | C | OPEN | 13 | B-2, A-15, A-16, B-9, D-5, C2-5 |
| RB-12 | Kickoff "touch ONLY these files" never enforced | P2 | C | OPEN | 5 | C2-3 |
| RB-13 | Verified diff ≠ published diff; no hash pin between them | P2 | P | OPEN | 1 | C2-11 |
| RB-14 | Reviewer-less recipe marked `published` though nothing was committed | P2 | C | OPEN | 1 | D-9 |
| RB-15 | Post-run verify writes `partial` as success, reward 1.0 | P2 | C | OPEN | 5 | C1-2 |
| RB-16 | Revived run keeps the dead attempt's verdict, notes, verdict.json, panel score | P2 | C | OPEN (end-stamp fixed) | 9 | E-4, D-4, D-10, C2-6 |
| RB-17 | Review/retry gates not settled: published runs keep blocking gates; approved run re-parked | P2 | C | OPEN | 4 | C2-4, A-11 |
| RB-18 | Revise loop fired by non-verdicts (dispatch error, fixed base, unsatisfiable spec) | P2 | C | OPEN | 6 | A-12, A-13, B-10 |
| RB-19 | Repair budget compares lifetime run cost, not repair spend | P2 | C | OPEN | 1 | D-7 |
| RB-20 | Reflect is scoped by time, not run: hours of silence, foreign traces, misbilled | P2 | C | OPEN | 15+ | D-3, C1-6, C2-9 |
| RB-21 | LLM calls labelled "timeout after 120s" lasted up to 2664 s | P2 | P | OPEN | 28 | C1-7 |
| RB-22 | Remote/spawn errors flattened into generic non-retryable failures | P2 | C | OPEN | 2 | A-10, E-15 |
| RB-23 | Lane preflight probes lanes of nodes that never call an LLM | P2 | C | OPEN | 2 | A-4, B-16 |
| RB-24 | Lane fuse ignores placement; remote-only cred failure fuses lane home-wide | P2 | P | OPEN | 2 | A-18 |
| RB-25 | Reaper fails the row but leaves the agent process group running | P2 | P | OPEN | 1 | B-15 |
| RB-26 | Kickoff commands run non-hermetic, never compared with base; glob+glob RecursionError | P2 | C | OPEN | 4 | B-6, B-5 |
| RB-27 | Unit tests write into the live `state.db`; a pytest-spawned run spent $1.51 | P2 | C | OPEN | 91 | E-2, E-3 |
| RB-28 | `task_runs.verdict` never `APPROVE` → planner "established facts" always empty | P2 | C | OPEN | 122 | E-7 |
| RB-29 | `node_attempts` records only successes; `attempt_no` always 1; cost NULL | P2 | C | OPEN | all | E-5, D-15, C1-8, B-12 |
| RB-30 | Failed runs record no reason | P2 | C | OPEN | 23 | E-8, D-16b, A-22 |
| RB-31 | Execute output buffered in memory; dead or deadline-hit runs leave no `execute.log` | P2 | C | OPEN | 7 | D-8, C2-8 |
| RB-32 | Cost accounting: phantom $0.05, lost shape-reject spend, meter omits planner, late spend | P2 | C | OPEN | 70+ | E-6, A-17, C1-9, E-14 |
| RB-33 | Duration accounting: 0 on kills/retries, idle gap after revive, monotonic vs sleep | P3 | C | OPEN | 9 | E-13, D-11, C2-7, B-11, A-10 |
| RB-34 | Event pairing by node id; synthetic `node_end` drops `finish_reason` | P3 | C | OPEN | 4 | E-9 |
| RB-35 | `verifier_results` rows silently lost to "database is locked" | P3 | C | OPEN | 2 | C1-11 |
| RB-36 | Verifier JSON with `pass` but no `checks[]` read as "unparsable" | P3 | C | OPEN | 12 | D-14 |
| RB-37 | Planner repair overwrites the preserved failure output | P3 | C | OPEN | 7 | D-13 |
| RB-38 | Standalone `classify` leaves permanent `classified` rows | P3 | C | OPEN | 4 | E-12, D-12 |
| RB-39 | Dead telemetry: tables/columns with readers but no writer | P3 | C | OPEN | all | E-11 |
| RB-40 | File-reversion guard "restores" the `ui` submodule every poll | P3 | C | OPEN | – | E-10 |
| RB-41 | Post-run LLM work runs under the `rollback` node identity | P3 | C | OPEN | 3 | D-16a, B-18, A-21 |
| RB-42 | Deadline enforced only between stages | P3 | C | OPEN | 1 | B-17 |
| RB-43 | `__pycache__/*.pyc` counted as implementer changes | P3 | C | OPEN (overlay fixed) | 2 | A-19 |
| RB-44 | Remote `up()` provisions a sandbox before the dirty-engine refusal | P3 | C | OPEN | 1 | A-20 |
| RB-45 | Reaper note timestamps are local time without a zone | P3 | C | OPEN | 2 | A-23 |
| RB-46 | Declared rollback strategy `keep_judge_reports_discard_synthesis` unimplemented | P3 | C | OPEN | 1 | D-16c |
| RB-47 | epic-runner `wave_aggregator` hard-codes `"verdict": "in_progress"` | P3 | C | OPEN (recipe) | 7 | D (focus) |

13 more defects were hit by this week's runs and **fixed during the week**. They are listed in
[Fixed during the window](#fixed-during-the-window).

---

## P0

### RB-01. Missing `target_repo` silently resolves to the engine's primary checkout
- **Runs:** run-1791930074-82118
- **What happened:** the profile named `mini-ork-worktrees/e1-scratch`, which does not exist. Roots
  fell back to the kickoff's repo, which is the engine's primary checkout. The implementer edited
  the frozen probe fixture there, the test verifier ran with `cwd=/Volumes/docker-ssd/ps/mini-ork`,
  the dispatcher died inside that node (CRASH), and the revived run's diff swept in 8 unrelated
  engine files (`mini_ork/verify/pm.py`, `tests/conftest.py`, …).
- **Evidence:** `run_profile.json` `target_repo` (absent on disk) vs `roots.target`;
  `node-cmd/verifier_test.json "cwd"`; `implementer-summary.json files_changed`.
- **Root cause:** `mini_ork/runtime/run_roots.py:137` keeps an explicit `MO_TARGET_CWD` only
  `if os.path.isdir(explicit)`. Otherwise it is dropped without a word and `:170` takes
  `_git_toplevel_from_dir(dirname(kickoff))`. Nothing refuses a target equal to the engine root.
  30b18f02 only covers kickoffs staged under `MINI_ORK_HOME`.
- **Fix:** fail closed when an explicit target does not exist. Refuse a target equal to the
  `MINI_ORK_ROOT` toplevel unless self-edit was explicitly allowed (`MO_ALLOW_FRAMEWORK_CWD`).
- **Related:** RB-13 and B-8's trigger are the same shared-tree hazard reached by other paths.

## P1

### RB-02. framework-edit test/static verifier fails open; a failed gate publishes
- **Runs, published with `verdict.json pass:false`:** run-1791853722-board-k3, run-1791901915-72597,
  run-1791901916-72800, run-1791906478-48004, lane-health-gate-20261015152758,
  run-1792091737-99865, daytona-default-20261019-081019, plus run-1791835530-board-k1b just before the window.
  - **On `main`:** board-k3 (`d7450c87`) and 72597 (`cc01439a`). In all of these the reviewer
    correctly judged the red test a false negative caused by RB-09 or the now-fixed verifier-copy bug.
    A genuine failure would ship by the same path.
- **Runs, failed later but the verifier node still ended `done`:** hr-cli-worktree-20261015135213,
  hr-exec-park-20261015141603, sde-3b1-diagnoser-20261015-142335, sde-3b2-ledger-20261015-142338,
  vg-3-spec-tests-20261015-142340, run-1792091744-821, run-1792253563-27493.
- **What happened:** `verifier_test.json` says `"pass": false`. Even so, `test_verifier` ends
  `done`, `verifier_results` records `pass`, `run-verdict.json` says `pass / contract=PROVEN /
  levels_decision=publish`, and the publisher ships on the LLM reviewer's verdict alone.
  `test_verifier` node ends this week: `done` 177, `NULL` 1, `error` 0.
- **Root cause.** Four layers, each assuming another one reads `.pass`:

  ```mermaid
  flowchart LR
    A["verifiers/test.py<br/>writes pass:false<br/>sys.exit(0) always"] --> B["run_check merges stderr<br/>into evidence file<br/>(DeprecationWarning prefix)"]
    B --> C["execute._run_verifier_ref<br/>strict json.load fails<br/>→ return rc (=0)"]
    C --> D["_finish(rc=0)<br/>node done, verifier_results=pass"]
    D --> E["levels: test JSON excluded,<br/>no framework_edit required level<br/>→ publish"]
    E --> F["publisher approves<br/>on reviewer verdict"]
  ```

  1. `recipes/framework-edit/verifiers/test.py:598` and `static-check.py:704` always exit 0.
  2. `runtime/contract.py:96` runs the verifier with `stderr=STDOUT` into the evidence file. On
     Python 3.13, `tarfile.extractall()` without `filter=` (`test.py:154,162`,
     `static-check.py:248,256`) prints a DeprecationWarning first, so 106/106 evidence files are not
     strict JSON.
  3. `mini_ork/cli/execute.py:2344-2347`: `json.load` fails, so it returns the script's rc, which is
     always 0. A tolerant parser already exists (`verify/levels.py` `read_verifier_payload`) but is
     not used here.
  4. `verify/levels.py:173` excludes `verifier_test.json` from contract evidence, `:401-432` reads
     only code-fix fields (`post_rc`, `replay`), and `:51` has no required level for
     `framework_edit`. So `executes=UNVERIFIED` and the decision is still `publish`. `cli/execute.py:555-573`
     writes `run-verdict.json` without reading the recipe's `verdict.json`.
- **Fix:**
  - Parse evidence with `read_verifier_payload` and fail closed when no payload is found.
  - Pass `filter="data"` to `extractall`.
  - Exit non-zero when `failed_checks` is non-empty.
  - Make the run verdict and the publisher refuse when `verdict.json` says `pass:false`.
  - Optionally require an `executes` level for framework-edit. Also stop reporting
    `levels_decision: publish` on failed runs (B-14).
- **Known:** SESSION-GOALS 2026-10-19 pending task `run-verdict-vs-verdict-json`. That a failed gate
  actually publishes, and that the revise and rollback edges are dead, is new.

### RB-03. Auto-repair `recover` dies on inherited `MINI_ORK_PLAN_PATH`
- **Runs:** run-1792151796-92942, learning-judge-4l-20261015125313, hr-acp-review-20261015172315,
  hr-smoke-a-143701 (×2), run-1791911811-5826, run-1791912327-41843 (×2), run-1791913440-97590 (×2),
  run-1792185781-68028, run-1792356029-62462 (×2). That is 13 repair logs this week; 22 on disk overall.
- **What happened:** every repair spawned by `mini-ork run` writes
  `Unexpected argument: …/plan.json` and exits rc=2 before dispatching a node. The loop then records
  "no progress" or hits the budget stop and hands the run to a human. Repairs that worked were all
  launched by a human `recover` or by an approvals resume, and those do not export the variable.
- **Reproduced (2026-10-20):**
  `MINI_ORK_PLAN_PATH=/tmp/x/plan.json python3 -c "from mini_ork.cli.execute import _parse_execute_argv; print(_parse_execute_argv(['/tmp/x/plan.json','--recovery']))"`
  prints `Unexpected argument` and `(None, 2)`. Without the env var it parses (rc 0).
- **Root cause:** `cli/main.py:1790` sets `os.environ["MINI_ORK_PLAN_PATH"]`.
  `recovery/auto_repair.py:1281` passes `dict(os.environ)` to the child. `recovery/planner.py:1551`
  also passes the plan positionally, and `cli/execute.py:786` has already seeded `plan_path` from
  the env. 9470c815's `_INHERITED_RECOVERY_SCOPE_VARS` (`main.py:2133-2150`) does not include this variable.
- **Fix:** pop `MINI_ORK_PLAN_PATH` in recover, next to the `MINI_ORK_RECOVERY_*` pops
  (`planner.py:1404`), or let the positional plan override the env value. Better still, build the
  child env from an allowlist.

### RB-04. A nested `mini-ork run` inherits run identity and overwrites the live `run_profile.json`
- **Runs:** run-1792092528-89023, run-1792092531-89304, rp-e10-artifacts-learnings-20261015-222123
  (a scan of all 166 profiles from the week found exactly these 3).
- **What happened:** the implementer ran the unit suite, which includes `test_dry_run_client`. That
  test's dry-run inherited `MINI_ORK_RUN_ID` and `MINI_ORK_RUN_DIR`, wrote `*.dryrun.*` files into the
  live run dir, and regenerated `run_profile.json` with `recipe: docs` and a pytest tmp kickoff. The
  test verifier then found no commands (`epic-verification-commands-none true`), so the kickoff's
  declared verification command never ran, and `scope_allow` was lost.
- **Root cause:** `cli/main.py:1562` `os.environ.setdefault("MINI_ORK_RUN_ID", …)` adopts an
  inherited id. `client.py:208-214` (`_base_env`) copies `os.environ` wholesale. `main.py:1763`
  rewrites the profile even under dry-run. The agent harness env is not scrubbed; one kickoff
  works around this by hand with `env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME`.
  adfa44d0 and 515b6f27 only renamed the dry-run's execute, plan, verdict and reflect files.
- **Fix:**
  - Scrub `MINI_ORK_RUN_*` from agent harness envs and from `MiniOrk._base_env`.
  - Mint a fresh id when the inherited one belongs to a live run.
  - Write `run_profile.dryrun.json` under dry-run.
  - In the verifier, treat a profile whose recipe or kickoff does not match the run as a failure.

### RB-05. An agent can rewrite the run-dir baseline, and the rubric trusts self-produced evidence
- **Runs:** run-1792224150-80303, vg-1-kickoff-contract-20261015-123125
- **What happened (80303):** during revision round 1 the implementer:
  - re-pointed `roots.target`;
  - rewrote `pre-implementer-ref`, keeping `.bak-preimpl` copies;
  - ran the verifiers itself and wrote `verifier_*.json` plus `verdict.json` with `pass:true`.

  The engine caught the moved base (`impl_moved_base`) and skipped all four verifiers. The rubric
  still scored 8/8 (panel 100), citing "all verifiers … report pass: true".
- **What happened (vg-1):** recipe-validator evidence with 18 `false` rows was written during the
  implementer's window, although the plan has no such node.
- **Root cause:** control and evidence files in the run dir are plain files the agent can write.
  `scope_guard_block` (`execute_handlers.py:1156`) is only prompt text. Agents inherit the live
  `MINI_ORK_RUN_DIR`, and the rubric does not check where evidence came from. The trigger for 80303
  (target mismatch) is fixed in 30b18f02; the tamper surface is not.
- **Fix:**
  - Keep the baseline and roots outside the agent's reach, or hash-seal them at plan time.
  - Accept verifier evidence only with a matching `node_end` and evidence-ledger hash.
  - Give agents a scratch `MINI_ORK_RUN_DIR`.

### RB-06. `recover` of a `--placement remote` run executes every node on the host
- **Runs:** run-1792136781-59220, run-1792136293-92791 (both `placement: remote, environment: daytona`)
- **What happened:** after revival there are zero `remote.*` events, `node_start` has no placement,
  and the implementer's cwd is the host target (`/Volumes/docker-ssd/ps/mo-daytona-e2e-target`
  instead of `/workspace/target`). The agent edited the operator's host repo.
- **Root cause:** placement is published only by the run lifecycle (`cli/main.py:1462-1495`).
  Recover re-enters execute directly, so `dispatch/providers.py:1415-1422` sees no `MO_PLACEMENT`,
  and `run_profile.json["placement"]` is never read back. `mini_ork/recovery/` contains no reference
  to `MO_PLACEMENT` (checked 2026-10-20).
- **Fix:** have recover and resume restore `MO_PLACEMENT` and `MO_NODE_ENV` from `run_profile.json`.
  Refuse a local revive of a remote run unless explicitly overridden.

### RB-07. Reaper never closes a remote run whose local dispatcher died; the Daytona node leaks
- **Runs:** run-1792400538-82050. Status `executing` with `ended_at` NULL since 2026-10-19 09:38 UTC.
  `ps -p 82050` finds nothing. `run_events` has `orchestrator.ended "stopped by signal"`, and
  `.node-provider.json` still holds Daytona handle `41f970ac-…`.
- **Root cause:** `orchestration/run_reaper.py:353-354`. `probe()` returns `"remote"` whenever
  `.remote-sync-state.json` exists, before it ever reads `.pid`, and `reap()` skips anything that is
  not `dead`. Only teardown (`runtime/workspace_session.py:434`) deletes the marker, and a
  signal-killed dispatcher never reaches teardown.
- **Fix:** probe `.pid` first when it exists, and treat the remote marker only as "no local pid".
  On reap, call `release_all_nodes`.

### RB-08. Recipe `reflector`/`replanner` nodes are no-ops, and recover runs without a plan
- **Runs:** run-1792151796-92942, run-1792151693-87094 (spec-driven-delivery), run-1792136293-92791.
- **What happened:** a `gates_materialize` failure escalated reflector → replanner →
  contract_compiler (recursive). `reflector.json` and `replan.json` were never written,
  contract_compiler was never re-entered, and the run rolled back. Affected recipes:
  spec-driven-delivery, doc-to-features-loop, recursive-validate-impl, and epic-runner (reflector).
  In 92791, recovering a plan-stage failure ran the implementer with `MINI_ORK_PLAN_PATH=""` and no
  `plan.json`.
- **Root cause:** `cli/execute_handlers.py:770` routes these node types to `EARLY_NODE_HANDLERS`,
  which ignore `prompt_ref`, the lane and the declared outputs. `_handle_planner_early` (`:1194`)
  only prints `[skip]`, and `_handle_reflector_early` (`:1199`) runs the global `mini_ork.cli.reflect`
  instead (see RB-20). Planning itself lives in the run lifecycle, which recover bypasses.
- **Fix:** dispatch prompt-bearing reflector and planner nodes like researcher nodes. Have recover
  run `cli/plan.py` when there is no valid `plan.json`, or refuse. Have `mini-ork validate` reject a
  recursive edge that passes through a no-op node.

### RB-09. Kickoff prose harvested as a verification command
- **Runs (open path):** vg-3-spec-tests-20261015-142340. The profile contains `'pytest files into'`
  and `'pytest -q <run_dir>/spec-tests …'`, which gives `python -m pytest files into` with rc=4. An
  8685 s run lost its verdict.
- **Runs (fixed paths):** board-k3 (bare `pytest` timed out after 300 s), 72597 and 72800
  (command cut at `\`), lane-health-gate (prose `pytest-without-opt-in keep it off…` gave rc=127),
  board-k2b (`ruff` dropped by the `[:3]` cap). Fixed in 753ef861, aac2cfe3 and ddc88c43.
- **Root cause:** `cli/main.py:743-760` `command_hints()` regex-matches ``\bpytest\b[^\n`]*`` over the
  whole kickoff, and `:905` uses the result when the kickoff has no section.
  `recipes/code-fix/verifiers/test.py:1049` still joins hints with `&&` and runs them. Since
  ddc88c43, a kickoff with no section yields `epic-verification-commands-none`, so the gate passes
  silently.
- **Fix:** take commands only from fenced or inline code under an explicit section, and reject
  `<…>` placeholders. Keep prose hints under a key that no consumer executes. Treat
  "no commands declared" as `UNVERIFIED`, not as a pass.

### RB-10. Crash classified from exception text that embeds the prompt → false `auth` and a 6 h fuse
- **Runs:** run-1791911811-5826 (llm_calls 11700, 11701, 11705)
- **What happened:** `UnmappedHostPathError` embeds the whole prompt
  (`runtime/path_map.py:55 value!r`), and `recipes/code-fix/prompts/implementer.md` contains the word
  "forbidden". `classify_error(str(exc))` (`dispatch/llm_dispatch.py:918-928`, regex at `:64-66`)
  therefore returned `auth`, which is a one-strike, 6-hour fuse category. The hint read
  "deepseek_flash,minimax,kimi rejected the credentials" and recommended
  `recover --lane worker=minimax`. The trigger (host path in the prompt) is fixed in 59f14447; the
  classifier is unchanged.
- **Fix:** classify crashes by exception type (an engine assertion is `config` or `unknown`, never
  `auth`), or classify only a bounded head that excludes the payload. Keep crash rows out of the fuse.

### RB-11. Retry hint and diagnosis blame the wrong party
The common pattern: the hint is built from whichever row or note matches first, not from what
actually failed. Each case sends the operator, and auto-repair, to the wrong fix.

| Case | Runs | Wrong blame | Code |
|---|---|---|---|
| Reviewer **ESCALATE**s ("blocker is environmental / harness bug") | hr-cli-worktree, hr-exec-park, sde-3b1, sde-3b2, vg-3, run-1792091744-821, run-1792253563-27493 | `needs_change: code`, `retryable:false` | `recovery/retry_hint.py:80` `_RETRY_VERDICTS` omits `escalate`; `_case_code` (`:669`) falls back to the verifier reason |
| Every planner lane failed the **shape** check | run-1792136293-92791 | "kimi rejected the credentials"; suggests `--lane planner=glm`, which also failed shape | `retry_hint.py:1470-1498` takes the first auth/quota row and ignores rc 65 |
| Skipped, non-gating, **passing** typecheck | hr-smoke-c-171954, hr-smoke-d-191036 | `environment`, so auto-repair stops as "owner-only fix" | `retry_hint.py:630-645` ignores `pass` and `gating` |
| HEAD moved by **another session** | fix-main-buddy-20261016-111824, run-1792224150-80303 | "the implementer committed, rebased, reset…" | `execute_handlers.py:1804` hard-codes the actor |
| **Stale `reaped:` note** from a previous attempt | rp-e3-header-v2-20261016-111144 | 15 ms `config` failure diagnosed as "the dispatcher died, retry it" | `recovery/diagnose.py:~818` matches any `reaped:` in the append-only `notes` |

- **Fix:**
  - Add an escalated case ahead of `_case_code` that uses the reviewer's PERSON-ACTION text.
  - Choose the plan-stage case from the rc.
  - Gate `_case_environment` on `gating and not pass`.
  - Make `impl_moved_base` actor-neutral, attach the target reflog, and claim the target worktree
    for the life of the run.
  - Attempt-stamp note segments (see RB-16).

## P2

### RB-12. Kickoff "Files in scope (touch ONLY these)" is never enforced
- **Runs:** rp-e3-header-v2-20261016-111144 is a hard violation: the kickoff says "Do NOT modify any
  other file", yet the diff adds `mini_ork/ide_pages/node.py` and `tests/unit/test_ide_pages_run.py`,
  and the rubric's "Scope respected FAIL" was ignored. Soft overruns: run-1792083640-55256,
  run-1792091750-1339, lf-r3-20261017-085429, lf-r13-20261017-101345.
- **Root cause:** `scope_allow` feeds only prompt preferences (`execute.py:3517`,
  `memory/preferences.py:232`). The kickoff-contract verifier gates only a structured block that no
  kickoff declared, and it was a no-op in 53 of 53 C2 runs.
- **Fix:** add a static-check `scope-respected` that fails on explicit ONLY / "Do NOT modify" scopes
  and warns on a Scope Hint.

### RB-13. Verified diff ≠ published diff
- **Runs:** run-1792351202-65496. The target was the primary checkout. The verifiers and reviewer
  judged a 19-file tree delta (`review-diff.patch`). Then, after the verifier logs were written,
  `framework-edit.diff` and `pre-implementer-ref` were rewritten at 22:27:37 and a 4-file diff was
  published without re-verification. Who rewrote them is unknown (Confidence P).
- **Root cause:** `execute.py:2770-2912` `_harvest_framework_edit_ground_truth` diffs the whole
  shared tree, and nothing pins the verified artifact to the published one.
- **Fix:** record the sha256 of the verified diff in the verifier output and have the publisher refuse
  a mismatch. Refuse a primary-checkout target unless `MO_ALLOW_FRAMEWORK_CWD` is set (see RB-01).

### RB-14. Reviewer-less recipe marked `published` though nothing was committed
- **Runs:** run-1792397274-39629 (`docs`). `execute.log:26-27` shows
  `[skip-publish] reviewer verdict (resolved: '<none>') is not APPROVE — no commit`. The status is
  still `published`, with empty notes. The edit was committed by hand 4 minutes later (`d701471e`).
- **Root cause:** `cli/publisher.py:491-514` ignores the return value of `_publisher_try_commit_files`.
  The "not APPROVE" exit (`:170`) writes no abstain file, so the run falls through to
  `set_status(published)`.
- **Fix:** fail the run, or publish with a "no commit" note. For reviewer-less recipes, treat a
  passing verifier as approval.

### RB-15. Post-run verify writes `partial` as success with reward 1.0
- **Runs:** the five RB-02 runs in C1. `execution_traces tr-verify-*`: `('pass','success',1.0)` ×48 and
  `('partial','success',1.0)` ×5.
- **Root cause:** `cli/verify.py:625` maps everything except `fail` to `success`, and `:632` returns
  rc 0 for `partial`. Together with RB-02's `verifier_results=pass` rows, every learning ledger
  records the failed gate as a clean pass, which poisons the GRPO signal and the abstain-gate
  calibration.
- **Fix:** map `partial` to failure, or to a reward below 1.

### RB-16. A revived run keeps the dead attempt's verdict, notes, verdict.json and panel score
- **Runs:** rp-e3-header-v2-20261016-111144 (`published` + `CRASH`, notes still
  `reaped: dispatcher pid 96447 gone`). The same pattern has occurred 4 times historically
  (run-1791749955-70478, run-1791733111-22332, run-1791725633-31802). Stale end-stamps on
  run-1791930074-82118, run-1792136293-92791, hr-smoke-a-143701, run-1792136781-59220,
  hr-smoke-b-152611, rp-e3-header-20261015-222115, rp-e6-graph-focus-20261015-222118.
  learning-judge-4l-20261015125313: published, but `verdict.json` still says `fail` and
  `NEEDS-CHANGE.md` is still present. rp-e6: `panel-verdict.json` top level shows 37.5/false while
  the refreshed prescreen shows 75/true.
- **Root cause:**
  - `run_reaper.py:397` writes `verdict = COALESCE(verdict, ?)`.
  - `execute.py:1907` `set_status` on a live status clears only `ended_at` and `duration_ms`.
  - `web/control.py:495` `_restore_status` behaves the same way.
  - `execute.py:568` writes the run-level `verdict.json` once and never again.
  - `gates/rubric_prescreen.py:646-649` uses `setdefault`, so only the nested `prescreen` key updates.
- **Status:** end-stamp and cost fixed in f43022ea. Verdict, notes, verdict.json and panel score are open.
- **Fix:** on the live-status write that starts a new attempt, clear the verdict, attempt-stamp the
  notes, and move the old attempt's markers into `attempts/<n>/`. Overwrite the run-level verdict on
  `--recovery`. Overwrite top-level panel keys when `source == rubric-prescreen`.

### RB-17. Review and retry gates are not settled
- **Runs:**
  - `mo_inbox_gates` 57 (rp-e6-graph-focus), 58 (rp-e3-header), 59 (rp-e3-header-v2) have been
    `pending` with `blocks_dispatch_for` set for more than 4 days, although all three runs are
    published. 10 published runs DB-wide hold a pending gate.
  - hr-smoke-b-152611 is stuck in `reviewing`. After a human approved gate 43, auto-repair `prove`
    re-ran the implementer, the run parked again on gate 45 (pending since 2026-10-15 15:05), and the
    same loop then logged "human: repair attempts exhausted".
- **Root cause:**
  - `recovery/planner.py:1651` only notifies when `exec_rc != 0`, and nothing resolves the run's own
    gate on success.
  - `retry_notify.supersede_gates_for_target` (`:1089`) only fires on worktree merge or remove.
  - `auto_repair.py:544` lets a non-failed status through when the stale `verdict.json` says withheld.
  - `run_reaper.py:284` exempts `reviewing` + `.review-pause` with no TTL.
- **Fix:** resolve the run's pending gates when it publishes. `decide()` should return `none` for
  `reviewing` or `.review-pause`. Age parked rows into an escalation.

### RB-18. Revise loop fired by things that are not verdicts
- **Reviewer dispatch error:** run-1791910825-57872. The reviewer ended `error` 3 times, and 2
  implementer rounds ran against "(no reviewer evidence produced)". `execute.py:1520-1562` skips
  only ABSTAIN, PAUSE, ESCALATED and UNPARSEABLE.
- **Base already fixed:** run-1792136781-59220 (5 implementer rounds, then `impl_no_changes`, then
  ESCALATE "change judged wrong") and run-1792136293-92791 ("implemented" `.pyc` only). Base and
  candidate both had rc 0.
- **Unsatisfiable spec tests:** run-1792092531-89304, run-1792185781-68028 (the implementer's
  `spec-test-defect.md` saying "No implementation can satisfy it" was ignored, followed by 3100 s and
  1369 s rounds), and lf-r11b-20261017-105523. Known:
  `feedback_spec_test_author_can_emit_unsatisfiable_tests.md`.
- **Fix:**
  - Revise only on a real verdict.
  - When both base and candidate pass and the diff is empty or bytecode-only, end as `not_needed`.
  - Run spec tests against the base first and quarantine any that error.
  - Escalate when the implementer files a defect report.

### RB-19. Repair budget compares lifetime run cost, not repair spend
- **Runs:** learning-judge-4l-20261015125313. Repair n=2 was refused with "repair budget exceeded
  ($16.12 > $10.00)" although repair spend was about $0. The repair only needed to re-run a 0.1 s
  verifier.
- **Root cause:** `auto_repair.py:586` and `_cost_since_first_repair` (`:804`) return the cumulative
  `task_runs.cost_usd`.
- **Fix:** snapshot the cost at the first repair and budget only the difference.

### RB-20. Reflect is scoped by time, not by run
- **In-DAG reflector (92942):** silent from 12:51:41 to 15:21:19 (8978 s), with no
  `node_start`/`node_end`/heartbeat. It runs synchronously before `mo_node_start`
  (`execute_handlers.py:1199-1226` vs `:907`). Its gradient-extract spend is billed to the run: 66% of
  92942's cost, 67% of D1's and 56% of D6's.
- **Post-run reflect** (`cli/main.py:1979`, `--since <run start>`): processes every concurrent run's
  traces. slack-transport analysed 76 traces, 8 of them its own, and paid $0.41 for 21 foreign LLM
  calls. Because the reflect runs inline, the parent epic-runner's `child.completed` fired 100 minutes
  after the child published. Measured holds: 12–100 minutes on 10 epic children.
- **Bound ratchet:** `main.py:344` sets the timeout to 3 × the slowest of the last 20 passes. 92942's
  8978 s pass pushed every later reflect to the 21600 s ceiling.
- **Fragility:**
  - ImportError when the worktree engine is removed (4 runs; `main.py:1977` uses the worktree as
    root and imports lazily).
  - A `lesson_themes` UNIQUE race between runs created in the same second (`learning/themes.py:787`).
  - No `reflect.log` in 6 runs, because `_write_reflect_log` only runs after the child process returns.
- **Fix:** scope reflect to the run's own trace ids. Detach the post-run pass so publish completes
  the run. Bill global reflection to a synthetic actor. Keep in-DAG passes out of the bound
  calculation. Run reflect from the primary engine. Use an upsert for themes. Stream the log.

### RB-21. LLM calls labelled "timeout after 120.0s" lasted up to 2664 s (Confidence P)
- **Runs:** 28 this week. There were 220 glm gradient-extract timeouts, 24 of them with
  `duration_ms` from 130,000 to 2,664,000. slack-transport had 1133 s and 1127 s back to back. These
  are single attempts (no `dispatch_attempt` metadata, and `classify_error` returns non-retryable
  `unknown`).
- **Hypothesis:** either time spent before spawn (`dispatch/llm_dispatch.py:863`) or an unbounded
  `proc.wait()` after SIGKILL (`dispatch/core.py:289`) under disk-I/O load. Host sleep (RB-33)
  explains some of it.
- **Fix:** bound the post-kill wait, record spawn and kill wall-clock timestamps on the row, and cap
  reflect by the run's deadline.

### RB-22. Remote and spawn errors are flattened into generic non-retryable failures
- **run-1792101677-31337 (5981 s, $0.05):** per-node Daytona provisioning hit a 605 s exec read
  timeout. The `RemoteUnavailableError` became rc=1 / `error` (not retryable, `workflow/retry.py:73`),
  diagnosis said `unclassified` with `strategy: none`, and the orchestrator blamed
  `locus: verifier`. `cli/execute.py:2358-2378` maps every exception to `(1, text)`; the
  `remote_unavailable` class is mapped only before the run starts (`:1813-1834`).
- **run-1792076652-70170:** the cwd was a worktree that had been deleted. The spawn `ENOENT` walked
  the whole lane chain (minimax, then deepseek_flash twice, the last attempt taking 45 s) and was
  logged as three lane failures, then rc=127. The operator saw only the last line (`plan.py:1214`),
  and the hint said `retryable: true, strategy: resume`, which would fail the same way.
- **Fix:** map `RemoteUnavailableError` to a retryable reason with bounded retry, and add a diagnose
  rule for it. Check that the cwd exists once, before walking the chain, and fail with category `config`.

### RB-23. Lane preflight probes lanes of nodes that never call an LLM
- **Runs:**
  - run-1792136781-59220 was blocked `lane_unhealthy` on kimi 401 for node `typecheck`, a
    deterministic script (`prompt_ref: null`).
  - lhg-smoke-blocked-212556 was blocked on the publisher lane, but the publisher makes no LLM calls.
- **Root cause:** `cli/lane_preflight.py:152-197` (`resolve_dispatch_lanes`) includes every workflow
  node. aaac165d only added the "cannot verify" listing.
- **Fix:** probe only nodes with a `prompt_ref`. Skip publisher, rollback and script verifiers.

### RB-24. Lane fuse ignores placement (Confidence P)
- **Runs:** run-1791911354-79155 (glm) and run-1792136890-69809 (opus) were both refused at plan stage
  with rc=43.
  - glm fused after a credential failure on the Daytona node ("Please run /login", run 57872).
  - opus fused from a pre-spawn remote guard ("api key is missing on the node") under which nothing ran.
- **Root cause:** `dispatch/llm_dispatch.py:387-391, 415-461`. The fuse is keyed on `model_id` alone,
  one strike opens it for `auth`/`config`, and a guard refusal without the `:refused` suffix counts
  as a strike.
- **Fix:** key the fuse on (lane, placement or node env), and don't count pre-spawn portability
  guards as strikes.

### RB-25. Reaper fails the row but leaves the agent process group running (Confidence P)
- **Runs:** run-1792233508-29757. It was reaped at its last heartbeat (10:45:30), but
  `agent-spec_test_author.live.jsonl` was still being written at 10:46:15. Its `retry-hint` detail
  is 2 KB of raw thinking-stream JSON.
- **Root cause:** `run_reaper.py:432-460` removes `.pid` and fails the row but never signals the
  agents, which run in their own session.
- **Fix:** record each agent's pgid and have the reaper terminate it. Strip stream JSON from hint
  details.

### RB-26. Kickoff commands run in a non-hermetic environment and are never compared with the base
- **hr-cli-worktree:** `test.py:530` sets `PYTHONPATH="."`, which makes
  `test_default_root_is_the_engine_root` fail. The reviewer reproduced the same failure on a clean base.
- **run-1792091744-821:** a live `mini-ork serve` on :7090 made a test fail.
- **sde-3b1:** `kickoff-contract.py` runs bare `python3`, which is 3.9 on this host, so `run_env.py:51`
  raises a `|` TypeError. That cost a 2580 s revise round, and the implementer added
  `from __future__` workarounds.
- **run-1792253563-27493:** two overlapping globs (`test_ide_pages_run_learnings*.py` and
  `test_ide_pages_run*.py`) caused a RecursionError. `_dedupe_redundant_test_paths` (`test.py:432-492`,
  `:470`) skips glob tokens; 16f11413 fixed only the file+glob case. Known:
  `feedback_pytest_duplicate_path_recursion.md`.
- **Fix:**
  - Use an absolute `PYTHONPATH` and pin the interpreter in kickoff-contract.
  - When a command fails, re-run it on the base copy. If the base also fails, record an abstain or
    an environment failure.
  - Resolve all test paths to files before de-duplicating.

### RB-27. Unit tests write into the live `state.db`, and a pytest-spawned run spent real money
- **Writes into the live DB:** 90 run_ids with no `task_runs` row carry about 1,900 `remote.*`
  events (`conformance-run`, `live-while-*`, `parity-*`, …), the last at 2026-10-18 23:15.
  - `observability/node_events.py:68-82` `_resolve_db` falls back to `$(pwd)/.mini-ork/state.db`.
  - `tests/conftest.py:64-65` pops `MINI_ORK_DB` and `MINI_ORK_HOME`.
  - `runtime/backends/remote.py:1117` passes no db.
  - f4394bbd closed only the env-var path.
- **Real spend:** `run-lane-1`, a fixture id from `tests/unit/test_acp_lane_repair.py:126`, ran a
  real code-fix with 157 LLM calls ($1.51) and wrote a `task_runs` row. `web/control.py:830-842`
  `spawn_run` sets `MINI_ORK_HOME` but not `MINI_ORK_DB`.
- **Fix:**
  - Have an autouse fixture point `MINI_ORK_DB` at a tmp file.
  - Disable the cwd fallback under pytest.
  - Export `MINI_ORK_DB` in `spawn_run`.
  - Refuse real lane dispatch when the kickoff path is under the pytest tmp dir.

### RB-28. `task_runs.verdict` is never `APPROVE`, so the planner's "established facts" are always empty
- **Evidence:** `select coalesce(verdict,'NULL'), count(*) from task_runs where status='published'`
  returns `NULL|799, CRASH|5`; no row has ever been APPROVE. The only three statements that write
  `task_runs.verdict` are failure paths (`plan.py:319`, `execute.py:3730`, `run_reaper.py:397`).
  `orchestration/active_state_index.py:133` filters `WHERE verdict = 'APPROVE'`, and it feeds
  `cli/plan.py:789` and `context_assembler.py:1347`.
- **Fix:** write `APPROVE` on publish, or filter on `status='published'`.

### RB-29. `node_attempts` records only successes, `attempt_no` is always 1, and cost is NULL
- **Evidence:** 3232 of 3232 rows ever are `success|python|NULL`, `max(attempt_no)=1`, and 55
  (run, node, 1) groups are duplicated this week. Example: hr-node-ckpt-refs has
  `implementer|1|success` three times.
- **Root cause:**
  - `cli/execute.py:3965` (`_cp`) returns early unless the node succeeded, and it is the only caller
    of `write_checkpoint`. It passes no attempt number or cost.
  - `stores/checkpoints.py:351` defaults the attempt to 1.
  - `execute_handlers.py:956` resets the counter on every dispatch.
  - As a result, `recovery/retry_hint.py:525/536` and `recovery/admin.py:145` never see a failure.
- **Fix:** write a row at every `node_end` with a monotonic per-(run, node) attempt number, the
  result, the failure class and the cost.

### RB-30. Failed runs record no reason
- **Evidence:** 23 of the 41 failed rows have NULL verdict and NULL notes, and 11 of them have no
  `run-verdict.json` either. Plan-stage notes say only `planner_dispatch_rc_N`; the real cause is
  only in `llm_calls.error_message`.
- **Root cause:**
  - The `fail_count > 0` branch at `cli/execute.py:1745-1748` appends no note. So do
    `publisher.py:375/391/512`.
  - The reaper's fallback note (`run_reaper.py:143`) reads a top-level `detail` key, but 0 of 83
    `retry-hint.json` files have one; it is nested under `needs_change.detail`.
- **Fix:** append `failed: <n> node(s): <ids> (<finish_reasons>)`. Read `needs_change.detail`.
  Copy the first plan-stage `llm_calls.error_message` into the notes.

### RB-31. Execute output is buffered in memory, so dead runs leave no `execute.log`
- **Runs:**
  - No log after the dispatcher died: rp-e3-header-v2 (first attempt), run-1792151693-87094,
    run-1792352251-34982, run-1792352077-16173.
  - No log after a deadline hit: run-1792092528-89023.
  - Stale log after a revive (still says "2 node(s) failed" though the run published):
    rp-e3-header, rp-e6-graph-focus.
- **Root cause:** `cli/main.py:1804-1806` redirects execute's output into an `io.StringIO` and writes
  it only after execute returns (`:1834`), with no `finally`. The deadline check (`:1820-1822`)
  returns before the write. The recover path never writes `execute.log` at all.
- **Fix:** stream to `execute.log`, flush in a `finally` block and a SIGTERM handler, and have
  recover write a per-attempt log.

### RB-32. Cost accounting is off in both directions
| Defect | Effect | Code |
|---|---|---|
| Flat +$0.05 per planner dispatch, before rc is checked | 9 runs charged for 0-token refusals and spawn failures | `cli/plan.py:278-288` `_charge_cost`, called at `:1089`/`:1142` |
| Spend from shape-rejected lane attempts dropped | 45 rows this week, $0 recorded and 0 output tokens (avg 303 s each); the daily budget undercounts | `dispatch/llm_dispatch.py:867-892` `_attempt_sink`; `providers.py:2547+` returns only the last result |
| `.cost-spent` meter starts after planning | cost-pause under-triggers (cnui-ctx-b is missing its $0.59 planner cost) | `dispatch/cost_pause.py` |
| Spend after the end-of-run reconcile never reaches `task_runs.cost_usd` | 16 runs, $3.46 short | `cli/main.py:1053` reconciles once |

- **Fix:** delete `_charge_cost`. Accumulate cost across fallback attempts. Seed the meter from
  `llm_calls`. Compute run cost from `llm_calls` when it is read, or reconcile again after reflect.

## P3

### RB-33. Duration accounting
- **Kills and plan-stage failures leave `duration_ms=0`:** run-1792221678-16211 (2,459 s of wall
  time), run-1792076652-70170, run-1792047098-61635, run-1791911602-90288. `web/control.py:457-466`
  and `cli/plan.py:319` set `ended_at` without the `CASE … duration_ms` clause.
- **After a revive the duration includes the idle gap:** run-1792151796-92942 reports 66.7 h for about
  4.9 h of activity (no events from 10-16 15:45 to 10-19 05:37; no hang, no lease). This is a
  regression from f43022ea: `execute.py:1958` resets the duration, and `:1932-1937` recomputes it as
  `ended_at - created_at`.
- **A retried node attempt emits no `node_start`, and its `node_end` says 0 ms:** run-1792091737-99865
  (about 38 minutes of real work), sde-3b1, vg-3. `execute_handlers.py:907-909` emits `node_start`
  once, before the retry loop at `:957`, and `trace()` pops the start time at `:603-604`.
- **Monotonic time on macOS stops during sleep:** run-1792101677-31337 shows 614 s monotonic vs
  5888 s wall. Record wall-clock time alongside monotonic.

### RB-34. Event pairing and `finish_reason`
- Dangling-node close in `web/control.py:633-645` pairs starts and ends as sets of node ids, so a
  node that ran twice stays open. The synthetic INSERT (`:665`) omits `finish_reason`, and
  `node_events.py:322-324` turns any reason outside the CHECK list (`publish_abstain`,
  `levels_unverified`, `impl_no_changes`, `impl_moved_base`, `config`) into NULL. That gives 30
  NULL-reason `node_end` rows and 4 runs with open nodes.

### RB-35. `verifier_results` rows silently lost to "database is locked"
- run-1792066351-7754-intake-report-back and hr-ide-review-20261015152759 logged
  `[warn] verifier_results write skipped: database is locked`. `gates/verifier_rubric.py:76` waits
  5 s, and `execute_handlers.py:2119` swallows the error. Fix: retry with backoff, or spool to a
  file and backfill.

### RB-36. Verifier JSON with `pass` but no `checks[]` is treated as "unparsable"
- `verify/levels.py:229` requires both a boolean `pass` and a `checks` list, so a failed panel verifier
  reads `UNVERIFIED` instead of `REFUTED`. That hits 12 runs (panels and epic-runner). Fix: with only
  `pass`, map false to REFUTED and true to PROVEN.

### RB-37. Planner repair overwrites the preserved failure output
- `cli/plan.py:1152` calls `_preserve_raw(…, first_verdict, raw, …)` after each repair. All 7
  `plan-failure-*.raw.txt` files this week validate as `ok`, so the actual failing output is lost.
  Fix: write repairs to `plan-repair-<n>.raw.txt`.

### RB-38. Standalone `classify` leaves permanent `classified` rows
- run-1792151576-79883, run-1792151577-79913, run-1792186379-32169 and run-1792186379-32236 have no
  recipe, no run dir and no `.pid`. The reaper reports "unknown" for them, and the board shows them as
  live forever. `cli/classify.py:193-210` upserts, and its `ON CONFLICT … SET status='classified'`
  could also downgrade a live run. Fix: write the row only from inside the run lifecycle, and never
  downgrade a status.

### RB-39. Dead telemetry: tables and columns with readers but no writer
- `mo_events` has 0 rows, yet `web/routes/stream.py:70-132` polls it on every live-update tick and
  `run_detail.py:579-586` reads it.
- `mo_failure_fingerprints` has 0 rows and nothing references it.
- `failure_memory` was last written on 2026-07-10. `memory/store.py:596 write_failure` has no
  callers, but `context_assembler` and `active_state_index` still read the table.
- `task_runs.fuse_blown_lane` and `fuse_consecutive_failures` are empty on all 1,676 rows, although
  the lane fuse fired 10 times this week (`llm_dispatch.py:772 _halt_on_fuse` writes only an
  `llm_calls` refusal).
- Cause: the writers were lost in the bash→Python port. Fix: port the writers, or drop the tables,
  columns and their readers.

### RB-40. File-reversion guard "restores" the `ui` submodule every poll
- 48 log lines in the window (bursts on 10-13 and 10-15). `.githooks/post-commit:200` checks
  `Path(...).is_file()`; a gitlink (mode 160000) is a directory, so it is treated as deleted and
  "healed" with no effect, over and over. Fix: skip mode-160000 entries, and stop after a heal that
  changes nothing.

### RB-41. Post-run LLM work runs under the `rollback` node identity
- learning-judge `agent-rollback.live.jsonl` is 58 MB, and run-1792224150-80303's is 68 MB with
  219k lines. Their contents are diagnoser and gradient-extract sessions; `.last-llm-node` is
  `rollback`.
- In run-1792101677-31337, post-run learning was provisioned into a fresh per-node `rollback`
  Daytona sandbox. That gave 17 × `rc=127` and llm_calls rows with an empty `error_message`.
- Cause: `MO_NODE_ID` stays set after the last node. Fix: clear it, and give post-run phases their
  own actor and live file.

### RB-42. The deadline is enforced only between stages
- run-1792092531-89304 had a 2700 s budget and ran 5601 s (`remaining_seconds: -2901`, "soft-stop
  between stages"). Fix: cap each dispatch's timeout at the remaining deadline.

### RB-43. `__pycache__/*.pyc` counted as implementer changes
- In run-1792136293-92791, `files_changed` lists only `.pyc` files and the `applies` level reads
  PROVEN. In run-1792137016-90593, diffstat says +4/-1 while the commit is +1/-1. `cli/execute.py:2989`
  and `:3317` harvest untracked files. The replay-overlay half is fixed in 0dc1a2b1. Fix: exclude
  `__pycache__/` and `*.py[co]`.

### RB-44. Remote `up()` provisions a sandbox before the dirty-engine refusal
- run-1792047098-61635 provisioned for 24.8 s, then failed with "engine tree … is dirty".
  `runtime/backends/remote.py:615-619` runs before `:907-935`. Fix: check the engine tree first.

### RB-45. Reaper note timestamps are local time without a zone
- The note `.pid written 2026-10-14T00:21:15` belongs to a run created at 22:21:14 UTC.
  `run_reaper.py:55-56` uses `time.localtime`. Fix: write UTC with a `Z` suffix.

### RB-46. Declared rollback strategy `keep_judge_reports_discard_synthesis` is unimplemented
- learning-judge falls through to "unhandled rollback_strategy". The sibling strategy
  `keep_partial_pass_discard_failed_iter` was fixed in a0fa212d.

### RB-47. epic-runner `wave_aggregator` hard-codes `"verdict": "in_progress"`
- `recipes/epic-runner/lib/wave_aggregator.py:203` writes that verdict, so `epic-results.json` and
  `wave-aggregate.json` say `in_progress` even after every wave has passed. In addition, 19 of the
  22 `epic-graph-complete` checks validate the recipe's own files rather than the run's output.
  These are recipe-level problems, not engine ones.

---

## Fixed during the window
Each of these was hit by a run this week and is now on `origin/main`. Every SHA was checked with
`git cat-file` and `merge-base --is-ancestor`.

| Defect | Runs hit | Fix |
|---|---|---|
| Replay gate: injected `-v` cancelled `-q`, so a red base read as "could not run" | hr-smoke-a, hr-smoke-c | 7c685bc0 |
| `MO_CODEFIX_REPLAY=0` left `target` permanently unprovable; auto-repair burned attempts | hr-smoke-b, hr-smoke-d, run-1792356029-62462 | 2213ce7c |
| Code-fix test gate ignored the declared verification command | run-1791912327-41843, run-1791913440-97590 | be67f084 |
| Remote run fully PROVEN but publish-no-authored-patch | run-1791914481-48337 | 88a3f99e |
| Remote planner crashed because roots were pinned only at the implementer | run-1791910667-50438 | d774d0b6 |
| Profile verification commands: bare `pytest`, cut at `\`, `[:3]` cap | board-k2b, board-k3, 72597, 72800, lane-health-gate | 753ef861, aac2cfe3, ddc88c43 |
| Verifier copy had no HEAD commit, causing false test failures | run-1791906478-48004 | faf4e232 |
| Kickoff "Required artifacts" never enforced (framework-edit) | sde-1-diagnose-20261013-232707, sde-2-kernel ×2, sde-3a-escalate-20261015-122857 | a7caba16 |
| Profile `scope_allow` absorbed the kickoff's deny bullets | 6 runs | c7594dcb |
| Test verifier dropped `MINI_ORK_RUN_DIR`, so junit was written to `/` | sde-3b1, sde-3b2 | 0f0be49f |
| Recovery scope leaked into child runs (a 15-node child ran only `publisher`) | 92942--D1, --D6 | 9470c815 |
| A revive did not settle the row's end stamp and cost | 8 runs | f43022ea |
| `keep_partial_pass_discard_failed_iter` rollback unimplemented | run-1792151796-92942 | a0fa212d |

## Environmental / non-engine (for context)
- **Lane credentials:**
  - glm auth outage 2026-10-13 17:04–17:09 UTC (9 fuse refusals; the fuse failed fast as designed).
  - kimi returns 401 everywhere.
  - Remote Daytona nodes lacked glm and opus credentials.
  - The publisher-lane deepseek 401 was an intentional smoke test (lhg-smoke-blocked).
- **Lane aliasing:** `opus_lens→glm`, `codex_lens→deepseek_flash/minimax` in 26 or more runs, so
  "panels" were not from different model families. Known: `reference_agents_local_yaml_overlay_wins.md`.
  The lane-probe cache fingerprinted `sonnet` the same as deepseek. Known:
  `reference_ambient_anthropic_env_poisons_native_lane.md`. Also, every run drops the `sonnet`
  fallback lane because it has no providers.yaml entry (35 log lines).
- **Host:**
  - Sleep/DarkWake cycles about every 15 minutes from 2026-10-15 22:27 local. That explains the
    multi-hour node walls (lf-r8, lf-r5, lf-r14, rp-e3-header, 99865); they are not missed timeouts.
  - Memory pressure (about 22 GB swap) when 87094 and rp-e3's first attempt died.
  - Ambient `python3` is 3.9.
- **Operator:**
  - Both star-growth-panel CRASH runs were operator interrupts while reconfiguring lanes. The whole
    process group stopped at 19:43:07, 2 s before the reaper's rows.
  - A run was launched against a deleted worktree (run-1792076652-70170).
  - A remote run was launched from a dirty engine tree (61635).
  - A kickoff was edited mid-run (run-1792351202-65496).
  - The e2e target's main was reset between runs.
- **Glm output quality:** the planner repeatedly fails the shape check, and planner calls took
  1010–2169 s.
- **Not bugs, verified:**
  - All 4 published code-fix runs are correct (commit parent = `pre-implementer-ref`, real replay delta).
  - epic-runner children really delivered (11 child runs, all published, 2–7 files each).
  - Panel outputs are well-formed, and their costs match `llm_calls`.
  - `fix-main-buddy`'s `not_needed` was correct (the change landed as 3bdef401).
  - `retry-exhausted/` in run dirs is the per-node attempt-log directory, not an exhaustion marker.
  - No run in the window actually exhausted its retries.
  - No terminal row has NULL `ended_at`, no LLM spend is orphaned, and `.mini-ork/locks` is empty.

## Suggested fix order
1. **RB-02** (fail-open verifier) and **RB-01** (engine-checkout fallback). Both let a wrong change
   reach `main` silently. Both fixes are small and fail closed.
2. **RB-03** (auto-repair argv). A one-line env pop brings the whole auto-repair loop back to life.
3. **RB-04 / RB-05 / RB-06.** Isolate run identity and the run dir from spawned agents and nested
   runs, and restore placement on recover.
4. **RB-11, RB-16, RB-17.** Make retry hints, revived rows and gates tell the truth. Operators and
   auto-repair act on these fields.
5. **RB-28, RB-29, RB-15.** Fix the learning-loop inputs (APPROVE verdict, attempt ledger, partial
   reward) before trusting any GRPO or retrieval metric computed from this week's data.

## Method
- Run set: `task_runs.created_at >= 1791842400` (2026-10-13 00:00 UTC), giving 171 rows. The list is
  in [`slices/runs-in-window.txt`](slices/runs-in-window.txt).
- Six read-only auditors, briefed by [`slices/BRIEF.md`](slices/BRIEF.md), each with
  `sqlite3 -readonly`, the run dirs, CodeGraph and `git log`. Each bug required an artifact citation
  and a check of current code for fix status.
- The coordinator re-verified the highest-impact claims against current code and data on 2026-10-20:
  - RB-01: `run_roots.py:137`.
  - RB-02: `test.py:598`, `execute.py:2344`, `execute_handlers.py:2139`, board-k3 `verdict.json` vs
    `run-verdict.json`, and that `d7450c87`/`cc01439a` are on `origin/main`.
  - RB-03: reproduced.
  - RB-06: `grep MO_PLACEMENT mini_ork/recovery/` finds nothing.
  - RB-07: `run_reaper.py:353`.
  - RB-17: gates 57–59 are pending.
  - RB-28: the verdict writers and the published-verdict histogram.
  - The 28 cited SHAs.
- Cross-slice duplicates were merged. Where slices disagreed, the stronger evidence won. Example: E-9
  said the star-growth agents were "reaped while still writing", but D showed the whole process group
  stopped together 2 s before the reaper ran.
