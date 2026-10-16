# Session gaps — `86880f3c-c218-464f-a7f2-8aebf5e8e18c`

**Session identity.** This is the **zed-mini-ork (fork) session working on the mini-ork
engine**, not a mini-ork-UI session. Ask was answered by
`verifier-verdict-failure-kind`; no `/contextnest-memory` skill exists in this
session's skill list, so the ContextNest MCP tools were used directly
(`cn_retrieve` scoped to this session id, `cn_inbox`).

**Session's own objective (work_unit `wu-7c1d4a02`).** Monitor the failed
mini-ork runs of the last 48h in both homes (engine
`/Volumes/docker-ssd/ps/mini-ork/.mini-ork`, researcher
`/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`), revive each
from its failed node only, fix the engine when a code defect is the cause, and
report per home.

**Repos.** engine `/Volumes/docker-ssd/ps/mini-ork` (SourceShift/mini-ork),
fork `/Volumes/docker-ssd/ps/zed-mini-ork`, UI `/Volumes/docker-ssd/ps/mini-ork-ui`.

---

## (a) Task table

| id | description | recorded | VERIFIED | evidence (command / sha / file:line) |
|---|---|---|---|---|
| `revive-engine-home-3` | revive the three revivable engine-home runs | completed | **completed** | `run-verdict.json` `verdict=pass failed_nodes=0 levels_decision=publish` for `rp-e3-header-20261015-222115`, `rp-e3-header-v2-20261016-111144`, `rp-e6-graph-focus-20261015-222118`; each run's diff reverse-applies in its target worktree (`git -C <wt> apply --check --reverse <run>/framework-edit.diff` → OK ×3) |
| `revive-779` | revive researcher run `run-1792059337-779` from its failed node | failed | **failed** (owner-blocked, 2 attempts) | `state.db` row still `status=failed`; `retry-exhausted/test.attempts.json` mtime **10-15 13:00:09** (untouched by the 23:06 retry) while `verifier_typecheck.log` is fresh **23:06:06** rc=0; `diagnosis.json` `rule_id=R7 layer=environment routes_to=owner`; `retry-gate.json inbox_id=38` |
| `fix-mo-target-cwd` | give a recovered closure the run's target, not the operator's cwd | done | **landed + on origin/main** | `8f00468b` — `git merge-base --is-ancestor 8f00468b origin/main` → IN; covered by `tests/unit/test_recover_cli_dispatch.py:125-158` |
| `fix-target-scope` | every recovery gets the target, not just verifier-rooted ones | done | **landed + on origin/main** | `9ee820c1` — ancestor of `origin/main` (`63fc80a0`) |
| `fix-pytest-asyncio-dup-paths` | drop test paths a sibling glob already collects | done | **landed + on origin/main** | `16f11413` — ancestor of `origin/main` |
| `report-both-homes` | per-home report of failed / revived / left / code landed | completed | **completed** | delivered in-session (second firing report) |
| `gap-rubric-contamination` | rubric/panel score a revived run against the superseded attempt's markers | documented, not fixed | **fixed + on origin/main** | `37d39474` — a green run-level verdict retires the older markers; `python3 -m pytest tests/unit/test_rubric_prescreen_py.py tests/unit/test_rubric_scoring_py.py -q` → 28 passed (pre-fix those two tests fail) |
| `gap-hint-entry` | `recover --strategy verify` enters at the first verifier, which can be a SIBLING of the failed node | open (cause unread) | **fixed + on origin/main** | `d1c0c871` — the hint's node is the default verify entry; `tests/unit/test_recover_verify.py` §8 (2 behavioural + 2 guard tests) |
| `gap-revive-row-settlement` | a revived row keeps the first attempt's `ended_at` / `cost_usd` | open | **fixed + on origin/main** | `f43022ea` — a live status clears the end stamp; the recover tail reconciles cost to the ledger; `tests/unit/test_recover_verify.py` §9 + `test_set_status_parity` |
| `researcher-install-lag` | researcher install's in-tree mini-ork misses the target fix | open (`user_action later`, inbox `cn-0f1acdaf`) | **still open** | `git -C …/researcher/.mini-ork/mini_ork rev-parse --short HEAD` → `ddc88c43`; `grep -c _recovery_target_cwd` → **0** there vs **2** in the engine (`mini_ork/recovery/planner.py`) |
| `rotate-dead-lanes` | rotate/re-pin the kimi (and deepseek fallback) lanes | open | **still open** | `runs/run-1792136293-92791/retry-hint.json` → `needs_change.kind=lane`, "kimi: API key rejected (HTTP 401) at https://api.kimi.com/coding/v1/messages"; `execute.log` also emits `[dispatch_chain_drop] fallback tail lane 'deepseek_flash' … has no providers.yaml entry` |
| `leftover-cleanup-delta` | review the uncommitted leftover-cleanup delta (from m3a/m3b/m3d) | open | **moot** | `/Volumes/docker-ssd/ps/mini-ork-worktrees/miniork-leftover-cleanup` does not exist (`ls -d` → No such file or directory) |
| `foreign-file-run_status.py` | one untracked foreign file left untouched | note | **resolved by its author** | file now committed by `d70f94cf` ("board: a run the operator closes lands in a terminal not_needed state"), `git merge-base --is-ancestor d70f94cf origin/main` → IN |

