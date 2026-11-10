# G1-ide-mo-new — task-status audit

Group: the newer half of the mini-ork IDE-surface work, 96 `tasks[]` records emitted 2026-10-14 → 2026-11-04, harvested from `<z-insight>` blocks and deduped by `task_id`. Repos read (read-only): `/Volumes/docker-ssd/ps/mini-ork`, `/Volumes/docker-ssd/ps/mini-ork-worktrees/*`, `/Volumes/docker-ssd/ps/zed-mini-ork`, plus read-only evidence from `/Volumes/docker-ssd/ps/attestia` for the rows whose subject actually belongs to that repo.

## Summary

Of 96 self-reported tasks, **60 are DONE**, **16 are OPEN**, **3 are STALE**, and **17 are UNKNOWN** (recounted 2026-11-10: `bif-e1` and `on-event-hook` closed by `d322c5ab`). The self-reports were mostly honest but badly *out of date*: almost every row that was emitted as `pending` while the work was already in flight is now backed by a real commit — the `eco6-k*` wave (K13 04b67bd5, K14 b10821ea, K16 e9682838), the whole RUN-BUGS queue (RB-08a 4bec0cb3, RB-09 87a52760, RB-13 0e0c9bd4, RB-20b c36418e9, RB-24 3f5c3190, RB-26 71aa5c54, RB-33 3da373d2, RB-50 d7bd608b), the RSI gap epics (SDE-4/5/6, RouteMoA/CoBa, SimUtil-UCB/Trace2Skill), the learning-loop waves (R1–R13), the rev6 review layers (R5a 7af706df, R5b 640d37e0), and the verification stack ⑧⑨⑫⑬⑭ are all verifiably landed. Four things genuinely still need doing: (1) **the SDD mechanism roadmap is still half-built** — I2 landed but I4/I6/I7/I8/I9 artifacts are absent and `kickoffs/sdd-mechanisms/DECISIONS.md` still records `sdd-i2-probe-author MISSING`, so there is no ratification gate and no SDD page in `mini_ork/ide_pages/`; (2) **the Attestia control plane is built but never applied or gated** — `docs/03-engineering/control-plane-mvp.md` says "Migration not yet applied to a live database" and `rlsIsolation.test.ts:25` skips the RLS suite unless `DATABASE_URL` is set; (3) **the IDE GUI smoke is still blocked on a human** — the orchestrator-page audit marks `replay-GUI` "open — blocked on user", which also keeps `ide-live-smoke` open, and the onboard path still only links zed.dev instead of installing a prebuilt signed build. The 17 UNKNOWNs are not failures: they are rows whose subject is an operational act (secret rotation, a Finder mount, a UI restart), an approval that never arrived, or a repo outside the three audited ones, and each row says what was searched.

