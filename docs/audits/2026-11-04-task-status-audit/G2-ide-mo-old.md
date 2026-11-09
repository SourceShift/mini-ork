# G2-ide-mo-old — task-status audit

Group: the older half of the mini-ork IDE-surface work, 97 `tasks[]` records emitted 2026-10-07 → 2026-10-13, harvested from `<z-insight>` blocks and deduped by `task_id`. Repos read (read-only): `/Volumes/docker-ssd/ps/mini-ork`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/*`, `/Volumes/docker-ssd/ps/zed-mini-ork`, plus `/Volumes/docker-ssd/ps/mini-ork-ui` (the pinned Rust UI crate the Zed fork renders).

## Summary

Of 97 self-reported tasks, **68 are DONE**, **14 are OPEN**, **1 is STALE**, and **14 are UNKNOWN** (`cost-policy`, `flow-rubric-negative` and `flow-legend-clip` moved OPEN → DONE on 2026-11-09; see the correction note in `SUMMARY.md`). The IDE-surface claims held up unusually well: nearly every Rust-UI row (flow track, replay controls, sections, sidebar/kanban, composer, story revisions) is backed by a real function in `mini-ork-ui/src/`, and the test-debt rows (pytest pollution `2881d268`, acp-red `58e1c5aa`, spy-test 34-passing) are verifiably fixed. The learning-loop program also mostly landed (S1-S4, S6, D2/D4, P1b/P2/P3/P6/P7b/P10, the B3 producer, inject ledger, adaptive holdout). The genuinely unfinished work clusters in five places: (1) **the shadow-mode adoption gate** — `MO_EVIDENCE_LEDGER` still defaults to `0` and the researcher home has no shadow flag, so I5 and the researcher enablement are both still off; (2) **the SDD mechanism roadmap is half-built** — I2 and I8 only, with I4/I6/I7/I9 absent and `sdd-va-ac4` still in progress; (3) ~~**two Rust-UI review defects that were never fixed** — the rubric score `-1` sentinel renders verbatim (`flow.rs:1191`) and the flow legend has no clip (`flow.rs:1322`)~~ — both fixed 2026-11-09 (`mini-ork-ui` `2480d71`, pinned by the fork's `6a34cb59`); (4) **the measured-lift / calibration trio** — the ctx-lift instrument exists but has never had n>=30 per arm and topic calibration is read-only (the cost-policy leg landed after the audit: `mini_ork/cost/budget.py`); (5) two small but concrete backlogs — `framework-edit-diff-scope` has no scope gate anywhere, and `fl-k5`'s `02010e90` is still an ancestor of no branch. The 14 UNKNOWNs are not failures of the work; they are rows whose subject points outside the three audited repos (ContextNest, the campaign backend, a reader/chapter-write product) or whose referenced defect inventory was never written down, and I say what I searched for each.

## Verdict table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| sde-3 | 2026-10-13 22:12 | pending | DONE | d1606e13 "feat(recovery): aggressive escalation policy (should_escalate)" + 1d88b72e SDE-3a wiring |
| cp-alternatives | 2026-10-13 17:02 | pending | DONE | 418286f8 E2B provider, cac7749b Modal provider, 01c439e2 docs(cloud-providers): Modal/Vercel/K8s status |
| i5-shadow-enable | 2026-10-11 20:47 | pending | OPEN | MO_EVIDENCE_LEDGER default is "0" (docs/operator/feature-flags.md:64); no home profile or shell sets the shadow value |
| topic-calibration | 2026-10-11 19:48 | pending | OPEN | only the read side shipped (dae5bdd1; ide_pages/orch.py:1296 reads /coord/topic-pairs); SESSION-GOALS.md:171 still pending |
| resend-inventory | 2026-10-11 19:48 | pending | UNKNOWN | searched commit log, docs/todos and kickoffs for mini-ork-50 / task-inventory; no artifact either way |
| s1-s6 | 2026-10-11 19:45 | pending | OPEN | S1 aae3dd7f, S2 e6ffe34b, S3 fc92bbbc, S4 811aa4a9, S6 healer retired (mini_ork/recovery/healer.py gone); S5 fixture purge has no commit |
| sdd-roadmap-continue | 2026-10-11 19:45 | in_progress | OPEN | I2 landed (68190111, 4c347c84); I8 in flight (kickoffs/w2/w2-i8a-recursion-regression.md); no I6/I7/I4/I9 artifacts |
| dedupe-ingest-hooks | 2026-10-11 16:56 | pending | DONE | ~/.claude/settings.json has exactly one /api/v1/cc/hook/<event> ingest command per event, no duplicates |
| minimax-lane-alias-drift | 2026-10-11 16:43 | pending | DONE | docs/todos/2026-10-13-remaining-tasks-resolution.md records "DONE (no work needed)"; tests/unit/test_recipe_lane_registry.py |
| zed-wizard | 2026-10-11 15:53 | pending | STALE | same doc records "zed-wizard - DROPPED (redundant)"; setup.py Features tab + features.py + /api/v1/features cover it |
| wu-7c3a91e2-n2 | 2026-10-11 15:43 | pending | DONE | d4338e11 "recover: the auto-repair child no longer inherits MINI_ORK_PLAN_PATH (RB-03)" |
| wu-7c3a91e2-s3 | 2026-10-11 15:43 | pending | DONE | fc92bbbc "feat(themes): author + verify lesson_themes lessons (close the B3 producer gap)" |
| wu-7c3a91e2-s4 | 2026-10-11 15:43 | pending | DONE | 811aa4a9 "feat(learning): reflect records each learning stage's health (the pass-stats producer)" |
| wu-7c3a91e2-s6 | 2026-10-11 15:43 | pending | DONE | S6 retired branch taken: mini_ork/recovery/healer.py absent, zero lessons_bank references in the tree |
| wu-7c3a91e2-s2 | 2026-10-11 15:43 | pending | DONE | e6ffe34b "a scheduled reflect pass that fills the gaps post-run reflect leaves" + 711ed954 fix |
| enable-orchestrator-canary | 2026-10-11 15:42 | pending | DONE | ec0edcd5 "feat(orchestrator): flip MO_RUN_ORCHESTRATOR default to ON" (a superset of canary) |
| p0-reflection-writeback | 2026-10-11 15:42 | pending | DONE | cd107afd "fix(reflect): a refused dispatch must not silently kill the gradient batch (P0)" |
| features-in-ide | 2026-10-11 15:38 | in_progress | DONE | mini_ork/ide_pages/features.py exists; 4529f980 ide(inbox): features shipped since yesterday |
| wu-7c3a91e2-d2 | 2026-10-11 13:34 | pending | DONE | same work as S2 - e6ffe34b scheduled reflect; no exit-78 launchd tick remains in the tree |
| t3-memory-index | 2026-10-11 13:22 | pending | DONE 2026-11-09 | MEMORY.md was 18.6 KB by then (it grows as the index gains hooks); seven settled themes — lit surveys, domains, product, benchmarks, learning loop, misc state, commercial boundary — moved verbatim into `MEMORY-ARCHIVE.md` (restorable), three over-long labels compressed. Now 17,051 bytes, under the 17.1 KB target; no link lost |
| wu-7c3a91e2-d4 | 2026-10-11 13:05 | pending | DONE | same work as S4 - 811aa4a9 pass-stats producer |
| t2-refine-fix | 2026-10-11 13:00 | pending | DONE | bf5a02e9 landed (docs/todos/2026-10-11-handoff-remaining-tasks.md T2) |
| t6-ram-sentinel-pytest-match | 2026-10-11 12:26 | pending | DONE | /Users/admin/bin/ram-sentinel.py:221-231 narrows KILL_PROTECTED to "-m pytest", exact-name pytest kept |
| cn-outcome-feedback | 2026-10-11 12:05 | pending | DONE | 7950699e "fix(contextnest): persist the CN context the engine injects so outcomes are credited" |
| send-task-inventory-miniork50 | 2026-10-10 20:37 | pending | UNKNOWN | searched commit log, docs/todos and inbox for mini-ork-50; no send artifact |
| peer-reconcile | 2026-10-10 20:34 | pending | DONE | 32beebc5 "Merge origin/main (quota triage fix 18e1a474) into local main"; 35d04ace on main |
| t2-zed-bundle-backup | 2026-10-10 20:34 | pending | DONE | /Volumes/docker-ssd/ps/backups/zed-mini-ork-20261011.bundle (25.8 MB, 2026-10-11) |
| cost-policy | 2026-10-10 12:52 | pending | **DONE `fc1323b2`** | `mini_ork/cost/budget.py`: a declared per-run ceiling (`MO_RUN_BUDGET_USD`, or `run_profile.json` → `budget_usd`) checked before every dispatch, a run that *parks* (`budget_exhausted`, marker `.budget-exhausted`) instead of failing, and a plugin-selectable policy (`MO_BUDGET_POLICY` via `PluginContext.budget_policy`) for the secure tier. `d9455494` made the printed unblock (`mini-ork resume`) real — it re-asks the policy and refuses while over / when the ceiling was removed. `f023005a` / `287e9dbe` closed the fail-closed edges: corrupt profile refuses, a park outlives a vanished ceiling, both holds are answered together, and the CLI now runs plugin discovery so the secure tier is reachable. `cost_ledger.py` still only records spend; enforcement is the new package's job |
| epic-reader-chapter-write-defects | 2026-10-10 06:50 | in_progress | UNKNOWN | reader/chapter-write product lives outside the three audited repos; no mini-ork commit or doc found |
| ci-acp-red | 2026-10-10 06:47 | pending | DONE | 58e1c5aa; tests/unit/test_acp_lane_repair.py:88 stubs mini_ork.acp.agent._setup_readiness_for_thread |
| jest-grader-adapter | 2026-10-10 06:41 | pending | DONE | d605ce7f "feat(heldout): jest/vitest grader adapter + high-water-mark incremental mining" |
| completion-contracts | 2026-10-09 10:52 | pending | DONE | 98a407cb "feat(artifacts): per-kind completion contracts - a well-formed fabrication must not pass" |
| b3-theme-producer | 2026-10-09 09:32 | pending | DONE | fc92bbbc themes.promote authoring path closes the B3 producer gap |
| safety-surface-sweep | 2026-10-09 07:21 | in_progress | UNKNOWN | docs/SECURITY-AUDIT.md predates the task (2026-07-21); no sweep report from 2026-10-09 found |
| p2-one-liners | 2026-10-09 07:03 | pending | UNKNOWN | the cited `SESSION-GOALS.md:225` no longer exists anywhere under `/Volumes/docker-ssd/ps` (searched 2026-11-09), and no doc defines A6/A3/A8; the three items cannot be implemented from a spec that is gone |
| flow-rubric-negative | 2026-10-09 07:02 | pending | OPEN | mini-ork-ui src/flow.rs:1191-1193 renders review.score verbatim (parse_review keeps it a string); no -1 filter |
| flow-legend-clip | 2026-10-09 07:02 | pending | OPEN | mini-ork-ui src/flow.rs:1322 legend absolute().top(6).right(12) with no containment/clipping |
| ide-restart-review | 2026-10-08 20:11 | pending | UNKNOWN | docs/reviews/ only has 2026-10-06-ide-pages-review.md; no screenshot review from 2026-10-08 |
| p5-restyle | 2026-10-08 19:06 | pending | DONE | 55bd028a pages in Orca's sections; kickoff db062af0 (p5a-page-restyle) |
| ide-orca-f2b-flow-20261008172728 | 2026-10-08 17:20 | in_progress | DONE | mini-ork-ui src/flow.rs draws the full track incl. the Build fan-out and Integrate pill (:973, :1076) |
| codex-config-model | 2026-10-08 17:13 | pending | OPEN | ~/.codex/config.toml:3 is still gpt-5.5; no recorded keep/restore decision in docs or commits |
| recurring-orchestration-monitor | 2026-10-08 14:25 | in_progress | UNKNOWN | no 3-minute cadence found; 59589568 adds a per-run orchestrator watcher, a different shape |
| p3-sidebar-inbox-kanban | 2026-10-08 13:18 | pending | DONE | mini-ork-ui src/sidebar.rs + sections.rs render_columns (kanban, :395); kickoff aa9da0d5 |
| p1-composer | 2026-10-08 13:18 | pending | DONE | mini-ork-ui src/sections.rs render_composer:570 with send button and per-section editor |
| orca-codefix-revise | 2026-10-08 12:55 | pending | DONE | 3af68f186e story steps open by their own default; db37f71731 first row clear of copy button; 83ab579d97 palette |
| pytest-pollution | 2026-10-08 12:47 | pending | DONE | 2881d268 "drop db_for's cached StateDB per test - three order-dependent failures" |
| close-b1-landed | 2026-10-08 12:47 | pending | DONE | ce3219bda8 on zed fork branch mini-ork (Orca neutral dark palette + spec-v2 plumbing) |
| ide-orca-f2a-flow | 2026-10-08 12:26 | pending | DONE | mini-ork-ui src/flow.rs render_flow_part_a:829 (Ticket-Design-Build-Integrate-Review) |
| ide-orca-f3-replay | 2026-10-08 12:26 | pending | DONE | mini-ork-ui src/flow.rs replay_controls:1535 - Pause (:1597), Restart, speed chips (:1614) |
| primary-ff-after-i5 | 2026-10-08 11:56 | pending | DONE | git merge-base --is-ancestor 7805bb92 HEAD returns 0 |
| lane-preflight-hint | 2026-10-08 11:29 | pending | DONE | mini_ork/cli/lane_preflight.py + tests/unit/test_lane_preflight_llm_nodes.py; cbc6088b RB-23 |
| framework-edit-diff-scope | 2026-10-08 11:29 | pending | OPEN | no files_changed-and-scope_allow check exists; kickoff_scope.py only feeds context_v2 files_in_scope, publisher.py has no scope code |
| publisher-node-end | 2026-10-08 10:59 | pending | DONE | e69e9665 "node_events: keep a node_end whose finish_reason is outside the column enum" - names the publish_abstain drop |
| revive-orca-b1-b2a | 2026-10-08 10:21 | pending | DONE | b1 + b2a are ce3219bda8 (palette, shared components, spec-v2 plumbing); b2b 337b71da52 |
| replay-early-applicability | 2026-10-08 10:21 | in_progress | DONE | 5f9d559a "code-fix decides replay applicability before any base build" + 3229f937 |
| spy-test-main | 2026-10-08 10:17 | pending | DONE | tests/unit/test_ide_pages_node_changes.py:587 test present; file runs 34 passed |
| needs-change-steps-format | 2026-10-08 10:17 | pending | DONE | 2ba6c480 "NEEDS-CHANGE.md - reasons hang under their step, not as steps" + e72cb34d |
| peer-review-fanout-plan | 2026-10-08 10:14 | in_progress | UNKNOWN | searched docs/ for the 5-part fan-out plan and peer judgements; nothing found |
| pytest-pollution-fix | 2026-10-08 10:12 | pending | DONE | 2881d268 (same fix as the sibling row) |
| epics-table-ingest | 2026-10-08 10:09 | pending | DONE | mini_ork/cli/epics.py subcommand "ingest <roadmap.md> - Parse roadmap markdown -> epics + deps" |
| ide-orca-b2b-story | 2026-10-08 09:50 | in_progress | DONE | salvage run ide-orca-b2b-story-20261008113340 -> 337b71da52; sections.rs render_story/files/findings/checks/agents |
| fix-c-cost | 2026-10-08 09:12 | pending | UNKNOWN | no C8-C10 defect list in docs/ or kickoffs/; no matching commits - cannot attribute |
| fix-d-ops | 2026-10-08 09:12 | pending | UNKNOWN | no D11-D12 defect list in docs/ or kickoffs/; no matching commits |
| recon-12-defects | 2026-10-08 09:12 | in_progress | UNKNOWN | the 12-defect inventory itself is not in the repo; searched docs/audits, kickoffs/auto, commit log |
| ide-sections-rs | 2026-10-08 07:38 | pending | DONE | mini-ork-ui src/sections.rs has one renderer per section kind (hero, callout, triage, story, files, findings, checks, agents, columns, composer, levels) |
| run-page-redesign-plan | 2026-10-07 22:11 | in_progress | DONE | docs/plans/2026-10-15-run-page-jtbd-ux-review.md + the redesign shipped (0bd07bea Dispatch Console layout) |
| fl-k5 | 2026-10-07 20:41 | pending | OPEN | 02010e90 is an ancestor of no branch (SESSION-GOALS.md section 1) and its per-test regression parsing is absent from mini_ork/verify/ |
| disable-agent-auto-memory | 2026-10-07 20:15 | in_progress | DONE | config/agent-claude-settings.json:6 "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1" |
| flaky-dod2 | 2026-10-07 19:17 | pending | UNKNOWN | coord_principals_test lives in ContextNest, outside the three audited repos |
| cn-boot-budget | 2026-10-07 19:17 | pending | UNKNOWN | CN_PROD_HEALTH_TIMEOUT is a ContextNest setting, outside the audited repos |
| context-v2-stable-ids | 2026-10-07 18:31 | pending | DONE `83842e62` | `context_v2.source_entries` emits `item_kind` (constraint/finding/prior_attempt/theme) per id, plus `held_out`; `node_block` and `cli/plan.py` share the builder, and the run page's ledger renders a kind breakdown |
| eng-mistakes-trends | 2026-10-07 18:07 | pending | DONE | ide_pages/learn/overview.py:42 "Recent learning events", :224 per-metric trend deltas; system health lives in ide_pages/setup.py |
| learn-p10 | 2026-10-07 16:43 | pending | DONE | e65b720a "docs(learn): usefulness contract for every learning surface" |
| learn-p3 | 2026-10-07 16:43 | pending | DONE | fc92bbbc author + verify theme lessons |
| sdd-va-ac4 | 2026-10-07 16:20 | in_progress | OPEN | SESSION-GOALS.md:497 still in_progress; no AC4 n>=30 batch artifact |
| researcher-revendor | 2026-10-07 16:20 | pending | DONE | researcher/.mini-ork/.vendored_from_sha = 2f253c45..., refreshed 2026-11-02 |
| ctx-lift | 2026-10-07 15:53 | pending | OPEN | instrument exists (mini_ork/cli/metrics_context.py:191 lift, MIN_N gate) but no n>=30-per-arm measurement recorded |
| threads-dag-thumbnails-build | 2026-10-07 15:21 | pending | DONE | 81dfa545 batched dags ide_page + mini-ork-ui src/thumb.rs renders the thumbnails (the user's own look is unverifiable) |
| w5-91-env | 2026-10-07 15:08 | pending | UNKNOWN | ONBOARDING_DEMO_BOOK_PATH targets the campaign backend, outside the audited repos |
| shadow-enable-researcher | 2026-10-07 14:59 | pending | OPEN | revendor done, but no shadow flag anywhere in researcher/.mini-ork/config |
| threads-row-ownership | 2026-10-07 14:53 | pending | UNKNOWN | a peer-coordination question; nothing recorded in the repos |
| eng-tabs | 2026-10-07 14:06 | pending | DONE | 1895af19 'Your code' tab, 07279422 Rules tab |
| unvetted-directives-decision | 2026-10-07 13:42 | pending | DONE | c4e853b3 "only verified learnings in prompt files - remove the 18 mock-scored directives, guard against more" |
| agent-perf-semantics | 2026-10-07 13:24 | pending | DONE | 13fe4e81 the RouteMoA screen reads slice outcomes, not groups-won; tests/unit/test_route_screening_slice_stats.py pins it |
| eng-rules | 2026-10-07 13:10 | pending | DONE | 4ceb277c path-scoped prefs + "mini-ork prefs preview"; 07279422 Rules tab |
| learn-p6 | 2026-10-07 12:43 | pending | DONE | 3b48d876 "feat(ide): Lessons tab" (ide_pages/learn/lessons.py) |
| learn-p4 | 2026-10-07 12:43 | pending | DONE | holdout wired: context_v2.DEFAULT_HOLDOUT=0.2 (:54), ledger held_out column, metrics_context lift - the share is 0.2, not 0.10 |
| learn-p1b | 2026-10-07 12:43 | pending | DONE | 811aa4a9 pass stats wired into reflect |
| test-env-guard | 2026-10-07 12:43 | pending | DONE | tests/conftest.py:39 MINI_ORK_NO_CWD_DB=1 and :72-73 pop MINI_ORK_DB/MINI_ORK_HOME per test |
| triage-blame-module | 2026-10-07 11:04 | pending | DONE | mini_ork/triage/blame.py exists (added c94a9a29) |
| triage-driver | 2026-10-07 11:04 | pending | DONE | mini_ork/triage/failures.py exists (classify-emit-promote) |
| learn-p7b | 2026-10-07 10:58 | pending | DONE | 2e10d81f "feat(ide): Memory tab" (ide_pages/learn/memory.py) |
| learn-inject | 2026-10-07 10:28 | in_progress | DONE | mini_ork/learning/ledger.py lesson-injection table with source_kind/source_id/held_out; f1780692 serves it; 216d17d7 renders it |
| learn-p2 | 2026-10-07 10:28 | pending | DONE | c1131222 group gradients into themes + framework/task split; d405ef48 own-class themes first |
| replay-adapter-nonpytest | 2026-10-07 09:46 | pending | DONE | 3a943c58 "the delta-gate replay reads jest/vitest/JUnit results, so target is provable outside pytest" |
| sdd-roadmap | 2026-10-07 09:14 | pending | OPEN | SESSION-GOALS.md:191 pending; roadmap (kickoffs/sdd-mechanisms/roadmap.md:22) still has I4/I6/I7/I9 unbuilt |
| ide-board-perf-r7 | 2026-10-07 00:10 | in_progress | DONE | 11d73b83 "perf(board): the board path opens state.db without importing FastAPI (db_for moves to mini_ork.web.db)" |

## Still genuinely open

| task | next action |
|---|---|
| i5-shadow-enable | Set `MO_EVIDENCE_LEDGER=shadow` in the one home/profile that should trial it, keep the code default at `0`, and record the trial window in `docs/operator/feature-flags.md`. |
| topic-calibration | Close the write side: emit a calibration notice from a reflect pass when the `/coord/topic-pairs` ratio drifts, then update `SESSION-GOALS.md:171`. |
| s1-s6 | Only S5 (fixture purge) is missing — sweep the `tests/fixtures` corpus for stale lesson/theme fixtures and commit the delete. |
| sdd-roadmap-continue / sdd-roadmap | Build I4, I6, I7, I9 from `kickoffs/sdd-mechanisms/roadmap.md:22`; finish I8 (`kickoffs/w2/w2-i8a-recursion-regression.md`) first so the roadmap stays ordered. |
| ~~t3-memory-index~~ | **DONE 2026-11-09** — 17,051 bytes; seven settled themes archived verbatim. |
| ~~cost-policy~~ | **DONE 2026-11-09** `fc1323b2` + `d9455494` + `f023005a`/`287e9dbe` — `mini_ork/cost/budget.py` enforces a per-run ceiling with a park-not-fail gate; see the per-task row. |
| p2-one-liners | Ship the three one-line items from the P2 list (A6 pill order, A3 `lesson_text`, A8 floor) — no commit exists. |
| ~~flow-rubric-negative~~ | **DONE 2026-11-09** `mini-ork-ui` `2480d71` (pinned by fork `6a34cb59`): `score_of` treats any negative parsed score as "no score" at the parse boundary, so `parse_step` and `parse_review` both inherit it; `flow::tests::the_parse_error_sentinel_is_not_a_score` / `a_real_score_survives` / `a_step_score_uses_the_same_rule` pin it. |
| ~~flow-legend-clip~~ | **DONE 2026-11-09** `mini-ork-ui` `2480d71` (pinned by fork `6a34cb59`): the legend is a right-aligned `flex_wrap` strip *above* the map viewport, so it is structurally outside the overlay plane and cannot paint over the last column; a long legend wraps instead of spilling. |
| codex-config-model | Record the decision: either pin `~/.codex/config.toml:3` to the intended model in `config/` or write down why gpt-5.5 is correct — the task is a decision and cannot close otherwise. |
| framework-edit-diff-scope | Add a `files_changed ⊆ scope_allow` check in `mini_ork/cli/execute.py` (or publisher verify) that refuses a run whose diff escapes its kickoff scope. |
| fl-k5 | Re-land `02010e90` as a new branch off `main` and add the per-test regression parsing to `mini_ork/verify/levels.py` (it is rc-only today). |
| ~~context-v2-stable-ids~~ | **DONE `83842e62`** — `context_v2.source_entries` emits `item_kind` (+ `held_out`) per id; both emitters share it and the run page renders a per-kind count. |
| sdd-va-ac4 | Collect one AC4 batch at n>=30 and record the result; the gate is instrumented but unmeasured. |
| ctx-lift | Run the A/B at n>=30 per arm and record the lift number in the metrics doc; today only the code path exists. |
| shadow-enable-researcher | Add the shadow flag to `researcher/.mini-ork/config` now that the revendor (`2f253c45`) has landed. |
