# E-engine-rsi — task status audit

**Group:** engine core (DAG nodes, executor, recipes, schemas, migrations, telemetry, sqlite, path authority) and RSI/learning (rsi-cycle, benchmark, L9, learning loop, GRPO, oracle, held-out eval, promotion).
**Rows audited:** 75 · **window 2026-10-07 → 2026-11-04** · read-only against `/Volumes/docker-ssd/ps/mini-ork`, its worktrees, and `/Volumes/docker-ssd/ps/zed-mini-ork`.

## Summary

Of 75 claimed tasks: **52 DONE**, **12 OPEN**, **7 STALE**, **4 UNKNOWN**
(recounted 2026-11-10 after `sweep-node` closed).

The RSI/learning half is in much better shape than the claims suggested: the rsi-cycle C1–C5 graph, Tier-0 fixes, lane unblock, reflector deadline cap, oracle-integrity instrument, held-out/injection ledger, benchmark_delta computation (785963d0) and the L9 called-writer fix (ef55e7d8) all have concrete receipts (commits, docs, or live DB rows) — with one correction (2026-11-08): the `benchmark_delta` writeback **computes nothing**, because `benchmark_results` / `benchmark_tasks` have no producer anything calls; the repair is the candidate↔run link, not a merge (`8a6cc25e` makes L9 say so instead of closing hollow). The engine-core migration and path work also mostly landed. What genuinely still needs doing clusters in five places: (1) the **anytime-valid certificate (E3) is built but never merged** — it exists only as commit `4ff3a0aa` under `refs/mini-ork/ckpt/*`, not on `main`, so the RSI "held-out + certificate" item is half-delivered; (2) ~~`fetch_run_recipes` guard~~ **DONE** `c34cc5db` — all seven `task_runs` readers now guard on `has_table`, so a pre-migration DB returns empty instead of raising; (3) ~~**`partition_key` is declarative only**~~ **DONE (2026-11-10)** — the field was never read by an executor (the compiler dropped it silently), so it is now *refused* at compile time with a message pointing at `mini_ork.orchestration.item_fanout`, and the property is gone from the schema; no data-driven sweep node is planned, `item_fanout` is the mechanism; (4) **environment debt**: the 401 `KIMI_API_KEY` is still exported in `config/secrets.local.sh`, 7 `mo-e2e-node-host` images (≈12.8 GB) remain, and a root-owned electron cache is still on disk; (5) **IDE/orca and journey work is graded but not closed** — 35 partial + 1 cli-only Orca rows and a 15-journey corpus against a 200 target.

