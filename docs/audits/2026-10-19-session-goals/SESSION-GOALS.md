# mini-ork — session goals & open tasks (last 7 days)

Generated 2026-10-19 from ContextNest (substrate `http://127.0.0.1:28080`, 545,792 fragments). 
Window: **2026-10-12 → 2026-10-19**. Scope: Claude sessions whose `project_cwd` is a mini-ork checkout 
(`ps/mini-ork`, `ps/zed-mini-ork`, `…/researcher/.mini-ork`) and that recorded ≥50 fragments (i.e. real work, not a one-line session-title stub).

> **Read this first.** Task ledgers below are the `tasks[]` arrays emitted in each session's last `<z-insight>` block. 
> They accumulate across turns and are **not reconciled against the repo**. A `pending` row in a session that finished hours ago is often already landed. 
> Section 4 therefore separates *ledger-open* from *verified-open*; only section 4's checks were re-run against git today.

## 1. Bottom line

- **22 substantive mini-ork sessions** in the window; **12 not done** (in-progress/idle/blocked/wrapping-up), **10 done**.
- **210 distinct pending user actions** and **175 distinct todo items** across those sessions after de-duplication.
- **Verified unmerged:** `02010e90` (code-fix verifier per-test regression + child env scrub) is present in the object store but is an ancestor of no branch and its added functions are absent from `origin/main`.
- **Verified landed despite looking open:** `5bb2e226` → `4a5cf19c`, `ed695c46` → `310284bf`; the four zed fork commits are on `fork/mini-ork`; the ui-crate commits `606ce20`/`b56cdc4` are on `origin/main` of `mini-ork-ui`.
- **Verified drift:** primary `mini-ork` is clean but **1 commit behind `origin/main`** (`9de7befc`); zed-mini-ork has one untracked spec file; `mini-ork-ui`'s local `main` is 3 behind.
- **Sessions that are genuinely blocked** (not merely idle): `4f781acd` (orchestrator tab), `8162bb3f` (SDD mechanisms), `da0b8c69` (run-page reason surface), `47ad145f` (run DAG state).

## 2. Method

```bash
curl -s http://127.0.0.1:28080/api/v1/sessions?limit=1000        # filter project_cwd ~ mini-ork, last_ts >= 2026-10-12
curl -s http://127.0.0.1:28080/api/v1/sessions/<id>/summary       # goal, progress, current_state, tasks[]
curl -s http://127.0.0.1:28080/api/v1/inbox                      # kind=todo / kind=user_action items
```
Merge/landing claims were checked with `git merge-base --is-ancestor <sha> origin/main` and subject matching on `origin/main`, not taken from the ledgers.

## 3. Repository state verified today

| Repo | HEAD | Working tree | vs upstream |
|---|---|---|---|
| `ps/mini-ork` (engine) | `dec2124e` | clean | **1 behind** `origin/main` (`9de7befc`); 0 local-only commits on `main` |
| `ps/zed-mini-ork` (fork) | `14f6bd962e` | 1 untracked (`docs/specs/judge-deepseek-tasks.md`) | synced with `fork/mini-ork` (0/0) |
| `zed-mini-ork/crates/mini_ork_ui` | detached `606ce20` | clean | `606ce20` is on `origin/main`; local branch `main` is 3 behind |
| `ps/mini-ork-ui` (separate clone) | `b56cdc4` on `try-stack-on-zoom` | — | `b56cdc4` is an ancestor of `origin/main` (landed) |

## 4. Verified merge status of commits the ledgers call open

| Commit | Subject | Ledger says | Verified |
|---|---|---|---|
| `02010e90` | fix(verify): code-fix verifiers — per-test regression + child env scrub | “Decide fate of unmerged commit 02010e90” | **NOT merged.** No branch contains it; `_pytest_available`, `_verbose`, `parse_pytest_outcomes` are absent from `origin/main`. |
| `5bb2e226` | fix(code-fix): link candidate dependency dirs onto the base worktree | “Push 5bb2e226…” | **Merged as `4a5cf19c`** (identical subject on `origin/main`). |
| `ed695c46` | Fix the CLI's default engine root, one directory too deep | “Rebase and push the peer's engine commit ed695c46” | **Merged as `310284bf`**. |
| `236e89c2`, `ae1307d9` | local-only engine commits | “unpushed” | **On `origin/main`** — stale ledger row. |
| `fee632c57d`, `aa900b1149`, `3685fec317`, `721aff43cc` | zed fork commits | “Push the four pending zed-mini-ork commits” | **All on `fork/mini-ork`**; branch is 0/0 with its upstream. |
| `f06b8d3719`, `2aabb9cc9a` | code-fix / fork pushes | pending | **Not resolvable in this clone** (`cat-file` absent). Either landed from another clone or the object was pruned. Cannot be verified here. |
| `e8bbb609`, `9470c815`, `05c9f139` | orchestrator / engine fixes | “reconcile / land” | **On `origin/main`.** |

## 5. Session goals

| Last activity | Session | Project | Progress | Goal | Open ledger rows |
|---|---|---|---|---|---|
| 2026-10-19 09:50 | `8d83aa4c` | `ps/mini-ork` | **in-progress** | Block dotfile credential scanning and turn on real human analytics for miniork.dev. | 31 |
| 2026-10-19 09:24 | `2e068f41` | `ps/zed-mini-ork` | **idle** | Keep the last 48h of mini-ork runs moving by resuming any run that can continue from its failed node | 11 |
| 2026-10-19 09:21 | `3e207e26` | `ps/mini-ork` | **in-progress** | Ship all eight implement-now verification techniques from the 2026-10-18 agentic verification review into mini-ork main, gated and tested. | 52 |
| 2026-10-19 08:47 | `f5671eaa` | `ps/mini-ork` | **done** | Get the local main line and origin/main onto one history without losing the local orchestrator-tab work | 33 |
| 2026-10-19 07:05 | `1165b056` | `ps/mini-ork` | **done** | Make a bare mini-ork run execute on a Daytona sandbox by default without breaking laptop/CI runs. | 21 |
| 2026-10-19 06:41 | `d5471234` | `ps/mini-ork` | **in-progress** | Revive the one product-impact failed mini-ork run whose deliverable is not already on origin/main, and decide whether the dirty primary tree is OSS-ready to commit and push | 41 |
| 2026-10-19 06:15 | `83e57620` | `ps/mini-ork` | **done** | Brief the user on the OpenClaw star-growth panel report and recommend what a new joiner should implement first | 33 |
| 2026-10-19 05:58 | `4f781acd` | `ps/zed-mini-ork` | **blocked** | Ship an orchestrator tab that streams like a node's live Stream and steers the orchestrator agent, landed in both the UI fork and engine | 22 |
| 2026-10-19 05:52 | `20a62c45` | `ps/mini-ork` | **done** | Prevent future code-fix runs from being permanently unpublishable when the replay instrument is opted out. | 9 |
| 2026-10-18 22:18 | `ad0657b6` | `ps/zed-mini-ork` | **done** | Ship the inline mini-ork thread cards end to end and report what landed. | 3 |
| 2026-10-18 21:29 | `f6e1ed90` | `ps/zed-mini-ork` | **done** | Make a click on any DAG node open that node's detail page, the same page an agents-table row opens | 51 |
| 2026-10-18 20:15 | `6881f0ad` | `ps/zed-mini-ork` | **wrapping-up** | Ship zed-mini-ork on Zed v1.24.3 with every fork change intact, built and published to fork/mini-ork | 74 |
| 2026-10-18 18:45 | `cb737d8d` | `ps/zed-mini-ork` | **done** | Make Zed's follow-the-agent crosshair open every file a mini-ork run's tool calls name, live and on replay | 70 |
| 2026-10-18 18:08 | `55d69b4d` | `ps/mini-ork` | **in-progress** | Restore the mini-ork board/run list in the IDE and make its startup layout deterministic across launches | 3 |
| 2026-10-18 01:24 | `86880f3c` | `ps/zed-mini-ork` | **in-progress** | Monitor failed mini-ork runs in both homes and revive every run that can continue from its failed node | 16 |
| 2026-10-17 20:48 | `1bc9e83a` | `ps/zed-mini-ork` | **wrapping-up** | Make the run page's Orchestrator tab a single node-Stream-style surface (activity + steering + docked composer), as the user originally specified. | 62 |
| 2026-10-17 09:33 | `443ef722` | `ps/mini-ork` | **done** | Know precisely which tasks from this session remain, which only look open because the ledger is stale, and what each open item is blocked on. | 20 |
| 2026-10-17 07:11 | `8162bb3f` | `ps/mini-ork` | **blocked** | Settle which SDD roadmap mechanisms actually shipped, recover the wiped P2 artifact-transport deliverable, and keep the mini-ork dirty-tree ledger accurate across concurrent sessions. | 38 |
| 2026-10-17 07:10 | `da0b8c69` | `ps/zed-mini-ork` | **blocked** | Make the run page say what happened and why — one page always, the reason promoted, raw output folded away, empty sections gone | 51 |
| 2026-10-17 07:10 | `732bb4cc` | `ps/mini-ork` | **done** | Answer the revive-monitor's commit request accurately without sweeping other sessions' work. | 7 |
| 2026-10-16 15:43 | `7cacb4ac` | `ps/zed-mini-ork` | **done** | Rewrite the round-2 interview prep doc on AI frameworks and agent security so it reads in the operator's plain-English voice and is easy to speak and memorize. | 1 |
| 2026-10-12 19:45 | `47ad145f` | `ps/zed-mini-ork` | **blocked** | Make the mini-ork run DAG, sidebar and header chip report each run's true final state | 2 |

## 6. Sessions NOT done — detail

### `8d83aa4c` — in-progress

- **Goal:** Block dotfile credential scanning and turn on real human analytics for miniork.dev.
- **Last activity:** 2026-10-19 09:50 · **project:** `/Volumes/docker-ssd/ps/mini-ork` · **782 fragments**
- **Where it stands:** WAF is live; Web Analytics needs Account Settings Read added to the token before I can create the site.
- **Topics:** cloudflare-waf, permissions, web-analytics

**Open ledger rows:**

- `in_progress` — A1 backend catalog via framework-edit  _(id: cp-a1)_
- `in_progress` — A2 teardown registry via framework-edit  _(id: cp-a2)_
- `in_progress` — B RemoteWorkspace provider seam via framework-edit  _(id: cp-b)_
- `in_progress` — Decisions explain themselves; one attention lane  _(id: c4c5)_
- `in_progress` — Repair origin/main: _wall return, node.py buddy import, 3 stale test files  _(id: fix-main-buddy)_
- `pending` — Add a git remote for miniork-docs  _(id: docs-repo-remote)_
- `pending` — Block dotfile probes via WAF  _(id: waf-block-scanning)_
- `pending` — Build Kubernetes Agent Sandbox provider (needs kind/kubeconfig approval)  _(id: k8s-agent-sandbox-provider)_
- `pending` — C11+C6+C12: state-aware run header on every tab  _(id: rp-e3-header)_
- `pending` — C13: graph focus + node reason  _(id: rp-e6-graph-focus)_
- `pending` — C14: board steer prints a receipt line  _(id: rp-e11b-steer-receipt)_
- `pending` — C22+C23: artifacts key-first, learnings actions  _(id: rp-e10-artifacts-learnings)_
- `pending` — Configure researcher home for Daytona (offered)  _(id: researcher-daytona-setup)_
- `pending` — Correlate paths and ASNs  _(id: path-asn-analysis)_
- `pending` — Decide and build the operator accept/adjust gate for SpecCard ratification  _(id: sdd-ratification-gate)_
- `pending` — Enable Cloudflare Web Analytics  _(id: enable-web-analytics)_
- `pending` — Environment picker in UI run launcher (offered)  _(id: ui-env-picker)_
- `pending` — Environment picker in the UI run launcher (offered)  _(id: ui-run-launcher-env)_
- `pending` — IDE renderer halves (C1, C2, C12/13/14-render, C24, C26)  _(id: ide-render)_
- `pending` — Implement run page P0 trust fixes (C1-C7)  _(id: run-page-p0)_
- `pending` — Integrate Vercel Sandbox provider (needs credentials)  _(id: vercel-sandbox-provider)_
- `pending` — Kubernetes Agent Sandbox provider  _(id: cp-k8s)_
- `pending` — Live-verify Modal provider (needs tokens)  _(id: modal-live-verify)_
- `pending` — Modal provider live verification  _(id: cp-modal)_
- `pending` — Real mini-ork run --placement remote --env daytona lands its diff  _(id: cp-daytona-e2e)_
- `pending` — Research and integrate popular Daytona alternatives (after Daytona 100%)  _(id: cp-alternatives)_
- `pending` — Restart researcher :7090 UI to load new code  _(id: restart-7090-ui)_
- `pending` — Rotate exposed Cloudflare API token  _(id: rotate-cf-token)_
- `pending` — Rotate the exposed Daytona API key  _(id: rotate-daytona-key)_
- `pending` — SDD operator ratification gate  _(id: sdd)_
- `pending` — Vercel Sandbox provider  _(id: cp-vercel)_

### `2e068f41` — idle

- **Goal:** Keep the last 48h of mini-ork runs moving by resuming any run that can continue from its failed node
- **Last activity:** 2026-10-19 09:24 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **697 fragments**
- **Where it stands:** No revivable run in the window; the epic's only remaining blocker is the operator acknowledgement now that the engine fix is live.
- **Topics:** engine sync, failed-run triage, operator gate

**Open ledger rows:**

- `in_progress` — Choose core-data vs ide_pages vs Rust FE boundary  _(id: verify-boundary)_
- `in_progress` — Choose how framework-edit levels should read  _(id: levels-choice)_
- `in_progress` — Verify breadcrumb in the launched IDE  _(id: node-crumb-visual)_
- `pending` — Decide target level and which safety surfaces must survive exact parity  _(id: design-parity-decision)_
- `pending` — Default branch, App token, PR #1, branch protection  _(id: repo-ops)_
- `pending` — Land engine branch after live runs finish; visual check in IDE  _(id: run-page-redesign-land)_
- `pending` — Operator decision on reviving the pick-files-directories epic  _(id: revive-epic-1792151796)_
- `pending` — Reconcile primary checkout with origin/main so :7090 serves the levels section  _(id: primary-reconcile)_
- `failed` — Decide the fate of the rp-c3 triage-truth change after impl_moved_base  _(id: rp-c3-triage-truth)_
- `failed` — Decide whether lf-r11b gets a fresh revision run without the builtins pytest shim  _(id: lf-r11b-revise)_
- `failed` — Fix the rp-c23 kickoff duplicate pytest glob and decide whether to recreate rp-c23  _(id: rp-c23-revive)_

### `3e207e26` — in-progress

- **Goal:** Ship all eight implement-now verification techniques from the 2026-10-18 agentic verification review into mini-ork main, gated and tested.
- **Last activity:** 2026-10-19 09:21 · **project:** `/Volumes/docker-ssd/ps/mini-ork` · **1756 fragments**
- **Where it stands:** Six runs are executing; vt9 and vt15 are staged pending file-claim release from vt8, vt11 and vt13.
- **Topics:** agentic verification, conformal abstention, framework-edit, oracle integrity, reward hacking

**Open ledger rows:**

