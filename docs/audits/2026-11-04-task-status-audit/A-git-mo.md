# A-git-mo — audit of 126 merge/push/worktree/branch/commit tasks (mini-ork)

Ground truth probes used throughout: `git -C /Volumes/docker-ssd/ps/mini-ork log --oneline --all --since=2026-09-20` (3822 commits), `git branch -a`, `git rev-list --count origin/main..wt/<slug>` per branch, `ls /Volumes/docker-ssd/ps/mini-ork-worktrees/` (128 entries), `git rev-parse HEAD origin/main` (both `ef55e7d8`).

## Summary

Of 126 rows: **92 DONE, 4 OPEN, 5 STALE, 25 UNKNOWN**. The merge/push machinery is broadly healthy and the *self-reported* statuses were overwhelmingly pessimistic or stale rather than dishonest: **90 of 126 rows claimed `pending`/`in_progress`/`failed` but the work had in fact landed**, while 4 rows claimed pending/in_progress are still genuinely unmerged branches sitting ahead of main in a live worktree. The genuinely-still-open set clusters into four things: (1) the benchmark_delta / L9 writeback (`785963d0`) is unmerged and would compute nothing if it were, because the benchmark tables have no producer (row 16) — declined by the 11-06 revive batch and the 2026-11-08 pre-push review, blocked on the missing candidate↔run link, not on the merge; (2) the RSI cycle's *product*: cycle worktrees `rsi-c1793812835-*`, `rsi-c1792959841-*`, `rsi-c1792964477-*` still hold unmerged fix commits; (3) the moot cycle-15 quarantine / evidence-worktree disposition, which has no decision record — the `git stash -u` guard that used to sit beside it closed on 2026-11-10 (`ca7e0f25`); (4) the Attestia work (archive vertical, live certificate e2e) has no code, branch, or worktree anywhere in either repo — it never started. Note the 25 UNKNOWNs are almost all one-off operational steps from 2026-10-07/08 sessions (verifier runs on one diff, timing gates, "confirm CI green") where the transcript, not git, was the only record.

Three clusters the audit listed as open have since closed, all verified 2026-11-10: the RSI Tier-1 wave-3 trio is on `main` (`56c6dacf`+`79c24ff7`+`4dbf8dbd`+`a9c7b818`; `bcc98e10`+`1c018305`; `10b73fd6`+`b232eae3`); the RRL plan docs landed as `92681b27` — the same 238-line diff as the never-merged `314714f5`, re-committed — with `dae784f8` and `a8d14b49` behind them, and the RRL build half as `a8ab7d79`+`ad4f23f9`; and the `e8b1c300` salvage is a *regression* of main, not missing work (row `rrl-build`).

Two structural notes, both corrected on 2026-11-09. **`config/rsi-cycle.yaml` being untracked is deliberate, not a missing commit**: it is gitignored as per-machine truth (`.gitignore:35-39`, "one `git add -A` away from being committed by accident"), and `config/rsi-cycle.example.yaml` is committed and is what the header tells you to copy. And the commits this audit read as dangling on audit day have since resolved: the wave-3 certificate landed as `56c6dacf` + `79c24ff7` (not `4ff3a0aa` alone) and `77517b50` was superseded by main's `398889bc`. `785963d0` (the benchmark-delta writeback) is still unmerged, and it would compute nothing if it were — its source tables have no called producer (row 16) — so `8a6cc25e` on `main` makes L9 refuse that hollow closure.

## Table

