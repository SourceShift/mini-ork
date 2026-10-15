# Failed mini-ork runs, last 4 days — why each failed, and how to revive it

*2026-10-15 · scope: every `task_runs` row with `status` `failed`/`rolled_back`
created 2026-10-11 → 2026-10-15 (28 runs) · method: `mini_ork.recovery.diagnose`
(SDE-1, side-effect-free), the retry hint, a "superseded" check (a later run of the
same kickoff published, the epic is `done`, or the run's files are already on
`main`), and each run's own evidence. Extends the 48 h doc
`docs/todos/2026-10-14-failed-runs-48h-triage.md` (its Daytona cluster is closed).*

## Verdict in one line

**26 of 28 need no revive** — their work shipped by another route, or they failed by
design. **2 need action**: one small doc run to relaunch, one probe run that must not
be revived until an engine bug is fixed. The more useful output is the 7 engine gaps
below that made these runs look like open failures.

## Per run

| # | Run | Recipe | Why it failed | Revive? |
|---|---|---|---|---|
| 1 | `run-1791749297-8997` | recipe-creator | reaped (dispatcher pid gone) before planning | **No** — superseded by `run-1791749955-70478` (published) |
| 2 | `run-1791751039-75088` | orch-smoke | `fail-probe` verifier — a deliberately failing smoke | **No** — failed by design |
| 3 | `run-1791751229-91816` | orch-smoke | same `fail-probe`; `repair.json` stuck `repairing` since 10-11 20:42 with no process | **No** — failed by design; clear the orphaned `repairing` state (gap G2) |
| 4 | `run-1791751570-28061` | — | killed by the user | **No** — superseded by `run-1791751582-29808` |
| 5 | `run-1791785128-75073` | epic-runner (`wit-3-integrations`) | reaped: dispatcher pid 75073 gone before any node ran | **No** — epic `wit-3-integrations` is `done`; roadmap marked delivered (`0c84a09a`) |
| 6 | `run-1791785128-75074` | epic-runner (`wit-2-documents`) | reaped: dispatcher pid gone | **No** — epic `done` |
| 7 | `run-1791785211-80557` | epic-runner (`wit-2-documents`) | codex lane out of quota before planning | **No** — epic `done` (hint `recover --lane planner=glm` is moot) |
| 8 | `run-1791785422-8678` | epic-runner (`wit-3-integrations`) | codex out of quota before planning | **No** — epic `done` |
| 9 | `run-1791786171-56599` | epic-runner (`wit-4-jira`) | codex out of quota before planning | **No** — `wit-4-jira` `done` via `run-1791898943-82971` |
| 10 | `run-1791786292-75182` | epic-runner (`wit-2-documents` attempt 2) | kimi rejected the credentials | **No** — epic `done` |
| 11 | `run-1791788217-61320` | epic-runner (`wit-3-integrations` attempt 2) | planner rc 65, then reaped; its child failed (row 12) | **No** — epic `done` |
| 12 | `run-1791788217-61320-communication…` | framework-edit (child) | interrupted mid-implementer: HEAD moved under it (an external merge) | **No** — its diff adds `mini_ork/integrations/{_base,telegram}.py`, which are already on `main`. The Slack/Discord transports were re-queued as new epics (`chat-slack-transport`, `chat-discord-transport`, `not started`) — forward work, schedule normally |
| 13–20 | `run-1791910667-50438` … `run-1791914481-48337` | code-fix (Daytona e2e cluster) | lane quota / planner rc / remote publish abstain (engine fault `88a3f99e`) | **No** — cluster closed: `run-1792047347-71903` published on current HEAD (see the 48 h doc) |
| 21 | `run-1792047098-61635` | code-fix | planner rc 1 | **No** — superseded by `run-1792047347-71903` |
| 22 | `run-1791930074-82118` | code-fix (frozen probe-1) | reaped; it ran with the **primary checkout as target** and "fixed" the frozen probe fixture `tally.py` in place (restored twice) | **Do NOT revive** — fix gap G4 first, then re-score probe-1 in a scratch copy |
| 23 | `hr-cli-worktree-20261015135213` | framework-edit | kickoff verification hit `test_native_dispatch_py::test_default_root_is_the_engine_root`; reviewer escalated a real finding: Zed launchers leak `MO_WORKSPACE_MODE` into the `run` child via `dict(os.environ)` | **No** — feature shipped as `a8dbc2dd`. Owner (`hr-*` session, live) should confirm the env-leak finding is handled |
| 24 | `hr-exec-park-20261015141603` | framework-edit | verification rc 2; reviewer escalated: edit in a forbidden file, and the run record is skipped even when `crashed=True` | **No** — shipped as `e27da337` (park for node review). Owner should confirm the `crashed=True` finding |
| 25 | `hr-smoke-a-143701` | code-fix | publish withheld (levels unverified), reviewer passed | **No** — owner iterating: `hr-smoke-b`/`hr-smoke-c` are `reviewing` now |
| 26 | `sde-3b2-ledger-20261015-142338` | framework-edit | engine bug: test verifier ran a kickoff command with `MINI_ORK_RUN_DIR` scrubbed | **Done** — salvaged + merged `6f60a89c`; bug fixed `23722ba8` |
| 27 | `vg-3-spec-tests-20261015-142340` | framework-edit | engine bug: profile prose scan ran `pytest files into` as a command (2.4 h of revise rounds) | **Done** — salvaged + merged `640f80d3`; prose-scan fix pending (VG-1c) |
| 28 | `run-1792076652-70170` | framework-edit (`cn-ui-shipped-today.md`) | planner dispatch rc **127** (command not found) — the launch env lacked the CLI on `PATH`; engine worktree `cn-ui-engine` exists; the doc never landed | **Yes** — relaunch with the standard launcher env (planner never ran, so a fresh run equals a resume): `MO_TARGET_CWD=<worktree> … bin/mini-ork run framework-edit kickoffs/auto/cn-ui-shipped-today.md`. Owner session (cn-ui) has ended; ~$1 |

