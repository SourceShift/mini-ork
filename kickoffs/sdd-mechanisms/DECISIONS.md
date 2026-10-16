# SDD mechanism decisions — status + flag record

Verified 2026-10-16 against `main` head `9ee820c1`. Every status below is from
`git`/file existence, not from a plan or a z-insight claim. Flag defaults are
read from the engine source (file:line cited).

## P1#7 — epic implementation status (declared path)

| epic | declared artifact | status | evidence |
|---|---|---|---|
| sdd-k05a-finalize-every-run | finalize path in publisher | LIVE | no dedicated flag (see §flags) |
| sdd-k05b-verdict-hygiene | `tests/unit/test_verdict_hygiene.py` | LIVE | test present |
| sdd-k1-baseline-metrics | `mini_ork/cli/metrics_sdd.py` (`mini-ork metrics sdd`) | LIVE | module present |
| sdd-i1-probe-validity | `mini_ork/verify/probe_validity.py` | LANDED (flag-gated) | file present; `FLAG=MO_PROBE_VALIDITY` (probe_validity.py:34) |
| sdd-i1-shadow | shadow mode of I1 | LANDED | same flag, `=shadow` |
| sdd-k05c-verifier-nodes-run | verifier node duration recording | LIVE | no dedicated flag |
| sdd-va-real-verifier | verified-artifact recipe verifier | LIVE | no dedicated flag |
| sdd-i3-kickoff-contract | `mini_ork/verify/kickoff_guard.py` | LANDED (flag-gated) | file present; `FLAG=MO_KICKOFF_GUARD` (kickoff_guard.py:33) |
| sdd-i5-evidence-ledger | `mini_ork/verify/evidence_ledger.py` | LANDED (flag-gated) | file present; `FLAG=MO_EVIDENCE_LEDGER` (evidence_ledger.py:35) |
| sdd-p2-artifact-transport | `docs/architecture/artifact-transport.md` | **DONE** | commit `0792dc43` in `origin/main`; gate PASS |
| sdd-i2-probe-author | `prompts/probe-author.md` | **MISSING** | absent 2026-10-16 |
| sdd-i4-slice-to-fit | `tests/unit/test_slice_to_fit.py` (+ code) | **MISSING** | absent |
| sdd-i6-policies | `schemas/policies.schema.json`, `.mini-ork/policies.yaml` | **MISSING** | both absent |
| sdd-i7-convergence-audit | `convergence_audit` / `gap_finder` code | **MISSING** | absent |
| sdd-i8-guarded-loop | `mini_ork/orchestration/guarded_loop.py` | **MISSING** | absent |
| sdd-i9-shared-contracts | `mini_ork/specdir/speckit.py` | **MISSING** | absent |

**Wanted vs not:** all six missing epics (I2/I4/I6/I7/I8/I9) remain wanted — none
has been de-scoped. Until each lands, this file is the only honouring of their
declared paths; the roadmap keeps claiming them.

**Dependency-ready order** (deps satisfied today): I2, I4, I6, I8. I7 waits on
I8; I9 waits on I4.

## P1#10 — flag decisions for the landed epics

The engine defines exactly **five** SDD flags. Grep evidence:
`MO_KICKOFF_GUARD`, `MO_PROBE_VALIDITY`, `MO_EVIDENCE_LEDGER`, `MO_CONTEXT_V2`,
`MO_REVIEW_ROUND_AWARE` (`mini_ork/settings_catalogue.py:92,102,113,134`).

| epic | flag | engine default | home value | effective | DECISION | reason |
|---|---|---|---|---|---|---|
| sdd-i1-probe-validity | `MO_PROBE_VALIDITY` | `off` (publisher.py:398) | `shadow` | shadow | **KEEP SHADOW** | Roadmap build rule is shadow-before-flip; a shadow estimate would have blocked 399 of 467 published runs, so `enforce` needs an n≥30 A/B first. Not a default — a recorded choice. |
| sdd-i1-shadow | `MO_PROBE_VALIDITY` | — | `shadow` | shadow | **SHADOW BY CONSTRUCTION** | This epic *is* the shadow mode. |
| sdd-i3-kickoff-contract | `MO_KICKOFF_GUARD` | `shadow` (kickoff_guard.py:19) | unset → shadow | shadow | **KEEP SHADOW (recorded)** | Engine default; no A/B to justify `enforce`. Made explicit rather than left to default. |
| sdd-i5-evidence-ledger | `MO_EVIDENCE_LEDGER` | `off` (evidence_ledger.py:17) | unset → off | off | **SET TO `shadow`** | Shadow records evidence without blocking — behaviour-neutral, so it is safe to enable now. Leaving it at engine-default contradicts P1#10. |
| sdd-k05a / k05b / k05c / k1 / va | *(none)* | — | — | live | **UNCONDITIONAL** | No flag exists in the engine's five-flag set, so six of the nine landed epics are already live; nothing to shadow. |

Adjacent (not roadmap epics, recorded here for completeness): `MO_CONTEXT_V2=on`
and `MO_REVIEW_ROUND_AWARE=on` in the home, both with a deterministic holdout arm.

## G2 — no IDE surface renders any SDD mechanism

`mini_ork/ide_pages/__init__.py` registers no `sdd` / probe-validity /
evidence-ledger / policy page, so the Zed-fork UI cannot draw the mechanisms even
where they are live. Closest surface: `mini_ork/ide_pages/run.py` exposes
`context-pack.v2.json` as a run-local audit file. This is **new scope** — it is
not one of the 15 roadmap epics and needs an owner/decision before it is built.

## P2#19 — `git stash -u` in the primary strands untracked deliverables

Root cause (2026-10-14): `stash@{0}`
("preserve-main-changes-before-p1-2-checkpointer-worktree") held the P2 tracked
files in its first tree and the two *untracked* files only in its `^3` tree — an
agent ran `git stash -u` in the primary. Hazard, not a fix-now: **never
`git stash -u` in the primary** (it sweeps in-flight runs' untracked outputs, and
`git stash show --name-only` hides the untracked half). Recovery: inspect
`stash@{N}^3`, or re-derive byte-exact from the run dir
(`context-pack.json:task_brief.content.kickoff` + `sessions/*.jsonl`). This is
documented, not guarded — a guard is **new scope**, owner TBD.