Six engine-home runs were reclassified this session from "genuine target-repo
revision" to **not revivable**: `hr-exec-park-20261015141603`,
`sde-3b2-ledger-20261015-142338`, `sde-3b1-diagnoser-20261015-142335`,
`vg-3-spec-tests-20261015-142340`, `hr-cli-worktree-20261015135213`,
`run-1792091744-821` — all six fail the single check
`verifier-test:epic-verification-commands-pass` and every target is **deleted**
(`run_profile.json:roots.target` → `exists=NO`).

---

## (b) Missing features & gaps

1. **A revive does not clear the previous attempt's failure markers, and the
   rubric grader reads them as this attempt's work product.** Fix site:
   `mini_ork/gates/rubric_scoring.py:141` (`artifact_summary`, listing at
   `:166`) — it should either skip attempt-scoped markers
   (`NEEDS-CHANGE.md`, `rolled-back.json`, `retry-hint.json`,
   `diagnosis.json`, `retry-gate.json`) for the second and later attempts, or
   receive the attempt start time and ignore older artifacts; the sibling
   direction is to move superseded markers under `attempts/<n>/`.
   `mini_ork/gates/rubric_prescreen.py:570` is the call site.
   **FIXED `37d39474`** (P0#1 of `AGGREGATE.md`) — a green run-level verdict
   (`verdict.json` / `run-verdict.json`) retires the markers older than it and
   the omission is named in the summary instead of being silently dropped; four
   tests in `tests/unit/test_rubric_prescreen_py.py`.
2. **`recover` re-runs the closure root, and the hint's own node may never
   re-dispatch.** On 779 the hint said `from_node: test`, but the revive
   re-ran `typecheck` (fresh log, rc=0), left `test`'s artifacts at their
   previous mtimes, and the reviewer escalated on the stale `rc=77` record.
   Exit 0, a fresh node-cmd and a real LLM verdict — all genuine, all about a
   different node. **Candidate defect, not proven** (the mechanism by which
   `test` was skipped was not read out of the executor). Consequence: monitor
   "progress" by `retry-exhausted/<node>.attempts.json` mtime, never by
   dispatch activity.
   **CAUSE FOUND, FIXED `d1c0c871`** (P0#3 of `AGGREGATE.md`): the mechanism was
   the entry node, not the checkpoint ledger. `--strategy verify` resolves its
   entry as `--from-node` or the first verifier in topo order; the hint's own
   `command` never carries `--from-node` (`retry_hint.py`), so the CLI ignored
   the answer the hint held, picked `typecheck`, and the closure became
   `descendants(typecheck)` — which excludes `test`, its **sibling**. The hint's
   node is now the default verify entry (explicit `--from-node` still wins; a
   hint naming a node the workflow lacks is ignored with a warning). Four tests
   in `tests/unit/test_recover_verify.py` §8, two of them regression guards.
   The `node_completion_checkpoints` wart found on the way — an abstaining
   verifier (`exit 0`, `status: unverified`) is recorded as `succeeded`, so the
   P1.2 ledger claims a check that never ran — is **still open**, deliberately
   out of scope here.
3. **Observability: a revived run's row keeps the first attempt's `ended_at`
   and `cost_usd`** (779: `ended_at 2026-10-15 13:04:07`, `cost_usd
   6.725933` after two revives), so an `ended_at`-keyed monitor never sees a
   recovery and the second attempt's spend is invisible.
   **FIXED `f43022ea`** (P2#12 of `AGGREGATE.md`), one cause per half: a live
   status now clears the end stamp (`execute.set_status` — that write is the
   per-attempt boundary, and `COALESCE(ended_at, ?)` is a within-attempt rule,
   the same invariant `web.control._restore_status` already applies), and the
   recover tail reconciles `cost_usd` to the ledger beside the rubric refresh
   (`main._reconcile_run_cost` was the reconcile's only caller and lives in the
   lifecycle tail a recover never enters). Three tests in
   `tests/unit/test_recover_verify.py` §9 + the `set_status` parity assertion.
4. **Dead lanes fail late.** `run-1792136293-92791` spent a planner dispatch to
   discover a 401; the `deepseek_flash` fallback tail is dropped from every
   chain (`dispatch_chain_drop` line, no `providers.yaml` entry) rather than
   repaired or loudly failed. Remaining three runs in this cluster
   (`…6781-59220`, `…6890-69809` `planner_dispatch_rc_43`) are blocked by it.
5. **A deleted target is discovered at verifier time, not preflight** — six
   runs ($8.22, $4.19, $3.36, $1.21, $1.15, $1.14) reached the epic
   verification command with `roots.target` already gone.
6. **Researcher install is behind the engine** by the recovered-target fix
   (`ddc88c43`; `planner.py` grep 0 vs 2). Its recipe verifiers *are*
   byte-identical to the engine's, so only the CLI/planner path lags.
7. **Two peer sessions' audit files sit uncommitted in the same new directory**
   (`732bb4cc-author-style-review.md`, `f6e1ed90-orchestrator-page-pm.md`) —
   an aggregation round racing on one path; harmless but worth a convention
   (one file per session, commit only your own).

---

## (c) Blocked on

- **Owner (user):** 779 and `run-1792045839-99014` — the researcher repo's test
  gate. 779's blocker is the repo's own `scripts/jest-guard.sh` load refusal
  (`REFUSE: 1-min load=11.25 > LOAD_MAX=8` → `rc=77`, `base_unrunnable=true`)
  recorded on the first attempt plus the hint's note that the suite cannot go
  green ("445 pre-existing environmental failures, none in touched files");
  99014's change already landed (`fc228bca7`) and its reviewer names the same
  HUMAN ACTION. Inbox `id=38` / `cn-b6461be2` waits on the decision.
- **Credentials:** kimi (401) and the missing `deepseek_flash` fallback —
  engine's own remedy `recover <run_id> --lane kimi=<replacement>`. Inbox
  `cn-045cd753`.
- **Owner (user):** apply (or decline) the ram-sentinel CPU-rule one-liner
  (`and not is_chrome_tab(info["argv"])` at
  `/Users/admin/bin/ram-sentinel.py:844`) plus `launchctl kickstart -k
  gui/$(id -u)/com.user.ramsentinel` — still unanswered from before the
  context break.
- **Nothing is blocked on this session.** The three engine defects found against
  779 are fixed and on `origin/main` (`37d39474`, `d1c0c871`, `f43022ea`) — but
   **none of them makes 779 green**: they make a future revive touch the node
  that actually failed, record the attempt's own end and spend, and stop the
  rubric grading the dead attempt. 779 itself stays where (c) puts it — the
  researcher repo's `jest-guard` load refusal is the owner's call.
- **The abstain ledger wart left open** (see (b)2): an abstaining verifier
  (`exit 0`, `status: unverified`) is written to `node_completion_checkpoints` as
  `succeeded`, so the P1.2 ledger records a check that never ran. Fixing it means
  reconciling two abstention contracts — the code-fix test verifier deliberately
  exits 0 (`recipes/code-fix/verifiers/test.py:1678`) while the engine's
  abstention rc is `ABSTAIN_RC = 3` (`verify/levels.py:176`) — which is a design
  decision, not a one-liner, so it was not folded into the fix above.

## Verification commands actually run

```
git log --oneline -1 && git rev-parse HEAD origin/main
git merge-base --is-ancestor {8f00468b,9ee820c1,16f11413,d70f94cf} origin/main
git log --oneline -S_recovery_target_cwd|-_link_candidate_dependencies|-_baseline_unrunnable
git -C <worktree> apply --check --reverse <run>/framework-edit.diff      # ×3 → OK
python3 -c "json.load(open('<run>/run-verdict.json'))"                   # ×3 → pass/0/publish
sqlite3 …/researcher/.mini-ork/state.db "SELECT status FROM task_runs WHERE id='run-1792059337-779'"
stat/python walk over runs/run-1792059337-779                            # mtimes: test stale, typecheck fresh
grep -n "NEEDS-CHANGE|rolled-back.json" -r mini_ork                      # → rubric_scoring.py:141,166; rubric_prescreen.py:570
grep -c _recovery_target_cwd <researcher install>/recovery/planner.py    # → 0 (engine: 2)
```

Later, for the three fixes in (b) — each run in
`mini-ork-worktrees/p2-row-settlement` (a worktree of `origin/main`) and pushed
to `origin/main` after a re-fetch + re-rebase:

```
python3 -m pytest tests/unit/test_rubric_prescreen_py.py tests/unit/test_rubric_scoring_py.py \
                  tests/unit/test_recover_verify.py tests/unit/test_mini_ork_execute_py.py -q  # → 140 passed
# each fix neutered (guard → `if False`, or the pre-fix UPDATE) and re-run:
#   P0#1 smoke  → the two suppression tests fail; P0#3 → 2 of 4 fail; P2#12 → 2 of 3 fail
git merge-base --is-ancestor {37d39474,d1c0c871,f43022ea} origin/main     # → IN ×3
```

Engine checkout note: `HEAD` moved under this session repeatedly (peer
fast-forwards through 23:13 and again at 23:40 — the audit rounds). The three
fixes were committed in a worktree of `origin/main` and pushed from there
(`git push origin HEAD:main`), so this session pushed nothing from the shared
checkout and the shared `main`'s unpushed audit commits were left alone. This
file's earlier revision was committed scoped to itself only.
