# C-runs-mo — run triage: failed runs, recover/revive, reruns, checkpoints, adoptions

Audit of 106 `tasks[]` rows emitted between 2026-10-07 and 2026-11-04 from the
`/Volumes/docker-ssd/ps/mini-ork` primary checkout. Ground truth came from
`git log --all --since=2026-09-01` (4,151 commits), `git show` on the named
fixes, the repo's own disposition docs (`docs/todos/2026-10-09..10-15`,
`docs/plans/2026-10-2*.md`, `docs/audits/2026-10-20-run-bugs/RUN-BUGS.md`,
`docs/audits/2026-10-19-session-goals/SESSION-GOALS.md`), the run dirs under
`.mini-ork/runs/` (810), and read-only SQLite against `.mini-ork/state.db`.

**Counts: 82 DONE / 3 OPEN / 6 STALE / 15 UNKNOWN**
(out of 106).

Read as a whole, the run-triage backlog is mostly *closed*: the 2026-10-20 RB
campaign (RB-01..RB-50) absorbed the whole `rbf-*` cluster, the `revive-wave` /
`orphaned-relaunch` / `revive-queue` items were dispositioned in the
`docs/todos/` triage series, and the RSI-cycle tasks (cycles 17, 18, 22,
`l9-real-link`) each ended in a named fix commit. The genuinely open work is
small and mostly UI: the armed-receipt requirement on the orchestrator tab, the
thread page's missing node detail, the environment picker, per-run scope
disclosure, and two CLI surfaces (`reap --run`, the release-checkpoint recipe).
The UNKNOWNs are mostly one family — three `k3`/`ask-k3` tasks and two
researcher-home recoveries whose run dirs no longer exist anywhere readable.