- `in_progress` — Build ContextNest release from main  _(id: build)_
- `in_progress` — CN boot budget 3600 + dod2 deflake PR #217  _(id: cn-boot-dod2)_
- `in_progress` — CN-1 principals/bindings/mailbox (code-fix run)  _(id: cn1)_
- `in_progress` — CN-4 hot-set activity claims  _(id: cn4)_
- `in_progress` — Concord P1d bash-write attribution (PR #220)  _(id: cn-13)_
- `in_progress` — Concord P2e owns implicit scope (PR #218)  _(id: cn-11)_
- `in_progress` — Concord P3b intent blend  _(id: cn-12)_
- `in_progress` — MO-2 run principals  _(id: mo2)_
- `in_progress` — Verify researcher/.mini-ork re-vendored to include the fixes  _(id: revendor-researcher)_
- `in_progress` — anti-gaming a: red-base per-test, env scrub, tamper guard  _(id: fl-k5a)_
- `in_progress` — anti-gaming b: weak F2P, flake rerun, surviving mutants into revise  _(id: fl-k5b)_
- `in_progress` — behavioral recomputation surface  _(id: vt14)_
- `in_progress` — code-fix: abstention must not read as red suite  _(id: fl-k1)_
- `in_progress` — conformal accept/abstain gate  _(id: vt10)_
- `in_progress` — discrimination-anchored test evidence  _(id: vt13)_
- `in_progress` — oracle-integrity audit  _(id: vt8)_
- `in_progress` — reward/evidence pairing  _(id: vt12)_
- `in_progress` — salvage: dedupe paths after normalization  _(id: fl-sd)_
- `in_progress` — verifier mutation-discrimination suite  _(id: vt11)_
- `pending` — CN-2 synchronous turn hook + lease identity via binding  _(id: cn2)_
- `pending` — CN-6 per-turn owns enforcement  _(id: cn6)_
- `pending` — Calibrate /coord/topic-pairs then enable topic notices in prod  _(id: topic-calibration)_
- `pending` — Concord P0/P0.5: principal registry + offline replay  _(id: concord-p0)_
- `pending` — Concord P2 hot-set strict leases + digest + scheduler admission  _(id: p2)_
- `pending` — Concord P4 overlap arbitration  _(id: cn-14)_
- `pending` — Concord arbitration, Bash-write attribution, intent drift, topic calibration  _(id: concord-p3-open)_
- `pending` — Decide fate of unmerged codefix-test-verifier commit 02010e90  _(id: orphan-02010e90)_
- `pending` — Fix flaky coord_principals_test dod2 TTL=0 race  _(id: flaky-dod2)_
- `pending` — Integrate ranked technique groups into Concord design doc  _(id: coord-design)_
- `pending` — P2d --owns per-turn enforcement (audit first)  _(id: owns)_
- `pending` — P3 topic overlap + arbitration  _(id: p3)_
- `pending` — P4 alone-vs-combined tests at merge  _(id: p4)_
- `pending` — Ping researcher-4d when the jest --json replay is on main  _(id: notify-researcher-jest)_
- `pending` — Pre-run lane-health gate for mini-ork  _(id: lane-health-gate)_
- `pending` — Publish-gate main-ref validation, P3, P4  _(id: remaining)_
- `pending` — Raise CN_PROD_HEALTH_TIMEOUT before boots exceed 1800 s  _(id: cn-boot-budget)_
- `pending` — Re-vendor ContextNest mini-ork after CN-2  _(id: revendor)_
- `pending` — Redeploy prod ContextNest after CN merges  _(id: cn-redeploy)_
- `pending` — Redeploy prod ContextNest with P1c  _(id: concord-p1c-deploy)_
- `pending` — Remove older duplicate cc/hook ingest hooks from ~/.claude/settings.json  _(id: dedupe-ingest-hooks)_
- `pending` — Run the 10-scenario live smoke  _(id: smoke)_
- `pending` — Tell researcher-4d when K1 is on main so it can re-vendor  _(id: notify-researcher-4d)_
- `pending` — Treat a worktree's own kickoff and .mini-ork artifacts as implicitly owned  _(id: owns-implicit-scope)_
- `pending` — User: make cn-prod + install-hooks  _(id: activate)_
- `pending` — Wrap RSI loop launchers in mini-ork concord run  _(id: concord-loop-wrap)_
- `pending` — anti-gaming: tamper guard, weak fail-to-pass, flake rerun, revise on surviving mutants  _(id: fl-k5)_
- `pending` — executor: revise loop before rollback  _(id: fl-k3)_
- `pending` — hidden regenerable ground truth  _(id: vt15)_
- `pending` — judge-gate validity audit  _(id: vt9)_
- `pending` — levels: target n/a for unmeasurable runners  _(id: fl-k1b)_
- `pending` — replay: jest/vitest structured results + results-file contract  _(id: fl-k4)_
- `pending` — test_dispatch_model_fails_fast_on_framework_cwd fails only in the primary checkout  _(id: primary-only-cwd-test)_

### `d5471234` — in-progress

- **Goal:** Revive the one product-impact failed mini-ork run whose deliverable is not already on origin/main, and decide whether the dirty primary tree is OSS-ready to commit and push
- **Last activity:** 2026-10-19 06:41 · **project:** `/Volumes/docker-ssd/ps/mini-ork` · **5474 fragments**
- **Where it stands:** Run is in the implementer phase on a tree a peer session is also editing; awaiting child-run completion and smoke_live.
- **Topics:** clippy-baseline, gates-materialize, oss-guard, precondition-probes, spec-driven-delivery

**Open ledger rows:**

- `in_progress` — Owner recovers b1-palette/b2a/b2b via mini-ork recover  _(id: orca-recover-3)_
- `in_progress` — Recurring orchestration + monitoring pass every 3 min  _(id: recurring-orchestration-monitor)_
- `in_progress` — Relaunch ide-orca-b2a-triage with verifier fix  _(id: relaunch-b2a)_
- `in_progress` — Revive and land run-orchestrator framework-edit run  _(id: revive-run-orchestrator)_
- `in_progress` — Revive ide-orca-b2b-story revision on GLM lane  _(id: revive-b2b)_
- `in_progress` — Revive the pick-files-directories SDD run  _(id: revive-run-1792151796-92942)_
- `in_progress` — Round-3 revision of run-1791469896-2926  _(id: k3-rev3)_
- `in_progress` — Route unpushed 331527c to its owning session  _(id: route-331527c)_
- `in_progress` — Watch run-ask-k3 outcome to confirm the re-vendored verifier fixes hold  _(id: watch-askk3)_
- `in_progress` — mini-ork IDE flow map part B (code-fix) — owner recovering, blocked on codex quota  _(id: ide-orca-f2b-flow-20261008172728)_
- `in_progress` — per-run orchestrator full loop scaffold (framework-edit run-orchestrator-scaffold-20261008124718)  _(id: wu-9c41d7e2)_
- `pending` — $CONTEXT_FILE prompt cleanup (deferred to next recipe-prompt touch)  _(id: context-file-cleanup)_
- `pending` — 14 ambient publish-no-authored-patch failures on main — awaiting decision  _(id: ambient-noauthpatch-failures)_
- `pending` — A6 order / A3 lesson_text / A8 floor  _(id: p2-one-liners)_
- `pending` — Apply migration 0068 to drop textual_gradients  _(id: b1-wire-or-remove)_
- `pending` — Compact MEMORY.md past soft limit  _(id: memory-compact)_
- `pending` — D1-D6 → per-run orchestrator implementation roadmap  _(id: orchestrator-roadmap)_
- `pending` — Diagnose+fix reflection gradient writeback outage  _(id: p0-reflection-writeback)_
- `pending` — Owner revises zed-mini_ork_ui change (story seed fix + palette/helper fixes)  _(id: orca-codefix-revise)_
- `pending` — Persist per-criterion verifier results + continuous reward_g  _(id: desaturate-reward-g)_
- `pending` — Recreate ask-author-k2 worktree + apply salvage end-to-end (round-3)  _(id: askk2-round3)_
- `pending` — Relaunch ide-orca-b1-palette with verifier fix  _(id: relaunch-b1-palette)_
- `pending` — Render injection ledger (v2 + learned/sources) in Learnings tab  _(id: p1-injection-ledger-render)_
- `pending` — Replace saturated reward_g with discriminating prior-run signals  _(id: a9-circular-evidence)_
- `pending` — Run one reflect pass to exercise themes.promote() and re-query lesson_themes  _(id: run-reflect-pass-prove-themes-promote)_
- `pending` — decide on adrs materialization  _(id: adrs-materialization)_
- `pending` — decide wire vs delete for empty learning tables + reward_g saturation  _(id: wire-or-delete)_
- `pending` — delete scripts/mini-ork-worktree.sh (bash-removal Phase 4)  _(id: phase4-delete-sh)_
- `pending` — determine whether panel lanes bypass cost_pause.check() and whether the pause threshold is therefore under-enforced  _(id: cost-sidecar-gap)_
- `pending` — enable MO_RUN_ORCHESTRATOR on a canary + widen wiring  _(id: enable-orchestrator-canary)_
- `pending` — fold $CONTEXT_FILE prompt cleanup into next recipe-prompt touch  _(id: ctxfile-prompt-cleanup)_
- `pending` — per-run scope disclosure + cross-run view  _(id: p2-16)_
- `pending` — reconcile 4 dead orca runs + 2 zombie executing rows (owner: mini-ork-orca-ui-redesign)  _(id: orca-zombie-rows)_
- `pending` — record P1#9/P2#16/P2#18 resolutions in d5471234-orchestrator-always-visible.md  _(id: audit-doc-update)_
- `pending` — reward-g saturation  _(id: reward-g-saturation)_
- `pending` — run page must require the armed receipt so an unwired run does not render as armed  _(id: p1-9-page-half)_
- `pending` — triage 14 ambient main test failures  _(id: ambient-14-failures)_
- `pending` — write outcome feedback + corrected statuses to ContextNest  _(id: cn-outcome-feedback)_
- `failed` — Fast-forward primary local main to origin/main  _(id: ff-local-main)_
- `failed` — k3 code-fix run: verify mock fixes for the two failing suites  _(id: run-1791469896-2926)_
- `failed` — mini-ork IDE flow map part A (code-fix) — same dead-codex-lane cause  _(id: ide-orca-f2a-flow-20261008165006)_

### `4f781acd` — blocked

- **Goal:** Ship an orchestrator tab that streams like a node's live Stream and steers the orchestrator agent, landed in both the UI fork and engine
- **Last activity:** 2026-10-19 05:58 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **634 fragments**
- **Where it stands:** UI change landed and pushed; engine commit e8bbb609 is local only because upstream has a parallel orchestrator implementation that requires a reconciliation decision
- **Topics:** ACP thread JSONL, engine landing, live stream, orchestrator tab

**Open ledger rows:**

- `in_progress` — CI verdict for bbd241d5  _(id: ci-bbd241d5)_
- `in_progress` — CI verdict for the pushed HEAD  _(id: ci-8273e27d)_
- `in_progress` — Full tests/unit run: is the ACP trio the only red?  _(id: full-unit-suite)_
- `in_progress` — UI/UX brief for the Orchestrator & intake page  _(id: orch-ux-brief)_
- `pending` — ASCII mockups of 2-3 candidate page spines for the designer  _(id: orch-spine-mockups)_
- `pending` — Add board answer CLI verb wrapping the profile-answer writer  _(id: board-answer-verb)_
- `pending` — Add board start CLI verb wrapping web.control.launch_run  _(id: board-start-verb)_
- `pending` — Add run_id column to intake_events  _(id: intake-run-id)_
- `pending` — Add session/kickoffs/races/planner/coord to _PAGE_TABS in test_ide_pages_actions.py  _(id: orch-tabs-walk)_
- `pending` — Capture populated + zero-state screenshots of the orch page  _(id: orch-screenshots)_
- `pending` — Decide whether to refresh the three ACP assertions  _(id: acp-trio-decision)_
- `pending` — GUI half of the live replay (pixel-level)  _(id: live-replay-gui)_
- `pending` — GUI half of the live replay on the orch tabs  _(id: gui-replay)_
- `pending` — P1: board start, kickoff new, Dispatch tab  _(id: p1-dispatch)_
- `pending` — P2: run scan + board answer + blocked-on-you panel  _(id: p2-answers)_
- `pending` — P3: board race-keep + intake_events.run_id migration  _(id: p3-races)_
- `pending` — P4: attention strip prepended to every tab  _(id: p4-attention)_
- `pending` — Reconcile local orchestrator series with origin/main's runner_orchestrator and land e8bbb609  _(id: engine-land-orch-tab)_
- `pending` — Unit tests for the six P0 sections  _(id: p0-tests)_
- `pending` — User replays the Orchestrator tab in the rebuilt GUI  _(id: gui-verify)_
- `pending` — Wiring pass: kickoff fix+log_path, thread-default chips, history do, fleet acts, errors callout, chain health  _(id: wire-existing-be)_
- `pending` — minimax lane out of quota for real  _(id: minimax-quota)_

### `6881f0ad` — wrapping-up

- **Goal:** Ship zed-mini-ork on Zed v1.24.3 with every fork change intact, built and published to fork/mini-ork
- **Last activity:** 2026-10-18 20:15 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **3148 fragments**
- **Where it stands:** Upgrade published at fork/mini-ork d99a14dc70; app runs, but visual confirmation waits for the user to unlock the Mac.
- **Topics:** gui-smoke, merge-resolution, shallow-history, submodule-pin, zed-fork-upgrade

**Open ledger rows:**

- `in_progress` — Attribute epic/board consolidation + app import  _(id: attribution-epic-board)_
- `in_progress` — Build mini_ork/documents/ extraction layer + tests  _(id: wit-2-documents)_
- `in_progress` — Click empty DAG background navigates to the run main page, on all three surfaces  _(id: dag-bg-click)_
- `in_progress` — Deliver cn-ui-board-badges  _(id: cn-ui-board-badges)_
- `in_progress` — Deliver cn-ui-coordination (real-shape fix running)  _(id: cn-ui-coordination)_
- `in_progress` — Deliver cn-ui-lane-decisions (ld-b atoms fix running)  _(id: cn-ui-lane-decisions)_
- `in_progress` — Deliver cn-ui-needs-you (settle after scheduler exits)  _(id: cn-ui-needs-you)_
- `in_progress` — Deliver cn-ui-run-memory (settle after scheduler exits)  _(id: cn-ui-run-memory)_
- `in_progress` — Deliver cn-ui-session-page (Re-test and paths fix running)  _(id: cn-ui-session-page)_
- `in_progress` — Deliver cn-ui-task-prior-art  _(id: cn-ui-task-prior-art)_
- `in_progress` — F1b: verifier limit of 4 fails loudly instead of truncating  _(id: f1b-verify-cap)_
- `in_progress` — Follow-up: authenticate every secure-tier read and fail closed  _(id: cn-ui-tiers-b)_
- `in_progress` — K2: /task command and Queue on board draft option  _(id: k2-task-command)_
- `in_progress` — K2b: queue resolves the kickoff root like the scheduler  _(id: k2b-queue-root)_
- `in_progress` — Live visual smoke of the rebuilt IDE  _(id: smoke-v1243)_
- `in_progress` — Merge cn-ui-memory-search after the test_empty_home decision, plus ctx-c  _(id: cn-ui-memory-search)_
- `in_progress` — Monitor the intake-delegation epics for state changes  _(id: intake-delegation-monitor)_
- `in_progress` — Plan the real-edge DAG: engine emission, IDE rendering, recipe wiring  _(id: real-graph-plan)_
- `in_progress` — Rebuild the IDE binary with the new pin  _(id: rebuild-mini-ork)_
- `in_progress` — Run script/mini-ork-build and report the result  _(id: build-latest-ui)_
- `in_progress` — Rust editor file chip in mini-ork-ui (pushed and pinned; IDE build running)  _(id: cn-ui-editor-file-chip)_
- `in_progress` — Stage 3 — the communication transports  _(id: wit-3-integrations)_
- `in_progress` — Watch the in-flight recover on run-1791785967-43108 (linear-adapter) and determine why it produces no writes  _(id: recover-43108-watch)_
- `in_progress` — Write the three-feature roadmap  _(id: wu-a41f8e07)_
- `pending` — Board column/card contract in spec.py + board_view.rs (blocked on the other session)  _(id: pm-stage-2)_
- `pending` — Capability-gated ticket write-back  _(id: wit-5-writeback)_
- `pending` — Communication: Telegram/Slack/Discord over the Thread  _(id: stage-3-comms)_
- `pending` — Confirm or revert the tier-B webhook (step 8 vs Stage 3 deferral)  _(id: step8-decision)_
- `pending` — Correct the triage doc's overstated §5 severity claim  _(id: triage-doc-s5-correction)_
- `pending` — Decide whether to push zed-mini-ork's mini-ork branch to fork  _(id: real-graph-push-mini-ork-branch)_
- `pending` — Deliver cn-ui-seen-before after memory-search frees cn_cmd.py  _(id: cn-ui-seen-before)_
- `pending` — Deliver shipped-today and needs-you once hr-ide-review releases inbox.py  _(id: cn-ui-shipped-today)_
- `pending` — Dispatch verdict-outcome, shipped-today, lane-decisions when capacity frees  _(id: cn-ui-batch-2)_
- `pending` — Enforce frozen probe fixture at write time  _(id: miniork-s7-probe-fixture-guard)_
- `pending` — F2: framework-edit static check runs the repo's blocking ruff tier  _(id: f2-static-ruff)_
- `pending` — File handling: document section + attachments  _(id: stage-2-documents)_
- `pending` — File handling: documents package + document section type  _(id: stage-2-files)_
- `pending` — File the six ContextNest prerequisite issues  _(id: cn-issues)_
- `pending` — Filter box, card quick actions, column totals + 'N more', fit-the-pane live refresh  _(id: board-features)_
- `pending` — Fix _synth_verify hardcoding python3 in mini_ork/cli/epics.py  _(id: synth-verify-python3)_
- `pending` — Fix framework-edit profile step prepending a bare pytest and dropping ruff from Verification commands  _(id: profile-verification-bug)_
- `pending` — Ingest and split the cn-ui roadmap onto the Tasks board with strengthened Verification commands  _(id: cn-ui-ingest)_
- `pending` — Jira adapter  _(id: wit-4-jira)_
- `pending` — Kill idle tmux sessions board-tasks/verify-fixes and clean the two worktrees once Kiro terminals close  _(id: cleanup-tmux-worktrees)_
- `pending` — Linear PM adapter + tests  _(id: wit-4-linear)_
- `pending` — Live GUI check of New task -> /task -> Queue on board, plus screenshots of new pages  _(id: gui-task-queue)_
- `pending` — Live smoke test each epic the scheduler marks done, against the worktree  _(id: smoke-test-delivered-epics)_
- `pending` — Live smoke the background click on all three DAG surfaces  _(id: dag-bg-click-smoke)_
- `pending` — Make the run-level verdict honor verdict.json tests_pass  _(id: run-verdict-vs-verdict-json)_
- `pending` — Merge wt/board-tasks to engine main after all gates pass  _(id: board-merge-main)_
- `pending` — Merge wt/verify-fixes to engine main after both gates pass  _(id: verify-fixes-merge)_
- `pending` — One hands-off GUI pass: run DAG edges, Runs tab, /task Queue on board, new ContextNest pages  _(id: gui-smoke-pass)_
- `pending` — Operator: restart :7090, script/mini-ork-build + relaunch IDE, check the epic-runner DAG tab  _(id: real-graph-operator-verify)_
- `pending` — Optionally apply the wit-2-documents salvage patch into the intake-delegation worktree and run its kickoff verification commands  _(id: smoke-test-wit-2-salvage)_
- `pending` — Phase 1: fail-closed epics add, /task command, and epic-card board with a Runs tab  _(id: board-phase1)_
- `pending` — Phase 2: per-card Retry on Needs-you cards (board verb + crate change)  _(id: board-card-retry)_
- `pending` — Reconcile local main (8 ahead) with origin/main so the landed fixes reach the running engine  _(id: engine-main-divergence)_
- `pending` — Record the planner node's produced artifact into task_runs.plan_path (Tier 1 writer half)  _(id: plan-artifact-writer)_
- `pending` — Replay 2-3 failed code-fix runs against HEAD to prove the publish blocker is closed  _(id: miniork-replay-receipt)_
- `pending` — Report: scheduler reclaim steals live epics whose runs exceed the grace window  _(id: reclaim-stale-bug)_
- `pending` — Restart the :7090 mini-ork server so the fix reaches the IDE  _(id: restart-7090)_
- `pending` — Run script/mini-ork-build and relaunch to pick up the three post-14:29 commits  _(id: rebuild-and-relaunch)_
- `pending` — Session p240's uncommitted mini_ork_ui edits in both zed submodules — not mine, flagged  _(id: p240-crate-uncommitted)_
- `pending` — Six pre-existing unit failures in acp/config-seam/inbox/learning — not mine, unresolved  _(id: suite-6-failures)_
- `pending` — Smoke test and hand-commit the stranded mini_ork/pm/providers/linear.py  _(id: linear-py-salvage)_
- `pending` — Thread spine: 0070_threads.sql + mini_ork/threads/ + retire the 409  _(id: stage-0-thread-spine)_
- `pending` — Wire or remove the unwired reflector node in recipes/epic-runner/workflow.yaml  _(id: epic-runner-reflector-wiring)_
- `pending` — Work intake: GitHub provider + mini-ork intake + issues page  _(id: stage-1-intake)_
- `pending` — Write ingestible ContextNest UI epic files from the discovery reports  _(id: cn-ui-epics)_
- `pending` — document IDE section + thread attachments  _(id: wit-2-attachment-section)_
- `pending` — epic-runner Stage 3 scope_allow omits mini_ork/integrations/ — implementer scoped out of its own deliverable  _(id: wit-3-scope-allow)_
- `pending` — fast-forward the primary's local main to f7b45c84  _(id: primary-main-refresh)_
- `pending` — file the 6 drafted ContextNest issues once the operator approves  _(id: contextnest-issues)_
- `pending` — issues IDE page + mini-ork pm CLI command  _(id: pm-stage-1)_

### `55d69b4d` — in-progress

- **Goal:** Restore the mini-ork board/run list in the IDE and make its startup layout deterministic across launches
- **Last activity:** 2026-10-18 18:08 · **project:** `/Volumes/docker-ssd/ps/mini-ork` · **146 fragments**
- **Where it stands:** Fix committed; cargo check green; app rebuild/relaunch pending so the running IDE picks it up
- **Topics:** dock-restore, gpui, mini-ork-sidebar, startup-race, zed-fork

**Open ledger rows:**

- `pending` — Rebuild mini-ork IDE and relaunch to verify rail opens  _(id: rebuild-relaunch)_
- `pending` — Reconcile primary checkout with origin/main  _(id: primary-reconcile)_
- `pending` — Refresh local main after worktree merge  _(id: post-merge-ff)_

### `86880f3c` — in-progress

- **Goal:** Monitor failed mini-ork runs in both homes and revive every run that can continue from its failed node
- **Last activity:** 2026-10-18 01:24 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **1094 fragments**
- **Where it stands:** Steady state seventh firing; no attempt has run and no target has returned, so no verdict can have changed.
- **Topics:** revive-monitor, steady-state

**Open ledger rows:**

- `in_progress` — Broadcast commit requests to mini-ork-owning sessions  _(id: commit-broadcast)_
- `in_progress` — Full rewrite of 06_question_bank_say_out_loud.md for readability and recall  _(id: rewrite-qbank-06)_
- `in_progress` — Monitor and revive failed mini-ork runs (both homes)  _(id: revive-monitor)_
- `in_progress` — Restart the ContextNest substrate on its prod data dir and verify health  _(id: restart-contextnest-prod)_
- `in_progress` — Triage failed runs in both mini-ork homes  _(id: monitor-48h-both-homes)_
- `pending` — Check whether the 7 running mo-* containers are referenced by a live mini-ork run  _(id: verify-running-mo-containers)_
- `pending` — Compact the 9.3 GB wal.canonical.sqlite and delete 24 GB of stale backups  _(id: compact-contextnest-checkpoint)_
- `pending` — Decide whether rp-c3 series restarts from salvage 6087a708  _(id: rp-c3-salvage)_
- `pending` — Expand B1, C1, C9, D6, 0.1 from their source files 02-05 and 10  _(id: expand-pointer-answers)_
- `pending` — Fix artifact_summary so a revive is not graded on superseded markers  _(id: rubric-contamination)_
- `pending` — Prune 8 redundant mo-e2e-node-host images (13.2 GB)  _(id: docker-image-prune)_
- `pending` — R5 blames the change for an all-unprobeable gate (handed to the classification workstream)  _(id: r5-unprobeable-gate)_
- `pending` — Remove 49 never-started mo-ws-*/mo-sess-* containers  _(id: trim-docker-never-started)_
- `pending` — a spec test that errors in its own setup must not be graded as a refutation  _(id: spec-test-tamper-class)_
- `failed` — Revive researcher run 779 from its failed node  _(id: revive-779)_
- `failed` — Revive the failed researcher-home runs  _(id: revive-researcher-home)_

### `1bc9e83a` — wrapping-up

- **Goal:** Make the run page's Orchestrator tab a single node-Stream-style surface (activity + steering + docked composer), as the user originally specified.
- **Last activity:** 2026-10-17 20:48 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **3137 fragments**
- **Where it stands:** Implementation complete and payload-verified; waiting on the user to rebuild/restart the IDE and confirm the tab renders as one stream surface.
- **Topics:** ide_pages, mini_ork_ui renderer, node stream layout, orchestrator tab, steering composer

**Open ledger rows:**

- `in_progress` — 51986 re-implementing via revise from_node=implementer  _(id: 51986-revise)_
- `in_progress` — Complete 61320 revive once its child clears the engine checkout  _(id: run-1791788217-61320)_
- `in_progress` — Confirm the four unit-a/unit-b CI jobs go green on run 38247661338  _(id: g-confirm)_
- `in_progress` — Finish P1.2 full-suite AC2 differential  _(id: p12-ac2)_
- `in_progress` — Make Setup & health the mini-ork configuration hub  _(id: setup-hub)_
- `in_progress` — Orchestrator tab as node-Stream-style surface  _(id: orch-tab-stream-redesign)_
- `in_progress` — Revive epic-runner run-1791785967-43108 (wit-4-linear) from epic_dispatcher  _(id: revive-43108)_
- `in_progress` — Revive framework-edit run-1791785128-75074-wit-2-documents  _(id: revive-75074-wit-2-documents)_
- `in_progress` — Revive framework-edit run-1791788217-61320-communication-transports-stage-3  _(id: revive-61320-stage-3)_
- `in_progress` — Revive wit-2-documents epic from epic_dispatcher  _(id: run-1791785128-75074)_
- `in_progress` — Sweep last-48h mini-ork runs in both homes, fix defects, revive  _(id: sweep-48h-revive)_
- `in_progress` — Wait for the researcher-home run to finish before dispatching anything  _(id: run-1791813321-51986)_
- `in_progress` — chat-listen-shape-be revive running on deepseek  _(id: chat-listen-revive)_
- `pending` — 2 local-only commits on engine main (236e89c2, ae1307d9) unpushed  _(id: engine-push)_
- `pending` — Apply and commit the cwd_guard existence check once the tree unfreezes  _(id: cwd-guard-exists)_
- `pending` — Classify a run that dies after planning but before the first node  _(id: d4-postplan-hint)_
- `pending` — Confirm retry-hint failed_node misattribution (implementer vs tier4_synth) in run-72687  _(id: hint-failed-node-attribution)_
- `pending` — Decide the node runtime for the researcher target worktrees  _(id: engine-node-runtime)_
- `pending` — Find and fix the literal 'not-a-lane' lane name that fails 88 calls  _(id: not-a-lane-literal)_
- `pending` — Fix researcher repo eslint parserOptions.project to cover server/**  _(id: fix-researcher-eslint-project)_
- `pending` — Fix the sonnet lane's spawn into a missing /private/tmp directory  _(id: sonnet-lane-missing-tmp-dir)_
- `pending` — Get the fix into the tree the engine home executes (engine checkout main/divergence)  _(id: make-fix-live-engine-home)_
- `pending` — Give the epic scheduler a real verdict for a plan-stage child failure (needs a verdict source that does not collide with retry-hint case 1.5)  _(id: epic-scheduler-verdict)_
- `pending` — Honour MO_TARGET_CWD (and add recover --target) when the recorded target is missing  _(id: d3b-target-override)_
- `pending` — Investigate failed llm_calls rows recorded with no error_message  _(id: empty-error-rows)_
- `pending` — Land the 8 local-only commits upstream so the checkout stops diverging  _(id: land-local-8-upstream)_
- `pending` — Left: target worktree langgraph-survey-r5 deleted; asks already answered  _(id: run-1791798883-27913)_
- `pending` — Pull the fixed engine into researcher/.mini-ork so that run's page shows the fix  _(id: sync-researcher-engine)_
- `pending` — Re-enter run-1791785128-75074 to reuse its pass:true child (validates commit 76140293)  _(id: revive-75074)_
- `pending` — Recreate orch-default-on and wiz-ide worktrees and resume run-1791749955-70478 / run-1791733111-22332  _(id: restore-worktrees)_
- `pending` — Resume 22332 from the reviewer node (target wiz-ide exists)  _(id: run-1791733111-22332)_
- `pending` — Resume run-1791790859-83222 from per_spec_dispatcher  _(id: revive-83222)_
- `pending` — Resume the only revivable researcher run from ratification_check  _(id: run-1791778111-99135)_
- `pending` — Revive 75073/80557/8678/43108/56599/75182 from epic_dispatcher (lane minimax), serially  _(id: revive-epics-6)_
- `pending` — Revive 75073/80557/8678/43108/56599/75182 serially  _(id: epic-backlog)_
- `pending` — Revive 75182 / 56599 / 8678 / 80557 — same class as 43108  _(id: revive-43108-siblings)_
- `pending` — Revive 83222 from per_spec_dispatcher to dispatch D2-D6  _(id: run-1791790859-83222)_
- `pending` — Revive researcher run-1791778111-99135 from ratification_check once the engine checkout frees  _(id: revive-99135)_
- `pending` — Revive run-1791733111-22332 from --from-node reviewer (lane glm)  _(id: revive-22332)_
- `pending` — Revive run-1791788217-61320 (standing request K) with --force --ack-change  _(id: revive-61320)_
- `pending` — Revive run-1792059337-779 from its reviewer once the test baseline runs  _(id: revive-779)_
- `pending` — Revive sibling failed epics 75182, 43108, 56599 serially  _(id: revive-epics)_
- `pending` — Run AC5 full pytest on a clean tree to close P1.5 D6  _(id: ac5-full-suite)_
- `pending` — Run script/mini-ork-build so the IDE draws the header desc  _(id: desc-rebuild)_
- `pending` — Tier1 eslint parserOptions.project resolution once eslint runs  _(id: fix-eslint-project-paths)_
- `pending` — Turn a structured verifier missing[] result into a retry hint  _(id: d6-verifier-hint)_
- `pending` — Update test_ide_pages_orch_tab.py to the stream shape after user confirm  _(id: orch-tab-tests)_
- `pending` — Update the 189-commits-behind researcher .mini-ork install  _(id: researcher-install-update)_
- `pending` — address the tier-4 panel's response.ok and trap-7 findings in the l5b code  _(id: l5b-panel-findings)_
- `pending` — build LangGraph-parity P1.3 (run --resume)  _(id: build-p1-3)_
- `pending` — epic-runner publisher abstains with publish_enabled=false  _(id: epic-publisher-abstain)_
- `pending` — land or abandon the epics-61320 communication-transports deliverable  _(id: decide-61320)_
- `pending` — remove lg-survey-rest and p1-3-root worktrees after the last tail exits  _(id: worktree-cleanup)_
- `pending` — remove or re-key the engine home's 401 KIMI_API_KEY  _(id: kimi-credential)_
- `pending` — remove tamper-kickoff worktree after post-commit watchdog exits  _(id: tamper-kickoff-worktree)_
- `pending` — resumed runs leave the previous attempt's verdict.json, which the rubric grader then reads  _(id: stale-run-verdict-on-resume)_
- `pending` — rotate the researcher home's revoked OPENROUTER_API_KEY  _(id: rotate-openrouter-key)_
- `pending` — run page DAG tab does not draw the classifier node that dags.py synthesises  _(id: classified-row-dag)_
- `pending` — task_state rule 6 reports a dead classification as working with a growing wall clock  _(id: classified-state-working)_
- `pending` — thread page runs table has no node detail and thread_runs is empty for every thread  _(id: thread-runs-node-detail)_
- `failed` — Revive researcher run-1791818713-44675 from tier1_compile  _(id: revive-44675)_
- `failed` — revive m3a from tier1_compile  _(id: revive-m3a)_

### `8162bb3f` — blocked

- **Goal:** Settle which SDD roadmap mechanisms actually shipped, recover the wiped P2 artifact-transport deliverable, and keep the mini-ork dirty-tree ledger accurate across concurrent sessions.
- **Last activity:** 2026-10-17 07:11 · **project:** `/Volumes/docker-ssd/ps/mini-ork` · **1283 fragments**
- **Where it stands:** Ledger closed: this session is recorded clean and the foreign artifact-transport.md edit was routed to single-run-orchestrator. Four user decisions still outstanding (SDD epic launch, G2 IDE page, P2#19 stash guard, MO_EVIDENCE_LEDGER=shadow flip).
- **Topics:** artifact-transport, cross-session-coordination, dirty-tree-ledger, sdd-mechanisms

**Open ledger rows:**

- `in_progress` — I1 probe validity (default-OFF flag)  _(id: sdd-i1)_
- `in_progress` — I5 evidence ledger via framework-edit (researcher-68)  _(id: sdd-i5)_
- `in_progress` — K0.5a finalize and record every run (execute.py half)  _(id: sdd-k05a)_
- `in_progress` — K0.5b verdict hygiene (researcher-68)  _(id: sdd-k05b)_
- `in_progress` — K1 baseline metrics (researcher-68)  _(id: sdd-k1)_
- `in_progress` — Kickoff guard (shadow default)  _(id: kickoff-guard)_
- `in_progress` — SDD roadmap I2 then I6/I8/I7/I4/I9  _(id: sdd-roadmap-continue)_
- `in_progress` — VA AC4 batch to n≥30  _(id: sdd-va-ac4)_
- `in_progress` — mini-ork failure taxonomy mapped to SDD mechanisms  _(id: miniork-failure-taxonomy)_
- `in_progress` — verified-artifact real verifier (researcher-68)  _(id: sdd-va-real-verifier)_
- `pending` — Build SDD epics I2/I4/I6/I7/I8/I9  _(id: sdd-missing-epics)_
- `pending` — C1 context v2 selection in shadow (file findings, kickoff constraints, noise removal)  _(id: ctx-c1)_
- `pending` — C3 same-task history + acknowledgements  _(id: ctx-c3)_
- `pending` — C5 recurrence/lift with holdout  _(id: ctx-c5)_
- `pending` — Collect n>=30 per arm and read lift in metrics context  _(id: ctx-lift)_
- `pending` — Confirm a primary-engine run writes probe-validity.json mode=shadow  _(id: shadow-verify)_
- `pending` — Decide MO_REVIEW_ROUND_AWARE=on  _(id: review-round-aware-on)_
- `pending` — Decide when to set MO_CONTEXT_V2=on  _(id: ctx-on)_
- `pending` — Design P2 artifact transport for cloud runs  _(id: artifact-transport-cloud)_
- `pending` — Enable MO_EVIDENCE_LEDGER=shadow on the mini-ork home  _(id: i5-shadow-enable)_
- `pending` — Fast-forward primary checkout after add-dag-thumbnails lands its edits  _(id: primary-ff)_
- `pending` — Fast-forward primary to include 7805bb92 after I5 r2  _(id: primary-ff-after-i5)_
- `pending` — Get revise-loop merged or released by mini-ork-ec  _(id: revise-loop-claim)_
- `pending` — Guard against primary stash -u (new scope)  _(id: guard-primary-stash-u)_
- `pending` — I1 shadow mode MO_PROBE_VALIDITY=shadow  _(id: sdd-i1-shadow)_
- `pending` — I1-I9 SDD roadmap epics (researcher-68)  _(id: sdd-roadmap)_
- `pending` — IDE page for SDD mechanisms (new scope)  _(id: sdd-ide-page)_
- `pending` — K0 unpublished-spend root cause (researcher-68)  _(id: sdd-k0)_
- `pending` — K0.5c record verifier durations + root-cause unrecorded verifier errors  _(id: sdd-k05c)_
- `pending` — Make planner/node context task-relevant (file-scoped findings, kickoff constraints)  _(id: ctx-pack-relevance)_
- `pending` — Re-dispatch I3 on fixed engine (researcher-68)  _(id: sdd-i3)_
- `pending` — Re-vendor researcher/.mini-ork past ef173ef8  _(id: revendor-researcher)_
- `pending` — Revendor researcher + enable shadow (researcher-68 with researcher-8b)  _(id: shadow-enable-researcher)_
- `pending` — Revendor researcher/.mini-ork with parity checks  _(id: researcher-revendor)_
- `pending` — Route libwit verifier failures into the repair loop  _(id: va2-libwit-repair-loop)_
- `pending` — retry_notify start guard keyed on kickoff content/step (mini-ork-55)  _(id: start-guard-bug)_
- `blocked` — Ingest + dispatch SDD epics I2/I4/I6/I8 (supervised, scoped)  _(id: sdd-epic-launch)_
- `blocked` — P2 artifact-transport design run stalled — needs revive  _(id: p2-artifact-transport-dispatch)_

### `da0b8c69` — blocked

- **Goal:** Make the run page say what happened and why — one page always, the reason promoted, raw output folded away, empty sections gone
- **Last activity:** 2026-10-17 07:10 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **1761 fragments**
- **Where it stands:** Implementation and build are complete; a peer asked me to commit and I declined because only the user can authorise that and their GUI verification has not run. Waiting on the user to either smoke-test or explicitly authorise landing first.
- **Topics:** commit-authority, renderer-change, run-page-IA, section-ids

**Open ledger rows:**

- `in_progress` — Build the mini-ork app bundle  _(id: build-bundle)_
- `in_progress` — Enable the native GPUI inspector in the release build  _(id: dev-inspector-feature)_
- `in_progress` — Every thread card lists all nodes of the run, one per line  _(id: node-roster-in-thread-cards)_
- `in_progress` — Full width left aligned layout on every mini-ork page  _(id: full-width-pages)_
- `in_progress` — Harness tab not visible in the running IDE  _(id: harness-tab-visibility)_
- `in_progress` — Node click in a thread card opens the same node detail as the run-page DAG  _(id: node-click-parity)_
- `in_progress` — Node status must reflect failure with a friendly reason  _(id: status-fix)_
- `in_progress` — Open a run's page on its last-executed node  _(id: run-page-last-node)_
- `in_progress` — Per-node Harness inspector tab (engine view + Rust TABS entry + crate landing)  _(id: harness-tab)_
- `in_progress` — Per-surface GUI smoke test on the built app  _(id: zoom-gui-smoke)_
- `in_progress` — Show the project's agent threads in the rail board, drafts included  _(id: threads-visible)_
- `in_progress` — grant dev.mini-ork.ide Removable Volumes access  _(id: tcc-grant)_
- `pending` — Bump the superproject gitlink and push fork  _(id: zoom-pin-bump)_
- `pending` — Carry kickoff_path into FleetRow and group superseded attempts in the inbox  _(id: inbox-lineage)_
- `pending` — Commit engine + crate changes once the user confirms  _(id: commit-part-0-3)_
- `pending` — Commit in mini-ork-ui main and bump the zed-mini-ork gitlink  _(id: commit-gitlink)_
- `pending` — Decide whether the outcome menu keeps its 'Open in web UI' button (outcome.py:350)  _(id: outcome-web-ui-btn)_
- `pending` — Decide whether to preserve the unpushed crate edits in the two working trees  _(id: preserve-crate-edits)_
- `pending` — Engine: harness view in node.py + _VIEWS + board_cmd choices  _(id: harness-view-engine)_
- `pending` — Exclude ui/ and target/ from rust-analyzer to stop the 6.5 GB scans  _(id: ra-scan-exclusions)_
- `pending` — Fix unclassified-cause copy, the hardcoded ? glyph, full-section ids, per-row landed/repairing pass  _(id: inbox-cheap-fixes)_
- `pending` — Flow map: thread zoom through Layout::compute, connectors, draw literals; real fullscreen  _(id: flow-zoom)_
- `pending` — Give failed rows Retry and Dismiss so the inbox can be emptied  _(id: inbox-actions)_
- `pending` — HID-driven smoke test per surface  _(id: gui-smoke)_
- `pending` — Lift small()/zoom cluster out of render_header; render it in every host incl. tab_mode overlay  _(id: zoom-cluster)_
- `pending` — Make the serve fast path carry MINI_ORK_IDE_SPEC so the IDE renders one run page  _(id: part-0-one-page)_
- `pending` — Move Detail / Recent events / changed files into one collapsed Diagnostics section  _(id: part-2-diagnostics)_
- `pending` — Node tab maximize in place (maximized + in_node_tab, close/Escape un-maximize)  _(id: node-tab-max)_
- `pending` — Optional orchestrator run-buddy reporting per stage and on an interval  _(id: run-buddy)_
- `pending` — Promote Why — evidence under the triage and fix the empty failure label  _(id: part-1-promote-why)_
- `pending` — Pure-function tests for clamp/step/box-fit/anchored-offset  _(id: zoom-unit-tests)_
- `pending` — Remove the marker lines from mini-ork-ui README  _(id: marker-removal)_
- `pending` — Restart the IDE and verify; then push the mini-ork branch  _(id: smoke-test)_
- `pending` — Restore a visible mini-ork thread list in the forked IDE  _(id: sidebar-thread-list)_
- `pending` — Rust: ninth TABS entry in the mini-ork-ui clone  _(id: harness-tab-rust)_
- `pending` — Scale List view literals by self.zoom  _(id: list-zoom)_
- `pending` — Tests: empty, not-configured MCP, missing skill, enabled:false, secret-safety  _(id: harness-tests)_
- `pending` — Update test_ide_pages_inbox_kanban.py to the new design  _(id: inbox-tests)_
- `pending` — User GUI smoke test of the redesigned run page  _(id: gui-smoke-run-page)_
- `pending` — User relaunches and checks all three changes  _(id: user-verify)_
- `pending` — User relaunches app from terminal with stderr tee'd and watches ram-sentinel log while clicking a changed file  _(id: confirm-live-kill)_
- `pending` — User relaunches bundle and verifies scaling/inspector/layout visually  _(id: user-verify-ui)_
- `pending` — Vanish empty sections and add counts to tab labels  _(id: part-3-empties)_
- `pending` — Zoom + real fullscreen on the flow map  _(id: part-4-flow-map)_
- `pending` — Zoom core: named constants + pure clamp/step/box-fit/anchor functions in graph.rs  _(id: zoom-core)_
- `pending` — clippy + mini-ork-check + mini-ork-build + HID-driven GUI smoke test  _(id: verify-zoom)_
- `pending` — reconcile the dirty ui submodule pointer (index 46dda688 vs checkout 70651b86)  _(id: sync-ui-gitlink)_
- `pending` — ui-check, clippy, mini-ork-check, mini-ork-build  _(id: verify-build)_
- `pending` — user relaunches and compares open time vs stock Zed  _(id: launch-verify)_
- `pending` — user relaunches mini-ork IDE.app and compares run page DAG against node tab DAG  _(id: user-relaunch-verify)_
- `pending` — user runs IDE to confirm status bar, rounding, row size, cache paint  _(id: visual-verify)_

### `47ad145f` — blocked

- **Goal:** Make the mini-ork run DAG, sidebar and header chip report each run's true final state
- **Last activity:** 2026-10-12 19:45 · **project:** `/Volumes/docker-ssd/ps/zed-mini-ork` · **64 fragments**
- **Where it stands:** Fixes in /tmp/mo-fold pass the affected suites; blocked on user implementing _published_chip and confirming whether to push origin/main with 2 unpushed local commits or rebase first
- **Topics:** node attempt pairing, regression suite, same-second tie rule

**Open ledger rows:**

- `pending` — Commit /tmp/mo-fold changes and land on origin/main  _(id: land-engine-fix)_
- `pending` — Map delivery kind to header chip label  _(id: published-chip)_

## 7. Sessions marked done — residual ledger rows worth a glance

These sessions declared `progress=done`, but their task array still carries non-completed rows. Most are historical (the row was completed later in the same session and the final block only shows the last snapshot); a few are real leftovers.

### `f5671eaa` — Get the local main line and origin/main onto one history without losing the local orchestrator-tab work

- `in_progress` — Confirm two hr reviewer findings handled on main
- `in_progress` — Per-node sandbox isolation + artifact transport design
- `in_progress` — SDE-1 typed run diagnosis
- `in_progress` — SDE-2 frozen kernel
- `in_progress` — SDE-3b2 diagnosis ledger + calibrated bar
- `in_progress` — Sweep RSI docs for undelivered items bearing on self-driving epic
- `in_progress` — VG-3 blind spec tests (F3)
- `pending` — Build recipes/release-checkpoint with release-checkpoint.json artifact and release-PR drafting
- `pending` — Clarify the level-2 spec path claim for a bare-home worktree run
- `pending` — Confirm hr reviewer findings handled on main
- `pending` — Decide cross-node artifact transport: cloud shared filesystem vs local relay + path contract
- `pending` — Design self-driving epic loop
- `pending` — Dispatch F1-F5 framework-edit verifier fixes
- `pending` — F3 blind held-out spec tests
- `pending` — Fast-forward primary checkout to origin/main
- `pending` — Fix probe runs mutating the primary checkout
- `pending` — Make run buddy interactive/steerable like Zed agent
- `pending` — Operator implements should_escalate()
- `pending` — Reconcile diverged local main in primary checkout
- `pending` — Relaunch run-1792076652-70170 (cn-ui shipped-today doc)
- `pending` — Restore the pre-fix copies of artifact-transport.md and per-node-sandboxes.md in the primary working tree
- `pending` — SDE-3 aggressive escalation wiring
- `pending` — SDE-3b model diagnoser + labels
- `pending` — SDE-4 supervisor: revive parked runs, labels, per-epic budget
- `pending` — SDE-4 supervisor: revive parked runs, ledger labels, per-epic budget
- `pending` — Schedule checkpoint runs via automations and CI and wire auto-tag on the release commit
- `pending` — Ship a prebuilt, signed Zed UI install path in onboard
- `pending` — Single-source the version and harden release.yml to require CI evidence for the tagged SHA
- `pending` — VG-1c harness fixes
- `pending` — WIP-commit leftover dirty worktrees after 1h notice
- `pending` — Write plan doc + dispatch phase 1 via mini-ork
- `pending` — release-checkpoint recipe + release.yml binding
- `failed` — Refresh local main (blocked by another session's dirty tree)

### `1165b056` — Make a bare mini-ork run execute on a Daytona sandbox by default without breaking laptop/CI runs.

- `in_progress` — R1 theme selection own-class first
- `in_progress` — R10 recurring across kickoffs
- `in_progress` — R12 findings remember their target repo
- `in_progress` — R14 reversion guard must not heal a rebase
- `in_progress` — R14 reversion guard must not heal a rebase — merge after reviewer
- `in_progress` — R2 pattern scope by task class
- `in_progress` — R5 harvest hygiene
- `in_progress` — R7a panel family gate
- `in_progress` — R7b merge-time lane-family warning + per-run lane map
- `in_progress` — R8 one kickoff-scope parser (awaiting merge)
- `in_progress` — R8 one scope parser — merge after reviewer
- `in_progress` — R9 v2 scope falls back to the profile allow list
- `in_progress` — Run learning-judge-4l panel and report synthesis
- `in_progress` — findings DB maintenance after merges
- `pending` — Dispatch solution plan R1-R13 as framework-edit kickoffs
- `pending` — Fix dedup, recurrence definition, theme ordering, double injection, scope fallback, rule targeting
- `pending` — Launch R9 + R7b after R8 merges
- `pending` — R11a finding staleness kickoff (written)
- `pending` — R3 R9 R7b R12 R11a R13 R11b
- `pending` — R3 inject each lesson once
- `pending` — Wave 2: R5 R2 R8 R10

### `83e57620` — Brief the user on the OpenClaw star-growth panel report and recommend what a new joiner should implement first

- `in_progress` — Full tests/unit run on pre-merge 2e8367f7 to attribute the 18 failures (task bhokop457)
- `in_progress` — POST /api/v1/runs/plan preview route
- `in_progress` — features IDE page
- `in_progress` — scheduler epic filter
- `pending` — 14 pre-existing local test failures (replay_antigaming x6, codefix_red_base x3, verify_levels x3, mcp_context_server, remote_nodes_e2e)
- `pending` — Cap the reflector NODE bound at --deadline (documented follow-up)
- `pending` — Clear stale node-provider lock + surface dirty-engine failure
- `pending` — Deadline-cap the reflector node (execute_handlers._handle_reflector_early)
- `pending` — Enforce the frozen probe fixture at write time
- `pending` — Fix the typecheck verifier envelope so contract level reads abstained, not unparsable
- `pending` — GET /api/v1/features over the registry
- `pending` — IDE wizard page
- `pending` — Learning-loop integrity fix set from peer mini-ork-7c
- `pending` — POST /api/v1/runs forwards extra_env
- `pending` — Patch test_acp_lane_repair.py fixture so it stubs _setup_readiness_for_thread (clears 14 CI failures)
- `pending` — Repro the path_map translation crash on HEAD
- `pending` — Resume the framework-edit epic child run-1791788217-61320
- `pending` — Unify the judge-panel JSON sidecar schema across lanes + validate it in panel-completeness
- `pending` — User decision: boot read-path representation for ContextNest (flat binary snapshot vs off SQLite)
- `pending` — User's pick among B/C/D/E/F triage follow-ups
- `pending` — Verify the effective daily budget window/cap before launching runs (225 cap vs 3736 spent/24h)
- `pending` — cap the reflect child's bound by the run deadline
- `pending` — clear the F841 red on main (ide_pages/run.py _wall is unfinished) — owned by the live worktree fix-main-buddy
- `pending` — code-fix arm vs control
- `pending` — decide whether a story step row click also opens the node tab
- `pending` — framework-edit blocks post-verification past deadline
- `pending` — mini-ork run --set/--feature routed through gate_env, or HTTP launch from the page
- `pending` — mini_ork/cost/policy.py feature registry + gate
- `pending` — optional: kickoffs/auto/ide-node-tab-dag.md per repo convention
- `pending` — public push
- `pending` — recipe references a lane missing from committed config
- `pending` — rename minimax alias
- `pending` — user rebuilds Zed fork and confirms click gesture + DAG strip layout

### `20a62c45` — Prevent future code-fix runs from being permanently unpublishable when the replay instrument is opted out.

- `in_progress` — Board shows imported tickets beside epics (run in flight)
- `in_progress` — Repoint dead-lane roles and re-dispatch Jira
- `in_progress` — chat-slack-transport run, then chat-discord-transport
- `pending` — Delete leaked acme test rows from the live pm mirror and guard tests against the real DB
- `pending` — Publish or accept f06b8d3719
- `pending` — Reconcile the diverged primary checkout (9 ahead / 40 behind, 17 peer-dirty files)
- `pending` — Remove GitHub smoke mirror rows, /tmp smoke files, last worktrees
- `pending` — Rust document section renderer + file picker in mini-ork-ui
- `pending` — Screenshot the thread page and board in the IDE

### `ad0657b6` — Ship the inline mini-ork thread cards end to end and report what landed.

- `in_progress` — Run minimax/glm judge panel on thread-card design
- `pending` — Fold panel corrections into revised design spec
- `pending` — Implement engine card builder, mini_ork_ui renderer, agent_ui hook

### `f6e1ed90` — Make a click on any DAG node open that node's detail page, the same page an agents-table row opens

- `in_progress` — Add the not_needed terminal run status (migration 0076 + canonical vocabulary + call-site sweep)
- `in_progress` — Board thread rows use a bottom separator
- `in_progress` — Build the IDE after the crate bump
- `in_progress` — Bump crates/mini_ork_ui gitlink to b56cdc4 and push to fork
- `in_progress` — Delegate the aggregated mini-ork gaps to their auditing sessions
- `in_progress` — Deliver remaining AGGREGATE.md gaps via fan-out
- `in_progress` — Design and implement per-run monitor sidecar
- `in_progress` — Hide Load more when the runs are exhausted
- `in_progress` — Re-run or recover the deorch-k3b lane for a clean verdict (blocked on load < 8)
- `in_progress` — Repoint the two .sh pointers and delete the script
- `in_progress` — Revive run-1791887097-86207 through the verifier chain
- `in_progress` — Run-page watching tone and empty state
- `in_progress` — Sources UI with secret-only writes (Epic D)
- `pending` — Build and launch the IDE for an end-to-end look at the tab
- `pending` — Bump the engine's ui/ submodule pin from d0303818 to the crate head 0aefe69
- `pending` — Commit Python side without capturing peer run-page IA work
- `pending` — Commit or park my uncommitted levels/retry_hint/outcome/run/spec edits
- `pending` — Commit or park the levels/retry_hint/ide_pages workstream
- `pending` — Commit+push mini-ork-ui and bump the gitlink
- `pending` — Confirm whether reflect is killed (timeout/OOM) or raises before side-channel #3 in the researcher home
- `pending` — Decide fate of replay-adapter-non-pytest and wire-api-contract-verifier
- `pending` — Decide scope for a Board card -> task detail page
- `pending` — Decide whether the superseded replay should be driven to published
- `pending` — Decide whether to turn MO_CONTEXT_V2 on so verified themes inject
- `pending` — Delete the 5 run-buddy section tests and re-aim the 2 flag tests that use MO_BUDDY as their example
- `pending` — Derive the reflect bound from home size or make reflect resumable per channel (D3)
- `pending` — Fix the 7 stale assertions in test_ide_pages_orch.py
- `pending` — Fold P0 4d + P1.2 abstention into one assignment and settle the canonical abstain contract
- `pending` — GUI live smoke of the Load more control (open /tmp/mo-smoke50, click last child, watch spinner then withdrawal)
- `pending` — Give the verifiers a shared failure_kind emitter, folding in P1.2's contract reconciliation (open, unowned, one decision)
- `pending` — Ingest roadmap.md and dispatch sdd-only epics under tmux
- `pending` — Linear and Jira integration from the IDE UI
- `pending` — Live --once and real-run smoke of the watcher
- `pending` — Make harvest() distinguish missing runs dir / missing DB / empty harvest instead of returning identical zeros (D4)
- `pending` — Merge origin/main and push the 7 local commits
- `pending` — Per-run stage-report feed with a writer (WS-2..WS-11 remain)
- `pending` — Persist reflect's stdout/stderr to <run_dir>/reflect.log instead of discarding it via capture_output=True
- `pending` — Push engine commits to origin main and mini-ork-ui commits to origin
- `pending` — Push engine, mini-ork-ui and fork commits
- `pending` — Reconcile emit_unverified's rc-0 contract with ABSTAIN_RC=3 so the ledger stops claiming a check that never ran
- `pending` — Reconcile local main (8 ahead / 40 behind) without losing unpushed audit lines
- `pending` — Relaunch the serve process on :7090 without the tmp home
- `pending` — Render unwired runs honestly in run.py -- blocked behind the rp-e6-land claim
- `pending` — Replicate failure_kind across the remaining 29 shell-out verifiers
- `pending` — Run the code_findings side-channel first, or as its own bounded child (D1)
- `pending` — User refines classify_spawn's pattern list: which rc/pattern means the tool never started
- `pending` — build zed-mini-ork and verify the card fix + Questions tab in the IDE
- `pending` — bump both consumer pins to 8ffc38b or newer
- `pending` — not_needed terminal state: migration 0076 + 21-site sweep
- `pending` — user decides whether to remove the README review markers from mini-ork-ui public main
- `failed` — Live-smoke the Load more control's render, spinner and withdrawal in the IDE

### `cb737d8d` — Make Zed's follow-the-agent crosshair open every file a mini-ork run's tool calls name, live and on replay

- `in_progress` — Author executable fences into the 5 unprobeable specs
- `in_progress` — Decide whether /run-started runs narrate their terminal transition
- `in_progress` — Deliver board-preflight + codefix-classifier-coverage + specdir-kickoff-precedence via SDD
- `in_progress` — Deliver pick-files-directories via SDD
- `in_progress` — Design the crate copy-in step and fork rewiring
- `in_progress` — Give the run page the node tab's layout: strip on every tab, run-id breadcrumb, roster and totals
- `in_progress` — Inventory mini-ork's dependencies on ui/ and ui.old/
- `in_progress` — Launch the IDE and confirm a held-open turn renders session/update
- `in_progress` — Reduce mini-ork IDE rebuild time
- `in_progress` — Revive engine-code-fix run-1792136781-59220 from its failed node
- `in_progress` — Ship the OpenBao secure-tier kit and live integration test
- `in_progress` — merge mini-ork-v1.23.2 into mini-ork, build and publish
- `in_progress` — rebuild mini-ork IDE.app (sccache disabled, 3rd attempt)
- `in_progress` — rebuild the IDE with the palette fix
- `in_progress` — sweep both homes and revive continuable runs
- `in_progress` — zed-mini-ork-62's uncommitted sidebar.rs edit needs a durable home
- `pending` — Actually rebase onto v1.23.2 and publish
- `pending` — Add .env("PWD", project) in board::run_action
- `pending` — Add a gitleaks/CI secret-scan gate
- `pending` — Add mcp_servers/env to WorkflowNode in workflow.schema.json
- `pending` — Author executable fences for the 5 run-initiation specs
- `pending` — Clear Contents/MacOS/.mini-ork and re-sign the bundle
- `pending` — Confirm ld64.lld links the full Zed binary
- `pending` — Confirm the DAG scrolls both ways in the running app
- `pending` — Decide whether CC-B1 brief-stage ships in the sa1 branch
- `pending` — Decide whether SourceShift/zed-mini-ork stays
- `pending` — Decide whether to commit docs/ux-briefs/2026-10-16-run-initiation-modes.md
- `pending` — Delete implicit remember_home at store.rs:253
- `pending` — Extract pure registry helpers (roots_of, registry_remove, write_registry) + forget/entries/open_or_init
- `pending` — Fix the two precondition ACs citing the nonexistent tests/unit/test_classify.py and dedupe codefix AC1/AC8
- `pending` — Fix the two root-config fixture-leak test failures
- `pending` — Framework-edit verifiers must carry submodule content
- `pending` — Land the uncommitted crate edits in a mini-ork-ui checkout on a branch
- `pending` — Launch the Dispatch pre-flight sheet once the peer commits
- `pending` — MCP registry with ${VAR} auth expansion + mini-ork mcp add|list|rm
- `pending` — Make a raw failure classify as code-fix (regexes, or make path_globs real)
- `pending` — Make render_project_picker match the menu (shared entries model)
- `pending` — Make the ContextNest caches fail closed instead of falling back to a cwd-relative .mini-ork
- `pending` — Make the run page treat a diff-publishing recipe's no-commit as expected
- `pending` — Materialise the submodule in git worktrees
- `pending` — P1-P4: mcp launch, zed setup, mcp proxy, skill registry
- `pending` — Push 5bb2e226, sync both homes, revive the 4 parked researcher runs
- `pending` — Push the four mini-ork commits to fork/mini-ork
- `pending` — Push the four pending zed-mini-ork commits (fee632c57d, aa900b1149, 3685fec317, 721aff43cc) to fork
- `pending` — R3: carry the crate into git worktrees
- `pending` — R4: make the framework-edit verifiers materialise submodule content, and give the fixture teeth
- `pending` — R5: carry the crate to remote nodes
- `pending` — Rebase and push the peer's engine commit ed695c46
- `pending` — Rebuild project_switcher with per-project submenus + Add project…
- `pending` — Remote nodes do not carry submodules
- `pending` — Run-scoped env.yaml + per-node env overrides via publish_env
- `pending` — S.pick_files directories flag → store.rs prompt_for_paths
- `pending` — Skill registry + node-boundary materialization + approved/used record
- `pending` — Sync /Volumes/docker-ssd/ps/mini-ork with origin/main (13 behind, stale working copies)
- `pending` — Tear down the openbao dev container
- `pending` — User picks what mini-ork/ui/ becomes
- `pending` — User runs script/mini-ork-build to prove the IDE links
- `pending` — User runs the app and walks the five project flows
- `pending` — Write the plan file
- `pending` — board preflight <kickoff.md> emitting recipe+marker+top3 scores+resolved:bool
- `pending` — bump mini-ork's ui pin to 7e90bbd
- `pending` — kimi lane 401 blocks four engine-home runs
- `pending` — mini-ork mcp launch <name>: resolve from vault, execvp the real server
- `pending` — mini-ork mcp proxy <name>: stdio-to-http bridge for remote servers
- `pending` — mini-ork zed setup emits mcp:<name> context servers via the launcher shim
- `pending` — mini_ork_ui does not compile after peer session p238's edit
- `pending` — push mini-ork-ui main then zed-mini-ork mini-ork
- `pending` — push mini-ork-ui main then zed-mini-ork mini-ork
- `pending` — script/mini-ork-upgrade --publish cannot complete a base advance
- `deleted` — README never removed the > [!IMPORTANT] lines

### `443ef722` — Know precisely which tasks from this session remain, which only look open because the ledger is stale, and wha

- `in_progress` — ACP tool-call locations for follow-the-agent
- `in_progress` — Agent-S non-interactive driver fix
- `in_progress` — Review policy + merge approval core + approvals CLI
- `pending` — Approve resumes parked run; request-changes re-runs node
- `pending` — Approve resumes parked run; request-changes re-runs node
- `pending` — Decide whether /run should narrate or stay silent
- `pending` — Decide whether to enable review gates on the shared home
- `pending` — Emit ACP tool-call locations from the mini-ork live projection
- `pending` — End-to-end live smoke incl. Agent-S GUI checks
- `pending` — Executor park/reviewing status
- `pending` — IDE restore/approve buttons
- `pending` — Live-verify automated merge holds
- `pending` — Per-node git-ref checkpoints with /restore and an IDE restore button
- `pending` — Per-node review: park/resume through oversight_inbox
- `pending` — Report code-fix replay base results parsing bug
- `pending` — Required approval at every merge path (workspaces.merge, finalize auto-merge, publisher)
- `pending` — Zed review card + /restore
- `pending` — Zed review card + slash commands
- `pending` — workspaces.merge approval gate + human surfaces
- `failed` — Agent-S GUI check of IDE review buttons

### `732bb4cc` — Answer the revive-monitor's commit request accurately without sweeping other sessions' work.

- `in_progress` — make the SAMSUNG volume appear in Finder
- `pending` — clean the author-style-review worktree now that reflect has exited
- `pending` — copy the final report out of the gitignored runs dir into the repo
- `pending` — local main diverged from origin; not this session's commit to reconcile
- `pending` — local main is cleanly FF-able now; deferred because another session is writing
- `pending` — run the recipe end-to-end to prove the three-artifact contract
- `pending` — shared verifier writer setting failure_kind (7 helpers + 70 inline verifiers)

### `7cacb4ac` — Rewrite the round-2 interview prep doc on AI frameworks and agent security so it reads in the operator's plain

- `pending` — Clear root-owned electron/node-gyp cache leftovers with sudo

## 8. Pending user actions (de-duplicated, latest first)


### urgency = now

- **2026-10-16 21:12** · session `da0b8c69` — Approve the run-page IA plan via ExitPlanMode so implementation can start
- **2026-10-16 21:34** · session `f6e1ed90` — Resolve or hand off the parked merge in /Volumes/docker-ssd/ps/mini-ork (MERGE_HEAD set, conflict on AGGREGATE.md)
- **2026-10-17 04:49** · session `f6e1ed90` — Rule on committing or parking the five uncommitted files
- **2026-10-17 07:10** · session `86880f3c` — Restore the two architecture docs from HEAD (git checkout -- docs/architecture/artifact-transport.md docs/architecture/per-node-sandboxes.md)
- **2026-10-17 07:18** · session `86880f3c` — Restore the two architecture docs from HEAD
- **2026-10-17 09:05** · session `55d69b4d` — restart the mini-ork IDE to load the rebuilt binary
- **2026-10-17 09:41** · session `2e068f41` — Pick option 1 (generic levels section rendered in Rust) or option 2 (keep composition in ide_pages)
- **2026-10-17 09:51** · session `cb737d8d` — Close the smoke IDE at /tmp/mo-smoke50 (or move it to another MO_SERVE_PORT) and confirm whether I should take the four code fixes in page.rs/graph.rs after the peer sessions settle.
- **2026-10-17 20:48** · session `1bc9e83a` — Rebuild and restart the IDE, open a live run's Orchestrator tab, and confirm it renders as one stream surface
- **2026-10-18 18:06** · session `2e068f41` — Create/install the GitHub App and set MINI_ORK_APP_ID plus MINI_ORK_APP_PRIVATE_KEY
- **2026-10-18 18:06** · session `2e068f41` — Approve pushing 2aabb9cc9a to fork/mini-ork
- **2026-10-18 19:50** · session `8d83aa4c` — Rotate the Cloudflare API token in the dashboard and update ~/ps/scripts/clf.sh
- **2026-10-18 19:52** · session `8d83aa4c` — Rotate the Cloudflare API token and update ~/ps/scripts/clf.sh
- **2026-10-19 09:46** · session `8d83aa4c` — Add Zone WAF Edit and Account Web Analytics Edit to the Cloudflare token
- **2026-10-19 09:50** · session `8d83aa4c` — Add Account Settings Read to the Cloudflare token

### urgency = soon

- **2026-10-16 17:53** · session `1bc9e83a` — rotate OPENROUTER_API_KEY in /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork/config/secrets.local.sh, or repoint the *_lens / lens_NN / judge_* lanes at direct gateways
- **2026-10-16 17:53** · session `1bc9e83a` — push the two local-only commits on the engine primary (236e89c2, ae1307d9) or tell me to
- **2026-10-16 18:11** · session `f6e1ed90` — Restart mini-ork serve so the IDE serves the fifth board bucket
- **2026-10-16 18:45** · session `86880f3c` — Rotate or re-pin the kimi and deepseek lanes
- **2026-10-16 19:53** · session `732bb4cc` — Give the path to the MKV file to copy
- **2026-10-16 19:55** · session `732bb4cc` — Give the path to the MKV so I can copy it via terminal
- **2026-10-16 19:55** · session `732bb4cc` — Check whether SAMSUNG now appears in Finder, or unplug/replug the stick
- **2026-10-16 21:03** · session `86880f3c` — Update the researcher home's mini-ork install (in-tree HEAD ddc88c43) or pin MO_TARGET_CWD per revive there
- **2026-10-16 21:03** · session `86880f3c` — Choose the fix direction for the contaminated rubric/panel: attempt-scope the failure markers, or pass the attempt start time and ignore older artifacts
- **2026-10-16 21:10** · session `86880f3c` — Decide on 779/99014's test gate: repair the researcher repo's pre-existing failing suites (or lower the jest-guard load refusal) and rerun the test verifier, or accept the test gate as blocked
- **2026-10-16 21:10** · session `86880f3c` — Replace or re-credential the dead lanes (kimi 401; the deepseek_flash fallback tail is missing from providers.yaml) and revive 92791/59220/69809 with --lane kimi=<replacement>
- **2026-10-16 21:15** · session `732bb4cc` — Decide whether to re-run the recipe end-to-end to validate the new three-artifact contract
- **2026-10-16 21:15** · session `f5671eaa` — Decide whether to build artifact transport, and choose cloud shared filesystem vs a `path` contract over the local relay
- **2026-10-16 21:15** · session `86880f3c` — Decide on 779/99014's test gate: repair the researcher repo's pre-existing failing suites (or raise the jest-guard load allowance) and rerun the test verifier, or accept the gate as blocked
- **2026-10-16 21:15** · session `86880f3c` — Replace or re-credential the kimi lane (401) and restore a deepseek_flash fallback entry in providers.yaml, then revive 92791/59220/69809 with --lane kimi=<replacement>
- **2026-10-16 21:15** · session `d5471234` — Launch the IDE, open the run, Overview, and confirm the 'Orchestrator — armed' section is drawn under 'Why? — evidence'
- **2026-10-16 21:16** · session `f6e1ed90` — Confirm the four GUI pixel surfaces (orchestrator section under Why—evidence; the 8-tab orch rail page; DAG background-click; run-page IA/zoom)
- **2026-10-16 21:23** · session `4f781acd` — Top up or re-point the minimax lane quota
- **2026-10-16 21:23** · session `4f781acd` — Run the orch tabs in the IDE and report what they draw, for the GUI half of the live replay
- **2026-10-16 21:23** · session `8162bb3f` — Green-light a supervised scoped run of SDD epics I2/I4/I6/I8, or hold
- **2026-10-16 21:25** · session `f6e1ed90` — Restart the :7090 serve (PID 81516 predates every commit)
- **2026-10-16 21:25** · session `f6e1ed90` — Go/no-go on the SDD epic run (ingest kickoffs/sdd-mechanisms/roadmap.md, then scoped sdd-only dispatch under tmux)
- **2026-10-16 21:27** · session `cb737d8d` — Rule on whether the publisher's publish-unattributable gate should treat an already-applied patch as attributable
- **2026-10-16 21:27** · session `cb737d8d` — Rule on whether a skipped check (no typecheck tool detected) or an opaque non-test command should fail a run
- **2026-10-16 21:27** · session `f5671eaa` — Decide P1#6: build artifact transport for real via a cloud shared filesystem, or keep the local worktree relay and give recipes a path contract that survives isolation
- **2026-10-16 21:30** · session `da0b8c69` — Launch the app and smoke-test the run page once the background build finishes
- **2026-10-16 21:31** · session `f6e1ed90` — Give a go/no-go on the scoped SDD epic dispatch (ingest the roadmap, then sdd-only under tmux)
- **2026-10-16 21:31** · session `da0b8c69` — Launch /Users/admin/ps/zed-mini-ork/target/mini-ork IDE.app and smoke-test the run page
- **2026-10-16 21:33** · session `cb737d8d` — Rule on where the 2026-10-16-run-initiation spec corpus lives: keep it under zed-mini-ork/docs/specs/ or split the 4 engine specs into the engine repo's specs/.
- **2026-10-16 21:34** · session `f6e1ed90` — Give a go/no-go on the scoped SDD epic dispatch
- **2026-10-16 21:34** · session `f6e1ed90` — Restart :7090 — kill 81516, then relaunch WITHOUT --home /private/tmp/mo-smoke50/.mini-ork
- **2026-10-16 21:40** · session `f6e1ed90` — Decide whether to assign the P0 4d emitter sweep (one shared writer plus call-site swaps) or leave it open
- **2026-10-16 21:44** · session `d5471234` — Release the rp-e6-land claim on mini_ork/ide_pages/run.py, or have session p238 land its staged patch
- **2026-10-16 21:44** · session `f6e1ed90` — Decide whether to assign the P0 4d emitter sweep now that its shape is known (one _stage.py template with four copies, plus 47 call sites that already route through a helper)
- **2026-10-16 21:46** · session `f6e1ed90` — Refine classify_spawn's detection in mini_ork/verify/pm.py:182 -- which rc or output pattern separates 'the tool never started' from 'the tool ran and rejected the input'
- **2026-10-16 21:46** · session `f6e1ed90` — kill 81516 and relaunch the :7090 serve without --home /private/tmp/mo-smoke50/.mini-ork
- **2026-10-16 21:46** · session `f6e1ed90` — Rule on the push-race trade in scripts/mini_ork_worktree.py: keep the re-gate (race window stays open, content always gated) or gate once and retry fetch/rebase/push in a tight loop (window mostly closed, a rebase may ship combined content ungated)
- **2026-10-16 21:46** · session `f6e1ed90` — Decide whether to assign the P0 4d + P1.2 abstention work as one item -- a shared failure_kind emitter plus a ruling on which abstention contract is canonical
- **2026-10-16 22:21** · session `f6e1ed90` — Fold the P0 4d + P1.2 abstention work into one assignment and pick the canonical abstention contract
- **2026-10-16 22:57** · session `86880f3c` — Add a ```bash fence to each acceptance criterion in docs/specs/2026-10-16-run-initiation/crate/*.md, or drop the spec-driven-delivery runs on that spec dir
- **2026-10-17 01:06** · session `86880f3c` — decide whether the engine should stop treating a spec test that errors in its own setup as a refutation
- **2026-10-17 02:40** · session `86880f3c` — Decide whether a spec test that dies in its own helper should stop being graded as a refutation of the change
- **2026-10-17 04:29** · session `86880f3c` — Decide whether a spec test that dies in its own helper should stop being graded as a refutation
- **2026-10-17 04:43** · session `d5471234` — Release the rp-e6-land claim on mini_ork/ide_pages/run.py, or let session p238 land its staged patch.
- **2026-10-17 04:44** · session `f6e1ed90` — Decide: commit my 5-file uncommitted workstream scoped, or park it
- **2026-10-17 04:49** · session `f6e1ed90` — Decide the P0 4d + P1.2 abstention scope so the drafted plan can start
- **2026-10-17 04:49** · session `f6e1ed90` — Restart the :7090 serve process without the /private/tmp home
- **2026-10-17 06:22** · session `f6e1ed90` — Choose rebase vs cherry-pick for local main reconciliation
- **2026-10-17 06:23** · session `1bc9e83a` — say go to ship the three-part fix in a worktree (run page classifier node, classified state split, thread runs node column)
- **2026-10-17 06:26** · session `86880f3c` — Sync the researcher home's engine checkout once its uncommitted working set settles
- **2026-10-17 06:26** · session `86880f3c` — Decide the spec-test grading policy for unsatisfiable run-authored tests (68028 class)
- **2026-10-17 07:10** · session `2e068f41` — Approve or decline pushing aeb9522 and 95a6a6a765 to fork
- **2026-10-17 07:10** · session `d5471234` — Decide rp-e10-land: release its claim on run.py (dropping its uncommitted +250/−17 patch and new test file), or keep run.py reserved for that workstream.
- **2026-10-17 07:10** · session `6881f0ad` — run script/mini-ork-ide, open a run, hit the graph's full-screen button, click empty background, and paste what happens
- **2026-10-17 07:10** · session `732bb4cc` — Decide whether to re-run the recipe end-to-end so the merged three-artifact contract is proven by a real run
- **2026-10-17 07:10** · session `732bb4cc` — Say whether to copy the final report out of the gitignored run dir into the repo
- **2026-10-17 07:10** · session `20a62c45` — kill 81516 and relaunch mini-ork serve without --home /private/tmp/mo-smoke50/.mini-ork
- **2026-10-17 07:10** · session `da0b8c69` — Either run the GUI smoke test, or explicitly authorise me to commit the eight files before testing
- **2026-10-17 07:10** · session `f5671eaa` — Decide P1#6: build artifact transport via a cloud shared filesystem, or keep the local worktree relay and give recipes a path contract that survives isolation
- **2026-10-17 07:10** · session `86880f3c` — Confirm the GUI smoke pass so run-page-ux-clarity can commit its 8 paths
- **2026-10-17 07:10** · session `86880f3c` — Rule on rp-e10-land's parked +250/-17 patch in its worktree
- **2026-10-17 07:10** · session `cb737d8d` — Decide whether to accept the fence blockers or have the 12 vacuous ACs demoted to preconditions and codefix AC3-AC5 given --dry-run before fences are written.
- **2026-10-17 07:10** · session `86880f3c` — Author specs with executable fences, or accept that runs 92942/87094 stay blocked
- **2026-10-17 07:11** · session `8162bb3f` — Approve or decline a tmux-supervised, scoped run of SDD epics I2/I4/I6/I8
- **2026-10-17 07:11** · session `86880f3c` — Approve pushing crate aeb9522 before the superproject pin 95a6a6a765
- **2026-10-17 07:11** · session `86880f3c` — Have 3f1634b9's owner verify and push it, or revert it before any push from this checkout
- **2026-10-17 07:18** · session `86880f3c` — Confirm the GUI pass so run-page-ux-clarity can commit its six files
- **2026-10-17 07:18** · session `86880f3c` — Decide on the six orchestrator commits on local main
- **2026-10-17 07:18** · session `f6e1ed90` — Decide how the 7 local commits reach origin/main: side-worktree replay push, or let the peer merge
- **2026-10-17 07:18** · session `86880f3c` — Have the parked run.py patch rebased onto current origin/main before it is committed
- **2026-10-17 07:21** · session `2e068f41` — Decide whether exact design parity may drop review approvals, retry hints, artifact inventory, and run inputs/diagnostics
- **2026-10-17 07:33** · session `f6e1ed90` — Say go for the live replay of the watcher (needs a run)
- **2026-10-17 07:33** · session `f6e1ed90` — Choose push path: side-worktree replay onto origin/main, or wait for the run.py peer work to land and merge once
- **2026-10-17 08:25** · session `86880f3c` — Restore the two architecture docs from HEAD in the engine checkout
- **2026-10-17 08:57** · session `2e068f41` — Approve the landing path: merge f3c16204 into the shared checkout's main, or review the diff first.
- **2026-10-17 08:57** · session `2e068f41` — Launch /Users/admin/ps/zed-mini-ork/target/mini-ork IDE.app, open a run, and report how the DAG tab and Outcome tab look.
- **2026-10-17 08:59** · session `f6e1ed90` — Say go and I run the live replay on a seeded throwaway run
- **2026-10-17 09:27** · session `2e068f41` — Relaunch the built mini-ork IDE and click a node, then click the run id in the breadcrumb
- **2026-10-17 09:31** · session `86880f3c` — Run the GUI pass so run-page-ux-clarity can commit its six files
- **2026-10-17 09:31** · session `86880f3c` — Decide whether the rp-c3 series gets a fresh run from salvage ref 6087a708
- **2026-10-17 09:32** · session `8d83aa4c` — Choose SDD option A (ratification gate only) or B (A plus LLM spec-triage at ingest)
- **2026-10-17 09:34** · session `2e068f41` — Pick option 1, 2, or 3 for framework-edit level reporting
- **2026-10-17 09:52** · session `55d69b4d` — Decide whether to reconcile the primary checkout (7 local-only watcher commits + uncommitted IA work) with origin/main
- **2026-10-17 10:32** · session `86880f3c` — Reconcile local engine main (ahead 7 / behind 21); --ff-only refused
- **2026-10-17 10:38** · session `86880f3c` — Reconcile the local engine main's 7-commit watcher series with origin/main
- **2026-10-17 13:16** · session `86880f3c` — Reconcile local engine main's 7-commit watcher series with origin/main
- **2026-10-17 13:16** · session `86880f3c` — Move or stash the untracked mini_ork/ide_pages/run_header.py before reconciling local main
- **2026-10-17 13:18** · session `86880f3c` — Delete the untracked mini_ork/ide_pages/run_header.py in the primary checkout before reconciling local main
- **2026-10-17 13:22** · session `2e068f41` — Reconcile the primary checkout with origin/main (or authorize a main-based serve) when the other session's IA work is committed
- **2026-10-17 15:24** · session `86880f3c` — Delete the untracked mini_ork/ide_pages/run_header.py, then reconcile local engine main
- **2026-10-17 16:14** · session `86880f3c` — Route the C17/C22 page-shape contract to whoever owns the IDE renderer
- **2026-10-17 16:14** · session `86880f3c` — Delete the untracked run_header.py and evaluate orch_tab.py before reconciling local engine main
- **2026-10-17 16:51** · session `8d83aa4c` — Reconcile the primary checkout at /Volumes/docker-ssd/ps/mini-ork
- **2026-10-17 16:51** · session `86880f3c` — Route the C23 Learnings-tab contract to the IDE renderer owner
- **2026-10-18 01:24** · session `86880f3c` — Resolve the untracked run_header.py and orch_tab.py, then reconcile local engine main
- **2026-10-18 08:32** · session `f5671eaa` — Approve starting Stage 0: single-source the version and make release.yml require a green CI run for the tagged SHA
- **2026-10-18 15:17** · session `20a62c45` — Decide who reconciles the primary checkout, which is now 9 ahead and 40 behind origin/main with 17 uncommitted peer files.
- **2026-10-18 18:00** · session `cb737d8d` — Launch target/mini-ork IDE.app and confirm the steer box appears on a live node.
- **2026-10-18 18:06** · session `2e068f41` — Switch the repository default branch to mini-ork and disable upstream schedules
- **2026-10-18 18:06** · session `2e068f41` — Close PR #1
- **2026-10-18 18:08** · session `55d69b4d` — Press cmd-alt-b now; run script/mini-ork-build and relaunch to verify the fix
- **2026-10-18 18:10** · session `2e068f41` — Fix the rp-c23 kickoff command to drop either test_ide_pages_run_learnings*.py or test_ide_pages_run*.py, then say whether the run should be recreated and resumed at static_check_verifier.
- **2026-10-18 18:21** · session `f6e1ed90` — Quit the running mini-ork IDE and reopen /Volumes/docker-ssd/ps/zed-mini-ork/target/mini-ork IDE.app
- **2026-10-18 18:22** · session `ad0657b6` — Approve the card-schema architecture so implementation planning can start
- **2026-10-18 18:59** · session `ad0657b6` — Ratify or override the four open design decisions (gating, forensics surface, prompt mechanism, branch)
- **2026-10-18 19:50** · session `f5671eaa` — Reconcile the primary checkout (ahead 11 / behind 44, dirty)
- **2026-10-18 20:15** · session `6881f0ad` — Unlock the Mac so the running mini-ork IDE window is visible
- **2026-10-18 21:29** · session `f6e1ed90` — Relaunch the mini-ork IDE app and click a DAG node to confirm the node tab opens
- **2026-10-18 21:56** · session `ad0657b6` — Rebuild the IDE before judging the cards on screen, and decide whether to push the two local commits
- **2026-10-19 05:36** · session `20a62c45` — Choose between accepting the green commit f06b8d3719 as-is or re-running with the replay instrument enabled.
- **2026-10-19 05:52** · session `20a62c45` — Decide whether to publish f06b8d3719 with MO_ALLOW_UNPROVEN_TARGET=1 or accept it as-is.
- **2026-10-19 05:52** · session `20a62c45` — Reconcile the primary checkout so it is no longer 40 commits behind origin/main.
- **2026-10-19 05:58** · session `4f781acd` — Rebuild and open the run page's Orchestrator tab to confirm the stream and steering
- **2026-10-19 05:58** · session `4f781acd` — Choose engine reconciliation: adopt the local series, adopt upstream's orchestrator, or port only orch_tab onto upstream's watcher
- **2026-10-19 06:31** · session `2e068f41` — Update /Volumes/docker-ssd/ps/mini-ork to origin/main (or run new dispatches from an origin/main worktree) so spawned child runs pick up 9470c815.
- **2026-10-19 07:27** · session `2e068f41` — Update the live engine checkout /Volumes/docker-ssd/ps/mini-ork to origin/main (or dispatch from an origin/main worktree) before reviving anything that spawns per-spec children.
- **2026-10-19 07:27** · session `2e068f41` — Decide whether to revive the epic: MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork /Volumes/docker-ssd/ps/mini-ork/bin/mini-ork recover run-1792151796-92942 --from-node per_spec_dispatcher --force --ack-change
- **2026-10-19 07:29** · session `f5671eaa` — Reconcile local main in the primary checkout (ahead 13, behind 62) and review the dirty docs/architecture/artifact-transport.md
- **2026-10-19 09:24** · session `2e068f41` — Decide on reviving the pick-files-directories epic now that the engine carries the fix: MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork bin/mini-ork recover run-1792151796-92942 --from-node per_spec_dispatcher --force --ack-change
- **2026-10-19 09:39** · session `8d83aa4c` — Export top paths and top ASNs for the same window
- **2026-10-19 09:41** · session `8d83aa4c` — Export top paths, ASNs and countries for 2026-10-18T09:37Z..2026-10-19T09:37Z, or add Zone Analytics Read to the token
- **2026-10-19 09:44** · session `8d83aa4c` — Rotate the Cloudflare token used in this turn
- **2026-10-19 09:44** · session `8d83aa4c` — Add Cloudflare WAF rules blocking /.env, /.git, /.svn, /.hg and the iisec UA

### urgency = later

- **2026-10-16 17:53** · session `1bc9e83a` — decide whether the publisher may trust tree content for files the implementer rewrote in place or generated with Bash (gates hand-committing cjd-cc-o1-outline)
- **2026-10-16 17:53** · session `1bc9e83a` — run `pnpm run test` in the researcher repo while 1-min load is below 8, or set JEST_GUARD_SOFT=1
- **2026-10-16 17:53** · session `1bc9e83a` — amend m1a3's scope to the 4 files, or tell me to
- **2026-10-16 18:11** · session `f6e1ed90` — Answer D1, D2 and D6, and say whether to proceed through WS-2..WS-11
- **2026-10-16 18:45** · session `86880f3c` — Decide whether to recreate the hr-smoke-target worktrees
- **2026-10-16 18:45** · session `86880f3c` — Say yes/no on relaunching run-1792076652-70170
- **2026-10-16 18:45** · session `86880f3c` — Answer the pending ram-sentinel CPU-rule fix
- **2026-10-16 21:03** · session `86880f3c` — Regenerate or delete /Volumes/docker-ssd/ps/mini-ork/.mini-ork/runs/rp-e6-graph-focus-20261015-222118/salvage.patch
- **2026-10-16 21:03** · session `86880f3c` — Answer the pending ram-sentinel one-line CPU-rule fix plus launchctl kickstart
- **2026-10-16 21:10** · session `86880f3c` — Update the researcher install's in-tree mini-ork (HEAD ddc88c43) so planner.py carries 8f00468b, or keep exporting MO_TARGET_CWD when recovering there
- **2026-10-16 21:10** · session `86880f3c` — Answer Request C: apply the ram-sentinel CPU-rule one-liner (and not is_chrome_tab(info["argv"]) at /Users/admin/bin/ram-sentinel.py:844) plus launchctl kickstart -k gui/$(id -u)/com.user.ramsentinel, or leave it
- **2026-10-16 21:13** · session `8162bb3f` — Prioritise sdd-ide-page, sdd-missing-epics, guard-primary-stash-u
- **2026-10-16 21:15** · session `3e207e26` — Decide whether to pursue the primary-only cwd-guard test failure
- **2026-10-16 21:15** · session `732bb4cc` — Approve cleaning the author-style-review worktree
- **2026-10-16 21:15** · session `83e57620` — Decide whether the one-line correction at docs/todos/2026-10-14-failed-runs-48h-triage.md:375 is worth a worktree + merge cycle
- **2026-10-16 21:15** · session `86880f3c` — Answer the ram-sentinel question: apply the one-liner (and not is_chrome_tab(info["argv"]) at /Users/admin/bin/ram-sentinel.py:844) plus launchctl kickstart -k gui/$(id -u)/com.user.ramsentinel, or decline
- **2026-10-16 21:15** · session `d5471234` — Decide whether to remove mini-ork-worktrees/_ide-engine and relaunch the IDE against the primary engine
- **2026-10-16 21:16** · session `f6e1ed90` — Provide Linear/Jira credentials and pick secure-vs-simple tier
- **2026-10-16 21:23** · session `4f781acd` — Say whether to refresh the two cost-arithmetic ACP assertions and whether /run should stay quiet
- **2026-10-16 21:23** · session `8162bb3f` — Decide build/park/drop for the two new-scope items (IDE page, stash guard)
- **2026-10-16 21:25** · session `f6e1ed90` — Say whether to push the two local-only commits (5e668ebb, 24506745)
- **2026-10-16 21:27** · session `cb737d8d` — Confirm which remote and branch the docs/specs/2026-10-16-run-initiation specs should land on
- **2026-10-16 21:31** · session `f6e1ed90` — Rule on P2#15: build a new stage-report writer, or leave it gone
- **2026-10-16 21:31** · session `f6e1ed90` — Decide the P1#6 artifact fork: cloud shared filesystem, or local relay with a surviving path contract
- **2026-10-16 21:37** · session `20a62c45` — Put one ticket credential in place as a key name and set that source enabled: true in config/pm.yaml
- **2026-10-16 21:39** · session `732bb4cc` — Decide whether to take on the failure_kind sweep (1 of 124 verifiers emits it)
- **2026-10-16 21:42** · session `732bb4cc` — Decide whether to take the failure_kind sweep, now scoped to 7 shared helpers + 70 inline verifiers
- **2026-10-16 21:44** · session `d5471234` — Let the two live framework-edit runs finish if you want the audit-doc update written
- **2026-10-16 21:44** · session `f6e1ed90` — Rule on the push-race trade in scripts/mini_ork_worktree.py -- keep the re-gate (window stays open, content always gated) or gate once and retry fetch/rebase/push in a tight loop (window mostly closed, a rebase may ship combined content ungated)
- **2026-10-16 22:21** · session `f6e1ed90` — Kill PID 81516 and relaunch the serve on :7090 without --home /private/tmp/...
- **2026-10-16 22:57** · session `86880f3c` — Answer the ram-sentinel question: apply the Chrome-tab CPU-rule one-liner (ram-sentinel.py:844) and kickstart com.user.ramsentinel?
- **2026-10-16 22:57** · session `86880f3c` — Decide whether an all-unprobeable spec should block at spec_lint instead of dispatching (it currently warns and runs anyway)
- **2026-10-17 01:06** · session `86880f3c` — run pnpm install in 99014's dispatch worktree or accept its test gate as blocked-on-red-base
- **2026-10-17 01:06** · session `86880f3c` — add bash fences to docs/specs/2026-10-16-run-initiation/crate/*.md or drop the two SDD runs
- **2026-10-17 01:07** · session `d5471234` — Nothing needed for the .sh repoint — it self-unblocks when lf-r14 (pid 29199) merges or releases tests/unit/test_githooks_py.py.
- **2026-10-17 02:40** · session `86880f3c` — Apply or decline the ram-sentinel CPU-rule one-liner
- **2026-10-17 04:29** · session `86880f3c` — Run pnpm install in 99014’s dispatch worktree
- **2026-10-17 04:29** · session `86880f3c` — Add ```bash fences to the SDD specs or drop the gate
- **2026-10-17 04:44** · session `f6e1ed90` — Rule on the retry_hint.py kind policy (spec-author defect vs change refutation)
- **2026-10-17 05:52** · session `86880f3c` — Run pnpm install in 99014's dispatch worktree and add ```bash fences to the SDD pair's specs
- **2026-10-17 06:26** · session `86880f3c` — Reap the 25 stale executing rows in the researcher state.db
- **2026-10-17 06:26** · session `f6e1ed90` — Say whether to push the 10 local-ahead commits to origin/main
- **2026-10-17 06:32** · session `f6e1ed90` — Decide whether the two untracked companion briefs should be committed as part of the series
- **2026-10-17 07:10** · session `6881f0ad` — decide whether to prune the shared repo or leave the gc warning in place
- **2026-10-17 07:10** · session `6881f0ad` — decide whether the run page's own DAG background click should force the first tab or stay a no-op
- **2026-10-17 07:10** · session `732bb4cc` — Decide whether to take the failure_kind sweep, scoped to 7 shared helpers + 70 inline verifiers
- **2026-10-17 07:10** · session `20a62c45` — Place one ticket credential as a key name and set that source enabled: true in config/pm.yaml
- **2026-10-17 07:10** · session `86880f3c` — Decide whether to reconcile the researcher/.mini-ork clone (284 dirty, 189 behind, b5f957fb local-only)
- **2026-10-17 07:11** · session `8162bb3f` — Decide build/park/drop for the G2 SDD IDE page and the P2#19 stash guard
- **2026-10-17 07:11** · session `8162bb3f` — Approve flipping MO_EVIDENCE_LEDGER=shadow in secrets.local.sh
- **2026-10-17 07:11** · session `3e207e26` — Decide how to treat the orchestrator commit 3f1634b9 sitting unpushed on local main
- **2026-10-17 07:18** · session `f6e1ed90` — Land the run.py tone/copy spec through rp-e10-land or reassign the file
- **2026-10-17 08:25** · session `86880f3c` — Decide whether to push or rebase the six unpushed orchestrator commits on local engine main
- **2026-10-17 08:33** · session `d5471234` — Say whether to investigate the .cost-spent vs llm_calls gap (panel lanes bypassing the pause check).
- **2026-10-17 08:39** · session `2e068f41` — Decide how the redesign lands: merge wt/run-page-redesign into local main (which also brings origin/main's 6 commits) or push to origin/main.
- **2026-10-17 09:05** · session `55d69b4d` — let the other session commit its run-page work, then fast-forward local main
- **2026-10-17 09:31** · session `86880f3c` — Reconcile local engine main with origin/main
- **2026-10-17 10:32** · session `86880f3c` — Restart rp-c3 from salvage 6087a708
- **2026-10-17 10:32** · session `86880f3c` — Update the researcher mini-ork clone to current origin/main
- **2026-10-17 12:25** · session `86880f3c` — Restart lf-r11b from salvage 85cdf18cd3a9 or fix the spec test's missing 'import pytest'
- **2026-10-17 13:22** · session `55d69b4d` — Decide when to reconcile the primary checkout with origin/main
- **2026-10-17 13:22** · session `2e068f41` — Reconcile the primary checkout with origin/main when the other session's IA work is committed
- **2026-10-17 16:24** · session `86880f3c` — Restart lf-r11b from salvage 85cdf18cd3a9
- **2026-10-17 16:51** · session `8d83aa4c` — Decide SDD Stage 1 Ingest: deterministic only vs LLM with accept/adjust
- **2026-10-17 17:24** · session `86880f3c` — Fix the kickoff's verification command used for C23 if it will be re-run (drop the overlapping glob)
- **2026-10-18 18:10** · session `2e068f41` — Decide whether lf-r11b should get a fresh implementer revision that removes the builtins pytest shim and the dead _window_landed helper.
- **2026-10-18 18:10** · session `2e068f41` — Decide whether the rp-c3 triage-truth change should be redone in a fresh worktree; its original target is pruned and the primary checkout is dirty with other sessions' work.
- **2026-10-18 18:22** · session `4f781acd` — Delete the two marker lines from mini_ork_ui/README.md yourself if you want it cleared
- **2026-10-18 19:50** · session `8d83aa4c` — Tell me the remote URL for the miniork-docs repo
- **2026-10-18 20:09** · session `83e57620` — Have the session holding mini_ork/acp/agent.py commit it, then reconcile primary main
- **2026-10-19 01:05** · session `2e068f41` — Decide the lf-r11b landing-signal remainder (retry-gate inbox 62): the gate blocks dispatch and its target worktree is gone, so it needs an operator call or a restart as a new run.
- **2026-10-19 08:24** · session `2e068f41` — Decide on reviving the pick-files-directories epic: MINI_ORK_HOME=/Volumes/docker-ssd/ps/mini-ork/.mini-ork bin/mini-ork recover run-1792151796-92942 --from-node per_spec_dispatcher --force --ack-change

## 9. Appendix A — TODO ledger (de-duplicated)

- **2026-10-19 09:50** · session `8d83aa4c` — Enable Cloudflare Web Analytics
- **2026-10-19 09:46** · session `8d83aa4c` — Block dotfile probes via WAF
- **2026-10-19 09:41** · session `8d83aa4c` — Correlate paths and ASNs
- **2026-10-19 09:24** · session `2e068f41` — Operator decision on reviving the pick-files-directories epic
- **2026-10-19 08:47** · session `f5671eaa` — Ship a prebuilt, signed Zed UI install path in onboard
- **2026-10-19 07:29** · session `f5671eaa` — Reconcile diverged local main in primary checkout
- **2026-10-19 06:09** · session `3e207e26` — conformal accept/abstain gate
- **2026-10-19 06:09** · session `3e207e26` — reward/evidence pairing
- **2026-10-19 06:09** · session `3e207e26` — judge-gate validity audit
- **2026-10-19 06:09** · session `3e207e26` — behavioral recomputation surface
- **2026-10-19 06:09** · session `3e207e26` — oracle-integrity audit
- **2026-10-19 06:09** · session `3e207e26` — hidden regenerable ground truth
- **2026-10-19 06:09** · session `3e207e26` — discrimination-anchored test evidence
- **2026-10-19 06:09** · session `3e207e26` — verifier mutation-discrimination suite
- **2026-10-19 06:07** · session `d5471234` — Revive the pick-files-directories SDD run
- **2026-10-19 05:58** · session `4f781acd` — User replays the Orchestrator tab in the rebuilt GUI
- **2026-10-19 05:58** · session `4f781acd` — Reconcile local orchestrator series with origin/main's runner_orchestrator and land e8bbb609
- **2026-10-19 05:52** · session `20a62c45` — Sync the primary checkout so runs use the fixed engine
- **2026-10-19 05:52** · session `20a62c45` — Publish or accept f06b8d3719
- **2026-10-19 05:36** · session `20a62c45` — Decide: accept f06b8d3719 or re-run with replay enabled
- **2026-10-18 20:15** · session `6881f0ad` — Live visual smoke of the rebuilt IDE
- **2026-10-18 19:58** · session `3e207e26` — Run 2000-paper agentic verification review and deliver implementable techniques
- **2026-10-18 19:52** · session `8d83aa4c` — Rotate exposed Cloudflare API token
- **2026-10-18 19:50** · session `f5671eaa` — release-checkpoint recipe + release.yml binding
- **2026-10-18 19:50** · session `8d83aa4c` — Add a git remote for miniork-docs
- **2026-10-18 19:40** · session `3e207e26` — Resolve test_dispatch_model_fails_fast_on_framework_cwd failing only in the primary
- **2026-10-18 18:59** · session `ad0657b6` — Fold panel corrections into revised design spec
- **2026-10-18 18:31** · session `ad0657b6` — Run minimax/glm judge panel on thread-card design
- **2026-10-18 18:22** · session `ad0657b6` — Implement engine card builder, mini_ork_ui renderer, agent_ui hook
- **2026-10-18 18:21** · session `f6e1ed90` — Push engine, mini-ork-ui and fork commits
- **2026-10-18 18:10** · session `2e068f41` — Fix the rp-c23 kickoff duplicate pytest glob and decide whether to recreate rp-c23
- **2026-10-18 18:10** · session `2e068f41` — Decide whether lf-r11b gets a fresh revision run without the builtins pytest shim
- **2026-10-18 18:10** · session `2e068f41` — Decide the fate of the rp-c3 triage-truth change after impl_moved_base
- **2026-10-18 18:08** · session `55d69b4d` — Rebuild mini-ork IDE and relaunch to verify rail opens
- **2026-10-18 18:06** · session `2e068f41` — Default branch, App token, PR #1, branch protection
- **2026-10-18 15:23** · session `4f781acd` — Decide whether to refresh the three ACP assertions
- **2026-10-18 15:23** · session `4f781acd` — GUI half of the live replay on the orch tabs
- **2026-10-18 15:23** · session `4f781acd` — minimax lane out of quota for real
- **2026-10-18 15:17** · session `20a62c45` — Reconcile the diverged primary checkout (9 ahead / 40 behind, 17 peer-dirty files)
- **2026-10-18 08:32** · session `f5671eaa` — Build recipes/release-checkpoint with release-checkpoint.json artifact and release-PR drafting
- **2026-10-18 08:32** · session `f5671eaa` — Single-source the version and harden release.yml to require CI evidence for the tagged SHA
- **2026-10-18 08:32** · session `f5671eaa` — Schedule checkpoint runs via automations and CI and wire auto-tag on the release commit
- **2026-10-18 01:24** · session `86880f3c` — Monitor and revive failed mini-ork runs (both homes)
- **2026-10-17 20:48** · session `1bc9e83a` — Orchestrator tab as node-Stream-style surface
- **2026-10-17 20:48** · session `1bc9e83a` — Update test_ide_pages_orch_tab.py to the stream shape after user confirm
- **2026-10-17 16:51** · session `8d83aa4c` — IDE renderer halves (C1, C2, C12/13/14-render, C24, C26)
- **2026-10-17 15:48** · session `f6e1ed90` — Commit Python side without capturing peer run-page IA work
- **2026-10-17 15:48** · session `f6e1ed90` — Bump crates/mini_ork_ui gitlink to b56cdc4 and push to fork
- **2026-10-17 13:40** · session `f6e1ed90` — Push engine commits to origin main and mini-ork-ui commits to origin
- **2026-10-17 13:40** · session `f6e1ed90` — Build and launch the IDE for an end-to-end look at the tab
- **2026-10-17 13:23** · session `55d69b4d` — Reconcile primary checkout with origin/main
- **2026-10-17 13:22** · session `2e068f41` — Reconcile primary checkout with origin/main so :7090 serves the levels section
- **2026-10-17 09:51** · session `cb737d8d` — Land fetch_page fallback, live-based steer gate, and mapped steer role
- **2026-10-17 09:45** · session `1165b056` — findings DB maintenance after merges
- **2026-10-17 09:44** · session `1165b056` — reharvest + prune + clear-artifact-files + backfill-target-repo + check-landing
- **2026-10-17 09:41** · session `2e068f41` — Choose core-data vs ide_pages vs Rust FE boundary
- **2026-10-17 09:34** · session `2e068f41` — Choose how framework-edit levels should read
- **2026-10-17 09:32** · session `8d83aa4c` — Decisions explain themselves; one attention lane
- **2026-10-17 09:32** · session `8d83aa4c` — SDD operator ratification gate
- **2026-10-17 09:31** · session `86880f3c` — Decide whether rp-c3 series restarts from salvage 6087a708
- **2026-10-17 09:27** · session `2e068f41` — Verify breadcrumb in the launched IDE
- **2026-10-17 09:05** · session `55d69b4d` — Refresh local main after worktree merge
- **2026-10-17 08:59** · session `f6e1ed90` — Live --once and real-run smoke of the watcher
- **2026-10-17 08:59** · session `f6e1ed90` — Run-page watching tone and empty state
- **2026-10-17 08:57** · session `2e068f41` — Land engine branch after live runs finish; visual check in IDE
- **2026-10-17 08:56** · session `1165b056` — R11b landing signal (patch-id)
- **2026-10-17 08:56** · session `1165b056` — reharvest + prune + clear-artifact-files + backfill-target-repo
- **2026-10-17 08:33** · session `d5471234` — determine whether panel lanes bypass cost_pause.check() and whether the pause threshold is therefore under-enforced
- **2026-10-17 08:14** · session `1165b056` — R11b patch-id landing check
- **2026-10-17 08:14** · session `1165b056` — R13 make the efficacy measurement meaningful
- **2026-10-17 08:03** · session `8d83aa4c` — One truthful status (triage node, human chips)
- **2026-10-17 07:33** · session `f6e1ed90` — Merge origin/main and push the local commits
- **2026-10-17 07:28** · session `1165b056` — R11a findings age out and go stale at HEAD
- **2026-10-17 07:21** · session `2e068f41` — Decide target level and which safety surfaces must survive exact parity
- **2026-10-17 07:18** · session `f6e1ed90` — Run-page watching tone and empty state (owned by rp-e10-land)
- **2026-10-17 07:18** · session `f6e1ed90` — Merge origin/main and push the 7 local commits
- **2026-10-17 07:16** · session `86880f3c` — Broadcast commit requests to mini-ork-owning sessions
- **2026-10-17 07:11** · session `8162bb3f` — Build SDD epics I2/I4/I6/I7/I8/I9
- **2026-10-17 07:11** · session `8162bb3f` — Add an SDD page so mechanisms render in the Zed-fork UI
- **2026-10-17 07:11** · session `8162bb3f` — Guard against `git stash -u` in the primary stranding untracked deliverables
- **2026-10-17 07:10** · session `cb737d8d` — Author executable fences into the 5 unprobeable specs
- **2026-10-17 07:10** · session `f5671eaa` — Decide cross-node artifact transport: cloud shared filesystem vs local relay + path contract
- **2026-10-17 07:10** · session `f5671eaa` — Restore the pre-fix copies of artifact-transport.md and per-node-sandboxes.md in the primary working tree
- **2026-10-17 07:10** · session `da0b8c69` — Commit engine + crate changes once the user confirms
- **2026-10-17 07:10** · session `da0b8c69` — User GUI smoke test of the redesigned run page
- **2026-10-17 07:10** · session `732bb4cc` — shared verifier writer setting failure_kind (7 helpers + 70 inline verifiers)
- **2026-10-17 07:10** · session `732bb4cc` — run the recipe end-to-end to prove the three-artifact contract
- **2026-10-17 07:10** · session `732bb4cc` — local main is cleanly FF-able now; deferred because another session is writing
- **2026-10-17 07:10** · session `732bb4cc` — copy the final report out of the gitignored runs dir into the repo
- **2026-10-17 07:10** · session `6881f0ad` — Six pre-existing unit failures (acp, config-seam, inbox, learning)
- **2026-10-17 07:10** · session `6881f0ad` — Click empty DAG background navigates to the run main page, on all three surfaces
- **2026-10-17 07:10** · session `d5471234` — per-run scope disclosure + cross-run view
- **2026-10-17 07:10** · session `d5471234` — record P1#9/P2#16/P2#18 resolutions in d5471234-orchestrator-always-visible.md
- **2026-10-17 07:10** · session `d5471234` — run.py three-state orchestrator section (off / armed / enabled-unwired)
- **2026-10-17 07:10** · session `1165b056` — R3 render themes once; no phantom planner retrievals
- **2026-10-17 06:58** · session `f6e1ed90` — Design and implement per-run monitor sidecar
- **2026-10-17 06:46** · session `2e068f41` — Improve inspector element references (track_caller helpers + tab-prefixed section ids)
- **2026-10-17 06:26** · session `f6e1ed90` — Optionally push the 10 local-ahead commits to origin/main
- **2026-10-17 06:26** · session `86880f3c` — Monitor and revive failed mini-ork runs in both homes
- **2026-10-17 06:23** · session `1bc9e83a` — task_state rule 6 reports a dead classification as working with a growing wall clock
- **2026-10-17 06:23** · session `1bc9e83a` — thread page runs table has no node detail and thread_runs is empty for every thread
- **2026-10-17 06:23** · session `1bc9e83a` — run page DAG tab does not draw the classifier node that dags.py synthesises
- **2026-10-17 06:22** · session `f6e1ed90` — Reconcile local main (8 ahead / 40 behind) without losing unpushed audit lines
- **2026-10-17 06:21** · session `1165b056` — R12 findings remember their target repo
- **2026-10-17 04:52** · session `1165b056` — R12 target repo on findings
- **2026-10-17 04:52** · session `1165b056` — R9 v2 scope falls back to the profile allow list
- **2026-10-17 04:52** · session `1165b056` — R13 measurable arms + report-only retirement
- **2026-10-17 04:52** · session `1165b056` — R7b merge-time lane-family warning + per-run lane map
- **2026-10-17 04:49** · session `f6e1ed90` — Fold P0 4d + P1.2 abstention into one assignment and settle the canonical abstain contract
- **2026-10-17 04:49** · session `f6e1ed90` — Commit or park the levels/retry_hint/ide_pages workstream
- **2026-10-17 04:49** · session `f6e1ed90` — Relaunch the serve process on :7090 without the tmp home
- **2026-10-17 04:44** · session `f6e1ed90` — Repoint the two .sh pointers and delete the script
- **2026-10-17 04:44** · session `f6e1ed90` — Commit or park my uncommitted levels/retry_hint/outcome/run/spec edits
- **2026-10-17 04:43** · session `d5471234` — delete scripts/mini-ork-worktree.sh (bash-removal Phase 4)
- **2026-10-17 02:21** · session `1165b056` — Launch R9 + R7b after R8 merges
- **2026-10-17 02:21** · session `1165b056` — R8 one scope parser — merge after reviewer
- **2026-10-17 02:21** · session `1165b056` — R11a finding staleness kickoff (written)
- **2026-10-17 02:21** · session `1165b056` — R14 reversion guard must not heal a rebase — merge after reviewer
- **2026-10-17 01:07** · session `d5471234` — repoint hook + AGENTS.md at scripts/mini_ork_worktree.py, then delete the .sh
- **2026-10-17 01:07** · session `f6e1ed90` — Repoint the two .sh pointers and delete the script (blocked: lf-r14)
- **2026-10-17 01:06** · session `86880f3c` — a spec test that errors in its own setup must not be graded as a refutation
- **2026-10-16 23:52** · session `1165b056` — R7b run profile records the effective lane map
- **2026-10-16 23:52** · session `1165b056` — R3 inject each lesson once
- **2026-10-16 23:52** · session `1165b056` — R14 reversion guard must not heal a rebase
- **2026-10-16 23:52** · session `1165b056` — R8 one kickoff-scope parser (awaiting merge)
- **2026-10-16 22:57** · session `86880f3c` — R5 blames the change for an all-unprobeable gate (handed to the classification workstream)
- **2026-10-16 22:21** · session `f6e1ed90` — Deliver remaining AGGREGATE.md gaps via fan-out
- **2026-10-16 21:46** · session `f6e1ed90` — User refines classify_spawn's pattern list: which rc/pattern means the tool never started
- **2026-10-16 21:46** · session `f6e1ed90` — Reconcile emit_unverified's rc-0 contract with ABSTAIN_RC=3 so the ledger stops claiming a check that never ran
- **2026-10-16 21:46** · session `f6e1ed90` — Give the verifiers a shared failure_kind emitter, folding in P1.2's contract reconciliation (open, unowned, one decision)
- **2026-10-16 21:46** · session `f6e1ed90` — Render unwired runs honestly in run.py -- blocked behind the rp-e6-land claim
- **2026-10-16 21:46** · session `732bb4cc` — local main diverged from origin; not this session's commit to reconcile
- **2026-10-16 21:44** · session `732bb4cc` — local main diverged from origin; needs the other session to reconcile
- **2026-10-16 21:44** · session `f6e1ed90` — Give the verifiers a shared failure_kind emitter (open, unowned pending a decision; one template, four copies, 47 call sites)
- **2026-10-16 21:44** · session `d5471234` — orchestrator section is per-run with LIMIT 6; build a cross-run view or disclose it in the UI
- **2026-10-16 21:44** · session `d5471234` — record the P1#9/P2#16/P2#18 resolutions in d5471234 and AGGREGATE
- **2026-10-16 21:44** · session `d5471234` — run page must require the armed receipt so an unwired run does not render as armed
- **2026-10-16 21:42** · session `732bb4cc` — give recipe verifiers a shared writer that sets failure_kind (7 helpers + 70 inline verifiers)
- **2026-10-16 21:40** · session `f6e1ed90` — Give the verifiers a shared failure_kind emitter (open, unowned pending a decision)
- **2026-10-16 21:39** · session `732bb4cc` — give every recipe verifier a shared envelope writer that sets failure_kind
- **2026-10-16 21:39** · session `f6e1ed90` — Give 123 verifier scripts a failure_kind emitter (unassigned)
- **2026-10-16 21:34** · session `f6e1ed90` — Delegate the aggregated mini-ork gaps to their auditing sessions
- **2026-10-16 21:27** · session `cb737d8d` — Author executable fences into the 5 unprobeable 2026-10-16-run-initiation specs (36 ACs)
- **2026-10-16 21:27** · session `cb737d8d` — Fix the two precondition ACs citing the nonexistent tests/unit/test_classify.py and dedupe codefix AC1/AC8
- **2026-10-16 21:26** · session `f5671eaa` — Clarify the level-2 spec path claim for a bare-home worktree run
- **2026-10-16 21:25** · session `f6e1ed90` — Ingest roadmap.md and dispatch sdd-only epics under tmux
- **2026-10-16 21:25** · session `8162bb3f` — IDE page for SDD mechanisms (new scope)
- **2026-10-16 21:25** · session `8162bb3f` — Ingest + dispatch SDD epics I2/I4/I6/I8 (supervised, scoped)
- **2026-10-16 21:25** · session `8162bb3f` — Guard against primary stash -u (new scope)
- **2026-10-16 21:15** · session `6881f0ad` — Rebuild the IDE binary with the new pin
- **2026-10-16 21:15** · session `6881f0ad` — Live smoke the background click on all three DAG surfaces
- **2026-10-16 21:15** · session `86880f3c` — Revive researcher run 779 from its failed node
- **2026-10-16 21:15** · session `86880f3c` — Fix artifact_summary so a revive is not graded on superseded markers
- **2026-10-16 21:15** · session `83e57620` — Correct the stale 'row still reads executing' claim at triage-doc line 375 (DB now says published)
- **2026-10-16 21:15** · session `732bb4cc` — clean the author-style-review worktree now that reflect has exited
- **2026-10-16 21:15** · session `732bb4cc` — run the recipe end-to-end to prove the families.json/clusters.json/cluster-assignment.json contract
- **2026-10-16 21:15** · session `3e207e26` — test_dispatch_model_fails_fast_on_framework_cwd fails only in the primary checkout
- **2026-10-16 21:15** · session `8162bb3f` — Guard against git stash -u stranding untracked deliverables in the primary
- **2026-10-16 21:15** · session `8162bb3f` — Add an SDD IDE page so mechanisms are visible in the Zed UI
- **2026-10-16 21:12** · session `da0b8c69` — Vanish empty sections and add counts to tab labels
- **2026-10-16 21:12** · session `da0b8c69` — Promote Why — evidence under the triage and fix the empty failure label
- **2026-10-16 21:12** · session `da0b8c69` — Make the serve fast path carry MINI_ORK_IDE_SPEC so the IDE renders one run page
- **2026-10-16 21:12** · session `da0b8c69` — Move Detail / Recent events / changed files into one collapsed Diagnostics section
- **2026-10-16 21:03** · session `86880f3c` — Revive the failed researcher-home runs
- **2026-10-16 19:55** · session `732bb4cc` — copy the MKV onto the formatted stick
- **2026-10-16 19:55** · session `732bb4cc` — make the SAMSUNG volume appear in Finder
- **2026-10-16 19:53** · session `732bb4cc` — reformat WIN11 stick as exFAT/MBR for Samsung TV USB playback
- **2026-10-16 18:45** · session `86880f3c` — Triage failed runs in both mini-ork homes
- **2026-10-16 18:34** · session `4f781acd` — Full tests/unit run: is the ACP trio the only red?
- **2026-10-16 18:11** · session `f6e1ed90` — Per-run stage-report feed with a writer (WS-2..WS-11 remain)
- **2026-10-16 17:53** · session `1bc9e83a` — revive run-1792059337-779 from its reviewer once the researcher jest baseline runs
- **2026-10-16 17:53** · session `1bc9e83a` — remove or re-key the engine home's 401 KIMI_API_KEY
- **2026-10-16 17:53** · session `1bc9e83a` — rotate the researcher home's revoked OPENROUTER_API_KEY
- **2026-10-16 17:53** · session `1bc9e83a` — 2 local-only commits on engine main (236e89c2, ae1307d9) unpushed
- **2026-10-16 17:53** · session `1bc9e83a` — Revive m3a-run-log-reader-removal from tier1_compile

## 10. Appendix B — session inventory

| Session | Fragments | Last | Project |
|---|---|---|---|
| `8d83aa4c-8e18-4326-8e52-b08c60908b4e` | 782 | 2026-10-19 09:50 | `/Volumes/docker-ssd/ps/mini-ork` |
| `2e068f41-c1c1-41fa-bd2a-42d63fd1fb4b` | 697 | 2026-10-19 09:24 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `3e207e26-3029-4418-bf4d-938cbfc7dcff` | 1756 | 2026-10-19 09:21 | `/Volumes/docker-ssd/ps/mini-ork` |
| `f5671eaa-8c0c-4051-9477-10aadbf837fd` | 1125 | 2026-10-19 08:47 | `/Volumes/docker-ssd/ps/mini-ork` |
| `1165b056-ac2a-4687-8f6b-82aef09794b3` | 563 | 2026-10-19 07:05 | `/Volumes/docker-ssd/ps/mini-ork` |
| `d5471234-9015-4d4d-a85e-9e5353821fde` | 5474 | 2026-10-19 06:41 | `/Volumes/docker-ssd/ps/mini-ork` |
| `83e57620-6d0b-41ec-9619-ff44c3c92318` | 1451 | 2026-10-19 06:15 | `/Volumes/docker-ssd/ps/mini-ork` |
| `4f781acd-be5b-4284-84e6-00f395cde3b3` | 634 | 2026-10-19 05:58 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `20a62c45-9604-44b0-808d-4a66dd73ae2d` | 701 | 2026-10-19 05:52 | `/Volumes/docker-ssd/ps/mini-ork` |
| `ad0657b6-0b61-4d58-9da3-567f7badf0e3` | 120 | 2026-10-18 22:18 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `f6e1ed90-7a0a-4081-b46e-e612f0af5fd0` | 1839 | 2026-10-18 21:29 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `6881f0ad-501e-4292-9b9c-d2cc2fa503a7` | 3148 | 2026-10-18 20:15 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `cb737d8d-b58f-4c61-97a2-24b432c9e14d` | 2468 | 2026-10-18 18:45 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `55d69b4d-b14c-4a62-a983-b0ba64987175` | 146 | 2026-10-18 18:08 | `/Volumes/docker-ssd/ps/mini-ork` |
| `86880f3c-c218-464f-a7f2-8aebf5e8e18c` | 1094 | 2026-10-18 01:24 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `1bc9e83a-e243-4b45-ae31-7f5754bf9745` | 3137 | 2026-10-17 20:48 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `443ef722-9f2b-4130-89df-de61856b8974` | 535 | 2026-10-17 09:33 | `/Volumes/docker-ssd/ps/mini-ork` |
| `8162bb3f-d38b-40e7-b9bc-6076e3751710` | 1283 | 2026-10-17 07:11 | `/Volumes/docker-ssd/ps/mini-ork` |
| `da0b8c69-720a-4bb1-8f6f-ce2d80683760` | 1761 | 2026-10-17 07:10 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `732bb4cc-0c80-474c-82c1-f3c225843d09` | 403 | 2026-10-17 07:10 | `/Volumes/docker-ssd/ps/mini-ork` |
| `7cacb4ac-f665-458c-b382-cc7527561fdd` | 130 | 2026-10-16 15:43 | `/Volumes/docker-ssd/ps/zed-mini-ork` |
| `47ad145f-8af5-4bd8-be21-a1edf3d032ce` | 64 | 2026-10-12 19:45 | `/Volumes/docker-ssd/ps/zed-mini-ork` |