## Per-task verdicts
| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| rsi-55434a3e482c48fc | 2026-11-04T17:20:46.198Z | in_progress | OPEN | `785963d0` "rsi: learning_record.benchmark_delta is never computed" (+64 lines self_improve.py, +59 tests) committed 18:48, after the 17:20 emission — but **corrected 2026-11-08**: the writeback is inert, because its source tables (`benchmark_results` / `benchmark_tasks`) have no called producer, so the *task* (a working delta) is still OPEN; see A row 16 and `docs/audits/2026-11-06-revive-batch.md` |
| rsi-c1793808348-e4c9d816 | 2026-11-04T16:06:24.797Z | in_progress | DONE | 1e5cb369 "rsi: every lesson injection is in-sample — held-out evaluation is hollow" (context_assembler.py, learning/ledger.py); emitted 16:06, commit 16:08 |
| apply-migration-0086 | 2026-11-04T14:03:59.392Z | pending | DONE | db/migrations/0086_intake_events_mandate_json.sql; schema_migrations row applied 2026-11-04T14:14:16Z; intake_events.mandate_json present |
| orca-parity | 2026-10-29T12:04:03.325Z | in_progress | OPEN | zed docs/orca-parity/matrix.md still counts 35 partial · 1 cli-only · 14 missing of 68; grading done, gaps not closed |
| orca-parity-implement | 2026-10-29T11:09:12.268Z | pending | OPEN | matrix.md still lists 1 cli-only + 35 partial; only a regrade landed (f04ce2c8), no row-closing commits |
| journey-model-agree | 2026-10-29T09:33:27.669Z | in_progress | DONE | 49ef6d25ab + docs/user-journeys/README.md (constructs, provenance, file locations, 15 journeys/5 personas) |
| journey-scale-200 | 2026-10-29T09:33:27.669Z | pending | OPEN | docs/user-journeys/journeys holds 5 persona files; 49ef6d25ab says 15 journeys — not 200 |
| console-backend-landing | 2026-10-29T08:27:03.408Z | pending | OPEN | no db/migrations/0147–0149 (max is 0086) and no console module in mini_ork/ |
| open-gaps | 2026-10-28T14:08:00.531Z | pending | DONE | docs/plans/2026-10-28-open-gaps.md "Gaps implemented (user's choice)" — S5/S4/N3/S1/N1/S2 with shas |
| rsi-held | 2026-10-27T08:57:45.249Z | pending | DONE `56c6dacf` + `79c24ff7` | certificate E3 on main with migrations 0089 (ledger table) and 0090 (reason_class); probe harvest + region routing already merged |
| rsi-lane-unblock | 2026-10-26T15:04:44.180Z | pending | DONE | docs/plans/2026-10-23-rsi-cycle.md "Cycle 21 (2026-10-27): the lane block is gone"; overlay repointed off minimax |
| c5-cadence | 2026-10-26T12:40:12.765Z | pending | DONE | 785e91f7 "one config file, a launchd cadence, and a doctor that says no (C5)"; config/rsi-cycle.yaml present |
| rsi-tier0-fixes | 2026-10-24T11:16:47.151Z | pending | DONE | 75c9974e kickoffs + docs/plans/2026-10-24-rsi-technique-status.md "Tier 0 progress" table (a8ea463b … 28e9c0b2) |
| rsi-phase1 | 2026-10-23T07:15:32.146Z | pending | DONE | same doc "Phase 1 status": C1–C4 in mini_ork/rsi/, recipes/rsi-cycle/, cli/rsi_cycle.py, db/migrations/0084_rsi_cycles.sql; C5 = 785e91f7 |
| rbf-merge-queue-8 | 2026-10-20T20:53:02.259Z | in_progress | DONE | d0983312 (dead-telemetry RB-39), c7217113 (gates-settle RB-17/19), 26717cdf + c36418e9 (reflect-scope RB-20) |
| rbf-remote-selfedit | 2026-10-20T14:19:10.931Z | pending | DONE | 9a64101d "remote: pin the engine sha once per run; a self-edit run is not defaulted to a remote node (RB-48, RB-44)" |
| vt8 | 2026-10-19T06:09:06.963Z | in_progress | DONE | mini_ork/gates/oracle_integrity.py added 48fab622, tests/unit/test_oracle_integrity_py.py, `mini-ork instruments oracle-integrity`; residual limitation documented bee36b66 |
| thread-card-impl | 2026-10-18T18:22:35.886Z | pending | DONE | zed 0884a83a77 + f06b8d3719 "thread cards inline from ACP _meta"; engine e35d7bbf emits the cards |
| orch-tab-stream-redesign | 2026-10-17T20:48:02.618Z | in_progress | DONE | zed 14f6bd962e "stream the orchestrator tab from its ACP thread"; submodule 606ce20 |
| p1-6-artifact-transport | 2026-10-17T07:10:30.011Z | pending | DONE | docs/architecture/artifact-transport.md records the decision (artifact store; index vs transport vs materialize; schema delta D2) |
| restore-reverted-docs-in-primary | 2026-10-17T07:10:30.011Z | pending | DONE | docs/architecture/artifact-transport.md and per-node-sandboxes.md both present and tracked (git status clean); 0792dc43 restore |
| suite-6-failures | 2026-10-17T07:10:15.928Z | pending | DONE | docs/plans/2026-10-28-open-gaps.md: "All claimed check suites on main f619e749: 1,154 passed, 0 failed"; CI fully green after 87669c33 |
| deliver-agg-gaps | 2026-10-16T22:21:25.794Z | in_progress | OPEN | docs/audits/2026-10-16-session-gaps/AGGREGATE.md tail still lists leftover/unclaimed items |
| gap-delegation | 2026-10-16T21:34:10.671Z | in_progress | DONE | AGGREGATE.md coverage: 13 sessions filed per-session reports beside it — the delegation outcome |
| dag-bg-click-smoke | 2026-10-16T21:15:44.651Z | pending | OPEN | docs/audits/2026-10-16-session-gaps/6881f0ad §b1: "not yet live-smoke-tested"; no later live-smoke record |
| kimi-credential | 2026-10-16T17:53:09.153Z | pending | OPEN | config/secrets.local.sh:5 still exports KIMI_API_KEY with a value; only the dispatch default was dropped (1172a9fd) |
| docker-image-prune | 2026-10-16T16:07:53.960Z | pending | OPEN | `docker images` still lists 7 × mo-e2e-node-host:1.83GB (task asked to prune 8) |
| node-tab-max | 2026-10-16T15:15:17.696Z | pending | DONE | crates/mini_ork_ui/src/graph.rs:639 maximized / :825 in_node_tab; node_tab.rs:81 sets in_node_tab |
| ws-12-not-needed | 2026-10-16T15:12:28.430Z | pending | DONE | d70f94cf + db/migrations/0076_task_runs_status_not_needed.sql; mini_ork/run_status.py is the single status partition |
| align-engine-ui-pin | 2026-10-16T14:47:34.820Z | pending | DONE | 6881f0ad report: ui-pin-zed done (e88610c48e); crates/mini_ork_ui now pinned at 38a1f7a1, far past 0aefe69 |
| rp-e6-graph-focus | 2026-10-16T09:19:47.067Z | pending | **STALE** | Re-measured 2026-11-09: `tests/unit/test_ide_pages_run_graph_focus.py` is on main (`git ls-tree -r origin/main`) and `run.py` already carries the delivered half — `_graph`/`_focus_node` give the DAG and the triage one owner for the focus (`run.py:950`, `:1515-1532`, `:1602-1610`). The worktree's uncommitted copy was byte-identical to main's file, so there was nothing left to merge; worktree + branch removed |
| rp-e10-artifacts-learnings | 2026-10-16T09:19:47.067Z | pending | DONE | c7363e73 "artifacts grouped by node and kind, learnings promote/reject (C22+C23)"; 86a10179 closes the engine half |
| per-node-isolation-design | 2026-10-15T19:46:08.029Z | in_progress | DONE | docs/architecture/per-node-sandboxes.md + artifact-transport.md design; dd15418f ships one sandbox per node (MO_NODE_ISOLATION) |
| reflector-node-cap | 2026-10-15T19:24:21.110Z | pending | DONE | cd9358e5 "fix(reflect): cap the reflector node's timeout to an armed --deadline" |
| peer-learning-loop | 2026-10-15T19:24:21.110Z | pending | OPEN | S1/S2/S4/S5/N1/N3 landed (open-gaps doc), but S3 framework-theme policy still needs the user's decision (2026-10-28-open-gaps.md:56) |
| p3-races | 2026-10-15T18:22:29.425Z | pending | DONE | docs/ux-briefs/2026-10-17-orchestrator-fe-design.md:510 "P3 … ✅ shipped 2026-10-16 — board race-keep, intake_events.run_id"; run_id column present in state.db |
| disk-root-owned-caches | 2026-10-15T10:45:24.626Z | pending | OPEN | ~/Library/Caches/electron/<hash> still owned root:staff (Mar 2025) |
| status-fix | 2026-10-15T07:03:18.132Z | in_progress | UNKNOWN | searched zed commits, docs/audits/2026-10-16-session-gaps/AGGREGATE.md and node_tab.rs for a friendly-failure-reason change; none found |
| reflector-node-deadline-cap | 2026-10-14T09:41:46.555Z | pending | DONE | cd9358e5 (same commit as #34) |
| rsi-sweep | 2026-10-13T21:17:00.667Z | in_progress | DONE | docs/plans/2026-10-24-rsi-technique-status.md — 117 planned techniques checked against code and live DB |
| real-graph-operator-verify | 2026-10-12T15:06:41.146Z | pending | STALE | :7090 answers HTTP 200; epic-runner runs are published; a one-off operator check superseded by later IDE rebuilds (96a21bd, 1481007) |
| real-graph-plan | 2026-10-12T13:44:20.036Z | in_progress | DONE | 6881f0ad report rows real-graph-engine-edges / ide-draws-edges / reflector-wiring all verified "done" |
| epic-runner-reflector-wiring | 2026-10-12T13:37:24.773Z | pending | DONE | recipes/epic-runner/workflow.yaml:54-76 wires the reflector node; depends_on rationale documented in-repo |
| plan-artifact-writer | 2026-10-12T11:27:23.124Z | pending | DONE | mini_ork/cli/execute.py:2837 UPDATE task_runs SET plan_path from plan_artifact(run_dir) |
| harness-view-engine | 2026-10-11T16:56:01.268Z | pending | DONE | mini_ork/ide_pages/node.py:86 _VIEWS has "harness"; _harness_view:3575; dispatch:4009 |
| wu-7c3a91e2-s5 | 2026-10-11T15:43:51.404Z | pending | DONE | docs/plans/2026-10-28-open-gaps.md:30 — 606 fixture rows purged from lesson_injections after integrity-checked backup |
| t4-lane-parity-test | 2026-10-11T13:22:46.075Z | pending | DONE | tests/unit/test_mini_ork_execute_py.py:199 rewritten hermetic (empty MINI_ORK_HOME); suite green per open-gaps doc |
| wu-7c3a91e2-d5 | 2026-10-11T13:05:09.525Z | pending | DONE | same S5 purge (2026-10-28-open-gaps.md:30) + f4394bbd "test(isolation): never let a test resolve the live state.db" |
| context-file-cleanup | 2026-10-10T20:34:02.431Z | pending | DONE | `0ca1b9d6` — every prompt now names `${MINI_ORK_RUN_DIR}/<artifact>`; the four lens prompts read `lens-bottleneck.md` (the retired `bottleneck-scan.md` name is gone). `tests/unit/test_recipe_prompt_artifact_refs_py.py` fails on any `CONTEXT_FILE` or braced `${RUN_DIR}`, and requires each writer prompt to name its own artifact |
| p4-skill-registry | 2026-10-10T12:08:45.701Z | pending | DONE | mini_ork/dispatch/skill_registry.py (granted_skills, materialize_skills, MaterializedSkills) + mini_ork/cli/skills.py |
| workflow-schema-sync | 2026-10-10T11:44:44.301Z | pending | DONE | schemas/workflow.schema.json:189 mcp_servers, :206 env |
| verify-dag-scroll-visual | 2026-10-10T11:34:05.491Z | pending | DONE | crates/mini_ork_ui/src/graph.rs:1701 "Drag the canvas to pan; the wheel/trackpad scrolls both ways" (ScrollHandle:770) |
| t3-artifact-contracts | 2026-10-10T08:01:07.218Z | pending | UNKNOWN | searched recipes/session-task-judge, docs/, kickoffs for T3/artifact-completion-contracts; no execution record |
| t2-backup | 2026-10-10T08:01:07.218Z | pending | UNKNOWN | searched zed docs/script, branches and tags for a T2 fork-bundle backup; none found |
| t6-sentinel | 2026-10-10T08:01:07.218Z | pending | UNKNOWN | searched docs for a T6 kill-protection shape decision; only unrelated cost-pause sentinels |
| contextnest-boot-decision | 2026-10-10T06:50:52.479Z | pending | OPEN | docs/audits/2026-10-19-session-goals/SESSION-GOALS.md:686 still `pending — User decision: boot read-path representation` |
| ctxfile-prompt-cleanup | 2026-10-09T16:54:46.673Z | pending | DONE | same as #49 — landed as `0ca1b9d6` (worktree `ctxfile-prompt-cleanup`, review verdict `approve`) |
| user-verify-ide | 2026-10-09T16:44:27.787Z | pending | STALE | superseded — IDE rebuilt/relaunched several times and node-tab-from-DAG shipped (96a21bd, 1481007); no separate user confirmation recorded |
| row-vs-title-click | 2026-10-09T16:44:27.787Z | pending | DONE | crates/mini_ork_ui/src/sidebar.rs:1485 opens node_tab::open_node_tab from the card row |
| wire-or-delete | 2026-10-09T16:43:12.054Z | pending | DONE | e404bd14 removes the dead healer/cleaner/finalize chain (the delete branch); reward saturation addressed by artifact@v1 aae3dd7f |
| b1-wire-or-remove | 2026-10-09T08:21:21.246Z | pending | DONE | bcee2dff "chore(db): drop orphaned textual_gradients table (0068)" + db/migrations/0068_drop_textual_gradients.sql |
| p1-injection-ledger-render | 2026-10-09T07:03:42.711Z | pending | DONE | mini_ork/ide_pages/run.py:2596 _injection_ledger, "Injected per node" :2834; 1e5cb369 adds the held-out/sources arm |
| sweep-node | 2026-10-08T10:09:20.409Z | pending | DONE | resolved as a refusal, not a feature: `mini_ork/workflow/compiler.py:272` raises `WorkflowCompileError` on a declared `partition_key` (naming `mini_ork.orchestration.item_fanout` as the sanctioned data-driven fan-out), the property is gone from `schemas/workflow.schema.json` `$defs/WorkflowNode`, `recipes/bdd-first-delivery/workflow.yaml` no longer declares it, and `tests/unit/test_workflow_partition_key_py.py` covers refusal + schema + recipe |
| registry-audit-recipe | 2026-10-08T10:09:20.409Z | pending | DONE | c944bbba + recipes/registry-audit/{workflow.yaml,register.py}; publisher rewrites Status cells |
| repo-dead-guard-fix | 2026-10-07T14:53:39.020Z | pending | DONE | `c34cc5db` — all seven unguarded `task_runs` readers now guard on `has_table` (RunDetailRepository's fetch_task_run_row/input_paths/correlation_row/trace_window/run_recipe/run_recipes/run_classifications, LearningRepository's fetch_task_run); both new tests fail with `sqlite3.OperationalError` when the guards are reverted |
| triage-failure-rsi | 2026-10-07T12:07:15.971Z | in_progress | DONE | mini_ork/triage/{blame,failures}.py; docs/plans/2026-10-13-self-driving-epic.md progress: SDE-1..SDE-6 merged |
| threads-dag-part-b | 2026-10-07T11:55:26.037Z | pending | DONE | crates/mini_ork_ui/src/thumb.rs (top-down mini-DAG, real edges, model mark) + submodule 742b471 |
| promote-recipe-param | 2026-10-07T11:04:28.284Z | pending | DONE | mini_ork/observability/bug_report.py:782 INSERT INTO epics(...,recipe); recipe kwarg at :653 |
| learn-node-tab | 2026-10-07T10:28:53.292Z | in_progress | DONE | mini_ork/ide_pages/node.py:2912 _learning_view + :4000 view=="learning" |
| learn-p1 | 2026-10-07T10:28:53.292Z | pending | DONE | injection ledger run.py:2596 + learning_pass_stats producer 811aa4a9 |
| learned-persist | 2026-10-07T09:55:07.188Z | pending | DONE | mini_ork/context_v2.py:1076 write_injection_record writes learned/<node_id>.{md,json} |
| heldout-impact-measure | 2026-10-07T09:46:40.824Z | pending | STALE | docs/plans/2026-10-24-rsi-technique-status.md:535 records "Held-out stack on/off eval … DROPPED … lessons +7.6% (n.s.)" |
| ide-node-stream-r6 | 2026-10-07T06:34:25.698Z | in_progress | STALE | docs/todos/2026-10-09-kickoff-triage.md lists ide-node-stream family as REVIVE; node detail later rebuilt (9 views incl. harness, node_tab.rs) |
| ide-node-stream-r5 | 2026-10-07T02:29:41.897Z | in_progress | STALE | same as #73 |
| ide-node-stream-r4 | 2026-10-07T01:13:20.152Z | in_progress | STALE | same as #73 |

## Still genuinely open (verdict = OPEN)

- **orca-parity** — Grade mini-ork against Orca and close the button gaps
  - Next: close the 1 cli-only row and pick the partials worth building; regrade after each
- **orca-parity-implement** — Implement cli-only and partial gaps in IDE
  - Next: implement the single cli-only gap (matrix.md), then the partials with an engine half already shipped
- **journey-scale-200** — Scale corpus to 200 journeys and emit gap report
  - Next: extend docs/user-journeys/journeys from 15 to 200 and regenerate graph.md/gaps.md
- **console-backend-landing** — Land console backend half (routes/service/schema/types + 0147–0149) after approval
  - Next: land the console backend (routes/service/schema/types) with migrations 0147–0149
- **rsi-held** — E2/E3 probe harvest + certificate + routing coverage (after cycle 21)
  - ~~Next: bring `wt/t1-certificate` through the high-risk merge path~~ — **DONE `56c6dacf` + `79c24ff7`**. Follow-up: the arm-time pre-flight that refuses before probe spend.
- **deliver-agg-gaps** — Deliver remaining AGGREGATE.md gaps via fan-out
  - Next: fan out the remaining AGGREGATE.md items, starting with the unclaimed readme-*.sh port target
- **dag-bg-click-smoke** — Live smoke the background click on all three DAG surfaces
  - Next: live-smoke the background click on the run page, full-screen overlay and node tab strip
- **kimi-credential** — remove or re-key the engine home's 401 KIMI_API_KEY
  - Next: delete the 401 key from config/secrets.local.sh or re-key it to a live kimi credential
- **docker-image-prune** — Prune 8 redundant mo-e2e-node-host images (13.2 GB)
  - Next: docker image prune the 7 mo-e2e-node-host tags (and stale agent-node tags)
- ~~**rp-e6-graph-focus**~~ — **STALE 2026-11-09**; the test and the `_focus_node` half are already on main, the worktree held a byte-identical copy. No action left.
- **peer-learning-loop** — Learning-loop integrity fix set from peer mini-ork-7c
  - Next: get the S3 framework-theme policy decision from the user, then implement or close it
- **disk-root-owned-caches** — Clear root-owned electron/node-gyp cache leftovers with sudo
  - Next: sudo rm the root-owned ~/Library/Caches/electron hash dir
- ~~**context-file-cleanup**~~ — **DONE** `0ca1b9d6`; see the per-task row.
- **contextnest-boot-decision** — User decision: boot read-path representation for ContextNest (flat binary snapshot vs off SQLite)
  - Next: user decision: ContextNest boot read-path (flat binary snapshot vs off SQLite)
- ~~**ctxfile-prompt-cleanup**~~ — **DONE** `0ca1b9d6` (same edit as #49).
- ~~**sweep-node**~~ — **DONE (2026-11-10), as a refusal rather than a feature.**
  `partition_key` was never read by any executor: the compiler dropped it
  silently while `dispatch_mode: partitioned` groups nodes by *type*. Wiring it
  up would have grown a second partitioning mechanism beside the sanctioned
  data-driven fan-out (`mini_ork.orchestration.item_fanout`, driven by a
  transform that writes the work list — `recipes/goal-loop` is the reference).
  So the compiler now rejects the declaration with a message naming
  `item_fanout`, the property is gone from `schemas/workflow.schema.json`, and
  `recipes/bdd-first-delivery/workflow.yaml` no longer carries it on its four
  partitioned nodes. `tests/unit/test_workflow_partition_key_py.py` pins the
  refusal, the schema rejection, and that the bdd recipe still compiles.
- ~~**repo-dead-guard-fix**~~ — **DONE** `c34cc5db`: all seven `task_runs` readers guard on
  `has_table` (see the per-task row); nothing left here.