## Table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| `l9-real-link` | 2026-11-04 | pending | **DONE** | ef55e7d8 "fix(rsi): L9 closes on a *called* writer" - L9 fires on main, closes on the child HEAD. **Caveat 2026-11-08:** closing on a called writer alone was the too-loose half — a writer whose source has no producer computes nothing (`8a6cc25e` now requires the producer chain too). The rule behaviour this row records is real; the L9 *gap* is still open (A row 16) |
| `rsi-cycle-real` | 2026-11-04 | in_progress | **DONE** | docs/plans/2026-10-23-rsi-cycle.md Phase 1: cycle rsi-c1793010091-2818cd9c merged, merged_sha 398889bc |
| `rsi-first-cycle` | 2026-11-04 | pending | **DONE** | same receipt: first merged cycle 2026-10-26, merged_sha 398889bc |
| `t1-certify-gate` | 2026-10-27 | in_progress | **DONE** | d526ca21 "feat(levels): a gate-mode certify veto withholds publish" + tests/unit/test_levels_certify_gate.py |
| `t1-wave3-runs` | 2026-10-27 | in_progress | **DONE** | 20 kickoffs/auto/rsi-t1-*.md + 17 .mini-ork/runs/rsi-t1-* dirs; aae3dd7f 564ce92f f4fffd5a |
| `cycle-22-live-run` | 2026-10-27 | pending | **DONE** | config/rsi-cycle.yaml:96-101 child_timeout_s 7200 with the cycle-21 note; afba7ecb cost_usd fix; later cycle dirs rsi-c1793283719+ exist |
| `bif-r4b` | 2026-10-27 | in_progress | **DONE** | 87f61e20 "data(reviewer-eval): score with in-run inputs"; 2340482d feat(reviewer-eval) |
| `t1-wave3` | 2026-10-27 | in_progress | **DONE** | docs/plans/2026-10-24-rsi-technique-status.md 'Tier 1 progress (landed 2026-10-27)' |
| `rsi-wave2` | 2026-10-27 | in_progress | **DONE** | 17 rsi-t1 run dirs; worktree rsi-wave2; wave commits 196a3603 f049dc4c |
| `rsi-cycle-18` | 2026-10-26 | in_progress | **DONE** | c79d189a (shlex / interpreter code-flag operand dropped) - documented in rsi-cycle.md |
| `rsi-cycle-17` | 2026-10-26 | failed | **DONE** | 24488aee (SUGGEST node capped at half its wall; errored cycle sums its own llm_calls) |
| `rsi-first-live-cycle` | 2026-10-25 | pending | **DONE** | first merged paid cycle rsi-c1793010091-2818cd9c, $1.00, 2026-10-26 |
| `eco6-running` | 2026-10-25 | in_progress | **DONE** | d10a34a2 (K10 OTel export), 2d1852b2 (K15b webhook) per docs/plans/2026-10-25-ecosystem-section6.md |
| `eco6-dispatch` | 2026-10-25 | pending | **DONE** | kickoffs/auto/eco6-k*.md written and delivered (K10 d10a34a2, K15a 47de862d, K15b 2d1852b2) |
| `loop-fix-deadline` | 2026-10-23 | pending | **DONE** | 28e9c0b2 (self-improve runner arms --deadline; loop defect D4/D5) |
| `loop-fix-reflect` | 2026-10-23 | pending | **DONE** | c3ed7c08 (durable reflect cwd + effective lane policy = defect #1) |
| `outreach-dryrun` | 2026-10-22 | pending | **UNKNOWN** | searched docs/ + log: only docs/SETUP.md (594eab86) exists; no fresh-machine walkthrough record |
| `ci-watch` | 2026-10-21 | in_progress | **STALE** | CI run 39554304043 superseded; head went green later (f619e749, then 87669c33) per docs/plans/2026-10-28-open-gaps.md |
| `rbf-inflight` | 2026-10-20 | in_progress | **DONE** | RB-16 1fa562a8 (revive clears verdict, stamps attempt) + RB-39 d0983312 telemetry |
| `rbf-failed-reason` | 2026-10-20 | in_progress | **DONE** | 85672b8f "run: every failed run records why" (RB-30) |
| `rbf-round3` | 2026-10-20 | in_progress | **DONE** | docs/audits/2026-10-20-run-bugs/RUN-BUGS.md fix-status: RB-01..RB-50 all fixed 2026-10-20/21 |
| `rbf-round2` | 2026-10-20 | in_progress | **DONE** | RB-10 8d28c247, RB-23 cbc6088b, RB-06 469d017c, RB-22 2088182f, RB-27 fcf07c68 |
| `rbf-recover-env` | 2026-10-20 | in_progress | **DONE** | RB-03 d4338e11 (child drops MINI_ORK_PLAN_PATH) + bba8ab01 |
| `rbf-run-roots` | 2026-10-20 | in_progress | **DONE** | RB-01 83421eaa (named-but-missing target fails closed) |
| `rbf-nested-run-id` | 2026-10-20 | in_progress | **DONE** | RB-04 d4b26138 (nested run never adopts a live run id) |
| `pick-screenshot-run` | 2026-10-19 | in_progress | **UNKNOWN** | no artefact/commit naming the abstain run opened in the IDE; run dirs for it absent |
| `recover-47ad145f` | 2026-10-19 | pending | **DONE** | ffd0bef5 RB-34 "dangling node events pair in order" (delivered by framework-edit run rbf-event-pairing) |
| `revive-run-1792151796-92942` | 2026-10-19 | in_progress | **DONE** | a0fa212d (rollback strategy, cites run-1792151796-92942) + 9470c815 (child recovery scope); run dirs --D1/--D6 exist |
| `primary-reconcile` | 2026-10-19 | pending | **DONE** | git rev-parse HEAD origin/main -> both ef55e7d8 |
| `release-stage1` | 2026-10-18 | pending | **DONE `0264da4d`** | The checkpoint landed the day the task was emitted, in a different shape than the row assumed: `0264da4d` (2026-10-18, same day) made `mini_ork/version.py` the only version copy, made `ci.yml` callable and `release.yml` call it (so the release gate IS the CI matrix), and fails closed when the tag ≠ `version.py` or the built wheel ≠ the tag — the comment at `release.yml:33` introduces that step, it is not a placeholder for one. Pinned by `tests/unit/test_version_single_source.py`. The row's "recipes/release-*" shape was not built on purpose: a release checkpoint must not need LLM credentials on a tag push, so it is deterministic Python inline in the workflow |
| `cost-sidecar-gap` | 2026-10-17 | pending | **DONE `e0a02954`** | Determination: panel lanes do NOT bypass the check — `execute_handlers.py:866-887` runs the `.cost-pause` gate pre-dispatch for every lane and each lane's node still charges through `charge_node_cost`. The real under-enforcement was the *amount*: `.last-llm-cost` is rewritten by every LLM call, so a multi-lane node moved the meter by the LAST lane's cost while the run spent the sum (three $10 lanes → $10 on a $25 threshold). `cost_pause.reconcile()` now raises the meter to `cost_ledger.run_cost(db, run_id)` after the sidecar delta, `max()`-guarded so nothing is double counted and a lagging ledger cannot lose the delta; `run_cost` → None falls back to the delta alone |
| `verify-new-contract-live` | 2026-10-17 | pending | **UNKNOWN** | recipes/author-style-imitation-review/artifact_contract.yaml and run style-review-20261016-160813 exist, but nothing records a three-artifact proof |
| `version-report` | 2026-10-17 | pending | **UNKNOWN** | searched docs/ and run dirs: no report copied out of the gitignored runs dir |
| `p1-9-page-half` | 2026-10-17 | pending | **DONE (superseded)** | 32882a66 deletes mini_ork/ide_pages/orch_tab.py and the run page's Orchestrator tab; the state readout it inferred is replaced by `<run_dir>/orchestrator.json` (mini_ork/orchestrator_thread.py), so there is no three-state section left to gate |
| `p2-16` | 2026-10-17 | pending | **DONE `e9bcbf63`** | Both halves shipped. *(1) Disclosure:* `ide_pages.run.declared_scope()` reads `run_profile.json`, and `_scope_section()` puts it on the run page — the declared target root, the allow-listed paths, the deny lines, and the run's worktree branch — under a sentence saying the declaration is **not a write sandbox**, because a scope line that reads like enforcement is worse than none; a run declaring nothing says "No scope declared". *(2) Cross-run:* `ide_pages.runs` gained a `Scope & claims` tab whose `_scope()` joins every live run's declared paths and flags a path claimed by more than one (`claimed by more than one live run`), and a workspace source that raises still renders "No open worktrees" instead of blanking the tab. The neighbouring truth that motivated it also landed: the Spend section shows the ceiling the dispatch gate enforces (`budget.declared()` wins over `MO_WATCH_COST_USD`) and a parked run says so. Pinned by `tests/unit/test_ide_pages_run_scope_py.py` |

| `classified-row-dag` | 2026-10-17 | pending | **DONE** | 47f3ed56 draws the classified run's node; mini_ork/ide_pages/dags.py:119 type=classifier |
| `thread-runs-node-detail` | 2026-10-17 | pending | **DONE `0b7461c0`** | `_runs()` now carries a `node` column — the open node of each moving attempt, via `ide_pages.runs._live_nodes_bulk` (one batched query, the board's own event reduction) — and every row opens its run (the node's logs/artifacts/checks live there). A finished attempt shows "—": liveness, not history. The "thread_runs is empty" half was measured on the CLI smoke fixtures in this home's DB (3 threads, all `origin=cli`, no runs), not a wiring gap — `threads.service.send` and the agent-server mirror both call `store.link_run` after `launch_run` |
| `rp-e3-header` | 2026-10-16 | pending | **DONE** | b324fae5 (C6 state-aware header) + 7d0ac72a (C9+C12); ui 669c1b2c95 title-first tab label |
| `run-page-p0` | 2026-10-15 | pending | **DONE** | C3 18ffcae7, C4+C5 fdf55574, C6 b324fae5, C8 a70f9271; confirm honoured at ui/src/page.rs:572; timeout ui/src/ide_http.rs:155 |
| `run-61320-resume` | 2026-10-15 | pending | **STALE** | docs/todos/2026-10-15-failed-runs-4d-revive-plan.md row 11: epic wit-3-integrations done, child diff already on main |
| `ui-env-picker` | 2026-10-15 | pending | **OPEN** | no environment/placement picker in mini_ork/ide_pages/*; only a note at nodes.py:366 |
| `buddy-interactive` | 2026-10-15 | pending | **OPEN** | no 'buddy' implementation found anywhere under mini_ork/ |
| `sde-4-supervisor` | 2026-10-15 | pending | **DONE** | f1f93760 (supervise revives parked runs) + a9cbcfc0 (superseded + epic back-links) + 7aa2611f (per-epic budget) |
| `vg-1c` | 2026-10-15 | pending | **DONE** | recipes/framework-edit/verifiers/kickoff-contract.py:324-327 prefixes PATH with dirname(sys.executable) |
| `relaunch-cn-ui-doc` | 2026-10-15 | pending | **DONE** | 4529f980 "ide(inbox): features shipped since yesterday, from ContextNest" is the kickoff's deliverable |
| `slack-discord-transports` | 2026-10-15 | in_progress | **DONE** | mini_ork/integrations/{slack,telegram,discord}.py; eco6 K15b 2d1852b2; bb258b91 telegram landing |
| `K6b2-resume` | 2026-10-15 | pending | **DONE** | mini_ork/cli/approvals_cmd.py:17-21 _maybe_resume_parked_run -> recover --strategy resume; request-changes -> --from-node --strategy retry |
| `slice-b-node-checkpoints` | 2026-10-15 | pending | **DONE** | c96414b1 (node completion checkpointer), f1955ad6 (per-node git checkpoints), b1d35bbf (restore button) |
| `ui-run-launcher-env` | 2026-10-15 | pending | **OPEN** | duplicate of ui-env-picker: no placement/env picker in any launch surface |
| `probe-mutation-bug` | 2026-10-15 | pending | **DONE** | 26b4850c "a run can edit a checkout only it can write" + e0e99542 frozen-path floor on every authored patch |
| `p2-artifact-transport-dispatch` | 2026-10-13 | blocked | **DONE** | docs/architecture/artifact-transport.md written (design delivered despite the stalled run) |
| `reflect-bound-fix` | 2026-10-13 | pending | **DONE** | 99d6b172 (post-verdict reflect capped at the --deadline remainder) + cd9358e5 (reflector node cap) |
| `artifact-transport-cloud` | 2026-10-11 | pending | **DONE** | docs/architecture/artifact-transport.md + artifact-graph.md |
| `carry-patch-placement` | 2026-10-11 | pending | **DONE** | mini_ork/cli/publisher_authored_patch.py:455-470 precedence CLI name > profile carry_patch > recovery.carry_patch |
| `researcher-recover` | 2026-10-11 | pending | **UNKNOWN** | run-le-1791359434-64879-1 absent from .mini-ork/runs; the researcher home is outside the readable repos |
| `n3-epic` | 2026-10-11 | pending | **DONE** | 53004136 "fix(recover): never reuse a node whose direct verifier failed after its output" (N3) |
| `wizard-launch-wiring` | 2026-10-11 | pending | **DONE** | 8c612b92 merged (docs/todos/2026-10-13-remaining-tasks-resolution.md, run-1791925489-75510) |
| `publisher-hang` | 2026-10-11 | in_progress | **DONE** | 99d6b172 (post-verdict reflect bounded by deadline) + cd9358e5 |
| `wu-7c3a91e2-judge` | 2026-10-11 | in_progress | **DONE** | laguna judge prompts land in the tracked tree (2a37fe44, d5ca9f33, 1c0134e8); --force revive plumbed in mini_ork/recovery/supervisor.py:418; run learning-judge-4l-20261015125313 holds 4 judge audits + synthesis.md + panel-verdict.json (that run still verdict=fail; family collision fixed later in dc2d8793) |
| `wu-7c3a91e2-n3` | 2026-10-11 | pending | **DONE** | 53004136 (N3 recover reuse), listed in Tier-1/gaps table |
| `orphaned-relaunch` | 2026-10-11 | pending | **DONE** | docs/todos/2026-10-11-orphaned-run-census.md (91 orphaned + 76 other-repo) and handoff T1 RESOLVED; fix bf5a02e9 |
| `fix-recover-lease-fence` | 2026-10-11 | pending | **DONE** | 1622178d "recovery: bind the lease token to the run it fences" |
| `regen-prescreen-on-recover` | 2026-10-11 | pending | **DONE** | 848760ea "fix(recovery): re-run rubric prescreen after a revived recover" |
| `persist-failed-dispatch-rows` | 2026-10-11 | pending | **DONE** | 4164b266 "fix(dispatch): leave llm_calls evidence for every failed dispatch attempt" |
| `researcher-run-recover` | 2026-10-10 | pending | **UNKNOWN** | same as researcher-recover: run dir absent, researcher home unreadable |
| `revive-wave` | 2026-10-10 | in_progress | **DONE** | docs/todos/2026-10-10-revive-set-base-drift-triage.md: all 10 targets closed as superseded |
| `levels-verdict-bug` | 2026-10-10 | pending | **DONE** | 64275077 (levels read the framework-edit test envelope; bare pass:false is REFUTED) + 673daa53 (run verdict/publisher refuse pass:false) |
| `launch-extra-env` | 2026-10-10 | pending | **DONE** | mini_ork/web/routes/runs.py:37 extra_env body forwarded to launch_run |
| `wu-7c3a91e2-t3` | 2026-10-10 | pending | **DONE** | recipes/audit-judge-panel-3l + 738a938b four-family judge panel |
| `green-rerun` | 2026-10-10 | in_progress | **UNKNOWN** | searched repo + log for b1gf5p0o1: no hit anywhere |
| `revive-queue` | 2026-10-09 | pending | **STALE** | superseded by docs/todos/2026-10-09-kickoff-triage.md reclassification of all 231 kickoffs (+10-10/10-11 dispositions) |
| `status-divergence` | 2026-10-09 | pending | **STALE** | docs/todos/2026-10-09-kickoff-triage.md CORRECTION: 'There is no status bug' - verdict.json is the implementer verdict, run-verdict.json the run outcome |
| `run-reflect-pass-prove-themes-promote` | 2026-10-09 | pending | **DONE** | lesson_themes holds 6,378 rows in .mini-ork/state.db; 26717cdf themes upsert (RB-20) |
| `a9-circular-evidence` | 2026-10-09 | pending | **DONE** | aae3dd7f artifact@v1 reward (mini_ork/learning/writeback.py:36) replaces the saturated status reward |
| `inbox-dismiss` | 2026-10-08 | pending | **DONE** | mini_ork/ide_pages/inbox.py:111/159 - board dismiss writes dismissed.json and the row hides |
| `run-1791469896-2926` | 2026-10-08 | failed | **UNKNOWN** | run dir absent from .mini-ork/runs; no k3/ask-k3 commit anywhere in --all log |
| `k3-rev3` | 2026-10-08 | in_progress | **UNKNOWN** | same: no run dir, no branch/worktree, no commit naming the round-3 revision |
| `watch-askk3` | 2026-10-08 | in_progress | **UNKNOWN** | no run-ask-k3 artefact or verdict exists to watch |
| `orchestrator-roadmap` | 2026-10-08 | pending | **UNKNOWN** | no D1-D6 roadmap document; SESSION-GOALS.md 10-19 still lists it pending |
| `p1-visual-review` | 2026-10-08 | pending | **UNKNOWN** | no screenshot review artefact found under docs/ or the ui repo |
| `criteria-continuation` | 2026-10-08 | pending | **DONE** | mini_ork/kickoff_scope.py _logical_lines joins wrapped bullets/continuations; 'kickoff-bullet-continuation' shipped per kickoff triage |
| `landed-markers` | 2026-10-08 | pending | **DONE** | 57 landed.json files under .mini-ork/runs (target was 49) |
| `ide-run-flow` | 2026-10-08 | in_progress | **DONE** | kickoff triage SHIPPED; mini_ork/ide_pages/run_flow.py |
| `b2b-kill-recover` | 2026-10-08 | pending | **DONE** | salvage 3a98638b1b + implementer 2f94dc61/7497809 in the fork |
| `recover-lease-renewal` | 2026-10-08 | pending | **DONE** | 151ca634 (refresh keyed on token identity; ownership lost on supersede) + refresh_lease called from execute.py/supervise.py |
| `revive-b1` | 2026-10-08 | pending | **DONE** | 83ab579d97 "Orca's neutral dark palette and state language, shared components" on the fork |
| `reap-run-filter` | 2026-10-08 | pending | **DONE** | `5075b919`: `reap(..., run_ids=…)` + repeatable `--run ID` (deduped, an absent id matches nothing), four tests in `tests/unit/test_run_reaper.py` |
| `eval-incoherent-rust` | 2026-10-08 | pending | **UNKNOWN** | coherence metric exists (mini_ork/learning/eval_judge.py:469) but no recorded finding for the coh=0 Rust case |
| `orca-zombie-rows` | 2026-10-08 | pending | **UNKNOWN** | still 'pending' in docs/audits/2026-10-19-session-goals/SESSION-GOALS.md; no reconciliation commit or artefact |
| `wu-9c41d7e2` | 2026-10-08 | in_progress | **DONE** | .mini-ork/runs/run-orchestrator-scaffold-20261008124718/landed.json -> landed as 3a913a81 |
| `orca-recover-3` | 2026-10-08 | in_progress | **DONE** | b1-palette 83ab579d97, b2a shipped, b2b salvage 3a98638b1b; ide-orca-b1/b2a/b2b run dirs present |
| `ide-run-panel-rust` | 2026-10-08 | pending | **DONE** | b57f5ee4 "the Run panel's data - Changes, Checks, Agents, Cost, Learned"; ui run-dock commits |
| `ide-run-dock` | 2026-10-08 | in_progress | **DONE** | kickoff triage SHIPPED; mini_ork/ide_pages/run_dock.py |
| `agent-session-isolation` | 2026-10-07 | in_progress | **DONE** | kickoff triage SHIPPED; run dir agent-session-isolation-20261007210322 holds the implementer diff + live proof |
| `concord-loop-wrap` | 2026-10-07 | pending | **DONE** | concord_run.start(run_id, recipe, kickoff) at mini_ork/cli/main.py:1943 - every run opens a concord session |
| `lane-repair-channel` | 2026-10-07 | in_progress | **STALE** | kickoff triage: lane-repair-channel SUPERSEDED, lane-repair-channel-r2 SHIPPED |
| `lane-repair-surfaces` | 2026-10-07 | pending | **DONE** | 3e05a64f one attention surface (inbox derived from stores) + needs-you truth shipped |
| `shadow-verify` | 2026-10-07 | pending | **DONE** | 339 probe-validity.json artefacts, e.g. run-1792390047-62007 with "mode": "shadow" |
| `lane-repair-explore` | 2026-10-07 | in_progress | **STALE** | superseded: kickoff triage lists lane-repair-resume SHIPPED |
| `wave5-carry-patch` | 2026-10-07 | pending | **UNKNOWN** | the wave5 researcher recipe is not in this repo; carry_patch support exists (publisher_authored_patch.py:455) |
| `triage-subcommand` | 2026-10-07 | pending | **DONE** | mini_ork/cli/triage.py registered at cli/main.py:120; tests/unit/test_triage_cli_py.py |
| `learn-run-tab` | 2026-10-07 | in_progress | **DONE** | kickoff triage SHIPPED; mini_ork/ide_pages/learn/ (lessons/memory/overview) |
| `threads-dag-prepush` | 2026-10-07 | pending | **DONE** | mini_ork/cli/board_cmd.py:1177 board runs --query/--offset/--limit with FTS5; ui load-more commits 2717205/70651b8 |
| `learning-tab-render` | 2026-10-07 | pending | **DONE** | kickoff triage learn-node-tab SHIPPED; learn pages render the injected block and item text |
| `recover-verify` | 2026-10-07 | in_progress | **DONE** | mini_ork/recovery/plan.py:808 strategy 'verify' (entry = first verifier) + e77fc90f r2 fixes |
| `recover-verify-only` | 2026-10-07 | pending | **DONE** | same: plan.py verify strategy overlay/planner-checkpoint/loop wiring; worktree recover-verify |

## Still genuinely open

- ~~`release-stage1`~~ — **DONE `0264da4d`**: the checkpoint landed the same day in a
  deterministic shape — single-source `mini_ork/version.py`, `release.yml` calls the
  full CI matrix as its gate, and both the tag↔version check and the wheel↔version
  check fail closed. Not rebuilt as a recipe: a tag push must not need LLM credentials.
- ~~`cost-sidecar-gap`~~ — **DONE `e0a02954`**: lanes do pass the gate; the meter under-charged
  each multi-lane node to its last LLM call, so `cost_pause.reconcile` now sets it from `llm_calls`.
- ~~`p1-9-page-half`~~ — superseded 2026-11-04 by 32882a66: the Orchestrator tab and
  `orch_tab.py` are gone; the run's orchestrator is the ACP thread recorded in
  `<run_dir>/orchestrator.json`, and the page button opens it.
- ~~`p2-16`~~ — **DONE `e9bcbf63`**: the run page carries a Scope section (declared
  root, allow-list, deny lines, worktree branch) that states it is not a write
  sandbox, and the runs page carries a `Scope & claims` tab that flags a path two
  live runs both declare. The Spend section now reads the ceiling the dispatch gate
  enforces and marks a parked run.
- ~~`thread-runs-node-detail`~~ — **DONE `0b7461c0`**: node column + row drilldown to the run; the
  empty `thread_runs` was the CLI smoke fixtures in this home, not a missing writer.
- `ui-env-picker` — Environment picker in UI run launcher (offered)  
  next: add a placement/env selector to the run launcher page spec
- `buddy-interactive` — Make run buddy interactive/steerable like Zed agent  
  next: decide whether to build the steerable run buddy or close the task as dropped
- `ui-run-launcher-env` — Environment picker in the UI run launcher (offered)  
  next: same as ui-env-picker (duplicate id)
- ~~`reap-run-filter`~~ — **DONE `5075b919`**: `mini-ork reap --run <id>` (repeatable) filters the sweep; an id absent from `state.db` matches nothing rather than erroring
