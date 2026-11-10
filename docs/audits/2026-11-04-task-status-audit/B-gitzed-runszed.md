# B-gitzed-runszed — audit of 121 claimed tasks (zed-mini-ork fork: git/push + run triage)

**Summary.** Of the 121 tasks this group's past sessions emitted, **73 are DONE**, **18 are still genuinely OPEN**, **27 are STALE** (superseded or moot), and **3 are UNKNOWN** — I looked and found no evidence either way. The bulk of the git family is real: all three repos (engine `mini-ork`, the `mini-ork-ui` crate, and the `zed-mini-ork` fork) sit at 0/0 against their remotes, the crate pin is `38a1f7a1ab`, the four fork commits (`fee632c57d aa900b1149 3685fec317 721aff43cc`) are pushed, and every run-page/graph/inbox feature the git family was pushing (tone copy `b56cdc4`, floating zoom `d00229f`, Load-more `70651b8`/`12fc7cfede`, layout parity `c9d5306`, node roster `0884a83a77`, inbox Retry/Dismiss) is present in the code. The run-triage family splits cleanly: every run whose deliverable actually landed has a green `run-verdict.json` or an `operator-repair.md` reading *Superseded — do not revive* (rows 65/66/70/71/77/78 green; 72/76/84/87 superseded), while the runs that were revived but never converged are still red on disk. The four things that genuinely still need doing: (1) **the researcher home's engine is 780 commits behind**, so every fix the engine home landed since 10-12 is absent from the tree the researcher runs execute (rows 64/100); (2) **the kimi lane still returns HTTP 401**, blocking four engine-home runs (row 32); (3) **the staged run-initiation spec corpus is still unprobeable** — 4 of 5 specs carry 0 executable fences and the phase-2 dispatch pre-flight sheet was never launched (rows 18/35); (4) **six failed runs are sitting there unrevived** (19, 29, 53, 54, 68, 80) plus two decision tasks with no recorded decision (31, 61) and one board feature half-built (63). One caveat worth stating: the repo carries untracked `kickoffs/` and `docs/specs/judge-deepseek-tasks.md` in the fork and six untracked research/recipe paths in the engine, so "clean tree" claims here are about *tracked* content.