| task_id | ts | claimed | verdict | evidence |
|---|---|---|---|---|
| verifiers-pin-ceiling | 2026-11-04 | pending | DONE | The change is on `main` as `d16aa83d` (same subject, same five files): `pyproject.toml` `verifiers<0.4.1`, `engine._have_verifiers` asks for `make_runtime`, `ci.yml` ceiling entry is `verifiers==0.4.0`, `THIRD_PARTY.md` records the 0.4.1 removal. `git diff d16aa83d f0d764db` over those files is empty — the dangling `f0d764db` is a duplicate of the merged commit, not pending work |
| merge-l9-child | 2026-11-04 | in_progress | OPEN | `785963d0`'s writeback (`record_benchmark_delta`) is unmerged — `git merge-base --is-ancestor 785963d0 origin/main` is false and the function is absent from `main` — and it is inert even so: it averages `benchmark_results` joined to `benchmark_tasks`, and the only writers of those tables (`benchmark_suite.add()` / `.run()`) are called by nothing, so the AVG is NULL and the UPDATE is a no-op. Declined by the 11-06 revive batch and again by the 2026-11-08 pre-push review; blocked on the missing candidate↔run link, not on the merge. `8a6cc25e` (on `main`) makes L9 refuse that hollow closure; review verdict `warn`, with one open nit: the `producer_missing` `evidence_key` names `benchmark_results` even when only `benchmark_tasks` lacks a producer. |
| rsi-cycle-plan | 2026-11-03 | pending | DONE | C1a–C5 landed: `83bb253a` candidate/ledger, `382bd86f` ranked queue, `df13833b` probes, `7321323d` one cycle end-to-end, `81b3d950` learn+collapse, `785e91f7` one config + doctor, `1a5b6075` eleven merge gates (all in main) |
| verif-wiring-merge | 2026-11-02 | in_progress | DONE | instruments landed on main `24e5bb8c feat(instruments): producers for oracle integrity and verifier mutation` + `0d6202e1`; no `wt/verif-wiring` branch remains |
| verif-wiring-final-smoke | 2026-11-02 | pending | UNKNOWN | grepped log for a post-merge smoke receipt; none. Commits `24e5bb8c`/`0d6202e1` are the code, not a smoke record |
| verif-wiring-clean | 2026-11-02 | pending | DONE | no `wt/verif-wiring` branch (`git branch -a`), worktree dir absent, and `.claims` holds only `publisher-optional-outputs` |
| rrl-docs | 2026-10-29 | pending | DONE | merged 2026-11-08: `92681b27` (research synthesis + build plan), `dae784f8` (what the loop actually built), `a8d14b49` (the RRL kickoffs the plan points at) — `docs/plans/` and `docs/research/` both on `main` |
| b3-merges | 2026-10-29 | pending | UNKNOWN | no `wt/*b3*` branch, no commit naming "four approved branches"; grepped log for b3/approved-branch |
| rrl-build | 2026-10-29 | pending | DONE — salvage dropped | both halves are on `main`: `a47b9fbf` (`research.first` builds the queue from techniques) and the benchmark half as `a8ab7d79` + `ad4f23f9`. The salvage `e8b1c300` must **not** be merged: it is an older revision of the same three files — its `benchmarks.py` has no `surface_key`/`CACHE_VERSION 2` and passes `import_root=root, hidden=False` where main passes `repo=/python=/root=/timeout=`, and its only 28 non-context lines are pre-`ad4f23f9` shapes. Merging it would regress the content-addressed cache and the resolved-path handling. Disposition: drop. |
| t0f-installer | 2026-10-29 | pending | DONE | `wt/t0f-installer` is an ancestor of origin/main; carries `90e08e75 fix(install): run system deps under bash, probe python3.13 first…` |
| t1-certificate | 2026-10-29 | pending | DONE `56c6dacf` + `79c24ff7` + `4dbf8dbd` + `a9c7b818` | `56c6dacf` is the certificate + alpha ledger (migration 0089, renumbered from 0086), `79c24ff7` the review's P1 wins (unreachable status, required_gains/max_e, provenance columns, append-only triggers, `reason_class` + migration 0090, `_begin_immediate`). `4dbf8dbd` adds the arm-time pre-flight (refuses before any probe spend when the frozen probe set cannot reach the bar) + the golden flag-off pin, the rationale-carried certificate line and the two-connection slot test; `a9c7b818` fixes the review block it raised (an `infra` label may no longer overwrite a measured quarantine) |
| t1-kernel-hole-3 | 2026-10-29 | pending | DONE `bcc98e10` + `1c018305` | the freeze landed as `bcc98e10`; the 2026-10-29 review's three required fixes landed as `1c018305` (freeze `triage/failures.py`, verifier inline copy = engine floor with a real parser, candidates whose whole fix surface is frozen are MONITOR_ONLY) |
| t1-collapse-floor-breaker | 2026-10-29 | pending | DONE `10b73fd6` + `b232eae3` | merged; the review's fixes are in `b232eae3` (shadow is the shipped default, tri-state floor signal, latch keyed on `task_class` in `collapse_floor_latch`, armed/shadow/off modes). 13 floor tests + 81-gate |
| commit-attestia-archive | 2026-10-28 | pending | OPEN | no `archive` subcommand in `mini_ork/cli/`, no `attestia` branch or worktree in either repo; "attestia" appears only as prose in docs/positioning |
| bif-f2 | 2026-10-27 | in_progress | DONE | `wt/bif-f2` is an ancestor of origin/main; salvage `89daa389 … bif-f2-precheck-and-failed-checks` not needed — branch merged |
| revise-non-verdict-red-on-main | 2026-10-27 | pending | DONE | `f619e749 fix(ci): clear the five red jobs on main` explicitly repairs `test_revise_non_verdict: asserted <run>/revise does not exist…` |
| rsi-cycle-publish | 2026-10-27 | in_progress | DONE `12e85c3f` + `3dd76201` | `c0b443e3` cherry-picked as `12e85c3f` (the `audit_live_corpus` backfill pass, wired into reflect) and pinned by `3dd76201` (four tests: missing DB, `limit=0`, recorded-once + idempotent re-run, judge outage). `77517b50` was **superseded**: main's `398889bc` already records `verifier_results.confidence` from the rubric axes, so the cycle output's verifier-emitted confidence would have regressed it. Follow-up the merge exposed: the cherry-pick wired the pass onto *every* reflect behind `enabled()` (default ON), so it re-judged the same head and re-billed a judge limit for nothing — `f9c089f8` makes it opt-in (`MO_LESSON_CONFLICT_AUDIT=1`), `0b8ecf0a` updates the technique-status row. Branch cleaned. |
| t1-probe-harvest-b | 2026-10-27 | in_progress | DONE | `3ec35d97 feat(probe-harvest): harvest git-oracle probes…` + `becd702d` + `93d9443d` + `c462c898` all in main; `wt/t1-probe-harvest-b` ahead=0 |
| t1-chain3 | 2026-10-27 | in_progress | DONE | probe-harvest a/b/c in main (above); `wt/rsi-wave3` ahead=0 |
| t1-chain2 | 2026-10-27 | in_progress | DONE | same probe cited above; `wt/rsi-wave2`/`wt/rsi-wave3` both ahead=0 |
| t1-chain1 | 2026-10-27 | in_progress | DONE | `09e53e69 feat(learning): MGM contrastive lane mining` + `21b10b4f fix(mgm): infra means the engine's non-learnable exits` in main |
| t1-wave2-merges | 2026-10-27 | in_progress | DONE | layer-routing (`efacad66`,`744fa61c`), MGM, node-reward (`aae3dd7f`) merged; collapse-floor breaker landed as `10b73fd6` + `b232eae3` — wave complete |
| t1-wave3-review | 2026-10-27 | in_progress | DONE | certificate gate merged (`56c6dacf` + `79c24ff7`); routing-coverage half had already landed |
| t1-wave2-gates | 2026-10-27 | in_progress | DONE | node-reward `aae3dd7f feat(learning): ground implementer reward in the attempt's verifier evidence` + `554a3c6e` in main; MGM `09e53e69` in main |
| rev6-inlock-review | 2026-10-26 | pending | DONE | `702123f9 fix(review): the pre-push LLM review never runs inside the merge lock` (2026-10-27) |
| quarantine-cleanup | 2026-10-26 | pending | OPEN | only the verb was fixed (`9f1a93a5 fix(rsi): quarantine release must drop the file claims`); no decision record for the 5 evidence worktrees, and review-*/t1-* worktrees remain |
| rev6-r2 | 2026-10-26 | pending | DONE | `f9a73858 feat(review): the pre-push code review runs again — Python gate, primary DB, patch-id cache` + `476e3648 … findings get a lifecycle — fixed, dismissed with a reason, or pre-existing` |
| review-kickoffs | 2026-10-26 | pending | DONE | all three landed: gaming scan `961fe0ef`, pre-push revival `f9a73858`, risk tiers `08229cad feat(merge): risk tiers from the change — sensitive paths need a person's approval` |
| live-e2e | 2026-10-25 | pending | OPEN | no Attestia certificate push code, branch, or worktree in mini-ork or zed-mini-ork; certificate sealing exists (`914559e1`) but no live e2e artifact |
| eco6-wave-running | 2026-10-25 | in_progress | DONE | tracked tests `tests/unit/test_eco6_k10_otel_live.py … k16_recursion_keys.py`; commits `d10a34a2` (K10), `b10821ea` (K14), `e9682838` (K16), `47de862d` (K15a) all in main |
| eco6-wave1-2a | 2026-10-25 | in_progress | DONE | tracked `test_eco6_k1…k7, k9`; `ac76a64a fix(cost-pause): the pause sentinel stops new dispatches` (K5) in main |
| installer-fixes | 2026-10-23 | pending | DONE | `90e08e75 fix(install): run system deps under bash, probe python3.13 first, keep the runtime pointer inside the venv` (lands 4 days after the task) |
| rbf-duration | 2026-10-20 | in_progress | DONE | `3da373d2 run: duration_ms is active time on every close path; kill frees the run's remote node (RB-33, RB-49)` |
| rbf-merge-queue-7 | 2026-10-20 | in_progress | DONE | `64275077` levels, `8d28c247` crash-classify, `cbc6088b` lane-preflight, `9a64101d` remote-selfedit, `469d017c` recover-placement — all in main |
| rbf-merges | 2026-10-20 | in_progress | DONE | same five commits as row above; `121ba1b2 docs: fix status for the 2026-10-20 run-bug audit (RB-01..RB-50)` records the outcome |
| rbf-test-isolation | 2026-10-20 | pending | DONE | `fcf07c68 tests: never write the operator's live state.db or spawn real runs (RB-27)` |
| rbf-merge-queue-2 | 2026-10-20 | in_progress | DONE | levels `64275077`, crash-classify `8d28c247`, remote-selfedit `9a64101d`, recover-placement `469d017c` all in main |
| rbf-merge-queue-1 | 2026-10-20 | in_progress | DONE | as above plus lane-preflight `cbc6088b` |
| rbf-merge-queue | 2026-10-20 | pending | DONE | as above (same five RB commits) |
| rbf-levels-framework-edit | 2026-10-20 | pending | DONE | `64275077 levels: framework-edit's test envelope decides executes; a bare pass:false is REFUTED (RB-02b, RB-36)` |
| primary-divergence | 2026-10-19 | pending | DONE | `git rev-parse HEAD origin/main` → both `ef55e7d8`; no divergence remains |
| release-cadence | 2026-10-18 | pending | DONE | `0264da4d release: single-source the version and make CI the release gate`, `.github/workflows/release.yml` verifies tag==package version, release `v0.10.0` (`b6802d4e`) shipped |
| release-checkpoint-recipe | 2026-10-18 | pending | STALE | no `recipes/release-checkpoint/`; superseded by the CI release gate `0264da4d` which does the checkpoint's job |
| post-merge-ff | 2026-10-17 | pending | DONE | local `main` == `origin/main` (`ef55e7d8`); no stale local main |
| guard-primary-stash-u | 2026-10-17 | pending | DONE | `ca7e0f25 fix(githooks): refuse a stash that captures untracked files` — `.githooks/reference-transaction` aborts a three-parent `refs/stash` at `prepared` (the `-u`/`--include-untracked`/`-a` signature), working tree untouched, `MO_ALLOW_UNTRACKED_STASH=1` to override; 4 new cases in `tests/unit/test_githooks_py.py` (block, plain-stash pass, escape-hatch pass, pre-existing untracked stash still pops) |
| dag-bg-click | 2026-10-17 | in_progress | DONE | `docs/audits/2026-10-16-session-gaps/6881f0ad-zed-ui-dag-click.md` → **shipped**; `e88610c48e Pin the UI crate at the DAG background-click change`; `crates/mini_ork_ui/src/graph.rs:1737` clears selection on background click |
| lf-r7b | 2026-10-17 | in_progress | DONE | `97b46ec8 feat(agents-config): warn when lens aliases collapse onto one model family; record the resolved lane map per run (R7b)` |
| lf-r8 | 2026-10-17 | in_progress | DONE | `c7594dcb fix(learning): one kickoff scope parser for paths, deny lists and Do-NOT rules (LF-R8)` |
| lf-r14 | 2026-10-17 | in_progress | DONE | `363b8562 fix(hooks): the file-reversion guard must not heal a rebase (LF-R14)` |
| wave-3a | 2026-10-17 | pending | DONE | `69469667` (R9) + `97b46ec8` (R7b) recorded as merged in `bbb297b3 docs(learning): record the wave-3a merges` |
| lf-r14-20261016-233245 | 2026-10-16 | in_progress | DONE | same as lf-r14: `363b8562` |
| lf-r8-20261016-231736 | 2026-10-16 | in_progress | DONE | same as lf-r8: `c7594dcb` |
| primary-ff-diverged | 2026-10-16 | pending | DONE | HEAD == origin/main (`ef55e7d8`) |
| p2-22-worktree-level2-spec-path | 2026-10-16 | pending | UNKNOWN | searched docs/architecture and docs/plans for a level-2 spec-path note for bare-home worktrees; no decision recorded |
| primary-main-refresh | 2026-10-16 | pending | DONE | `f7b45c84` is an ancestor of `ef55e7d8`; local main is current |
| fix-main-buddy-f841 | 2026-10-16 | pending | DONE | `_wall` is implemented and returns `str` at `mini_ork/ide_pages/run.py:1378` (no F841); `3bdef401 fix(ide): finish the run-buddy removal that left main red` |
| fix-main-buddy | 2026-10-16 | in_progress | DONE | `3bdef401`, `d3b52b38 ide(node): restore the node page after the run-buddy removal`, `fae2a7a5 One orchestrator per run, and the run buddy is gone` |
| pre-merge-base-full-run | 2026-10-16 | in_progress | UNKNOWN | only a 2026-10-19 session-goals line (`docs/audits/2026-10-19-session-goals/SESSION-GOALS.md:668`) lists it `in_progress`; no attribution doc for `2e8367f7` |
| refresh-local-main | 2026-10-15 | failed | DONE | dirty-tree blocker gone; HEAD == origin/main |
| hr-findings-confirm | 2026-10-15 | in_progress | DONE | all six `wt/hr-*` branches return `rev-list --count origin/main..` = 0 (hr-acp-review, hr-docs, hr-ide-review, hr-node-ckpt-refs, hr-reaper-review-status, hr-fix-isolate-home) |
| hr-findings | 2026-10-15 | pending | DONE | same probe: every hr worktree branch fully merged |
| ff-local-main | 2026-10-15 | failed | DONE | HEAD == origin/main (`ef55e7d8`) |
| review-part1-merge-gate | 2026-10-15 | pending | DONE | `08229cad feat(merge): risk tiers from the change — sensitive paths need a person's approval` |
| review-part2-node-park | 2026-10-15 | pending | DONE | `e27da337 execute: park a run for human node review (status reviewing, rc 9, no rollback)` + `b1afcaa2 fix(approvals): approving a parked node resumes at the waiting node` |
| sync-primary | 2026-10-11 | pending | DONE | HEAD == origin/main; the dirty todos overlap is gone (only untracked new files) |
| publish-c9e15858 | 2026-10-11 | pending | DONE | `c9e15858 fix(verifiers): resolve the target repo's package manager, not the host's` is an ancestor of origin/main |
| route-331527c | 2026-10-11 | in_progress | UNKNOWN | `git cat-file -t 331527c` → "Not a valid object name" in both mini-ork and zed-mini-ork; cannot trace ownership |
| orphan-relaunch | 2026-10-11 | in_progress | UNKNOWN | `docs/todos/2026-10-11-orphaned-run-census.md` exists but records no relaunch decision |
| land-rsi-e1 | 2026-10-11 | pending | DONE | `wt/land-e1` ahead=0 (fully merged into main) |
| laguna-lane-committed-config | 2026-10-11 | pending | DONE | `ab693ca2 fix(lanes): commit laguna_lens into the tracked registries + a drift guard` |
| t5-hygiene-commit | 2026-10-10 | pending | DONE | `git status --porcelain` shows no modified tracked files (only 6 untracked new files from later work) |
| t5-hygiene | 2026-10-10 | pending | DONE | same probe: primary tracked tree is clean |
| primary-dirt-12-files | 2026-10-10 | pending | DONE | no tracked modifications in `git status --porcelain`; the 12-file dirt is gone |
| land-23a6cedb | 2026-10-10 | pending | DONE | `git branch -a --contains 23a6cedb` lists `main` |
| hygiene-commit | 2026-10-10 | pending | DONE | 409 kickoff files tracked under `kickoffs/auto/` (405 on disk) — the bulk hygiene commit happened |
| push-zed-mini-ork-branch | 2026-10-10 | pending | DONE | `git -C /Volumes/docker-ssd/ps/zed-mini-ork status -sb` → `## mini-ork...fork/mini-ork` with no ahead/behind |
| revive-run-1791567564 | 2026-10-10 | pending | UNKNOWN | no salvage commit and no run dir for `1791567564` in `~/.mini-ork/runs` (7 dirs) |
| ambient-noauthpatch-failures | 2026-10-09 | pending | UNKNOWN | grepped log for ambient/no-authored-patch fixes; only `e3956bb2` (unrelated in-lock precheck). No decision record |
| askk2-round3 | 2026-10-08 | pending | UNKNOWN | no `wt/askk2*` / `ask-author-k2*` branch on disk; no salvage commit for that run |
| eval-judge-incoherent | 2026-10-08 | pending | UNKNOWN | `tests/unit/test_eval_judge.py` exercises axis parsing + float fallbacks, but no commit ties it to "score 0.0 despite high axes" |
| publisher-own-delta | 2026-10-08 | pending | DONE | `3b56dbe3 fix(publish): an in-place publish commits only the run's authored patch`; mechanism lives in `mini_ork/cli/publisher_authored_patch.py`, used at `publisher.py:419` |
| ide-orca-f2b-flow | 2026-10-08 | pending | DONE | `crates/mini_ork_ui/src/flow.rs` implements the Review hub (`hub: Option<Step>`, :171), legend (`:1322`) and stage chips (`stages`, :1346) |
| publisher-whole-file-commit | 2026-10-08 | pending | DONE | `resolve_authored_patch` abstains on empty outputs (`publisher.py:418-424`, `Abstain`); `e6531659 publisher: a delegating run's empty-outputs abstain is a no-commit pass` |
| hero-criteria-merge | 2026-10-08 | pending | DONE | `93f1c380 fix(cli,ide): a wrapped kickoff bullet stays one item; the run hero does not repeat the title` |
| researcher-revendor-pin | 2026-10-08 | pending | UNKNOWN | submodule pin bumps exist (`43e74cfa`, `f0200188`) but none names researcher-64/orca #14; the researcher repo is outside the readable set |
| wt-researcher-defects-merge | 2026-10-08 | pending | STALE | no `wt/researcher-defects` branch, no worktree, no merge commit; abandoned rather than landed |
| execute-py-auto-repair-gate | 2026-10-08 | pending | DONE | `mini_ork/cli/execute.py:1089` `if not run_id or context_env("MO_FAILURE_TRIAGE","") != "1"` + `daa740ca feat(execute): the triage hook stands down when auto-repair owns the trigger` |
| merge-triage-commits | 2026-10-08 | pending | STALE | same as wt-researcher-defects-merge: branch gone, nothing in main |
| ide-hero-criteria | 2026-10-08 | pending | DONE | same commit as hero-criteria-merge: `93f1c380` |
| needs-you-truth-live-proof | 2026-10-07 | pending | DONE | `ac8d6e16 fix(ide): 'needs you' tells the truth — one count, gates close when their worktree lands, no same-second spinner`; `tests/unit/test_needs_you_truth.py` |
| owns-implicit-scope | 2026-10-07 | pending | DONE | `scripts/mini_ork_worktree.py:_check_claims` allows `kickoffs/auto/{slug}.md` as an implicit claim ("its kickoff") |
| escalate-baseline-fail | 2026-10-07 | pending | UNKNOWN | searched log and docs for a recorded gate strategy for the pre-existing retry-notify failure; none |
| impl-redeliver-diff | 2026-10-07 | pending | UNKNOWN | one-off implementer recovery step; no commit or run-dir receipt found |
| files-abs-from-worktree | 2026-10-07 | pending | DONE | `tests/unit/test_files_abs_resolve.py` exists; kickoff `a0b99643 chore(ide): kickoff — files-abs-resolve (changed files resolve to a real path)` |
| eng-rules-tab-verify | 2026-10-07 | pending | UNKNOWN | framework-edit verifiers exist (`recipes/framework-edit/verifiers/*.py`) but no receipt for this specific run on a diff |
| eng-your-code-tab-r2 | 2026-10-07 | in_progress | DONE | `mini_ork/ide_pages/learn/code.py:93-99` carries the round-2 fixes ("ONE set of findings, not two that happen to agree (reviewer, round 2)") |
| implementer-lane-repair-channel | 2026-10-07 | pending | DONE | `7cbdf4bc feat(recovery): lane repair follows the resumed run to the end; a refused /recover closes the card instead of waiting` |
| fix-steering-test-order-dep | 2026-10-07 | pending | DONE | `27860f60 test(learning): steering fake is bypassed once the real module is imported — patch the package attribute too`; test at `tests/unit/test_learned_record.py:305-316` |
| static-check-verifier | 2026-10-07 | pending | DONE | `recipes/framework-edit/verifiers/static-check.py` (35 KB) exists and is wired in `workflow.yaml` |
| test-verifier | 2026-10-07 | pending | DONE | `recipes/framework-edit/verifiers/test.py` (34 KB) exists and is wired in `workflow.yaml` |
| test_verifier | 2026-10-07 | pending | UNKNOWN | one-off "kickoff pytest gate + ruff + live-backup proof" on a diff; no receipt |
| verify | 2026-10-07 | pending | UNKNOWN | one-off three-file pytest+ruff+live read-only proof; no receipt |
| lane-repair-b-recover-lane-flag | 2026-10-07 | pending | DONE | `mini_ork/recovery/planner.py:143,191` document `[--lane <alias>=<lane>]`; parser at `:676-730` |
| scope-allow-test-file | 2026-10-07 | in_progress | UNKNOWN | `mini_ork/kickoff_scope.py` scope_allow builder exists; no receipt that `tests/unit/test_ide_pages_learn.py` was widened |
| verifier-round | 2026-10-07 | pending | UNKNOWN | one-off static-check/test/reviewer round on a regenerated diff; no receipt |
| scope-test-conflict | 2026-10-07 | pending | UNKNOWN | `tests/unit/test_ide_pages_learn.py` exists but no record of the scope/lessons-tab conflict resolution |
| triage-ci-verify | 2026-10-07 | in_progress | UNKNOWN | 2026-10-09 `f3901046 docs(triage): CORRECT the kickoff triage — no status divergence` discusses triage, not CI status for those pushes |
| fix-iso-to-epoch | 2026-10-07 | pending | DONE | `mini_ork/acp/fleet.py:130` `_iso_to_epoch` accepts int/ISO, returns None on junk, assumes UTC for naive (`dt.replace(tzinfo=timezone.utc)`) |
| harden-order-test | 2026-10-07 | pending | UNKNOWN | no test found seeding the real `decided_at` format with an epoch-granularity ordering assertion |
| scope-declare-test-file | 2026-10-07 | pending | UNKNOWN | no receipt that the scope declaration was added before an implementer run |
| impl-revision | 2026-10-07 | pending | DONE | `mini_ork/ide_pages/_lanes.py` resolves recipe dir + workflow.yaml home-aware (`recipe_dir(home, name)`: "project overlay wins over engine") |
| execute-hook | 2026-10-07 | pending | DONE | `mini_ork/cli/execute.py:1081-1106` gates triage on `MO_FAILURE_TRIAGE=1`; `13772eeb feat(triage): wire the failure triage into dispatch and the run fail branch` |
| fix1-cold-guard | 2026-10-07 | pending | DONE | `reflection_pipeline.py:1262-1278` probes `lesson_text`, falls back to `base_select`, and fails closed on a missing table; `e8c1fb0e` |
| fix2-stale-error | 2026-10-07 | pending | DONE | `pattern_induction.py:123-129 _reset_dispatch_error()` under `_dispatch_error_lock`, called per cluster at `:685`; `e8c1fb0e` |
| fix3-comment | 2026-10-07 | pending | DONE | `pattern_induction.py:88-101` comment now explains the redirect-vs-lock hazard accurately (and `:439-446` docstring); `e8c1fb0e` |
| reflect-r5 | 2026-10-07 | in_progress | STALE | a 2026-10-07 "still running" claim is obsolete: `~/.mini-ork/runs` holds 7 dirs, none matching `tr-reflect-1791367724` |
| impl-regen-diff | 2026-10-07 | pending | UNKNOWN | one-off implementer regeneration step; no receipt |
| framework-edit-verifier | 2026-10-07 | pending | DONE | `recipes/framework-edit/verifiers/_verdict_merge.py` + `verdict.json` referenced in `artifact_contract.yaml`/`workflow.yaml`/`static-check.py` |
| emergent-no-lesson | 2026-10-07 | pending | DONE | `reflection_pipeline.py:1245` `MO_EMERGENT_VERIFY_REQUIRE_LESSON` defaults to 1; rows without a lesson stay `proposed`; `e8c1fb0e` |
| r4-failing-test | 2026-10-07 | pending | STALE | the stat-skip window was removed by r5: `tests/unit/test_ide_pages_search.py:145-149` says "r4 used `_stat_mtime_for` … r5 uses the SQL candidate filter instead" |
| r4-timing-gate | 2026-10-07 | pending | UNKNOWN | no timing measurement recorded for the researcher home (cold/warm) anywhere in docs |
| user-merge | 2026-10-07 | pending | DONE | `2c808e46 feat(ide): run Kickoff tab + Threads search and paging over every run` is an ancestor of origin/main; `wt/ide-kickoff-search` no longer exists |
| user-timing | 2026-10-07 | pending | UNKNOWN | the user-run cold/warm timing gate (<2 s) has no recorded result |
| r6-fix3-dead-ts-branch | 2026-10-07 | pending | DONE | `raw_ts` appears nowhere in `mini_ork/` or `tests/` (grep -l returns nothing) |
| r6-fix1-log-proxy-gate | 2026-10-07 | pending | DONE | `mini_ork/ide_pages/node.py:1065-1072` — "r6 fix #1 — the log-line proxy only fires when the stream is genuinely …" `if session_path is None and log_path is not None` |
| r6-fix2-telemetry-pad | 2026-10-07 | pending | DONE | `mini_ork/ide_pages/node.py:238` `if cand.stat().st_mtime < node.start - 5: continue` (pad is −5) |

