# Kickoff backlog triage — `kickoffs/auto/` (2026-10-09, CORRECTED)

231 kickoffs classified by the run's **actual outcome**.

> **Correction.** The first version of this report claimed a "39-row `task_runs.status` divergence"
> and read `verdict.json` as the run outcome. That was **wrong**. `verdict.json` is the *implementer-artifact*
> verdict (static+tests pass); the **run** outcome is `run-verdict.json` (`.verdict` = pass/fail) and `task_runs.status`.
> On the real signal they **agree** — e.g. `ide-node-stream` has `verdict.json pass:true` *and* `run-verdict.json`
> `verdict:fail (failed_nodes=2)` with a rollback; `status='failed'` is correct. There is **no status bug**.

## Method (single reliable signal: the run's own outcome)

1. **Launched?** — `task_runs.kickoff_path` join + `runs/<slug>-<ts>/` dirs.
2. **Outcome?** — `task_runs.status` (`published` = shipped; `failed`/`rolled_back` = failed) and, where present, the run dir's `run-verdict.json` `.verdict`.
3. **Superseded?** — a `-rN` revision whose family has a shipped sibling.

(Matching kickoffs to `git` commits was tried and rejected: token overlap over-matches, slug-literal under-matches.)

## Result

| Bucket | n | Meaning |
|---|---|---|
| SHIPPED | 67 | `status='published'` or `run-verdict pass` |
| SUPERSEDED | 14 | failed, but a family sibling shipped |
| REVIVE | 52 | failed, no shipped sibling — candidate to re-dispatch |
| NEVER_LAUNCHED | 98 | no run record |
| **total** | **231** | |

## REVIVE (candidates to re-dispatch via mini-ork)

