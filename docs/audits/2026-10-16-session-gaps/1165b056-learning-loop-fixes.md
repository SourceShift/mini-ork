# Session gaps — `1165b056` (learning-loop fix program)

Session id `1165b056-ac2a-4687-8f6b-82aef09794b3`. Scope: the mini-ork **learning
loop** (context v1/v2 injection, `code_findings` harvest, IDE "Learning & memory
→ Your code" tab) and the four-family judge panel that verified the audit.

Verified against `origin/main` at `5b8d4a98` (fetched 2026-10-16 23:2x CEST) and
the engine worktree `/Volumes/docker-ssd/ps/mini-ork-worktrees/learning-fixes`
(branch `wt/learning-fixes`, base `2e8367f7` + kickoffs). Every "VERIFIED"
row below was checked with `git log origin/main --grep`, `git merge-base
--is-ancestor`, and a grep of the working tree — not with the recorded status.

Plan of record: `docs/plans/2026-10-15-learning-loop-fixes.md` (`9fb802fa` on
`origin/main`), Q1–Q9 decided by the panel's recommendation.

## (a) Task table

| id | description | recorded status | VERIFIED status | evidence |
|---|---|---|---|---|
| R1 | own-class themes first, member-class sets, honest lesson header | shipped (wave 1) | **SHIPPED** | `d405ef48` on origin/main (`git log origin/main --grep "LF-R1)"`) |
| R2 | emergent patterns scoped to the task class before ranking | shipped (wave 2a) | **SHIPPED** | `5b8d4a98`; migration renumbered `0075`→`0077` in review; `git merge-base --is-ancestor 5b8d4a98 origin/main` → true; gate `98 passed` |
| R3 | inject a lesson once (v1 and v2 both fire today) | pending | **NOT SHIPPED** | `mini_ork/cli/execute.py:3567` still `return pref_block + v2_block + block`; no commit greps for `LF-R3` |
| R4 | "Make it a rule" drafts a scoped learned rule | shipped (wave 1) | **SHIPPED** | `aaa87512`; its `0074_preference_origin.sql` was rewritten in review to the guarded-ALTER idiom (the run's version died on "duplicate column") |
| R5 | harvest only code problems, honest reasons/files/severities | shipped (wave 2a) | **SHIPPED** | `95a4af77`; gate `149 passed`; live-DB copy: `prune_receipts → 166`, `clear_artifact_files → 280`, both idempotent |
| R6 | one row per finding per run; prune verdict echoes | shipped (wave 1) | **SHIPPED** | `b2f5b224` |
| R7a | a judge panel fails when two judges are one model family | shipped (wave 1) | **SHIPPED** | `dc2d8793`; `mini_ork/verify/panel_heterogeneity.py` present on origin/main; `recipes/audit-judge-panel-4l` present |
| R7b | run profile records the *served* lane, not the alias | pending | **NOT SHIPPED** | no `LF-R7b` commit; no served-model write in `mini_ork/cli/main.py` |
| R8 | one kickoff-scope parser; Do-NOT reads sentences | running | **IN PROGRESS** | run `lf-r8-20261016-231736`, worktree `mini_ork-worktrees/lf-r8` (off `95a4af77`) |
| R9 | v2 scope falls back to the profile allow list | pending | **NOT SHIPPED** | `mini_ork/context_v2.py:261 scope_findings()` has the parent-dir fallback only |
| R10 | "recurring" = recurring across kickoffs | shipped (wave 2a) | **SHIPPED** | `fda51845`; gate `57 passed` |
| R11a | finding staleness at HEAD (age window → quoted-code) | pending | **NOT SHIPPED** | no `staleness` symbol in `mini_ork/learning/code_findings.py` |
| R11b | patch-id landing check against origin/main | pending | **NOT SHIPPED** | no `patch_id` symbol; no migration |
| R12 | findings remember which repo they are about | pending | **NOT SHIPPED** | no `target_repo` symbol in `mini_ork/learning/code_findings.py`; no `*target_repo*` migration |
| R13 | measurable arms + report-only retirement | pending | **NOT SHIPPED** | no `v2_legacy` / `effective` symbol in `mini_ork/cli/metrics_context.py` or `context_v2.py` |
| judge panel | 4-family panel verifies the audit against code + data | delivered | **DELIVERED** | run `learning-judge-4l-20261015125313`; `recipes/audit-judge-panel-4l` (workflow + 4 judge prompts + synthesizer + `verifiers/panel-completeness.py` + 11 tests) on origin/main |
| panel gate hardening | judge panels must not fail on a wrapped prose line or extra sidecar keys | delivered | **DELIVERED** | `13af18a4`; 2 regression tests; dry run on the live artifacts rc=0 |
| live-DB cleanup | re-run the harvester so history gets the cleaned parser | recorded as "after R5" | **NOT DONE** | R5 is merged, but `reharvest`/`prune`/`clear-artifact-files` have not been run against `$MINI_ORK_HOME/state.db` (deferred while R8 is live) |

## (b) Missing features & gaps

1. **R3 — the learning loop still injects the same lesson twice.** `execute.py:3567`
   returns `pref_block + v2_block + block`; `plan.py:_inject_context` runs the v1
   producers before the v2 decision, so a lesson can arrive as a preference
   block, a v2 theme *and* a v1 block, and the v2 UCB retrievals it records are
   phantom. This is the highest-value remaining item: R1/R2/R5 all narrowed what
   goes in, but nothing stops the same row going in twice.
2. **R12 — findings are repo-blind.** `code_findings` has no repo column and
   `scope_findings` matches a relative prefix, so `calc.py` findings from one
   target repo are offered to any repo with the same relative path (observed
   across `vt-smoke-engine-b` / `vt5-smoke` / `vt6-smoke`). Kickoff is written:
   `kickoffs/auto/lf-r12-target-repo.md`.
3. **R11a/R11b — no landing signal.** Nothing distinguishes a finding that was
   fixed from one that was never looked at; retirement decisions (R13) are
   therefore blind.
4. **R9 — scope is under-read.** 38 of 257 headed kickoffs parse to `[]` because
   `paths_in_text` accepts backticked tokens only; R8 fixes the parser, R9 must
   make v2 fall back to the profile's allow list.
5. **R7b — provenance.** Run profiles still record the lane *alias*, not the
   model that served it, so a panel's heterogeneity claim cannot be audited from
   the profile alone (R7a's checker reads `llm_calls` instead).
6. **NEW BUG (not in R1–R13): the post-commit reversion watchdog wedges every
   worktree merge.** `.githooks/post-commit:132-152` restores a file when its
   working-tree blob equals a **parent** commit's blob. `git rebase origin/main`
   checks out a base whose content for the just-committed files *is* that
   parent's content → the guard reads it as a revert, checks the file back in
   from HEAD, and the following `pick` aborts with *"Your local changes would be
   overwritten"*. Observed twice in this session: `lf-r5`
   (`.mini-ork/file-reversion-guard.log` `restored-reverted-to-2e8367f7…` for
   `context_v2.py`, `code_findings.py`, `test_code_findings.py`) and `lf-r2`
   (same, plus `reflect.py` restored 8× at 5 s intervals). Workaround used here:
   export `MO_REVERSION_GUARD_DISABLED=1` for the merge, and re-run after the
   60 s window. It should be fixed at the source (skip the guard while
   `.git/rebase-merge` or `.git/rebase-apply` exists).
7. **Live-DB cleanup not applied yet** (166 non-code rows, 280 artifact paths on
   a *copy* of the live DB; the live DB is untouched).
8. **No task existed for the panel recipe's own regressions.** The locked model
   was verified by re-running the live artifacts; there is no standing test that
   the *checked-in* judge prompts still produce lane-distinct panels.

## (c) Blocked on

1. **Primary checkout's local `main` has diverged from `origin/main`.**
   `git merge --ff-only origin/main` → *"Not possible to fast-forward"*. Per
   repo rule I did **not** merge, rebase or reset `main`; every fix in this
   session was pushed as `HEAD:main` from its worktree, so nothing is lost.
   **Update (peer session `verifier-verdict-failure-kind`, same day):** the
   divergence is a *patched-id duplicate* — local `351481b1` has the same
   `git patch-id --stable` as `8d8be650` already on `origin/main`, so
   `git pull --rebase` drops it as already-applied; the `--ff-only` refusal is
   the duplicate SHA, not new drift. Clearing it is that session's call, not
   mine.
2. **A second session was pushing to `origin/main` continuously** (origin moved
   `0abc587d → e0171807 → fda51845 → 5b8d4a98 → 40eec013` inside one merge
   attempt). Three consecutive `make worktree-merge` runs for R2 lost the push
   race; the merge landed via `fetch → rebase → push` in a tight loop. That
   session has since reported its pushing is finished, so the races should
   subside.
3. **The primary checkout has a parked merge** (`MERGE_HEAD` present, HEAD still
   `351481b1`, no git process running) with a conflict on
   `docs/audits/2026-10-16-session-gaps/AGGREGATE.md`. The peer who discovered
   it did not complete it and neither did I — completing or aborting another
   session's merge on a shared tree is not a call either of us should make
   unilaterally.
4. **Engine worktree is not rebased.** `wt/learning-fixes` serves the live runs
   (R8), and the repo rule is to rebase the engine only while no run is live, so
   the R8 run is executing engine code that predates R5/R2/R10.
5. **R9 and R7b are queued behind R8** because R8 owns `context_v2.py` and
   `cli/main.py` in the ownership registry (one live claim per file).

## Commands actually run (evidence provenance)

```
git log --oneline origin/main --grep "LF-R1)" --fixed-strings -1     → d405ef48
git log --oneline origin/main --grep "LF-R5)" --fixed-strings -1     → 95a4af77
git merge-base --is-ancestor 5b8d4a98 origin/main                    → true
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_pattern_task_scope.py \
  tests/unit/test_context_assembler_py.py tests/unit/test_inject_verified_only.py \
  tests/unit/test_migrate_py.py                                      → 98 passed in 101.99s
grep -n "return pref_block + v2_block + block" mini_ork/cli/execute.py → 3567:  (R3 open)
tail .mini-ork/file-reversion-guard.log                              → restored-reverted-to-2e8367f7… (watchdog)
```