## Engine gaps these runs exposed (the class-level revive)

| Gap | Evidence | Fix |
|---|---|---|
| **G1 superseded runs stay "failed"** | 22 of 28 are already delivered elsewhere, yet sit in `task_runs` as failures and are eligible for repair sweeps | Supervisor (SDE-4) marks a failed run `superseded` when its epic is `done`, a later run of the same kickoff published, or its diff is already on `main` |
| **G2 orphaned `repairing` state** | row 3: `repairing` since 10-11, no process | Supervisor lease timeout → `gave_up` with a note |
| **G3 epic → failed-run back-links** | `wit-2-documents` / `wit-3-integrations` are `done` with `last_run_id` pointing at failed runs | When an epic is closed by another route, record the delivering run/commit |
| **G4 probe runs target the primary** | row 22 mutated a frozen fixture in the shared checkout | Probe scoring runs must target a scratch copy of the fixture, never `MINI_ORK_ROOT` |
| **G5 rc 127 is "not started", not "environment"** | row 28 | Diagnosis rule: planner/dispatch rc 127 → `environment`, cite the missing binary |
| **G6 reviewer escalations stay `unclassified`** | rows 23, 24, 26, 27 — each reviewer named the cause in prose | SDE-3b1 model diagnoser (in flight) reads the review text |
| **G7 harness bugs burn revise rounds** | rows 26, 27 + the live `sde-3b1` run (flaky `test_wait_for_spawner…` under load) | VG-1c: no prose-scan commands, measures on the engine venv, harden the timing test; plus "unfixable by revise" detection |

## Actions

1. Relaunch row 28 (cn-ui "shipped since yesterday" doc) — needs a yes, since its owner session ended.
2. Do not revive row 22; queue G4.
3. Ask the `hr-*` owner to confirm the two reviewer findings (rows 23, 24) are handled on `main`.
4. G1–G3 go into SDE-4 (supervisor); G5 into a diagnosis rule; G6/G7 already queued (SDE-3b1, VG-1c).