- `agents-local-overlay` — Per-user LLM lane config: tracked agents.yaml = template, agents.local.yaml 
- `arx-0-scout` — ARX-0 arxiv-scout recipe — autonomous paper-to-epic discovery
- `heldout-runner` — Held-out task runner: execute mined tasks and grade them with hidden tests
- `ide-control-plane` — The mini-ork thread is a control plane: no pickers, direct dev allowed, imag
- `ide-control-plane-r2` — Thread control plane + image attachments — revision 2 (Opus review of run id
- `ide-kickoff-search` — IDE: the run's Kickoff tab, and Threads search + paging (`board runs`)
- `ide-kickoff-search-r2` — IDE kickoff tab + board runs search — revision 2 (Opus review of run ide-kic
- `ide-kickoff-search-r3` — IDE board runs search — revision 3 (Opus review of run ide-kickoff-search-r2
- `ide-kickoff-search-r4` — IDE board runs search — revision 4 (measured on the researcher home after r3
- `ide-kickoff-search-r5` — IDE board runs search — revision 5: simplify staleness (Opus review of ide-k
- `ide-node-changes` — IDE node detail: what a node did (Changes view) and full-length documents
- `ide-node-changes-r2` — IDE node changes + full documents — revision 2 (Opus review of run ide-node-
- `ide-node-changes-r3` — IDE node changes — revision 3 (Opus review of run ide-node-changes-r2-202610
- `ide-node-stream` — IDE node detail — every DAG node opens its agent's stream; live nodes can be
- `ide-node-stream-r2` — IDE node detail — revision 2 (Opus review of run ide-node-stream-20261006224
- `ide-node-stream-r3` — IDE node detail — revision 3 (Opus review of run ide-node-stream-r2-20261006
- `ide-node-stream-r4` — IDE node detail — revision 4 (Opus review of run ide-node-stream-r3-20261007
- `ide-node-stream-r5` — IDE node detail — revision 5 (Opus review of run ide-node-stream-r4-20261007
- `ide-node-stream-r6` — IDE node detail — revision 6 (Opus review of run ide-node-stream-r5-20261007
- `ide-orca-b1-palette` — mini-ork IDE: Orca colours, shared components, spec-v2 plumbing (Rust, crate
- `ide-pages-p1-fixes` — IDE pages — P1 fixes from the panel review (actions, confirms, honest number
- `ide-pages-review` — Review: `board page` — the mini-ork IDE pages, before they merge
- `node-artifacts` — IDE node inspector: the artifacts a node was given and produced, and the cod
- `node-commands` — IDE node stream for non-agent nodes: the full command and its output
- `node-commands-r2` — Non-agent node stream — revision 2 (Opus review of run node-commands-2026100
- `node-overview` — IDE node inspector: an Overview of what the node did, and transcripts that a
- `publish-carry-plus-edits` — A recover publishes exactly what its implementer produced: carry patch only 
- `recover-verify` — `mini-ork recover`: reuse finished nodes for real, and a verify-only retry
- `recover-verify-r2` — `mini-ork recover` verify strategy — revision 2 (Opus review of run recover-
- `retry-notify` — Tell a failed run's owner what to fix and how, then continue when they say i
- `rsi-i1-control-arm` — I1 matched-attempt control arm for the apply gate (G06-T03)
- `rsi-i1-control-arm-repair` — I1 repair: make the matched-attempt control arm correct and green
- `rsi-i2-verifier-audit` — I2 verifier validity audit as a promotion precondition (G03-T08)
- `rsi-i2-verifier-audit-repair` — I2 repair: feed the audit's detectors from collapse_history (they see 0 rows
- `rsi-i2b-audit-sensitivity` — I2b: the verifier audit must flag a textbook collapse (live smoke found it s
- `rsi-i3-bsg-va-wiring` — I3 validation-evidence replay on real code-fix runs (G03-T11)
- `rsi-i3-bsg-va-wiring-repair` — I3 repair: the base replay must include the candidate's test files
- `rsi-i4-collapse-halt` — I4 collapse halt: wire collapse_detector into the circuit breaker (G01-T04)
- `rsi-i4-collapse-halt-repair` — I4a repair: collapse halt that can actually fire, as an independent trip
- `rsi-i4b-collapse-writer` — I4b apply loop writes collapse_history (G01-T04, part b)
- `rsi-i5-harness-sweep` — I5 harness modules as apply-sweep targets (G02-T01, E6 slice)
- `rsi-i5-harness-sweep-repair` — I5 repair: make cost-per-solved-task a real, per-arm gate condition
- `rsi-i6-abstain-gate` — I6 calibrated abstain gate (G04-T05)
- `rsi-i6-abstain-gate-repair` — I6 repair: pass-through before requiring confidence; keep the exact-set guar
- `rsi-i6b-abstain-saturation` — I6b: abstain_gate must defer MORE when the verifier is unreliable, not never
- `run-orchestrator` — Per-run orchestrator — full loop scaffold (default off)
- `vt1-mr-relations` — VT1 metamorphic relations as an execution-anchored check in the Assay oracle
- `vt2-hackability-audit` — VT2 exploit-generation hackability audit for registered gates (G09-T05)
- `vt3-equivalence-operator` — VT3 Declared equivalence operator for the behavioral verifier (G10-T07)
- `vt4-differential-delta` — VT4 differential behavioural-equivalence term in the Assay oracle (G10-T02)
- `vt5-mutation-adequacy` — VT5 mutation-based suite adequacy audit for the code-fix test verifier (G03-
- `vt6-level-vector` — VT6 non-nested correctness level vector on run verdicts + publish gate (G08-

## NEVER_LAUNCHED (by family)

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

## SUPERSEDED
`eng-your-code-tab`, `ide-board-perf`, `ide-board-perf-r2`, `ide-board-perf-r3`, `ide-board-perf-r4`, `ide-board-perf-r5`, `ide-board-perf-r6`, `lane-repair-channel`, `lane-repair-hint-r2`, `learn-gate`, `learn-memory-tab`, `learn-overview2`, `learn-themes`, `retry-hint-r2`

## SHIPPED
`agent-session-isolation`, `auto-repair-loop`, `eng-code-findings`, `eng-path-rules`, `eng-path-rules-r2`, `eng-rules-tab`, `eng-your-code-tab-r2`, `eval-green-abstention`, `failed-run-lifecycle`, `files-abs-resolve`, `ide-board-perf-r7`, `ide-dag-lane-logos`, `ide-orca-b2a-triage`, `ide-orca-b2b-story`, `ide-orca-f2a-flow`, `ide-orca-f2b-flow`, `ide-orca-f3-replay`, `ide-orca-p2-run-panel`, `ide-orca-p3a-inbox-board`, `ide-orca-p3b-board-inbox-render`, `ide-orca-p4a-lanes-composer`, `ide-orca-p4b-jump-status`, `ide-orca-p5a-page-restyle`, `ide-orca-p5b-table-restyle`, `ide-run-dock`, `ide-run-flow`, `ide-run-outcome`, `ide-run-story`, `ide-spec-v2`, `induce-lane`, `interrupted-resume`, `kickoff-bullet-continuation`, `lane-repair-channel-r2`, `lane-repair-hint`, `lane-repair-resume`, `learn-gate-r2`, `learn-inject`, `learn-ledger`, `learn-lessons-detail`, `learn-memory-tab-r2`, `learn-memory-tab-r3`, `learn-node-tab`, `learn-overview2-r2`, `learn-prefs`, `learn-promotion-reasons`, `learn-restructure`, `learn-run-tab`, `learn-themes-r2`, `learned-db-default`, `merge-oss-guard`, `needs-you-truth`, `node-timeout-orphans`, `planner-context-cleanup`, `post-run-verify-reuse`, `publisher-authored-patch`, `recover-revival-fixes`, `recover-task-class`, `retry-hint`, `retry-hint-interrupted`, `review-pr210-scheduler-retry`, `sdd-i3-kickoff-contract`, `sdd-i5-evidence-ledger-r2`, `test-order-pollution`, `verified-only-inject`, `verified-only-prompts`, `vt7-defaults-on`, `withheld-publish-outcome`