## Verdict table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| v4-dead-detector | 2026-11-04 18:24 | pending | **DONE `d97c9ee1`** | the triage funnel is the production writer: `mini_ork/triage/failures.py:24` imports `blame_attributor.attribute` and :413 calls it per failure, so `defect_attributions` fills on a real failure. The V4 rule still reports a candidate, but its detail now says the call never fires on live data — a data condition, not an unwired writer (the audit's row read the harvest docstring without following the import) |
| verification-flags-doc | 2026-11-04 14:03 | pending | STALE | superseded by `docs/operator/feature-flags.md` "## Verification stack" + `docs/architecture/verification-stack.md` ①–⑭ table; no `docs/operator/verification-flags.md` was ever needed |
| verif-wiring-docstring | 2026-11-02 14:58 | pending | DONE | `mini_ork/gates/verifier_mutation.py` now states the measured/`kill_score` contract (acd9ccaf) |
| acp-sidebar | 2026-11-01 20:33 | pending | DONE | both named prerequisites exist: `mini_ork/web/routes/agent_server.py:143` POST `/api/conversations`; `mini_ork/web/routes/sockets.py:306` ws `/sockets/events/{conversation_id}` |
| pytest-collision | 2026-11-01 13:45 | pending | DONE | no duplicate basenames remain: 900 `tests/test_*.py` all unique; `extensions/vscode/test/` = verbs/discovery/sse, unique |
| shared-config-guard | 2026-10-30 07:16 | pending | DONE | `.githooks/pre-push:57` `_drop_repo_env()` called at :199; pinned by `tests/unit/test_pre_push_repo_env.py` (e58c4219) |
| rrl-7 | 2026-10-29 14:31 | failed | UNKNOWN | searched log 10-07→11-04 for `--domain verification` and rrl docs; only worktrees `rrl-engine`, `rrl-plan`, `rrl-3b-replay-scratch` exist — no plan/monitor doc |
| attest-m1-checker | 2026-10-29 13:18 | pending | OPEN | ten `.att` programs + `// REJECTED` variants are untracked under `docs/research/agentic-language-v0/programs/`; `docs/research/2026-10-29-agentic-language-design-v0.md` §10 item 1 is still unimplemented — no parser/typechecker module |
| rrl-6 | 2026-10-29 09:48 | pending | UNKNOWN | no rrl CLI/monitor/docs commit in the window; only the `rrl-*` worktree dirs were found |
| rls-force-refactor | 2026-10-29 09:32 | pending | DONE | attestia 40ef419 "fix(tenant): hold one RLS-scoped connection per request" + `server/database/migrations/0150_platform_rls_coverage.sql` |
| concierge-completeness | 2026-10-29 09:32 | pending | DONE | attestia e2f7ef6 "make the console operational" + b13d399; `server/concierge` present |
| console-ui-landing | 2026-10-29 09:10 | pending | DONE | attestia d0d2eb0 "ship the Railway-shaped console"; `app/` = 632 files |
| t1-apply-chain | 2026-10-29 08:24 | pending | OPEN | layer routing efacad66 / 744fa61c / b6045f8d and H2 kickoffs 2cfad913 / 9676ad37 landed, but no prompt-bloat guard exists (grep `prompt_bloat`/`PROMPT_BLOAT` in `mini_ork/` → 0 hits) |
| ci-shard-timeout | 2026-10-28 18:33 | pending | DONE | 619f4cf5 "ci: ground the 30 min shard timeout in measured runner time"; fdf95513 "run each python test shard on every runner core" |
| brace-2604-support | 2026-10-28 15:07 | pending | UNKNOWN | no `ubuntu`, `26.04`, `bubblewrap`, or `sandbox-exec` commit in the window log; no matching branch or worktree |
| console-log-search-ui | 2026-10-28 13:02 | pending | DONE | attestia de2ceed "retain service logs and search the archive" |
| rls-tenant-gate | 2026-10-28 13:02 | pending | OPEN | `server/platform/__tests__/rlsIsolation.test.ts:25` — `const describeRls = databaseUrl ? describe : describe.skip`; the gate never runs without `DATABASE_URL` |
| otlp-tracing | 2026-10-28 13:02 | pending | DONE | attestia 6113a29 "export OTLP spans … off by default"; mini-ork side K10 `d10a34a2` |
| fix-probe-grader | 2026-10-27 12:04 | in_progress | DONE | `mini_ork/learning/probe_scorer.py:809` `[sys.executable, "-E", "-P", "-B", …]`; 89553eeb clean-snapshot grading; 1eb1842e / 8e9eaa85 |
| bif-e1 | 2026-10-27 09:42 | in_progress | DONE | stale-by-hours: `d322c5ab` (2026-10-27 11:35, on `main`) wired the hook — `node_events._insert_event` (:451-456), `recursive.mo_recursive_emit_event` (:170), `run_reaper.close_run_record` (:296, `run_finished`); `tests/unit/test_event_hook_wired.py` (36 tests with `test_mo_emit_hook_py.py`) green under python3.11 |
| gsc-sitemaps | 2026-10-27 09:30 | pending | UNKNOWN | `/Volumes/docker-ssd/ps/miniork-site/sitemap.xml` exists; no Search-Console submission artifact in any audited repo |
| on-event-hook | 2026-10-27 09:30 | pending | DONE | duplicate of bif-e1 — closed by `d322c5ab`; the module now has three callers in production code |
| ci-verify-e27d3ac4 | 2026-10-26 12:40 | in_progress | UNKNOWN | e27d3ac4 is a *docs* commit ("record phase 1 as built"); no CI-run receipt exists in the repos to confirm the later green state |
| rev6-r5 | 2026-10-26 12:25 | pending | DONE | rev6 R5a salvage 7af706df + R5b rounds-to-pass 640d37e0; decision record `docs/plans/2026-10-26-review-layers.md` |
| eco6-k16 | 2026-10-25 18:03 | in_progress | DONE | e9682838 removed the three unread `recursion:` keys |
| eco6-docs | 2026-10-25 18:03 | pending | DONE | `docs/plans/2026-10-25-ecosystem-section6.md` carries the Decisions table + "Progress (complete 2026-10-25)" |
| bifrost-research | 2026-10-25 14:06 | in_progress | OPEN | B1 437b2fa3 + mock provider landed; B2 and B4 explicitly deferred per `docs/plans/2026-10-27-dispatch-resilience.md` |
| eco6-k14-k16 | 2026-10-25 13:50 | pending | DONE | K14 b10821ea (five edge types → aliases), K16 e9682838 |
| plugins-entrypoints | 2026-10-25 13:44 | pending | DONE | c554c0ad "long-lived service runtime + plugin entry-point discovery"; `mini_ork/plugins.py` present |
| eco6-wave3 | 2026-10-25 11:51 | pending | DONE | K13 04b67bd5, K14 b10821ea, K16 e9682838 |
| attestia-control-plane-mvp | 2026-10-25 11:14 | in_progress | DONE | `docs/03-engineering/control-plane-mvp.md:3` "Status: implemented 2026-10-25" |
| apply-migration | 2026-10-25 11:14 | pending | OPEN | same doc: "Migration not yet applied to a live database" |
| eco6-decide | 2026-10-25 09:49 | pending | DONE | the Decisions table in `docs/plans/2026-10-25-ecosystem-section6.md` is the consolidated record |
| sde-6 | 2026-10-22 09:02 | pending | DONE | SDE-6a 824d5700, SDE-6b ad52777f (`docs/plans/2026-10-22-rsi-gaps.md`) |
| sde-5 | 2026-10-22 07:44 | pending | DONE | SDE-5a 7a85d5df, SDE-5b d7694192 |
| sde-4 | 2026-10-22 07:13 | pending | DONE | SDE-4a f1f93760, SDE-4b a9cbcfc0 |
| router | 2026-10-22 07:13 | pending | DONE | RouteMoA cf53d09e, CoBa 9622b46d (CoBa later removed by K2 3de6813e) |
| memory | 2026-10-22 07:13 | pending | DONE | SimUtil-UCB 2d3b55a8, Trace2Skill bef6e2e5 |
| rbf-merge-queue-9 | 2026-10-20 21:19 | in_progress | DONE | RB-13 0e0c9bd4, RB-20b c36418e9; "root planner stays the no-op" is the deliberate fix-forward in 4bec0cb3 |
| rbf-reflect-deadline | 2026-10-20 21:18 | in_progress | DONE | c36418e9 "the detached post-run reflect enforces its own deadline (RB-20b)" |
| rbf-diff-hash-pin | 2026-10-20 21:01 | in_progress | DONE | 0e0c9bd4 "what was verified is what gets published (RB-13)" |
| rbf-queue | 2026-10-20 19:50 | pending | DONE | RB-33 3da373d2 (duration on every close path), RB-20b c36418e9 |
| rbf-merge-queue-5 | 2026-10-20 17:30 | in_progress | DONE | RB-50 d7bd608b, RB-24 3f5c3190 (fuse per placement), RB-09 87a52760 (command hints) |
| rbf-merging | 2026-10-20 17:23 | in_progress | DONE | RB-08a 4bec0cb3, RB-50 d7bd608b, RB-24 3f5c3190, RB-09 87a52760 |
| rbf-merge-queue-4 | 2026-10-20 17:20 | in_progress | DONE | RB-32a 0438ab65 (no phantom $0.05), RB-26 71aa5c54 (hermetic commands), RB-08a 4bec0cb3 |
| rbf-reaper-remote | 2026-10-20 13:58 | in_progress | DONE | f3c9eb78 "reap a remote-placement run whose local dispatcher died (RB-07, RB-45)" |
| harden-orch-arrivals-cli-test | 2026-10-19 14:53 | pending | DONE | `tests/unit/test_intake_mandate_py.py:88` passes explicit `db=`/`root=` paths, no cwd `state.db`; bd69f443 "the local source reads the database the pass was handed" |
| verify-4f781acd | 2026-10-19 10:40 | pending | OPEN | `docs/audits/2026-10-16-session-gaps/4f781acd-orch-page-redesign.md` — `replay-GUI` "**open — blocked on user** — needs the IDE window" |
| enable-web-analytics | 2026-10-19 09:50 | pending | UNKNOWN | searched `/Volumes/docker-ssd/ps/miniork-site/` (`_headers`, `sitemap.xml`) and the commit log; no Cloudflare Web Analytics beacon |
| zed-prebuilt-install | 2026-10-19 08:47 | pending | OPEN | `mini_ork/cli/onboard.py:344-349` only points at zed.dev; no prebuilt/signed install path |
| vt14 | 2026-10-19 06:09 | in_progress | DONE | ⑬ behavioral recomputation: `mini_ork/certify/recompute.py` + `mini_ork/verify/behavioral.py` (`docs/architecture/verification-stack.md:110`) |
| vt12 | 2026-10-19 06:09 | in_progress | DONE | ⑨ reward↔evidence pairing is **live**: `mini_ork/learning/reward_audit.py` + `writeback.py` (`verification-stack.md:106`) |
| vt9 | 2026-10-19 06:09 | pending | DONE | ⑧ judge validity: `mini_ork/gates/judge_validity.py` (`verification-stack.md:105`) |
| vt15 | 2026-10-19 06:09 | pending | DONE | ⑭ hidden regenerable checks: `mini_ork/certify/hidden_checks.py` (`verification-stack.md:111`) |
| vt13 | 2026-10-19 06:09 | in_progress | DONE | ⑫ discrimination-anchored test evidence: `mini_ork/certify/discrimination.py` (`verification-stack.md:109`) |
| vt10 | 2026-10-19 06:09 | in_progress | STALE | ⑪ conformal abstention — "removed (669c181c)" (`docs/architecture/verification-stack.md:108`) |
| run-1792356029-62462 | 2026-10-19 05:52 | pending | DONE | zed-mini-ork f06b8d3719 published the run-1792356029-62462 target |
| smoke-v1243 | 2026-10-18 20:15 | in_progress | UNKNOWN | no visual-smoke record for v1.24.3 in the repos; a GUI smoke needs a live window, which is exactly the blocked step in the 4f781acd audit |
| rotate-cf-token | 2026-10-18 19:52 | pending | UNKNOWN | a secret rotation leaves no repo receipt; searched config/ and docs/ for the token name |
| docs-repo-remote | 2026-10-18 19:50 | pending | OPEN | `/Volumes/docker-ssd/ps/miniork-docs` has **no remote** (`git remote -v` empty) |
| rebuild-relaunch | 2026-10-18 18:08 | pending | DONE | zed fork b60a6e02c2 + 1e19ae6ffd pin bump/rebuild; `crates/mini_ork_ui/src/page.rs:787-816` carries the new tab badges |
| orch-tab-tests | 2026-10-17 20:48 | pending | DONE (superseded) | 32882a66 deleted the tab *and* `tests/unit/test_ide_pages_orch_tab.py` (002395a6, e8bbb609 covered a surface that no longer exists); the successors are the binding tests in `tests/unit/test_ide_pages_run.py` + `tests/unit/test_orchestrator_thread.py` |
| ide-render | 2026-10-17 16:51 | pending | DONE | `zed-mini-ork` `crates/mini_ork_ui/src/page.rs:787-816` (tab badges) and `flow.rs:1408/1660` (timers) |
| sdd | 2026-10-17 09:32 | pending | OPEN | `kickoffs/sdd-mechanisms/DECISIONS.md` records `sdd-i2-probe-author MISSING`; no ratification gate exists |
| sdd-ide-page | 2026-10-17 07:11 | pending | OPEN | no `sdd` module in `mini_ork/ide_pages/` (full listing checked) |
| sdd-missing-epics | 2026-10-17 07:11 | pending | OPEN | I4/I6/I7/I8/I9 artifacts absent: `tests/unit/test_slice_to_fit.py`, `schemas/policies.schema.json`, `.mini-ork/policies.yaml`, `mini_ork/orchestration/guarded_loop.py`, `mini_ork/specdir/speckit.py`; only I2 landed (68190111, 4c347c84) |
| audit-doc-update | 2026-10-17 07:10 | pending | STALE | the resolutions live in `docs/audits/2026-10-16-session-gaps/AGGREGATE.md` rounds 7-9 (P1#9 → 0af700ca), not in the per-session doc |
| classified-state-working | 2026-10-17 06:23 | pending | DONE `788c4722` | Rule 6 now asks `run_reaper.probe` before it answers working: a status that still claims to move but whose `.pid` names only dead processes projects as `failed` ("the dispatcher is gone — …; /recover restarts it, /stop closes it"). Only `dead` counts — `unknown` (no `.pid`: a recursive run classified in its parent, a mirrored remote run) proves nothing. `run_mark` mirrors it so tile counts and rows agree; the "five-rule" docstring became "per the kickoff's rule spec" + a rule-6 paragraph. Six tests in `tests/unit/test_acp_task_state.py` |
| lf-r9 | 2026-10-17 04:52 | in_progress | DONE | R9 69469667 (`docs/plans/2026-10-15-learning-loop-fixes.md`) |
| rebuild-mini-ork | 2026-10-16 21:15 | in_progress | DONE | zed fork pin bump + rebuild b60a6e02c2 / 1e19ae6ffd |
| finder-visibility | 2026-10-16 19:55 | in_progress | UNKNOWN | an external SAMSUNG volume's Finder visibility leaves no repo receipt; searched mounts config and docs |
| rotate-openrouter-key | 2026-10-16 17:53 | pending | UNKNOWN | rotation is not verifiable from the repos; no `~/.researcher` change is recorded here |
| contextnest-issues | 2026-10-16 10:44 | pending | UNKNOWN | searched `docs/` and `docs/todos/` for the six drafts; only `docs/audits/2026-10-19-session-goals/SESSION-GOALS.md` mentions ContextNest issues |
| rp-e11b-steer-receipt | 2026-10-16 09:19 | pending | DONE | d71a28d8 "the steer receipt rides in the payload, not as a second stdout line" |
| lf-wave3-5 | 2026-10-15 20:21 | pending | DONE | R3 1ec260da, R9 69469667, R7b 97b46ec8, R12 72182582, R11a 3515640d, R13 7c73545d, R11b 220faf02 |
| lf-r2 | 2026-10-15 20:21 | in_progress | DONE | R2 5b8d4a98 |
| lf-r5 | 2026-10-15 20:21 | in_progress | DONE | R5 95a4af77 |
| lf-wave2 | 2026-10-15 19:31 | pending | OPEN | R5 95a4af77, R2 5b8d4a98, R10 fda51845 landed; **R8 stays off** until rsi-e8 is scoped (`ecosystem-section6.md:28`) |
| act-c-fixture | 2026-10-15 19:24 | pending | UNKNOWN | searched `mini_ork/learning/probe_harvest.py` and the log for a write-time frozen-fixture guard; no guard and no commit found |
| restart-7090-ui | 2026-10-15 19:17 | pending | UNKNOWN | an operational restart leaves no repo receipt |
| rotate-daytona-key | 2026-10-15 19:17 | pending | UNKNOWN | rotation not verifiable from the repos |
| researcher-daytona-setup | 2026-10-15 19:17 | pending | UNKNOWN | the row says "offered"; no researcher-home config change found in the audited repos |
| ide-live-smoke | 2026-10-15 17:20 | blocked | OPEN | same audit line — `replay-GUI` "open — blocked on user — needs the IDE window" |
| sde-3b | 2026-10-15 11:41 | pending | DONE | a1f458f1 model diagnoser (SDE-3b1), 07e0534d/513cb218 ledger; 02dc0cb8 "SDE-3b, VG-3, VG-1b merged" |
| K7a-acp-surfaces | 2026-10-15 11:04 | pending | DONE | `mini_ork/acp/thread_cards.py:642` `review_card(...)`, `:152` `"actions": out_actions`; `agent.py:5014` iterates card actions |
| K7b-ide-surfaces | 2026-10-15 11:04 | pending | DONE | `mini_ork/acp/commands.py:245` `"restore"`, `:1339` `handle_approve` |
| reward-g-saturation | 2026-10-15 10:59 | pending | OPEN | `docs/plans/2026-10-24-rsi-technique-status.md:385` — reward_g row: "the same-family check zeroes most verdict deltas" |
| adrs-materialization | 2026-10-15 10:59 | pending | UNKNOWN | no `docs/adrs/` directory; no `adr`-matching commit in the window log |
| slice-a-follow-locations | 2026-10-15 10:47 | pending | DONE | 4193c429 "acp: send tool-call locations so Zed can follow a mini-ork run"; `agent.py:955-956` `_live_locations_sent` |
| learn-fixes | 2026-10-15 10:33 | pending | DONE | f11ad3b1 (divergence dedupe + CLI), 5b8d4a98 (R2 scope fallback), theme-ordering in the R-wave |
| s3-policy | 2026-10-15 10:26 | pending | OPEN | `docs/plans/2026-10-28-open-gaps.md` — "Still open: … S3 (framework-theme policy) … need a user decision" |
| should-escalate | 2026-10-15 10:22 | pending | DONE | d1606e13 "aggressive escalation policy (should_escalate)" + 1d88b72e SDE-3a wiring |
| act-b-e | 2026-10-15 10:21 | pending | OPEN | a decision task ("user's pick among B/C/D/E/F"); no pick is recorded in docs or config |
| wave2-loop | 2026-10-15 05:53 | pending | DONE | S4 811aa4a9 (pass-stats producer), S2 e6ffe34b (scheduled reflect), S6 healer retired (`mini_ork/recovery/healer.py` gone) |
| wave3-durable | 2026-10-15 05:53 | pending | DONE `fe32deca` | N4: `stores.lease.take_lease(db, run_id)` takes the run's lease even when a live one names another owner (fencing is what makes the steal safe — the displaced token's next `fence_or_reject` fails), and `mini-ork recover <run> --take-lease` falls back to it when `acquire_lease` refuses, printing whose lease it displaced. The audit's grep→0 was the missing *reclaim*, not the lease itself: `acquire_lease` has been used by `recovery/planner.py` all along, and the open half was the 15-minute TTL a dead holder blocked a run for |
| act-d-repro | 2026-10-14 11:19 | pending | UNKNOWN | no `path_map` crash repro or fix commit in the window; the translation code is at `mini_ork/dispatch/providers.py:1939` |