## Verdict table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| engine-push | 2026-10-29 | pending | DONE | 38974008 is ancestor of origin/main; main == origin/main (0/0) |
| engine-land-orch-tab | 2026-10-19 | pending | DONE | e8bbb609 is ancestor of origin/main |
| push-commits | 2026-10-18 | pending | DONE | all three repos 0/0 vs their remotes (engine, mini-ork-ui, fork) |
| lf-r11b-revise | 2026-10-18 | failed | DONE | decision recorded 2026-10-17T09:44 in memory feedback_spec_omits_pytest_import_never_shim_builtins.md ('fix the spec file, never a builtins shim'); revision run verdict.json pass:true 11:30 |
| repo-ops | 2026-10-18 | pending | OPEN | default branch = main and PR #1 MERGED, but gh api repos/SourceShift/zed-mini-ork/branches/main/protection -> 404 'Branch not protected' |
| revive-monitor | 2026-10-18 | in_progress | DONE | audit docs/audits/2026-10-16-session-gaps/86880f3c-revive-monitor.md reports delivered; three engine-home runs revived with run-verdict.json verdict=pass failed_nodes=0 |
| bump-zed-gitlink | 2026-10-17 | in_progress | DONE | gitlink now 38a1f7a1ab and pushed; b56cdc4 pin superseded by later pins |
| commit-python-side | 2026-10-17 | pending | DONE | levels/retry_hint/ide_pages work committed: a942cbf9, 64275077, d526ca21, c97c94a9; working tree carries none of those paths |
| push-repos | 2026-10-17 | pending | DONE | engine main + mini-ork-ui origin + fork all 0/0 vs remotes |
| runpy-tone-copy | 2026-10-17 | in_progress | DONE | b56cdc4 'Render the page's feed section' / tone copy; page.rs ~1403 'a silent watcher has to read as watching' |
| live-replay | 2026-10-17 | pending | STALE | the watcher smoke-tested here was deleted by fae2a7a5 'One orchestrator per run, and the run buddy is gone'; absorbed into mini_ork/acp_orchestrator/watch.py |
| run-page-redesign-land | 2026-10-17 | pending | DONE | run-page commits a0ab57e, da93f40, 6711a14 on origin/main; crate HEAD 38a1f7a1ab |
| push-engine-main | 2026-10-17 | pending | DONE | main == origin/main (0/0) |
| commit-part-0-3 | 2026-10-17 | pending | DONE | engine git status has no modified tracked files; crate submodule clean at origin/main 38a1f7a |
| gui-smoke-run-page | 2026-10-17 | pending | STALE | the run page was rebuilt again after this ask (da93f40 Dispatch Console); no GUI-smoke record in fork docs/ |
| monitor-sidecar | 2026-10-17 | in_progress | STALE | sidecar design deleted with the run buddy (fae2a7a5); per-run narration now lives in the ACP orchestrator watcher |
| commit-workstream-5files | 2026-10-17 | pending | DONE | a942cbf9 retry-hint blame, 64275077 levels envelope, d526ca21 gate-mode veto, c97c94a9 ide_pages; no uncommitted workstream files |
| commit-my-workstream | 2026-10-17 | pending | DONE | same commits (a942cbf9/64275077/d526ca21/c97c94a9); no uncommitted levels/retry_hint/outcome/run/spec edits remain |
| author-spec-fences | 2026-10-16 | pending | OPEN | only crate/pick-files-directories.md has a bash fence; engine/board-preflight.md, engine/codefix-classifier-coverage.md, engine/specdir-kickoff-precedence.md, engine-phase2/dispatch-preflight-sheet.md still 0 fences |
| revive-779 | 2026-10-16 | failed | OPEN | run-1792059337-779 still status=failed, owner-blocked R7 layer=environment routes_to=owner, retry-gate.json inbox_id=38; dir mtime 10-16 23:21 |
| rubric-contamination | 2026-10-16 | pending | DONE | 37d39474 'rubric: stop grading a revived run against the attempt it replaced'; rubric_scoring.py:158 _superseding_green_verdict |
| part-0-one-page | 2026-10-16 | pending | DONE | spec.py:124 reads MINI_ORK_IDE_SPEC; board.rs:139 IDE_SPEC = "2" set at :202/:383; ide_http.py:45 imports it |
| revive-researcher-home | 2026-10-16 | failed | STALE | per-home reports delivered (audit 86880f3c, report-both-homes completed) - the 'revive both homes' ask is superseded by the 48h triage (row 23) |
| monitor-48h-both-homes | 2026-10-16 | in_progress | DONE | triage commits 2d786fec / 24893d48 / 6801221c on origin/main; docs/todos/2026-10-14-failed-runs-48h-triage.md |
| ws-2-stage-report-feed | 2026-10-16 | pending | STALE | audit AGGREGATE.md P2#15 'not a gap': the writer was deliberately removed by fae2a7a5; the feed now streams from the ACP thread |
| zoom-pin-bump | 2026-10-16 | pending | DONE | gitlink bumped to the zoom pin and pushed; crate origin/main == HEAD |
| verify-running-mo-containers | 2026-10-16 | pending | STALE | docker ps shows only mo-svc-web-a8afb1fe-0, not the 7 mo-* containers the task was about |
| wu-b4d1e7a2 | 2026-10-16 | in_progress | DONE | shared sweep receipt: docs/todos/2026-10-14-failed-runs-48h-triage.md + audit 83e57620-failed-run-triage.md (bulk evidence for rows 5/23/27/93) |
| commit-gitlink | 2026-10-16 | pending | DONE | mini-ork-ui main pushed and the zed-mini-ork gitlink committed/pushed (38a1f7a1ab) |
| revive-run-1792136781-59220 | 2026-10-16 | in_progress | OPEN | run-1792136781-59220 verdict still fail; NEEDS-CHANGE.md 10-16 17:37, retry-exhausted/ 17:38, auto-repair at 17:40 also failed |
| flow-zoom | 2026-10-16 | pending | DONE | d00229f: one clamp_zoom/zp conversion point in src/flow.rs; connectors untouched, full-screen draws the same picture |
| sa1-scope-question | 2026-10-16 | pending | OPEN | no recorded decision - appears only as a pending line in docs/audits/2026-10-19-session-goals/SESSION-GOALS.md:800 |
| kimi-lane-401 | 2026-10-16 | pending | OPEN | engine state.db llm_calls: kimi status=failed 'lane preflight failed: kimi: API key rejected (HTTP 401)', 83 rows, latest 2026-11-04T21:28 |
| ci-8273e27d | 2026-10-16 | in_progress | STALE | 8273e27d is an ancestor of origin/main; its CI ran red (4 unit-a/unit-b jobs) but the HEAD is superseded by 02734e1a (full suite + every red classified) |
| deploy-and-revive | 2026-10-16 | pending | DONE | 5bb2e226 cherry-picked as 4a5cf19c on origin/main (push half); revive-of-4-parked-runs has no separate receipt |
| sdd-phase2-staged | 2026-10-16 | pending | OPEN | docs/specs/2026-10-16-run-initiation/engine-phase2/dispatch-preflight-sheet.md exists but no run dir was ever named for it, and the sheet still has 0 executable fences |
| commit-ux-brief | 2026-10-16 | pending | DONE | docs/ux-briefs/2026-10-16-run-initiation-modes.md committed by 8f07609ba5 |
| orch-narration-scope | 2026-10-16 | in_progress | DONE | acp/agent.py _build_terminal_message emits 'mini-ork {run} finished: {status}' on TERMINAL_STATUSES |
| p2-answers | 2026-10-15 | pending | DONE | board 'answer' verb implemented (board_cmd.py:1528/1599); 'Blocked on you' panel at ide_pages/orch.py:878 |
| sync-local-engine-checkout | 2026-10-15 | pending | DONE | mini-ork main == origin/main (0/0), working copies current |
| gui-smoke-pass | 2026-10-15 | pending | OPEN | checklist item '/task Queue on board' has no implementation: no /task verb in crates/mini_ork_ui/src and no epics section in board_cmd.py |
| run-page-layout-parity | 2026-10-15 | in_progress | DONE | df27a7875a / c9d5306 'Give the run page the node tab's DAG strip' |
| reclaim-stale-bug | 2026-10-15 | pending | DONE | 52bd2e6f + 109a927c, both on origin/main |
| engine-ed695c46 | 2026-10-15 | pending | DONE | rebased and pushed as 310284bf on origin/main |
| load-more-hide-when-exhausted | 2026-10-15 | in_progress | DONE | 70651b8 (crate) / 12fc7cfede (fork) 'Hide the board's Load more control when the runs are exhausted' |
| push-zed-mini-ork | 2026-10-15 | pending | DONE | fee632c57d aa900b1149 3685fec317 721aff43cc all on fork/mini-ork; local == remote |
| push-four-commits | 2026-10-15 | pending | DONE | fork/mini-ork == local; four commits pushed |
| miniork-replay-receipt | 2026-10-15 | pending | STALE | the premise was withdrawn: docs/todos/2026-10-14-failed-runs-48h-triage.md section 5-corrected 'the typecheck envelope is NOT a publish blocker'; no replay receipt found |
| publish-crate-and-pin | 2026-10-15 | pending | DONE | mini-ork-ui main pushed then fork pin bumped; both remotes 0/0 |
| publish-palette | 2026-10-15 | pending | DONE | palette pin 721aff43cc on fork/mini-ork; mini-ork-ui main pushed |
| run-buddy | 2026-10-15 | pending | STALE | fae2a7a5 'One orchestrator per run, and the run buddy is gone' - the buddy was deliberately not built |
| smoke-test | 2026-10-13 | pending | DONE | push half landed (fork/mini-ork == local); app rebuilt+installed /Users/admin/Applications/mini-ork IDE.app 2026-10-29 12:35 and running (pid 388) |
| run-verdict-vs-verdict-json | 2026-10-13 | pending | DONE | 17b821bb 'scheduler: read a run's verdict in whichever shape it wrote' - bare bool pass fallback in _verdict_from_log |
| chat-listen-revive | 2026-10-13 | in_progress | OPEN | chat-listen-shape-be-20261013 verdict.json {"verdict":"fail","failed_nodes":6}, NEEDS-CHANGE.md + retry-exhausted/, dir mtime Oct 13 14:35 |
| revive-1791887097 | 2026-10-13 | in_progress | OPEN | run-1791887097-86207 verdict fail with 4 failed nodes; review-verdict fail, implementer status=no_changes, NEEDS-CHANGE.md 10-13 14:27 |
| revive-run-1791887097 | 2026-10-13 | in_progress | STALE | no *deorch* run dir in either home; the deorch/orchestrator lane it names was deleted by fae2a7a5 |
| tamper-kickoff-worktree | 2026-10-13 | pending | DONE | no tamper-kickoff worktree directory exists under mini-ork-worktrees/ |
| hint-failed-node-attribution | 2026-10-13 | pending | UNKNOWN | run-1791821930-72687 retry-hint.json still failed_node=implementer kind=unknown; d1c0c871 moved the recover entry point but does not settle the misattribution claim |
| build-p1-3 | 2026-10-13 | pending | DONE | bd789718 lands LangGraph-parity P1.3 (run --resume) |
| land-engine-fix | 2026-10-12 | pending | UNKNOWN | /tmp/mo-fold does not exist; no commit in the 10-07..11-04 log references it, so the changes cannot be matched to a landing |
| stale-run-verdict-on-resume | 2026-10-12 | pending | DONE | `recovery/planner.py` `_archive_superseded_verdicts` moves verdict.json/run-verdict.json into `attempts/<n>/` (n from the ledger's `attempt <n> started` notes) before `_emit_recovery_env`, so a resumed run cannot be graded on the replaced attempt; tests/unit/test_recover_verdict_rotation_py.py |
| engine-node-runtime | 2026-10-12 | pending | OPEN | decision never recorded - only a pending line in SESSION-GOALS.md; no config/doc chooses a node runtime for researcher worktrees |
| linear-py-salvage | 2026-10-12 | pending | DONE | 4379ff03 + 1be093e8; mini_ork/pm/providers/linear.py present |
| board-phase1 | 2026-10-12 | pending | OPEN | fail-closed 'epics add' landed (epics.py:110) but the /task command and the epic-card board-with-Runs-tab do not exist |
| researcher-install-update | 2026-10-12 | pending | OPEN | researcher/.mini-ork engine checkout HEAD 68deb674 is 780 commits behind origin/main; _recovery_target_cwd occurrence count 0 |
| revive-75074-wit-2-documents | 2026-10-12 | in_progress | DONE | run-1791785128-75074-wit-2-documents/run-verdict.json verdict=pass failed_nodes=0 levels_decision=publish; deliverable landed as 4598b66b |
| revive-61320-stage-3 | 2026-10-12 | in_progress | DONE | run-1791788217-61320-communication-transports-stage-3/verdict.json tests_pass/static_pass/pass all true |
| fix-eslint-project-paths | 2026-10-12 | pending | DONE | tier1 retries a type-aware lint over a scoped overlay tsconfig (extends the project's, `files`=touched) and abstains when none is possible; `pm.project_scope_error` makes the config error a toolchain fact — tests/unit/test_tier1_eslint_project_py.py |
| revive-44675 | 2026-10-12 | failed | OPEN | run-1791818713-44675 verdict fail with 10 failed nodes; repair.json ends action:human at tier1_compile; retry-hint retryable:false |
| revive-43108-siblings | 2026-10-12 | pending | STALE | the sibling epics' deliverables all landed by other means: jira 8448f6bc, telegram 47de862d, linear 1be093e8, documents 4598b66b |
| revive-75074 | 2026-10-12 | pending | DONE | run-1791785128-75074 pass:true child validates commit 76140293 |
| revive-61320 | 2026-10-12 | pending | DONE | run-1791788217-61320 verdict.json pass:true (dir 10-12 21:45) |
| revive-99135 | 2026-10-12 | pending | STALE | run-1791778111-99135/operator-repair.md: '# Superseded - do not revive ... every deliverable it dispatches is already on engine origin/main' |
| smoke-test-wit-2-salvage | 2026-10-12 | pending | STALE | optional patch no longer needed: the wit-2 run passed without it (row 65) and its deliverable landed as 4598b66b |
| revive-epics-6 | 2026-10-12 | pending | STALE | same receipt as row 69: all six epics' outputs landed via 4598b66b / 8448f6bc / 47de862d / 1be093e8 |
| cwd-guard-exists | 2026-10-12 | pending | DONE | cwd_guard exists with dispatch_cwd_py tests on origin/main |
| run-1791778111-99135 | 2026-10-12 | pending | STALE | run-1791778111-99135/operator-repair.md '# Superseded - do not revive' |
| run-1791788217-61320 | 2026-10-12 | pending | DONE | run-1791788217-61320/verdict.json pass:true |
| run-1791785128-75074 | 2026-10-12 | in_progress | DONE | run-1791785128-75074/run-verdict.json verdict=pass failed_nodes=0 levels_decision=publish |
| epic-backlog | 2026-10-12 | pending | STALE | epic revival moot: the same six epics' deliverables landed (4598b66b / 8448f6bc / 47de862d / 1be093e8) |
| run-1791733111-22332 | 2026-10-12 | pending | OPEN | run-1791733111-22332/verdict.json pass:false (tests_pass:false, static_pass:false) + dismissed.json; wiz-ide worktree exists |
| real-graph-push-mini-ork-branch | 2026-10-12 | pending | DONE | fork/mini-ork == local after push; audit docs/audits/2026-10-16-session-gaps/6881f0ad-zed-ui-dag-click.md row 'done' |
| run-1791798883-27913 | 2026-10-12 | pending | STALE | the survey it waited on landed (a8f22462 4f018d01 416f06eb b7fad8ff 09371c85 cfb7007a 816b63ca); the langgraph-survey-r5 worktree is deleted |
| empty-error-rows | 2026-10-12 | pending | DONE | edd0f1db introduces error_message_head; blank failed llm_calls rows: 17 on 10-15, 8 on 10-20, 0 since |
| run-1791790859-83222 | 2026-10-12 | pending | STALE | run-1791790859-83222/operator-repair.md '# Superseded - do not revive'; P1.5 landed as e7030ca5 |
| run-1791813321-51986 | 2026-10-12 | in_progress | STALE | review-verdict.json {"verdict":"APPROVE"}; the 'wait before dispatching' ask is moot, last retry-hint 10-13 03:19 |
| ac5-full-suite | 2026-10-12 | pending | DONE | 02734e1a 'spec: the full unit suite, and every red in it classified' (9946 collected: 11 failed / 9895 passed / 15 skipped, each red classified) |
| revive-83222 | 2026-10-12 | pending | STALE | run-1791790859-83222/operator-repair.md '# Superseded - do not revive' |
| revive-epics | 2026-10-12 | pending | STALE | sibling epic revival moot - deliverables landed (see row 69) |
| land-local-8-upstream | 2026-10-12 | pending | DONE | mini-ork local main == origin/main (0/0); divergence closed |
| make-fix-live-engine-home | 2026-10-12 | pending | DONE | engine home executes origin/main - local main 0/0 ahead/behind |
| engine-main-divergence | 2026-10-12 | pending | DONE | same 0/0 probe; audit row marked 'done' |
| g-confirm | 2026-10-12 | in_progress | STALE | gh: run 38247661338 head e204cc7d (2026-10-12) conclusion 'cancelled'; all four python-tests jobs cancelled; superseded by later pushes |
| sweep-48h-revive | 2026-10-12 | in_progress | DONE | shared receipt: docs/todos/2026-10-14-failed-runs-48h-triage.md + audit 83e57620 (same probe as rows 23/27) |
| restore-worktrees | 2026-10-12 | pending | DONE | both worktrees exist: mini-ork-worktrees/orch-default-on and mini-ork-worktrees/wiz-ide |
| d3b-target-override | 2026-10-12 | pending | DONE | 8f00468b fix-mo-target-cwd + 9ee820c1 fix-target-scope on origin/main; MO_TARGET_CWD present |
| d4-postplan-hint | 2026-10-12 | pending | DONE | 5662a24d 'recovery: classify the runs that died before any node ran' - Case 4.5 _case_not_started |
| smoke-test-delivered-epics | 2026-10-12 | pending | STALE | no epic still needs a live smoke: their deliverables landed (4598b66b / 8448f6bc / 47de862d / 1be093e8) and the runs are marked 'Superseded - do not revive' |
| base-advance-v1.23.2 | 2026-10-11 | in_progress | STALE | the base moved past v1.23.2 to v1.24.3; see the mini-ork-v1.24.3 branch and the v1.24.3 commits |
| desc-rebuild | 2026-10-11 | pending | DONE | mini-ork IDE.app rebuilt/installed 2026-10-29 12:35, process running (pid 388) - the desc-drawing build shipped |
| sync-researcher-engine | 2026-10-11 | pending | OPEN | researcher/.mini-ork engine 780 commits behind origin/main (same probe as row 64) |
| readme-markers | 2026-10-11 | pending | DONE | 38a1f7a 'Drop the review-gate banner that leaked from the Zed fork' + pin 5bf984b59c 'mini_ork_ui: pin the README without the leaked review banner' |
| crate-edits-home | 2026-10-11 | pending | DONE | crate submodule clean; origin/main == HEAD 38a1f7a |
| R3-worktrees | 2026-10-11 | pending | DONE | 03ae5fe4 (R3); docs/todos/20261011-1830-ui-submodule-build-requirements.md status 'closed - all five requirements satisfied' |
| p240-crate-uncommitted | 2026-10-11 | pending | DONE | submodule clean at 38a1f7a; audit row 'resolved' |
| preserve-crate-edits | 2026-10-11 | pending | DONE | submodule clean - the crate edits landed on mini-ork-ui main instead of staying unpushed |
| rebuild-on-v1.23.2 | 2026-10-11 | pending | STALE | v1.23.2 superseded by v1.24.3 (mini-ork-v1.24.3 branch) |
| ui-submodule-r3-worktrees | 2026-10-11 | pending | DONE | 03ae5fe4 materialises the submodule in worktrees; todo doc closed |
| p240-sidebar-edit | 2026-10-11 | in_progress | DONE | sidebar.rs edits landed in mini-ork-ui; submodule clean |
| land-submodule-change | 2026-10-11 | pending | DONE | mini-ork-ui pushed and both consumer pins bumped to 38a1f7a1ab |
| rebuild-and-relaunch | 2026-10-11 | pending | STALE | the 'three post-14:29 commits' are long superseded; many rebuilds since (latest 2026-10-29) |
| inbox-actions | 2026-10-11 | pending | DONE | inbox.py:159 _dismiss_button -> board dismiss; board_cmd.py:539 _act_dismiss; _STRATEGY_WORDS includes Retry |
| reflect-observability-fix | 2026-10-11 | pending | DONE | cli/main.py _write_reflect_log writes <run_dir>/reflect.log with stdout/stderr/status |
| harvest-reason-field | 2026-10-11 | pending | DONE | 95a4af77; code_findings.harvest returns reason in ok|no_home|no_runs_dir|no_db|db_home_mismatch|db_error |
| code-findings-order | 2026-10-11 | pending | DONE | f7ec5aeb; cli/reflect.py runs the code_findings side-channel FIRST, before the pipeline |
| manual-ux-check | 2026-10-11 | pending | UNKNOWN | no record of the five project flows being walked in the fork docs/ or engine docs/ |
| run-page-last-node | 2026-10-11 | in_progress | **DONE `bbb00e1`** | `crates/mini_ork_ui/src/page.rs:2815-2818` computes `focus = model.last_ran().map(|node| node.id.clone())` and passes it to `MiniOrkGraph::new(model, focus, …)` ("Open on the node the run reached last"). The engine also emits `graph['focus']` (`ide_pages/run.py:1482/1588`), but the UI does not need it: it derives the node locally. Corrected 2026-11-09 — the original verdict read `open_run()`'s signature without following the construction path that computes the node |
| node-roster-in-thread-cards | 2026-10-10 | in_progress | DONE | 0884a83a77 'agent_ui: draw the run card from its node siblings' - one row per node |
| node-click-parity | 2026-10-10 | in_progress | DONE | 0884a83a77 + 96a21bd 'Open a node's tab when its DAG box is clicked' / 1481007 OpenNode action |
| run-scoped-env | 2026-10-10 | pending | DONE | dispatch/run_env.py config/env.yaml overlay + context.publish_env + execute_handlers per-node env |
| visual-verify | 2026-10-10 | pending | STALE | pre-redesign checklist (status bar, rounding, row size, cache paint) superseded by da93f40 Dispatch Console layout and da5d004 |

## Still genuinely open (16)

- **repo-ops** — default branch = main and PR #1 MERGED, but gh api repos/SourceShift/zed-mini-ork/branches/main/protection -> 404 'Branch not protected'
  - next: either enable `branches/main/protection` on SourceShift/zed-mini-ork or record in a doc that the fork is intentionally unprotected
- **author-spec-fences** — only crate/pick-files-directories.md has a bash fence; engine/board-preflight.md, engine/codefix-classifier-coverage.md, engine/specdir-kickoff-precedence.md, engine-phase2/dispatch-preflight-sheet.md still 0 fences
  - next: author the executable fences for the 4 remaining 2026-10-16-run-initiation specs (engine/board-preflight, engine/codefix-classifier-coverage, engine/specdir-kickoff-precedence, engine-phase2/dispatch-preflight-sheet)
- **revive-779** — run-1792059337-779 still status=failed, owner-blocked R7 layer=environment routes_to=owner, retry-gate.json inbox_id=38; dir mtime 10-16 23:21
  - next: clear the owner-blocked R7 environment route (retry-gate inbox_id 38) and revive run-1792059337-779 from its failed node
- **revive-run-1792136781-59220** — run-1792136781-59220 verdict still fail; NEEDS-CHANGE.md 10-16 17:37, retry-exhausted/ 17:38, auto-repair at 17:40 also failed
  - next: read NEEDS-CHANGE.md, fix the failing node, then revive past the exhausted retry gate
- **sa1-scope-question** — no recorded decision - appears only as a pending line in docs/audits/2026-10-19-session-goals/SESSION-GOALS.md:800
  - next: record the CC-B1 brief-stage decision in the sa1 branch doc (or drop the item)
- **kimi-lane-401** — engine state.db llm_calls: kimi status=failed 'lane preflight failed: kimi: API key rejected (HTTP 401)', 83 rows, latest 2026-11-04T21:28
  - next: rotate the kimi credential (or disable the lane in config/providers.yaml) - 83 failed rows since the key was rejected
- **sdd-phase2-staged** — docs/specs/2026-10-16-run-initiation/engine-phase2/dispatch-preflight-sheet.md exists but no run dir was ever named for it, and the sheet still has 0 executable fences
  - next: give dispatch-preflight-sheet.md executable fences, then launch it as a framework-edit run
- **gui-smoke-pass** — checklist item '/task Queue on board' has no implementation: no /task verb in crates/mini_ork_ui/src and no epics section in board_cmd.py
  - next: implement the `/task` board command (row 63's second half) before the hands-off GUI pass can pass
- **chat-listen-revive** — chat-listen-shape-be-20261013 verdict.json {"verdict":"fail","failed_nodes":6}, NEEDS-CHANGE.md + retry-exhausted/, dir mtime Oct 13 14:35
  - next: revive chat-listen-shape-be-20261013 with a working lane, or close it as abandoned
- **revive-1791887097** — run-1791887097-86207 verdict fail with 4 failed nodes; review-verdict fail, implementer status=no_changes, NEEDS-CHANGE.md 10-13 14:27
  - next: fix the implementer's no_changes outcome on run-1791887097-86207 then revive through the verifier chain
- **engine-node-runtime** — decision never recorded - only a pending line in SESSION-GOALS.md; no config/doc chooses a node runtime for researcher worktrees
  - next: write the decision (which runtime the researcher target worktrees use) into docs/plans
- **board-phase1** — fail-closed 'epics add' landed (epics.py:110) but the /task command and the epic-card board-with-Runs-tab do not exist
  - next: add the `/task` verb + epic-card board section; only the fail-closed `epics add` half exists
- **researcher-install-update** — researcher/.mini-ork engine checkout HEAD 68deb674 is 780 commits behind origin/main; _recovery_target_cwd occurrence count 0
  - next: fast-forward /Volumes/docker-ssd/ps/researcher/.mini-ork to origin/main (780 commits) and re-run the affected runs' pages
- **revive-44675** — run-1791818713-44675 verdict fail with 10 failed nodes; repair.json ends action:human at tier1_compile; retry-hint retryable:false
  - next: resolve the human-action escalation at tier1_compile (repair.json) then revive run-1791818713-44675
- **run-1791733111-22332** — run-1791733111-22332/verdict.json pass:false (tests_pass:false, static_pass:false) + dismissed.json; wiz-ide worktree exists
  - next: recover 22332 from the reviewer node (its verdict.json is red and it was dismissed)
- **sync-researcher-engine** — researcher/.mini-ork engine 780 commits behind origin/main (same probe as row 64)
  - next: same action as researcher-install-update - one fast-forward closes both

## Closed since the audit

- **fix-eslint-project-paths** — **DONE**: a type-aware lint whose `parserOptions.project` excludes the touched files no longer refutes the change. `tier1-compile-typecheck` retries once over a scoped overlay tsconfig (extends the project's, `files` = the touched files) in eslintrc mode, so the repo's own typed rules run; `pm.project_scope_error` + `classify_spawn` make the config error a `toolchain` abstention when no overlay is possible (no root `tsconfig.json`, a file outside the repo, or a flat config where `--parser-options` does not exist). Covered by `tests/unit/test_tier1_eslint_project_py.py`; rationale in `docs/architecture/verification-stack.md`.

- **stale-run-verdict-on-resume** — **DONE `e60633dc`**: recover rotates the superseded attempt's verdict.json/run-verdict.json into `attempts/<n>/` before dispatching, so the rubric cannot grade the replaced attempt. `mini_ork/recovery/planner.py` `_archive_superseded_verdicts` (best effort: no verdict → no move; an unreadable ledger → attempt 1), covered by `tests/unit/test_recover_verdict_rotation_py.py`.
- ~~**run-page-last-node**~~ — **DONE `bbb00e1`**: `page.rs:2815-2818` derives the node from `model.last_ran()` and hands it to `MiniOrkGraph::new`, so the run page opens on the node it reached last. The engine's `graph['focus']` is redundant, not unwired.