## Still genuinely open (verdict = OPEN)

1. ~~**verifiers-pin-ceiling**~~ — **DONE**, not pending: the change landed as `d16aa83d` on `main`; `f0d764db` is an identical dangling duplicate (verified: `git diff d16aa83d f0d764db` over the five touched files is empty). Nothing to merge, no TTY approval outstanding.
2. **merge-l9-child** — not a merge problem. The writeback (`785963d0`) is unmerged *and* inert: `benchmark_suite.add()` / `.run()` are the only writers of `benchmark_tasks` / `benchmark_results` and nothing calls either, so `record_benchmark_delta`'s AVG would be NULL and the column never moves. Declined by the 11-06 revive batch (`docs/audits/2026-11-06-revive-batch.md`) and by the 2026-11-08 pre-push review. The relaxation that closed L9 on "a called writer" (`ef55e7d8`) was itself the hole; `8a6cc25e` requires the delta writer *and* both benchmark producers to be wired, and reports `producer_missing` naming the half that is absent. Next: build the candidate↔run link, then a real producer.
3. ~~**rrl-docs**~~ — **DONE 2026-11-08**: merged as `92681b27` + `dae784f8` + `a8d14b49` (research synthesis, build plan, the RRL kickoffs). `wt/rrl-plan` was cleaned. Note the two docs are a *plan*, not the built loop: the Status table in the plan names the per-step commits.
4. ~~**rrl-build**~~ — **DONE, salvage dropped**: `research.first` landed (`a47b9fbf`) and the benchmark half landed independently as `a8ab7d79` + `ad4f23f9`. `e8b1c300` is an older revision of the same three files (no `surface_key` cache, pre-resolved-path call shape) — merging it would regress `main`. Nothing left to land.
5. ~~**t1-certificate**~~ — **DONE `56c6dacf` + `79c24ff7` + `4dbf8dbd` + `a9c7b818`**: the follow-up is closed — the arm-time pre-flight (Win 2) and the suite pins (Win 5) landed in `4dbf8dbd`, and `a9c7b818` fixes the pre-push review's evidence-overwrite finding. At shipped settings the pre-flight refuses code-fix (2 non-anchor probes vs a 7-gain bar), which is the honest answer: the lever is more probes or a lower bar, not `MO_APPLY_PROBE_MAX_TASKS`.
6. ~~**t1-kernel-hole-3**~~ — **DONE `bcc98e10` + `1c018305`** (freeze + the review's three fixes; floor now 23 entries). Nothing left.
7. ~~**t1-collapse-floor-breaker**~~ — **DONE `10b73fd6` + `b232eae3`**: merged in shadow mode with a `task_class`-keyed latch. Nothing left.
8. ~~**t1-wave2-merges**~~ — **DONE**: the collapse-floor half landed in `10b73fd6` + `b232eae3`.
9. ~~**t1-wave3-review**~~ — **DONE**: the certificate half landed in `56c6dacf` + `79c24ff7`.
10. ~~**rsi-cycle-publish**~~ — **DONE `12e85c3f` + `3dd76201`**: `c0b443e3` landed as `12e85c3f` with tests; `77517b50` was superseded by main's `398889bc` (rubric-derived confidence). Nothing left.
11. **commit-attestia-archive** — no archive vertical exists in the code at all. Next: either scope it explicitly (a `mini-ork archive` command) or close it as won't-do.
12. **live-e2e** — no Attestia endpoint or certificate-push code. Same next action as #11.
13. **quarantine-cleanup** — `quarantine release` was fixed (`9f1a93a5`) but nobody decided the five evidence worktrees. Next: record the disposition in `docs/decisions/` and remove the survivors.
14. ~~**guard-primary-stash-u**~~ — **DONE `ca7e0f25`**: `.githooks/reference-transaction` refuses a `refs/stash` commit with three parents (the `-u`/`-a` signature) at `prepared`, which aborts the whole stash — nothing leaves the working tree. Only creation is guarded; `pop`/`drop` never route through the hook, so existing stacks still apply.

## Notes on method / limits

- Every DONE cites a commit sha that `git merge-base --is-ancestor` confirms is on `origin/main`, or a file path that currently contains the change.
- For merge-queue tasks (rbf-merge-queue*, hr-findings*, t1-chain*) one probe settles the whole family; those rows share a citation and say so.
- 25 UNKNOWNs are dominated by 2026-10-07/08 per-run verifier and timing steps whose only record was the emitting session's transcript.
- Read-only throughout: no writes to either repo, no commits, no merges.