## Still genuinely open

1. ~~**v4-dead-detector**~~ — **DONE `d97c9ee1`**: `mini_ork/triage/failures.py:413` calls `blame_attributor.attribute` per failure, so the funnel writes `defect_attributions`.
2. **attest-m1-checker** — write M1: a Python parser + type checker that accepts the ten `.att` programs and rejects the 43 variants; commit them alongside `docs/research/agentic-language-v0/programs/`.
3. **t1-apply-chain** — add the missing prompt-bloat guard (the layer-routing and H2 halves are already merged).
4. **sdd-missing-epics** — build I4/I6/I7/I8/I9, starting with the artifacts `DECISIONS.md` names as MISSING.
5. **sdd** — stand up the operator ratification gate once the probe-author mechanism exists.
6. **sdd-ide-page** — add an `ide_pages/sdd.py` projection so the mechanisms render in the Zed-fork UI.
7. ~~**classified-state-working** — fix `acp/task_state.py` Rule 6 so a dead `classified` classification is not reported as working, and update the "five-rule" docstring.~~ **DONE `788c4722`**.
8. ~~**wave3-durable** — implement N4 lease reclaim (`recover --take-lease` or release on unclean exit) so a killed dispatcher cannot orphan a run.~~ **DONE `fe32deca`**.
9. **apply-migration** — apply `0100_control_plane.sql` + `0150_platform_rls_coverage.sql` to a live database.
10. **rls-tenant-gate** — run `rlsIsolation.test.ts` against a real `DATABASE_URL` and make the skip fail closed.
11. **verify-4f781acd / ide-live-smoke** — run the GUI replay of all eight orchestrator tabs with the IDE window open (one human in the loop).
12. **bifrost-research** — scope B2 and B4 (both explicitly deferred in `docs/plans/2026-10-27-dispatch-resilience.md`).
13. **lf-wave2** — decide whether R8 (opt-in supervisor dispatch) proceeds once rsi-e8 is scoped.
14. **docs-repo-remote** — add a remote to `/Volumes/docker-ssd/ps/miniork-docs`.
15. **zed-prebuilt-install** — ship a prebuilt, signed Zed install path in `mini-ork onboard` instead of a zed.dev link.
16. **reward-g-saturation** — measure the reward-g same-family zeroing and pick a fix (new family or a different normalization).
17. **s3-policy** — surface the framework-theme policy decision to the user and record the answer.
18. **act-b-e** — get the user's pick among the B/C/D/E/F triage follow-ups and record it.
