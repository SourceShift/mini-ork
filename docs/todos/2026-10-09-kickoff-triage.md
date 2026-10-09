# Kickoff backlog triage — `kickoffs/auto/` (2026-10-09)

231 kickoffs classified by **run-record evidence**: was it launched, and did its own run pass.

## Method (two reliable sources)

1. **Launched?** — `task_runs.kickoff_path` join (DB records every run's kickoff) + presence of `runs/<slug>-<ts>/`.
2. **Passed?** — the run dir's own `verdict.json` (`pass` flag) = ground truth, independent of the noisy `task_runs.status` column.

(A third source — matching kickoffs to `git` landing commits — was tried and **rejected**: token overlap over-matches (`cert-c1` matched an unrelated recovery commit) and literal-slug matching under-matches (`heldout-runner` ships as the prose "held-out runner"). Some never-launched kickoffs are in fact commit-shipped; they are flagged below, not silently folded in.)

## Result

| Bucket | n | Meaning |
|---|---|---|
| SHIPPED | 110 | published run, or the run's own `verdict.json pass:true` |
| FAILED_REAL | 4 | the run ran and failed (`verdict.json pass:false`) — revive |
| FAILED_NO_ARTIFACT | 19 | launched, aborted before producing a pass artifact — revive or drop |
| NEVER_LAUNCHED | 98 | no run record at all — the real backlog |
| **total** | **231** | |

### Headline findings

1. **Only 4 kickoffs truly failed.** The `task_runs.status='failed'` column is unreliable — 39 rows read `failed` while their own `verdict.json` says `pass:true` (a status-write divergence, same class as the reflect-trace leak).
2. **The real backlog is 98 never-launched kickoffs**, concentrated in coherent families (below).
3. **Commit-shipped-but-never-launched exists** (e.g. `heldout-runner` → `5693e0e7`), so NEVER_LAUNCHED is an upper bound on outstanding work.

## NEVER_LAUNCHED — by family

- **zed** (27): zed-s0-recipe-catalog-and-client-caps, zed-s1-task-status-titles, zed-s2-fleet-view, zed-s3a-recipe-catalog-inspector, zed-s3b1-recipe-author-core, zed-s3b2-guided-recipe-creation, zed-s4-task-worktrees, zed-s5-review-merge-discard, zed-s6a-automations-engine, zed-s6b1-automations-surface, zed-s6b2-automation-approval, zed-s7a-race, zed-s7b-kickoff-helper, zed-slice0c-acp-rebuild, zed-w1-native-worktrees, zed-z10-acp-setup-auth, zed-z2-history-attach, zed-z3-live-agent-output, zed-z4-diffs, zed-z5-slash-commands, zed-z6-modes-plan, zed-z7-mcp-context, zed-z8-setup-docs, zed-z9a-mcp-control, zed-z9b-orchestrator-harness, zed-z9c1-orchestrator-thread, zed-z9c2-thread-history
- **rsi** (19): rsi-e1-code-arm, rsi-e2-probe-harvest, rsi-e2a-pre-state, rsi-e3-certificate, rsi-e4-edit-scope, rsi-e5-harness-contrast, rsi-e6-harness-evolution, rsi-e7-layer-routing, rsi-e8-unfireable-kernel, rsi-g1-cascade-blindspot, rsi-g2-null-verdict, rsi-g3-gate-fuzzer, rsi-g4-memory-retirement, rsi-h1-harness-edit-operator, rsi-h2-hack-probe, rsi-h3-harness-integrity, rsi-h4-collapse-precursor, rsi-h5-metric-anchor, rsi-h9-active-eval
- **rlm** (11): rlm-1-trace-contract, rlm-10-daytona-body, rlm-11-doc-update, rlm-2-lane-router-grouping, rlm-3-store-port, rlm-4-decision-service, rlm-5-deadline-budget, rlm-6-context-paging, rlm-7-integration-validate, rlm-8-bookgen-reward-source, rlm-9-offline-loop-bookgen
- **arx** (8): arx-1-vero-harness, arx-2-grasp-skills, arx-3-tacomas-split, arx-4-skillcat-stages, arx-5-siga-context-self-update, arx-6-apo-prompt-mutation, arx-7-engram-typed-memory, arx-8-arxiv-cron
- **cert** (6): cert-c1-port-oracle-engine, cert-c2-certify-cli, cert-c3-repo-context, cert-c4-redefine-guard, cert-c5-invariant-evidence, cert-c6-inconclusive-invariants
- **cost** (3): cost-ledger-budget-guards, cost-meter-engine-fallback, cost-meter-list-price
- **acp** (1): acp-launch-robustness
- **apply** (1): apply-significance-gate
- **docs** (1): docs-no-change-guard
- **framework** (1): framework-edit-diff-scope
- **impl** (1): impl-run-verified-reward
- **install** (1): install-oneliner
- **lane** (1): lane-preflight-hint
- **node** (1): node-event-emission-rewire
- **recover** (1): recover-lease-renewal
- **run** (1): run-cost-reconcile
- **sqlite** (1): sqlite-readonly-idle-wal
- **t6** (1): t6-higram-micrograph-path-level-localization
- **t7** (1): t7-scm-valuetagger-and-adaptive-forgetting-threshold
- **t8** (1): t8-selective-forgetting-eviction-pass
- **t9** (1): t9-timem-temporal-hierarchical-memory-tree
- **u4c** (1): u4c-goal-loop-sweep-node-and-kickoff-templating
- **u4d** (1): u4d-goal-loop-wave-kickoff-env
- **verifier** (1): verifier-env-scrub
- **vt1** (1): vt1-mr-relations.smoke
- **vt2** (1): vt2-hackability-audit.smoke
- **vt3** (1): vt3-equivalence-operator.smoke
- **vt4** (1): vt4-differential-delta.smoke
- **vt5** (1): vt5-mutation-adequacy.smoke
- **vt6** (1): vt6-level-vector.smoke

## Revive queue (FAILED_REAL + FAILED_NO_ARTIFACT)

- `agents-local-overlay` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `arx-0-scout` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `heldout-runner` — FAILED_REAL — verdict.json pass:false
- `ide-orca-b1-palette` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `ide-pages-p1-fixes` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `ide-pages-review` — FAILED_REAL — verdict.json pass:false
- `node-commands` — FAILED_REAL — verdict.json pass:false
- `publish-carry-plus-edits` — FAILED_REAL — verdict.json pass:false
- `rsi-i1-control-arm-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i1-control-arm` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i2-verifier-audit-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i2-verifier-audit` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i2b-audit-sensitivity` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i3-bsg-va-wiring-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i3-bsg-va-wiring` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i4-collapse-halt-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i4-collapse-halt` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i4b-collapse-writer` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i5-harness-sweep-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i5-harness-sweep` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i6-abstain-gate-repair` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i6-abstain-gate` — FAILED_NO_ARTIFACT — launched, no pass artifact
- `rsi-i6b-abstain-saturation` — FAILED_NO_ARTIFACT — launched, no pass artifact

## Full table
| slug | title | bucket | evidence |
|---|---|---|---|
| `acp-launch-robustness` | ACP / web launch: start the run on the right Python, and never wait fo | NEVER_LAUNCHED | no DB run, no run dir |
| `agent-session-isolation` | Agent sessions run with mini-ork's settings, not the operator's person | SHIPPED | published run |
| `agents-local-overlay` | Per-user LLM lane config: tracked agents.yaml = template, agents.local | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `apply-significance-gate` | Apply gate: exact significance test on per-task flips (opt-in enforcem | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-0-scout` | ARX-0 arxiv-scout recipe — autonomous paper-to-epic discovery | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `arx-1-vero-harness` | ARX-1 Adopt VeRO verifier-guided self-improvement harness | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-2-grasp-skills` | ARX-2 GRASP — gated regression-aware skill proposer | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-3-tacomas-split` | ARX-3 TacoMAS — split fast capability loop from slow topology loop | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-4-skillcat-stages` | ARX-4 SkillCAT — three-decision skill pipeline rename | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-5-siga-context-self-update` | ARX-5 SIGA — self-rewriting grounding from trajectories | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-6-apo-prompt-mutation` | ARX-6 MemMachine + APO — auto-prompt optimization per role | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-7-engram-typed-memory` | ARX-7 ENGRAM — typed lightweight memory orchestration | NEVER_LAUNCHED | no DB run, no run dir |
| `arx-8-arxiv-cron` | ARX-8 Cron-style daemon for arxiv-scout | NEVER_LAUNCHED | no DB run, no run dir |
| `auto-repair-loop` | Auto-repair: a failed run fixes itself in a bounded feedback loop, the | SHIPPED | published run |
| `cert-c1-port-oracle-engine` | C1 — certify: port the solve-time oracle engine into `mini_ork/certify | NEVER_LAUNCHED | no DB run, no run dir |
| `cert-c2-certify-cli` | C2 — `mini-ork certify`: one command that says whether a change is act | NEVER_LAUNCHED | no DB run, no run dir |
| `cert-c3-repo-context` | C3 — certify: show the oracle the code under test, and refuse probes t | NEVER_LAUNCHED | no DB run, no run dir |
| `cert-c4-redefine-guard` | C4 — certify: reject probes that import the code under test and then r | NEVER_LAUNCHED | no DB run, no run dir |
| `cert-c5-invariant-evidence` | C5 — certify: put each invariant's source and failure detail in the ce | NEVER_LAUNCHED | no DB run, no run dir |
| `cert-c6-inconclusive-invariants` | C6 — certify: an invariant failure counts against the patch only if th | NEVER_LAUNCHED | no DB run, no run dir |
| `cost-ledger-budget-guards` | Budget guards and run cost read the llm_calls ledger, not task_runs.co | NEVER_LAUNCHED | no DB run, no run dir |
| `cost-meter-engine-fallback` | Cost meter: fall back to the engine's shipped pricing table | NEVER_LAUNCHED | no DB run, no run dir |
| `cost-meter-list-price` | Price non-Anthropic models on claude-CLI lanes at their real list pric | NEVER_LAUNCHED | no DB run, no run dir |
| `docs-no-change-guard` | docs recipe: fail the implementer when it changed nothing | NEVER_LAUNCHED | no DB run, no run dir |
| `eng-code-findings` | Code findings — harvest what reviews and verifiers said about each fil | SHIPPED | published run |
| `eng-path-rules-r2` | Path-scoped rules — revision 2: the table rebuild must survive depende | SHIPPED | published run |
| `eng-path-rules` | Rules that follow your code — path-scoped preferences + `mini-ork pref | SHIPPED | published run |
| `eng-rules-tab` | "Rules" tab — everything agents are told in this repo, and control ove | SHIPPED | published run |
| `eng-your-code-tab-r2` | "Your code" tab — revision 2 (Opus review of run eng-your-code-tab-202 | SHIPPED | published run |
| `eng-your-code-tab` | "Your code" tab — what mini-ork has learned about each part of your co | SHIPPED | verdict.json pass:true |
| `eval-green-abstention` | A green-but-unverified test suite is an abstention, not a failure, in  | SHIPPED | published run |
| `failed-run-lifecycle` | A failed run is handed to auto-repair right after execute, and post-ru | SHIPPED | published run |
| `files-abs-resolve` | Changed files resolve to a real path, so "Open" works and the label sh | SHIPPED | published run |
| `framework-edit-diff-scope` | framework-edit.diff must be scoped to the run's declared scope — concu | NEVER_LAUNCHED | no DB run, no run dir |
| `heldout-runner` | Held-out task runner: execute mined tasks and grade them with hidden t | FAILED_REAL | verdict.json pass:false |
| `ide-board-perf-r2` | IDE board perf — revision 2 (Opus review of run ide-board-perf-2026100 | SHIPPED | verdict.json pass:true |
| `ide-board-perf-r3` | IDE board perf — revision 3 (Opus review of run ide-board-perf-r2-2026 | SHIPPED | verdict.json pass:true |
| `ide-board-perf-r4` | IDE board perf — revision 4 (Opus review of run ide-board-perf-r3-2026 | SHIPPED | verdict.json pass:true |
| `ide-board-perf-r5` | IDE board perf — revision 5 (Opus review of run ide-board-perf-r4-2026 | SHIPPED | verdict.json pass:true |
| `ide-board-perf-r6` | IDE board perf — revision 6 (Opus review of run ide-board-perf-r5-2026 | SHIPPED | verdict.json pass:true |
| `ide-board-perf-r7` | Board poll without FastAPI — revision 7 | SHIPPED | published run |
| `ide-board-perf` | IDE pages — P1 revision, a `board --json` fast enough to poll, lanes t | SHIPPED | verdict.json pass:true |
| `ide-control-plane-r2` | Thread control plane + image attachments — revision 2 (Opus review of  | SHIPPED | verdict.json pass:true |
| `ide-control-plane` | The mini-ork thread is a control plane: no pickers, direct dev allowed | SHIPPED | verdict.json pass:true |
| `ide-dag-lane-logos` | mini-ork IDE: the step graph is back as the Graph view, and every step | SHIPPED | published run |
| `ide-kickoff-search-r2` | IDE kickoff tab + board runs search — revision 2 (Opus review of run i | SHIPPED | verdict.json pass:true |
| `ide-kickoff-search-r3` | IDE board runs search — revision 3 (Opus review of run ide-kickoff-sea | SHIPPED | verdict.json pass:true |
| `ide-kickoff-search-r4` | IDE board runs search — revision 4 (measured on the researcher home af | SHIPPED | verdict.json pass:true |
| `ide-kickoff-search-r5` | IDE board runs search — revision 5: simplify staleness (Opus review of | SHIPPED | verdict.json pass:true |
| `ide-kickoff-search` | IDE: the run's Kickoff tab, and Threads search + paging (`board runs`) | SHIPPED | verdict.json pass:true |
| `ide-node-changes-r2` | IDE node changes + full documents — revision 2 (Opus review of run ide | SHIPPED | verdict.json pass:true |
| `ide-node-changes-r3` | IDE node changes — revision 3 (Opus review of run ide-node-changes-r2- | SHIPPED | verdict.json pass:true |
| `ide-node-changes` | IDE node detail: what a node did (Changes view) and full-length docume | SHIPPED | verdict.json pass:true |
| `ide-node-stream-r2` | IDE node detail — revision 2 (Opus review of run ide-node-stream-20261 | SHIPPED | verdict.json pass:true |
| `ide-node-stream-r3` | IDE node detail — revision 3 (Opus review of run ide-node-stream-r2-20 | SHIPPED | verdict.json pass:true |
| `ide-node-stream-r4` | IDE node detail — revision 4 (Opus review of run ide-node-stream-r3-20 | SHIPPED | verdict.json pass:true |
| `ide-node-stream-r5` | IDE node detail — revision 5 (Opus review of run ide-node-stream-r4-20 | SHIPPED | verdict.json pass:true |
| `ide-node-stream-r6` | IDE node detail — revision 6 (Opus review of run ide-node-stream-r5-20 | SHIPPED | verdict.json pass:true |
| `ide-node-stream` | IDE node detail — every DAG node opens its agent's stream; live nodes  | SHIPPED | verdict.json pass:true |
| `ide-orca-b1-palette` | mini-ork IDE: Orca colours, shared components, spec-v2 plumbing (Rust, | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `ide-orca-b2a-triage` | mini-ork IDE: render the run outcome sections (triage, callout, hero)  | SHIPPED | published run |
| `ide-orca-b2b-story` | mini-ork IDE: render the run Story and its evidence (story, files, fin | SHIPPED | published run |
| `ide-orca-f2a-flow` | mini-ork IDE: the run flow map, part A (Rust, crates/mini_ork_ui) — ca | SHIPPED | published run |
| `ide-orca-f2b-flow` | mini-ork IDE: the run flow map, part B (Rust) — Review hub + spokes, o | SHIPPED | published run |
| `ide-orca-f3-replay` | mini-ork IDE: replay a run on its flow map (Pause / Restart / 0.5×–2×) | SHIPPED | published run |
| `ide-orca-p2-run-panel` | mini-ork IDE: the right-hand Run panel (Orca's right sidebar): Changes | SHIPPED | published run |
| `ide-orca-p3a-inbox-board` | IDE pages: an attention-first Inbox, a Kanban board, and a composer to | SHIPPED | published run |
| `ide-orca-p3b-board-inbox-render` | mini-ork IDE: draw the Kanban `columns` and the steering `composer`, a | SHIPPED | published run |
| `ide-orca-p4a-lanes-composer` | IDE data: per-lane health in the status bar header, and a "steer this  | SHIPPED | published run |
| `ide-orca-p4b-jump-status` | mini-ork IDE: ⌘J jump palette, and per-lane health in the status bar | SHIPPED | published run |
| `ide-orca-p5a-page-restyle` | IDE pages in Orca's language: Lanes as agent rows, Verify as checks, A | SHIPPED | published run |
| `ide-orca-p5b-table-restyle` | mini-ork IDE: tables, key-value lists and lists drawn in Orca's style | SHIPPED | published run |
| `ide-pages-p1-fixes` | IDE pages — P1 fixes from the panel review (actions, confirms, honest  | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `ide-pages-review` | Review: `board page` — the mini-ork IDE pages, before they merge | FAILED_REAL | verdict.json pass:false |
| `ide-run-dock` | Run page v2: the right-hand Run panel's data — Changes, Checks, Agents | SHIPPED | published run |
| `ide-run-flow` | Run flow map data: a run as Ticket → Design → Build → Integrate → Revi | SHIPPED | published run |
| `ide-run-outcome` | Run page v2: one true outcome, the next action next to it, goal and cr | SHIPPED | published run |
| `ide-run-story` | Run page v2: the step-by-step story (Orca-style transcript of what eac | SHIPPED | published run |
| `ide-spec-v2` | IDE page spec v2: section helpers for the Orca-style run workbench, an | SHIPPED | published run |
| `impl-run-verified-reward` | Implementer reward = the run verified, minus a cost penalty (env-gated | NEVER_LAUNCHED | no DB run, no run dir |
| `induce-lane` | Pattern induction — use the reflect lane, and say so when every analys | SHIPPED | published run |
| `install-oneliner` | One-line install: `curl -fsSL …/install.sh / sh` works without a clone | NEVER_LAUNCHED | no DB run, no run dir |
| `interrupted-resume` | An interrupted run resumes with one click (and by itself), without --a | SHIPPED | published run |
| `kickoff-bullet-continuation` | A kickoff bullet that wraps onto a second line stays one item; the run | SHIPPED | published run |
| `lane-preflight-hint` | Lane preflight (lane-repair B) — classify "unknown lane" failures and  | NEVER_LAUNCHED | no DB run, no run dir |
| `lane-repair-channel-r2` | Lane repair (C) — revision 2 (Opus review of run lane-repair-channel-* | SHIPPED | published run |
| `lane-repair-channel` | Lane repair (C) — tell the user in their Zed thread, and resume on the | SHIPPED | verdict.json pass:true |
| `lane-repair-hint-r2` | Lane repair (A) — revision 2: pick the run's own failed node, not a re | SHIPPED | verdict.json pass:true |
| `lane-repair-hint` | Lane repair (A) — classify a dead-lane failure, suggest a working lane | SHIPPED | published run |
| `lane-repair-resume` | Lane repair (B) — resume a failed run from its failed node on a differ | SHIPPED | published run |
| `learn-gate-r2` | Learning gate — revision 2 (Opus review of run learn-gate-202610071213 | SHIPPED | published run |
| `learn-gate` | Learning gate — "approved" requires an authored lesson; extractor fail | SHIPPED | verdict.json pass:true |
| `learn-inject` | Learning injection — record what each node was given; inject only patt | SHIPPED | published run |
| `learn-ledger` | Learning ledger — count every injection, give the pipeline a place to  | SHIPPED | published run |
| `learn-lessons-detail` | Lessons tab — open any learning and read it in full | SHIPPED | published run |
| `learn-memory-tab-r2` | Memory tab — revision 2 (Opus review of run learn-memory-tab-202610071 | SHIPPED | published run |
| `learn-memory-tab-r3` | Memory tab — revision 3: Lane fit from execution traces, not agent_per | SHIPPED | published run |
| `learn-memory-tab` | Memory tab — what mini-ork knows about you, which lane fits which work | SHIPPED | verdict.json pass:true |
| `learn-node-tab` | IDE node Learning tab — show what this node was given and what was lea | SHIPPED | published run |
| `learn-overview2-r2` | Overview additions — revision 2 (Opus review of run learn-overview2-20 | SHIPPED | published run |
| `learn-overview2` | Overview — why a class fails, one-click reap for stuck runs, learning- | SHIPPED | verdict.json pass:true |
| `learn-prefs` | Preferences you set reach every LLM node — `mini-ork prefs` | SHIPPED | published run |
| `learn-promotion-reasons` | Self-improve decisions in plain words, and whether each change is actu | SHIPPED | published run |
| `learn-restructure` | Learning & memory page — new tab structure, Overview outcomes, Bug rep | SHIPPED | published run |
| `learn-run-tab` | IDE run Learnings tab — show the text, scope "produced" to this run | SHIPPED | published run |
| `learn-themes-r2` | Learning themes — revision 2 (Opus review of run learn-themes-20261007 | SHIPPED | published run |
| `learn-themes` | Learning themes — group 10k gradients into themes, split mini-ork self | SHIPPED | verdict.json pass:true |
| `learned-db-default` | Agents get their learned lessons in every run — default MINI_ORK_DB to | SHIPPED | published run |
| `merge-oss-guard` | `mini_ork_worktree.py merge` refuses to push unclaimed paths, confiden | SHIPPED | published run |
| `needs-you-truth` | "Needs you" tells the truth: one count, no stale gates, no stuck spinn | SHIPPED | published run |
| `node-artifacts` | IDE node inspector: the artifacts a node was given and produced, and t | SHIPPED | verdict.json pass:true |
| `node-commands-r2` | Non-agent node stream — revision 2 (Opus review of run node-commands-2 | SHIPPED | verdict.json pass:true |
| `node-commands` | IDE node stream for non-agent nodes: the full command and its output | FAILED_REAL | verdict.json pass:false |
| `node-event-emission-rewire` | Re-wire node lifecycle events into `dispatch_node` | NEVER_LAUNCHED | no DB run, no run dir |
| `node-overview` | IDE node inspector: an Overview of what the node did, and transcripts  | SHIPPED | verdict.json pass:true |
| `node-timeout-orphans` | A node timeout kills everything the agent started, including commands  | SHIPPED | published run |
| `planner-context-cleanup` | Planner context: no unverified gradients, no other sessions' or projec | SHIPPED | published run |
| `post-run-verify-reuse` | Post-run verify reuses the DAG's verifier results — no duplicate suite | SHIPPED | published run |
| `publish-carry-plus-edits` | A recover publishes exactly what its implementer produced: carry patch | FAILED_REAL | verdict.json pass:false |
| `publisher-authored-patch` | In-place publish commits only the run's AUTHORED patch — never whole f | SHIPPED | published run |
| `recover-lease-renewal` | Recover must own the lease it re-dispatches under — no fenced-limp rec | NEVER_LAUNCHED | no DB run, no run dir |
| `recover-revival-fixes` | `mini-ork recover` can actually revive a run: close the dead attempt,  | SHIPPED | published run |
| `recover-task-class` | A recovered run keeps its task class (it silently became "generic" and | SHIPPED | published run |
| `recover-verify-r2` | `mini-ork recover` verify strategy — revision 2 (Opus review of run re | SHIPPED | verdict.json pass:true |
| `recover-verify` | `mini-ork recover`: reuse finished nodes for real, and a verify-only r | SHIPPED | verdict.json pass:true |
| `retry-hint-interrupted` | An interrupted run is "resume from <node>", not "Failed at ?; the caus | SHIPPED | published run |
| `retry-hint-r2` | Retry hints — revision 2 (Opus review of run retry-hint-20261007103745 | SHIPPED | verdict.json pass:true |
| `retry-hint` | Retry hints: say whether a failed run can be retried, and what must ch | SHIPPED | verdict.json pass:true |
| `retry-notify` | Tell a failed run's owner what to fix and how, then continue when they | SHIPPED | verdict.json pass:true |
| `review-pr210-scheduler-retry` | Bug-audit PR #210 — opt-in scheduler retry loop (report only) | SHIPPED | published run |
| `rlm-1-trace-contract` | Trace contract: objective_domain + structured normalized reward | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-10-daytona-body` | [researcher-repo] Native Daytona Body adapter (Layer 2) | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-11-doc-update` | [doc] Strategy doc update — Brain/Body + decision-service framing | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-2-lane-router-grouping` | lane_router objective_domain grouping on normalized g | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-3-store-port` | Store-port abstraction for the brain libs | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-4-decision-service` | Stateless decision service (read-path) | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-5-deadline-budget` | Per-request --deadline budget | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-6-context-paging` | Paged-context seam | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-7-integration-validate` | Integration validation: shared brain end-to-end | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-8-bookgen-reward-source` | [researcher-repo] Book-gen reward_source plugin | NEVER_LAUNCHED | no DB run, no run dir |
| `rlm-9-offline-loop-bookgen` | [researcher-repo] Offline learning loop on book-gen traces (Layer 1) | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e1-code-arm` | E1 code-arm probe harness — measure a code improvement | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e2-probe-harvest` | E2 probe-set growth from failures | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e2a-pre-state` | E2a pre-state capture — make the pre-implementer baseline durable and  | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e3-certificate` | E3 sequential acceptance certificate | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e4-edit-scope` | E4 edit-scope declaration and rollback targets | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e5-harness-contrast` | E5 harness-effect isolation | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e6-harness-evolution` | E6 point the apply sweep at the harness | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e7-layer-routing` | E7 route each failure to its source layer | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-e8-unfireable-kernel` | E8 the unfireable kernel | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-g1-cascade-blindspot` | G1 — measure the router's calibration error and the gate's blind spot | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-g2-null-verdict` | G2 — give the goal-loop a null verdict ("nothing to fix") | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-g3-gate-fuzzer` | G3 — a fuzzer that attacks the gate, not the artifact | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-g4-memory-retirement` | G4 — retire the guidance that went wrong, reversibly | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h1-harness-edit-operator` | H1 — turn a batch of run failures into a typed harness-edit proposal,  | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h2-hack-probe` | H2 — a reward-hacking monitor over a generation history, with a frozen | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h3-harness-integrity` | H3 — a harness-tampering audit: a decidable taxonomy over applied harn | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h4-collapse-precursor` | H4 — silent-collapse precursors: early warning that fires before the m | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h5-metric-anchor` | H5 — who grades the grader: anchor discipline for an evolved eval metr | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-h9-active-eval` | H9 — spend the fuzzing budget where the failures are likely *and* dive | NEVER_LAUNCHED | no DB run, no run dir |
| `rsi-i1-control-arm-repair` | I1 repair: make the matched-attempt control arm correct and green | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i1-control-arm` | I1 matched-attempt control arm for the apply gate (G06-T03) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i2-verifier-audit-repair` | I2 repair: feed the audit's detectors from collapse_history (they see  | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i2-verifier-audit` | I2 verifier validity audit as a promotion precondition (G03-T08) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i2b-audit-sensitivity` | I2b: the verifier audit must flag a textbook collapse (live smoke foun | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i3-bsg-va-wiring-repair` | I3 repair: the base replay must include the candidate's test files | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i3-bsg-va-wiring` | I3 validation-evidence replay on real code-fix runs (G03-T11) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i4-collapse-halt-repair` | I4a repair: collapse halt that can actually fire, as an independent tr | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i4-collapse-halt` | I4 collapse halt: wire collapse_detector into the circuit breaker (G01 | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i4b-collapse-writer` | I4b apply loop writes collapse_history (G01-T04, part b) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i5-harness-sweep-repair` | I5 repair: make cost-per-solved-task a real, per-arm gate condition | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i5-harness-sweep` | I5 harness modules as apply-sweep targets (G02-T01, E6 slice) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i6-abstain-gate-repair` | I6 repair: pass-through before requiring confidence; keep the exact-se | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i6-abstain-gate` | I6 calibrated abstain gate (G04-T05) | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `rsi-i6b-abstain-saturation` | I6b: abstain_gate must defer MORE when the verifier is unreliable, not | FAILED_NO_ARTIFACT | launched, no pass artifact |
| `run-cost-reconcile` | A run's reported cost is everything it spent | NEVER_LAUNCHED | no DB run, no run dir |
| `run-orchestrator` | Per-run orchestrator — full loop scaffold (default off) | SHIPPED | verdict.json pass:true |
| `sdd-i3-kickoff-contract` | I3 (revision 2, fresh run on the current engine) — kickoff contract li | SHIPPED | published run |
| `sdd-i5-evidence-ledger-r2` | I5 (revision 2, fresh run) — evidence ledger bound to code state (verd | SHIPPED | published run |
| `sqlite-readonly-idle-wal` | Read-only state.db opens fail on an idle WAL database | NEVER_LAUNCHED | no DB run, no run dir |
| `t6-higram-micrograph-path-level-localization` | T6 HiGram MicroGraph path-level localization | NEVER_LAUNCHED | no DB run, no run dir |
| `t7-scm-valuetagger-and-adaptive-forgetting-threshold` | T7 SCM ValueTagger and adaptive forgetting threshold | NEVER_LAUNCHED | no DB run, no run dir |
| `t8-selective-forgetting-eviction-pass` | T8 Selective forgetting eviction pass | NEVER_LAUNCHED | no DB run, no run dir |
| `t9-timem-temporal-hierarchical-memory-tree` | T9 TiMem temporal-hierarchical memory tree | NEVER_LAUNCHED | no DB run, no run dir |
| `test-order-pollution` | Three tests fail only when their whole file runs: find the leaking tes | SHIPPED | published run |
| `u4c-goal-loop-sweep-node-and-kickoff-templating` | U4c — goal-loop: wire the sweep execution node + per-unit child kickof | NEVER_LAUNCHED | no DB run, no run dir |
| `u4d-goal-loop-wave-kickoff-env` | U4d — goal-loop: separate the wave's own kickoff from the child kickof | NEVER_LAUNCHED | no DB run, no run dir |
| `verified-only-inject` | Only verified learnings in node prompts at dispatch | SHIPPED | published run |
| `verified-only-prompts` | Only verified learnings in prompt files — revert the 18 unverified dir | SHIPPED | published run |
| `verifier-env-scrub` | Run target-repo test suites without provider keys or mini-ork state in | NEVER_LAUNCHED | no DB run, no run dir |
| `vt1-mr-relations` | VT1 metamorphic relations as an execution-anchored check in the Assay  | SHIPPED | verdict.json pass:true |
| `vt1-mr-relations.smoke` | Live smoke: vt1-mr-relations (G10-T01 metamorphic relations in the Ass | NEVER_LAUNCHED | no DB run, no run dir |
| `vt2-hackability-audit` | VT2 exploit-generation hackability audit for registered gates (G09-T05 | SHIPPED | verdict.json pass:true |
| `vt2-hackability-audit.smoke` | Live smoke: vt2-hackability-audit (G09-T05) | NEVER_LAUNCHED | no DB run, no run dir |
| `vt3-equivalence-operator` | VT3 Declared equivalence operator for the behavioral verifier (G10-T07 | SHIPPED | verdict.json pass:true |
| `vt3-equivalence-operator.smoke` | VT3 live smoke: declared equivalence operator against a LIVE `mini-ork | NEVER_LAUNCHED | no DB run, no run dir |
| `vt4-differential-delta` | VT4 differential behavioural-equivalence term in the Assay oracle (G10 | SHIPPED | verdict.json pass:true |
| `vt4-differential-delta.smoke` | Live smoke: vt4-differential-delta (G10-T02 differential behavioural-e | NEVER_LAUNCHED | no DB run, no run dir |
| `vt5-mutation-adequacy` | VT5 mutation-based suite adequacy audit for the code-fix test verifier | SHIPPED | verdict.json pass:true |
| `vt5-mutation-adequacy.smoke` | VT5 live smoke: suite-adequacy audit inside a real `mini-ork run code- | NEVER_LAUNCHED | no DB run, no run dir |
| `vt6-level-vector` | VT6 non-nested correctness level vector on run verdicts + publish gate | SHIPPED | verdict.json pass:true |
| `vt6-level-vector.smoke` | VT6 live smoke: level-vector publish gate inside a real `mini-ork run  | NEVER_LAUNCHED | no DB run, no run dir |
| `vt7-defaults-on` | VT7 Turn the verification stack ON by default (G10-T01, G10-T02, G03-T | SHIPPED | published run |
| `withheld-publish-outcome` | A run whose publish was withheld is "needs you", not "failed at implem | SHIPPED | published run |
| `zed-s0-recipe-catalog-and-client-caps` | Zed S0 — one recipe catalog (project + engine), and record what the ed | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s1-task-status-titles` | Zed S1 — every task shows working / needs you / done / failed, live, i | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s2-fleet-view` | Zed S2 — the fleet view: `/runs` as a filterable table, `/status <run> | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s3a-recipe-catalog-inspector` | Zed S3a — browse and inspect recipes: `/recipes` and `/recipe <name>` | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s3b1-recipe-author-core` | Zed S3b-1 — recipe authoring core: spec → recipe files → validation →  | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s3b2-guided-recipe-creation` | Zed S3b-2 — create and edit recipes in a Zed thread: interview → draft | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s4-task-worktrees` | Zed S4 — one isolated workspace per task: a git worktree + branch for  | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s5-review-merge-discard` | Zed S5 — review, merge, discard: a finished task waits for the user's  | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s6a-automations-engine` | Zed S6a — automations engine: scheduled recipe runs that work with the | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s6b1-automations-surface` | Zed S6b-1 — automations in Zed: proposals, MCP tools, /automations tab | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s6b2-automation-approval` | Zed S6b-2 — schedule a recipe from a Zed thread: ask → proposal card → | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s7a-race` | Zed S7a — /race: the same task on 2–3 models, each in its own worktree | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-s7b-kickoff-helper` | Zed S7b — kickoff helper: draft the kickoff with the user, check it, t | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-slice0c-acp-rebuild` | Zed Slice 0c — land `mini-ork acp` on the current base | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-w1-native-worktrees` | Zed W1 — tasks live in Zed's own worktrees, so Zed's panels show them | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z10-acp-setup-auth` | Zed Z10 — first-run setup as ACP terminal auth (`mini-ork acp --setup` | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z2-history-attach` | Zed Z1+Z2 — run history and attach-to-running-run over ACP | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z3-live-agent-output` | Zed Z3 — every agent's live output, streamed into the Zed thread | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z4-diffs` | Zed Z4 — what the run changed, as diffs in Zed's Review Changes | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z5-slash-commands` | Zed Z5 — slash commands: runs, status, learnings, cost, lanes, stop, r | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z6-modes-plan` | Zed Z6 — the run's DAG as a live plan in the thread | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z7-mcp-context` | Zed Z7 — `mini-ork mcp-context`: runs, learnings, cost and lanes as MC | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z8-setup-docs` | Zed Z8 — `mini-ork zed setup/status/uninstall` and docs/ZED.md | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z9a-mcp-control` | Zed Z9a — control tools on `mini-ork mcp-context --control` | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z9b-orchestrator-harness` | Zed Z9b — orchestrator harness: one conversational turn on a chosen la | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z9c1-orchestrator-thread` | Zed Z9c-1 — the thread is an orchestrator conversation (model + mode p | NEVER_LAUNCHED | no DB run, no run dir |
| `zed-z9c2-thread-history` | Zed Z9c-2 — orchestrator threads persist, show in Thread History, and  | NEVER_LAUNCHED | no DB run, no run dir |
