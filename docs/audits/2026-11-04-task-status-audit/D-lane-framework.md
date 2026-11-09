# D-lane-framework — audit of 97 reported tasks

Window: 2026-10-07 → 2026-11-04. Repos read: `/Volumes/docker-ssd/ps/mini-ork`
(main, 1135 commits in window) and `/Volumes/docker-ssd/ps/zed-mini-ork`
(179 commits in window). Read-only; nothing modified.

## Summary

Of the 97 rows, **54 are DONE**, **17 are OPEN**, **2 are STALE**, and **24 are
UNKNOWN** (`panel-health-hackability` moved OPEN → DONE on 2026-11-09; see
`SUMMARY.md`'s post-audit re-probe). The framework/lane half of this backlog is in much better shape than
its self-reported status implies: lane affinity, the mock provider, the RBF
verifier envelopes (RB-02a/b, RB-12, RB-15, RB-21), the SDD ratification CLI,
the RSI recipes (`rsi-cycle` / `rsi-critic`), the certify tamper-class fix, the
judge-panel sidecar unification, and the artifact-completion contracts all
shipped with a commit to prove it. What genuinely still needs doing clusters in
four places: (1) **the failure_kind emitter sweep** — three separate rows
(`failure-kind-sweep`, `p0-4d-emitter-sweep`, `emitter-sweep-29`) all point at
one unbuilt shared writer; only 4 verifier files carry `failure_kind` today and
the project's own audit note says "the verifier-provenance plan is done; the
emitter sweep is not"; (2) **the SDD epic delivery** — `sdd-epic-run`,
`sdd-epic-launch`, `sdd-engine-run`, `b2-preflight` and `g4-author-fences` are
one blocked workstream: the roadmap kickoffs exist but I2/I4/I6/I8 were never
dispatched, `board preflight` does not exist, and 4 of the 5 acceptance-criteria
specs still have no executable fence; (3) **cloud sandbox providers** — Modal is
built but never live-verified, Vercel and Kubernetes were only researched
(`cp-modal/cp-vercel/cp-k8s` + their three siblings all duplicate this); (4) a
small tail of one-off defects (`va2-libwit-repair-loop`,
`pm-stage-2`). The 24 UNKNOWN rows are mostly from the Zed-fork sessions where
the artifact is a manual operator action or a per-commit CI verdict that leaves
no in-repo trace.

## Table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| verifiers-pin-fix | 2026-11-04T15:19 | in_progress | STALE | superseded: `pyproject.toml:43` declares `verifiers>=0.2.0,<0.4.1`, and `.github/workflows/ci.yml:211-235` (job `verifiers-pin`) tests **both** ends as exact pins (`verifiers==0.2.0` on py3.11, `verifiers==0.4.0` on py3.12) with the reason in a comment: a range re-resolves, so `>=0.4,<0.5` silently became 0.4.1 while `THIRD_PARTY.md` recorded a check against 0.4.0 |
| verif-wiring-run | 2026-11-02T13:47 | in_progress | DONE | `24e5bb8c` "feat(instruments): producers for oracle integrity and verifier mutation, shadow for judge validity" |
| readme-certify-ship | 2026-10-29T11:45 | pending | OPEN | last translation regen `e5406b67` (2026-10-25); English README modified later by `594eab86` (2026-11-02) → translations stale |
| rrl-design | 2026-10-29T08:46 | pending | DONE | `docs/research/2026-10-29-agentic-language-design-v0.md` (Attest v0 design) + `recipes/agentic-language-pains/kickoff.md` exist (both untracked on disk) |
| t1-dependents | 2026-10-27T10:45 | pending | OPEN | probe-harvest `3ec35d97`, layer-routing `efacad66`, suite strengthening `564ce92f`, certify gate `d526ca21` shipped; **no prompt-bloat guard commit** |
| bif-b3 | 2026-10-27T09:42 | in_progress | DONE | `f6b762e0` "feat(dispatch): lane affinity — a node's later rounds start on the lane that served it" |
| bif-b6 | 2026-10-27T09:42 | in_progress | DONE | `f8dad712` "feat(dispatch): a `mock` provider kind with rule files"; `mini_ork/dispatch/providers.py:880 _build_mock`, `:2611 'mock': MockEngine()` |
| bifrost-kickoffs | 2026-10-26T12:16 | pending | DONE | `kickoffs/auto/bif-{b1,b1b,b2,b3,b6,e1,f1,f2,g1,r2e,r4b,r5c,s1,s2,v1}*.md` all present |
| rsi-step-4 | 2026-10-25T15:40 | pending | DONE | `recipes/rsi-cycle/` — workflow.yaml + 3 lens prompts + synthesize.md + kickoff writer in `mini_ork/rsi/select.py` (4 lenses reduced to 3 by documented decision in workflow.yaml) |
| rsi-step-5 | 2026-10-25T15:40 | pending | DONE | `recipes/rsi-critic/workflow.yaml`: `critic_glm` + `critic_minimax` + `verdict_merge` + `panel_composition` |
| eco6-designs | 2026-10-25T10:25 | in_progress | DONE | `kickoffs/auto/eco6-k8-routemoa-slice-stats.md`, `eco6-k11-post-publish-verify.md`, `eco6-k15a-…`, `eco6-k15b-…` |
| kickoff-template | 2026-10-22T17:55 | pending | UNKNOWN | searched whole repo for "outreach" — zero hits; `mini_ork/kickoff_lint.py:47` treats "Success criteria" as an Acceptance synonym, but no outreach template file exists |
| rbf-merge-queue-3 | 2026-10-20T17:02 | in_progress | DONE | verify-partial `ea0c69c2` (RB-15), timeout-wallclock `be27fb4d` (RB-21), scope-respected `9ce327e0` (RB-12) |
| rbf-verifier-envelope | 2026-10-20T13:58 | in_progress | DONE | `673daa53` "verify: a verifier's own pass:false fails its node (RB-02a)"; follow-up `445f5f91` |
| vt11 | 2026-10-19T06:09 | in_progress | DONE | `0537d16b` "gates: score verifiers by mutation discrimination; certify the kill rate"; `mini_ork/gates/verifier_mutation.py` |
| primary-only-cwd-test | 2026-10-18T19:40 | pending | UNKNOWN | test still at `tests/test_dispatch_cwd_py.py:72`; related `41c20d78` scrubs MO_TARGET_CWD; did not run pytest (read-only) so cannot confirm it now passes in the primary |
| thread-cards-revised-spec | 2026-10-18T18:59 | pending | DONE | `docs/specs/2026-10-18-thread-cards-design.md` "Status: revised after the three-lane judge panel… Panel corrections (binding)"; `f06b8d3719` |
| rp-c23-revive | 2026-10-18T18:10 | failed | UNKNOWN | no `rp-c23` kickoff in either repo; `86a10179` "docs(audit): record C23 landed and close the engine half" is the only trace |
| levels-choice | 2026-10-17T09:34 | in_progress | UNKNOWN | `docs/architecture/verification-stack.md` documents the level vector for **code_fix** recipes (row ④); no statement of how **framework-edit** levels should read |
| c4c5 | 2026-10-17T09:32 | in_progress | DONE | `3e05a64f` "feat(inbox): one attention surface, derived from the stores that already know" |
| g4-author-fences | 2026-10-17T07:10 | in_progress | OPEN | 5 specs carry acceptance criteria; only `crate/pick-files-directories.md` has a ```bash fence — `engine/{board-preflight,codefix-classifier-coverage,specdir-kickoff-precedence}.md` and `engine-phase2/dispatch-preflight-sheet.md` have 0 |
| failure-kind-sweep | 2026-10-17T07:10 | completed | DONE | `04980796` + `d2902b6a` — nine shell-out verifiers now declare `failure_kind=toolchain` via the shared `mini_ork.verify.pm.run_logged`; suite verifiers use `classify_suite_spawn` (launcher messages only, first 20 log lines) so a failing test that prints an ENOENT cannot be excused, and rc 126/127 and signal-killed runners abstain. The abstention is per-probe (`feature-acceptance`) and per-check (`static-check`), so one real failure still refutes. The other ~20 verifiers shell out to a local harness script, where "command not found" is not a reachable state — deliberately not swept |
| spec-test-tamper-class | 2026-10-17T01:06 | pending | DONE | `c259100a` "fix(certify): a defective test is not evidence against the patch"; `mini_ork/certify/probe.py:27` |
| p0-4d-emitter-sweep | 2026-10-16T21:46 | completed | DONE | the shared piece exists now: `mini_ork.verify.pm.run_logged(argv, evidence, stream=…)` returns `(rc, tail)`, and `classify_suite_spawn` turns the tail into the abstention (`04980796` + `d2902b6a`) |
| sdd-epic-run | 2026-10-16T21:25 | pending | OPEN | `kickoffs/sdd-mechanisms/DECISIONS.md` (2026-10-16): I2/I4/I6/I8 **MISSING**; `docs/specs` still lists no delivered epics |
| sdd-epic-launch | 2026-10-16T21:25 | blocked | OPEN | same; SESSION-GOALS:925-935 shows the go/no-go was requested 4× and never given |
| ci-bbd241d5 | 2026-10-16T14:18 | in_progress | UNKNOWN | commit `bbd241d5` exists (2026-10-16 "spec: the three ACP failures are fae2a7a5's stale arithmetic"); no in-repo CI verdict recorded |
| sdd-engine-run | 2026-10-16T11:58 | in_progress | OPEN | no `preflight` subcommand in `mini_ork/cli/board_cmd.py`; no specdir kickoff-precedence code in `mini_ork/specdir/`; classifier-coverage artifact absent |
| b2-preflight | 2026-10-16T09:50 | pending | OPEN | `mini_ork/cli/board_cmd.py` subcommand list has no `preflight`; only `board start … --dry-run` |
| sdd-ratification-gate | 2026-10-16T09:38 | pending | DONE | `564cf8d1` "feat(specs): `mini-ork specs ratify`, the operator half of the SDD ratification gate"; `mini_ork/cli/specs.py` |
| lf-r10 | 2026-10-15T20:21 | in_progress | DONE | `kickoffs/auto/lf-r10-recurring.md` (commit `2e8367f7`) |
| act-e-lock | 2026-10-15T19:24 | pending | UNKNOWN | no node-provider lock or dirty-engine surfacing found in `mini_ork/remote/providers/` or `mini_ork/node_agent/` |
| p1-dispatch | 2026-10-15T19:17 | pending | DONE | `board start --kickoff` in `mini_ork/cli/board_cmd.py`; `mini_ork/cli/kickoff_cmd.py`; orch `dispatch` tab in `_PAGE_TABS` |
| orch-tabs-walk | 2026-10-15T19:17 | pending | DONE | `tests/unit/test_ide_pages_actions.py:322` — `"orch": ("session","dispatch","decide","kickoffs","races","planner","coord","intake")` |
| k8s-agent-sandbox-provider | 2026-10-15T19:17 | pending | OPEN | `docs/architecture/cloud-sandbox-providers.md:330` — "Not built … Needs a cluster to verify" |
| vercel-sandbox-provider | 2026-10-15T19:17 | pending | OPEN | `docs/architecture/cloud-sandbox-providers.md:329` — "Not built … needs VERCEL_TOKEN/TEAM_ID/PROJECT_ID" |
| modal-live-verify | 2026-10-15T19:17 | pending | OPEN | `modal_sandbox.py` + 11 fake-SDK tests exist; doc:328 "live pending a Modal token" |
| wire-existing-be | 2026-10-15T16:31 | pending | UNKNOWN | searched for chips/history-do/fleet-acts/errors-callout builders; no single receipt ties the 6-part pass together |
| cn-ui-batch-2 | 2026-10-15T12:56 | pending | UNKNOWN | no `shipped-today` / `lane-decisions` / `verdict-outcome` builder in `mini_ork/ide_pages/` |
| learning-fixes-R1-R13 | 2026-10-15T12:00 | pending | DONE | `docs/plans/2026-10-15-learning-loop-fixes.md` (R1–R13 plan) + `kickoffs/auto/lf-r1…lf-r14*.md` |
| f3 | 2026-10-15T11:41 | pending | UNKNOWN | no F3 / blind-held-out-spec-test artifact or commit found |
| wave1-truth | 2026-10-15T10:26 | pending | DONE | S1 `554a3c6e` (provisional exit-code rewards, per-node grounding) + N1 `9b8c4d37` (judge artifacts record the served lane) |
| verifier-fixes | 2026-10-15T10:22 | pending | OPEN | SESSION-GOALS:620 still lists "Dispatch F1-F5 framework-edit verifier fixes" as pending; no F1–F5 kickoff set exists |
| cp-modal | 2026-10-15T10:19 | pending | OPEN | duplicate of modal-live-verify: built, live verification needs a token |
| cp-vercel | 2026-10-15T10:19 | pending | OPEN | duplicate of vercel-sandbox-provider; not built |
| cp-k8s | 2026-10-15T10:19 | pending | OPEN | duplicate of k8s-agent-sandbox-provider; not built |
| reverdict-older-false-escalations | 2026-10-15T09:43 | pending | DONE | both resolved: replay adapter `31770020` (go/cargo), api_contract wired `e1331def` |
| act-b-typecheck | 2026-10-14T11:19 | pending | DONE | `3abe8ebe` "test and typecheck verifiers say when their tool never started"; `64275077` levels envelope (RB-02b/RB-36) |
| plan-doc-phase1 | 2026-10-13T21:17 | pending | UNKNOWN | no phase-1 plan doc + kickoff found; zed `docs/specs/2026-10-16-run-initiation/engine-phase2/` has only a spec sheet |
| profile-verification-bug | 2026-10-13T02:07 | pending | DONE | `753ef861`, `aac2cfe3` "read fenced commands in a kickoff's Verification section", `ddc88c43` |
| l5b-panel-findings | 2026-10-12T19:38 | pending | UNKNOWN | "l5b" appears only as a SESSION-GOALS:466 pending line; no l5b code in either repo |
| sonnet-lane-missing-tmp-dir | 2026-10-12T14:21 | pending | UNKNOWN | no `/private/tmp` spawn guard in `mini_ork/dispatch/`; `mini_ork/workspaces.py:575` only resolves the `/tmp` symlink for the content-sha helper |
| not-a-lane-literal | 2026-10-12T14:21 | pending | STALE | `"not-a-lane"` exists only as intentional invalid-lane test input (`tests/test_dispatch_health_py.py:42,68,71`, `tests/test_dispatch_cli_py.py:156`); no config carries it |
| d6-verifier-hint | 2026-10-12T07:15 | pending | DONE | `mini_ork/recovery/retry_hint.py:467` — structured `missing`/`failures` are read into the hint |
| epic-scheduler-verdict | 2026-10-12T07:05 | pending | OPEN | SESSION-GOALS:440 still pending; no plan-stage child-failure verdict source in `mini_ork/scheduler.py` |
| emitter-sweep-29 | 2026-10-12T05:46 | completed | DONE | same job as failure-kind-sweep; landed as `04980796` + `d2902b6a` against the 9 verifiers that spawn a host tool (the shared helper is `run_logged`, not a copy-pasted block) |
| stale-epic-line-683 | 2026-10-11T19:45 | pending | UNKNOWN | `mini_ork/ide_pages/orch.py:680-690` now holds race-keep code; the original "stale epic panel-review line" cannot be located |
| n1-epic | 2026-10-11T19:45 | pending | DONE | `9b8c4d37` "feat(panels): judge artifacts record the lane that actually served each judge" |
| R4-verifiers | 2026-10-11T17:06 | pending | DONE | `recipes/framework-edit/verifiers/{static-check,test}.py:136-281 _materialise_submodules` |
| harness-tab | 2026-10-11T16:57 | in_progress | DONE | `22409e93` "Add a per-node Harness view to the board node inspector"; `d10aa34e` |
| ui-submodule-r4-archive | 2026-10-11T16:45 | pending | DONE | `docs/todos/20261011-1830-ui-submodule-build-requirements.md` "closed — all five requirements satisfied … R4 `882cc8cc`"; `_materialise_submodules` present |
| wu-7c3a91e2-n1 | 2026-10-11T15:43 | pending | DONE | `9b8c4d37` (served lane in panel artifacts) |
| wu-7c3a91e2-s1 | 2026-10-11T15:43 | pending | DONE | `554a3c6e` (verifier verdicts → trace rewards, provisional labelling) |
| stage-1-intake | 2026-10-11T15:23 | pending | DONE | `60c00154` "docs(audit): session gaps — work-intake delivery is shipped, not un-started"; `mini_ork/intake/{poll,webhooks,dispatch}.py` |
| skills-fix-epic | 2026-10-11T14:31 | pending | UNKNOWN | no skill-registry seam epic or Zed UI join found; nearest is `4252e299` "make the skill registry a real grant pipeline" |
| pm-stage-2 | 2026-10-11T13:53 | pending | OPEN | no `board_view.rs` and no `spec.py` in the zed fork; the board column/card contract does not exist |
| inbox-lineage | 2026-10-11T13:45 | pending | DONE | `5aae0796` "fix(inbox): group retries by lineage, count harness runs, and make rows actionable" |
| wu-7c3a91e2-d1 | 2026-10-11T13:34 | pending | DONE | `554a3c6e` (verdicts stamped into trace rewards at source) |
| t3-artifact-completion-contracts | 2026-10-11T12:26 | pending | DONE | `98a407cb` "feat(artifacts): per-kind completion contracts…"; `mini_ork/execute_compat.py:130 register_completion_contract`; `kickoffs/auto/artifact-completion-contracts.md` |
| user-verify-ui | 2026-10-11T07:07 | pending | UNKNOWN | manual operator relaunch/visual check; no receipt |
| dev-inspector-feature | 2026-10-11T06:14 | in_progress | DONE | zed `script/mini-ork-invariants.txt:20-22` enforces `inspector` / `dev-inspector` features in `crates/zed/Cargo.toml` |
| va2-libwit-repair-loop | 2026-10-10T20:35 | pending | OPEN | no libwit verifier → repair-loop wiring; only `recipes/frontier-llm-research/verifiers/collect-latest-libwit.py` |
| rec2-never-launched | 2026-10-10T20:34 | pending | DONE | `docs/todos/2026-10-10-never-launched-by-family.md` (confirmation by family) |
| wu-7c1e4a02 | 2026-10-10T07:38 | pending | UNKNOWN | several `audit(refactor-audit): publish synthesis from …` commits exist but none ties to this panel's run id |
| item-d-artifact-contracts | 2026-10-10T06:41 | pending | DONE | `98a407cb` + `kickoffs/auto/artifact-completion-contracts.md` |
| judge-json-schema-divergence | 2026-10-09T22:42 | pending | DONE | `recipes/audit-judge-panel/verifiers/panel-completeness.py:157` "structured sidecars (one shared schema across both lanes)" + validation |
| wu-4c1f8a02-adapters | 2026-10-09T16:49 | pending | DONE | `d605ce7f` "feat(heldout): jest/vitest grader adapter + high-water-mark incremental mining" |
| item-d | 2026-10-09T16:49 | pending | DONE | `98a407cb` (per-kind completion contracts) |
| kickoff-doc | 2026-10-09T16:44 | pending | UNKNOWN | `kickoffs/auto/ide-node-tab-dag.md` does not exist |
| desaturate-reward-g | 2026-10-09T09:56 | pending | UNKNOWN | `mini_ork/trace_store.py:30 compute_reward_g` exists, but no per-criterion verifier persistence found |
| p4-jump-status | 2026-10-08T18:26 | pending | UNKNOWN | no jump palette or lane status bar builder in `mini_ork/ide_pages/` |
| codex-lane-model | 2026-10-08T16:19 | pending | UNKNOWN | `config/providers.yaml:136-138` codex is `kind: codex-native` with no `model:` pin; `gpt-6-astra` appears only in a test simulating a 400 — no commit proves the fix |
| sdd-i5 | 2026-10-08T11:56 | in_progress | DONE | `kickoffs/sdd-mechanisms/DECISIONS.md`: "sdd-i5-evidence-ledger LANDED (flag-gated)"; `mini_ork/verify/evidence_ledger.py:35 FLAG = "MO_EVIDENCE_LEDGER"` |
| revise-feedback-error-lines | 2026-10-08T10:06 | pending | DONE | `df432ff6` "fix(execute): a revise round carries the verifier's real error lines (issue #13)" |
| fix-a-verifier | 2026-10-08T09:12 | pending | UNKNOWN | no A1–A4 artifact or commit found |
| fix-b-lane | 2026-10-08T09:12 | pending | UNKNOWN | no B5–B7 artifact or commit found |
| concord-p3-open | 2026-10-07T19:17 | pending | DONE | `docs/architecture/concord-protocol.md:275` P1d shell-write attribution, `:274` P3b intent blend, `:276` P4 arbitration (all "shipped"); `mini_ork/cli/cn_cmd.py:133 ack-overlap` |
| kickoff-guard | 2026-10-07T18:07 | in_progress | DONE | `mini_ork/verify/kickoff_guard.py` (`MO_KICKOFF_GUARD`, default `shadow` at :19); DECISIONS.md "KEEP SHADOW (recorded)" |
| sdd-i3 | 2026-10-07T17:58 | in_progress | DONE | DECISIONS.md "sdd-i3-kickoff-contract LANDED (flag-gated)"; `mini_ork/verify/kickoff_guard.py` |
| ctx-pack-relevance | 2026-10-07T15:06 | pending | DONE | `82acde38` "fix(context): pack-accuracy pass — … relevance floor, hotspot recency" |
| repo-status-staleness-fix | 2026-10-07T14:53 | pending | DONE | `mini_ork/web/repositories.py:644-667 derive_node_statuses` — "a revise round's new node_start retires the prior round's ended_at / duration_ms / verdict" |
| framework-edit-probe-set | 2026-10-07T13:38 | pending | DONE | `7be81f11` "probes: frozen framework-edit probe set; per-recipe probe budgets; arms run local" |
| lane-repair-plan | 2026-10-07T12:45 | pending | UNKNOWN | no lane-repair plan or kickoff set under `docs/plans/` or `kickoffs/` |
| fe-static-verifier-false-fail | 2026-10-07T12:43 | pending | UNKNOWN | no diff-apply-check / verdict.json key-order fix found in `recipes/framework-edit/verifiers/` |
| induce-lane | 2026-10-07T10:28 | in_progress | DONE | `docs/plans/2026-10-07-learning-memory-page-refactor.md:317` — "P0 induction lane · `induce-lane` · merged; lesson backfill running on glm" |
| api-contract-wiring | 2026-10-07T09:46 | pending | DONE | `e1331def` "feat(verifiers): wire the api_contract verifier into post-mvp-delivery" |
| panel-health-hackability | 2026-10-07T09:46 | pending | DONE `a5001821` | `_eval_panel_health` now passes only `panel_healthy` and returns `"defer"` for `indeterminate` (`mini_ork/gates/native_gates.py:215`). The seeded weakness that was the hackability positive control did **not** get a sibling canary: `oracle-liveness` / `oracle-stability` measure 0.0 too, so the control moved into the harness — `hackability.control()` borrows a live registry row, audits a gate whose evaluator always answers `"pass"`, and requires 1.0; `gate-fuzz --hackability` runs it every pass and warns (non-fatally) when it drops. Live check on a bootstrapped copy: panel-health 0.000 (was 0.200), liveness 0.000, control `oracle-coalition` 1.000 `ok=true`. Four unit tests pin the control, including the broken-operator-set case. |

## Still genuinely open

1. ~~**failure-kind-sweep** / **p0-4d-emitter-sweep** / **emitter-sweep-29**~~ —
   **done** (`04980796` + `d2902b6a`, 2026-11-08). Closed as nine verifiers, not 29: the
   sweep was scoped by reachability, and the remaining shell-out verifiers run
   a local harness script the repo ships, so "the tool never launched" is not a
   state they can be in. What was actually missing was not the emitter but the
   *evidence*: every one of the nine redirected its spawn's output to an
   evidence file and then classified on `""`, so the manager-refusal and
   `npx` cases (both exit 1) were invisible. `run_logged` returns that output.
2. **sdd-epic-run** / **sdd-epic-launch** — get the operator go/no-go, then
   `bin/mini-ork epics ingest kickoffs/sdd-mechanisms/roadmap.md` and dispatch
   I2/I4/I6/I8 (`prompts/probe-author.md`, `test_slice_to_fit.py`,
   `policies.schema.json`, `guarded_loop.py` all still absent).
3. **b2-preflight** — add a `board preflight <kickoff.md>` subcommand to
   `mini_ork/cli/board_cmd.py` emitting recipe + marker + top-3 scores + `resolved:bool`.
4. **sdd-engine-run** — deliver the three engine specs: board-preflight,
   codefix-classifier-coverage, specdir-kickoff-precedence.
5. **g4-author-fences** — add a ```bash executable fence to the 4 remaining
   acceptance-criteria specs under `docs/specs/2026-10-16-run-initiation/`.
6. **readme-certify-ship** — regenerate the 12 translations from the current
   English README (last regen 2026-10-25; English changed 2026-11-02).
7. **modal-live-verify** / **cp-modal** — run
   `scripts/smoke_sandbox_node.py --provider modal` with a real Modal token.
8. **vercel-sandbox-provider** / **cp-vercel** — build `providers/vercel_sandbox.py`
   (needs `VERCEL_TOKEN`/`TEAM_ID`/`PROJECT_ID`).
9. **k8s-agent-sandbox-provider** / **cp-k8s** — build the `Sandbox` CRD
   provider (needs a cluster).
10. ~~**panel-health-hackability**~~ — **DONE `a5001821`**. The sibling canaries
    the smoke kickoff named (`oracle-liveness` / `oracle-stability`) turned out
    to measure 0.0 already, so no oracle gate could inherit the control. It
    moved into the harness instead: `hackability.control()` audits a gate whose
    evaluator always answers `"pass"` and requires 1.0, and `gate-fuzz
    --hackability` runs it on every pass. The `"defer"` flip then shipped with
    the control intact.
11. **verifier-fixes** — author and dispatch the F1–F5 framework-edit verifier
    fix kickoffs.
12. **epic-scheduler-verdict** — add a plan-stage child-failure verdict source
    to `mini_ork/scheduler.py` that does not collide with retry-hint case 1.5.
13. **t1-dependents** — the only missing piece is the prompt-bloat guard.
14. **pm-stage-2** — the zed-fork board column/card contract
    (`spec.py` + `board_view.rs`) still does not exist.
15. **va2-libwit-repair-loop** — route `collect-latest-libwit.py` failures into
    the repair loop.
16. **not-a-lane-literal** (STALE, listed for closure) — nothing to do; the
    literal is intentional test input.
