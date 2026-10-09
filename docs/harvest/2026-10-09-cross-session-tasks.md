# Cross-session task harvest — 2026-10-09

Source: Claude Code cross-session messaging (`SendMessage`) from `mini-ork-50`.
Method: every live peer session on this machine was asked to reply with
`TASK | status | subject | repo` and `BLOCKED-ON-USER | …` lines, open items only.

- peers listed by `ListAgents`: 48
- messaged: 47 (one named session is not on the messaging bridge)
- transcripts fallback (14d): 78 sessions, 33 open `tasks[]`, 17 `requires_user_action[]`

## Exclusions applied

- **Not a deliverable** — `BLOCKED-ON-USER` lines are decisions/environment, not work.
- **Not mini-ork's to take** — `job-3e` and `libwit-v1-ff` hold personal
  interview-prep material. `job-3e` said so explicitly. Hard exclude.
- **Not this repo** — most harvested work targets the `researcher` repo
  (`/Volumes/docker-ssd/Migration/Development/researcher`) or `ContextNest`.
  mini-ork can run them only with the right target repo / recipe.

## Cross-cutting blockers (block everything)

| # | blocker | source |
|---|---|---|
| B1 | daily budget circuit **tripped** — `MO_DAILY_BUDGET_USD` $50 vs measured $168.68/24h | mini-ork-0a |
| B2 | `DEEPSEEK_API_KEY` unset → deepseek lanes disabled | contextnest-72 |
| B3 | `ONBOARDING_DEMO_BOOK_PATH` unset → run-le-1791359434 recovery blocked | 0b591680 |
| B4 | no prod JWT — several researcher FE smokes unreachable | researcher-79/68/e9 |

B1 is decisive: a scheduler tick today will halt on the budget gate.

## A. mini-ork repo — deliverable candidates

| session | status | task |
|---|---|---|
| mini-ork-97 | blocked | strip `<z-insight>` from the IDE transcript render (`_overview_final`); needs the fix-location pick |
| mini-ork-0e | pending | execute `kickoffs/auto/artifact-completion-contracts.md` (item D) |
| mini-ork-0e | pending | jest grader adapter + high-water-mark incremental mining (`scripts/mine_heldout_tasks.py`) |
| mini-ork-0e | pending | commit staged agent_s smoke work + ~50 untracked kickoffs/evals |
| mini-ork-0e | pending | push parked zed `mini-ork` branch commits 2f94dc6 + 241355e |
| mini-ork-0a | pending | enable `MO_EVIDENCE_LEDGER=shadow` in the shared home secrets |
| mini-ork-0a | in_progress | SDD roadmap continuation: I2 → I6, I8, I7, I4, I9 |
| mini-ork-0a | pending | VA-2: route libwit verifier failures into the repair loop (BUG-006) |
| mini-ork-0a | pending | re-vendor `researcher/.mini-ork` to current main |
| cron-session | in_progress | revive the bounded REVIVE set (worker pid 84365, 12 targets, serial) |
| cron-session | pending | confirm the 98 NEVER_LAUNCHED kickoffs by family (triage rec#2) |
| cron-session | pending | investigate the recurring `failed=4` verification-stage failure across revive targets |
| 0b591680 | pending | fix pre-existing `test_changes_view_cached_or_computed_called_once` on main |
| 0b591680 | pending | NEEDS-CHANGE.md code steps: reasons as sub-bullets in `retry_notify._step_code` |
| 0b591680 | pending | `run.py` pairs new `node_start` with the previous round's `node_end` |
| d6d3f6a8 | pending | commit staged agent_s work + untracked kickoffs/evals (dup of mini-ork-0e) |
| 8162bb3f | pending | fast-forward primary to include 7805bb92 after I5 r2 |
| fe441c2f | pending | replay adapter so code-fix can prove targets in non-pytest repos |
| fe441c2f | pending | held-out eval, verification stack on vs off |
| fe441c2f | pending | wire `api_contract` verifier into a recipe |
| 1a4be064 | pending | distill G05 (next group in sequence) |

## B. researcher repo — mostly blocked on prod/browser smoke

Blocked-on-user smokes (not deliverable): reader preview sample click, Evidence-rail
claim jump, audiobook-grid covers, block-toolbar Ask send, `script_status_changed`
push, reserved-cost panel, cover-lightbox, AI-draft Accept parity.

Genuinely deliverable in researcher (pending, no user gate):
- `logger.ts:200` — winston timestamp format `:ms` → `.SSS`
- lazy-import heavy module-scope SDKs (gcs 7.69s, neo4j 9.17s, daytona 7.49s)
- parallelize the 249 sequential route `await import()`s in `app.ts` (103s of 196s boot)
- nodemon watch churn / `dist/**` ignore verification
- `FragmentSlotsPanel` — surface `no_pick_returned` diagnostics gaps
- slot-plan provider parse flake (30-section, unconfirmed cause)
- `chapterDerivativesStale.test.ts:194` masked TS syntax error
- strip 2 `[LANG:xx]` leaking pre_blocks (97964336, 4370c197)
- `ModelPreferenceSection.tsx:67-87` stale model after composer-picker change
- map terminal segment timeout to a reader-facing error (~10 LOC + test)
- delete merged worktrees (several sessions)
- confirm main deploys green + remove their worktrees (researcher-c2)

### researcher-64 (session 6206c133) — peer inventory, 2026-10-10

Reported read-only by the peer session; **inventory only, no work started**. Not
handed to mini-ork — researcher is outside this harvest's scope (mini-ork +
ContextNest).

- in_progress — Prod book job `job_1791227051195_4751f7cc` ("Turning Clients Into
  Referrals"): ch1 committed; ch2 recorded failed because the job was paused by
  the user mid-attempt and the awaiting-author seam aborts on a stopped job.
- pending — Ledger failed mini-ork runs (status=failed, $0, carrying the run id)
  so the compose debug view can open their agent logs
  (`server/services/bookGeneration/miniOrkRunLogService.ts`).
- pending — Delete the now-unrendered `ChapterWorkbench` / `ChapterStepTimeline`
  components and update their tests (`src/pages/compose/components/write/`).
- pending — launchd supervision for the laptop prod book worker (currently a
  tmux watchdog loop; `infra/` + `scripts/dev-worker-watchdog.sh`).

Blocked on the **user** (a peer cannot approve these):
- approve updating 8 outdated FE tests (ChapterVoiceSelect 5, theater 2,
  library r2-covercard 1) left behind by the compose-write change.
- answer the one blocking author question on ch2 of `job_1791227051195_4751f7cc`,
  then unpause the job so ch2 re-runs.
- approve hiding the compose header job cost from non-admin authors.

## C. ContextNest repo

- raise `MO_DAILY_BUDGET_USD` 150 → 225, then resume kickoff 06 attempt 2
- kickoff 07 (error-recovery dead code, tests required)
- kickoff 08 (key-management warnings, tests required)
- open PR `chore/fix-compiler-warnings` → main, verify MERGED

## D. Nothing open

`mini-ork-b2`, `researcher-4a` (2 brief questions), `researcher-ef` (all shipped).

## Lane assignment for the handover

Per instruction: prefer **glm-5.3** and **MiniMax-M3**; minimize deepseek.

| role | lane |
|---|---|
| planner / deep reasoning | glm-5.3 (opus only for the hardest) |
| implementer | MiniMax-M3 primary, glm-5.3 fallback |
| reviewer / lens panel | glm-5.3 + MiniMax-M3 |
| verifier | deterministic (no LLM) |
| deepseek | drop from the tail — B2 already disables it |
