# Session gaps — 8162bb3f (SDD completeness + P2 artifact-transport recovery)

Session: `8162bb3f-d38b-40e7-b9bc-6076e3751710` (Claude Code, mini-ork primary checkout)
Reported: 2026-10-16. Repo head at audit time: `9ee820c1` on `main`.

This session had three jobs: (1) answer whether the SDD mechanism programme is
implemented in full, (2) explain why it is invisible in the Zed-fork IDE, (3)
recover the P2 `artifact-transport` deliverable after it vanished. Nothing was
"implemented" by this session except the P2 recovery — so the gap surface is
mostly *other* work this session measured, not work it owns.

## (a) Task table

| id | description | recorded status | VERIFIED status | evidence |
|---|---|---|---|---|
| `sdd-completeness-audit` | Is the SDD mechanism programme implemented in full + correctly? | done | DONE (analysis only) | file-existence sweep of the 15 epics; 6 absent (see §b) |
| `zed-ui-visibility` | Why can't the SDD work be seen in the Zed-fork UI? | done | DONE (analysis only) | `mini_ork/ide_pages/__init__.py` — no `sdd`/probe/evidence/policy page registered |
| `sdd-p2-artifact-transport` | Deliver `docs/architecture/artifact-transport.md` + kickoff + roadmap epic | done | **DONE, verified** | commit `0792dc43` is an ancestor of `origin/main`; doc gate PASS; kickoff sha `9656f151…` matches guard; run `run-1791751582-29808` verdict `pass` |
| `sdd-ide-page` | Add an SDD page so mechanisms render in the Zed UI | pending | NOT STARTED | `grep -n '"sdd"' mini_ork/ide_pages/__init__.py` → no match; no `mini_ork/ide_pages/sdd.py` |
| `sdd-missing-epics` | Build I2/I4/I6/I7/I8/I9 | pending | NOT STARTED | 6 candidate files MISSING (see §b) |
| `guard-primary-stash-u` | Stop a `git stash -u` in the primary from stranding untracked deliverables | pending | NOT STARTED | root-caused 2026-10-14; no guard committed |

Commands actually run:

```
git log --oneline -1 0792dc43                      → 0792dc43 restore(sdd-p2): artifact-transport doc + kickoff + roadmap epic
git merge-base --is-ancestor 0792dc43 origin/main  → IN origin/main
bash -c 'test -f docs/architecture/artifact-transport.md && grep -q blob_ref … && grep -q artifact_id … && grep -q slot …'  → GATE PASS
shasum -a 256 kickoffs/sdd-mechanisms/p2-artifact-transport-for-cloud-runs.md → 9656f15139fe2ccf362ee3dbec3fce2a906fc19082569a4d9992c126a6c78b6b (matches kickoff-guard.json sha_before)
```

## (b) Missing features & gaps

**G1 — 6 of 15 SDD roadmap epics are unimplemented** (verified absent 2026-10-16):

| epic | expected artifact | status |
|---|---|---|
| I2 probe-author | `prompts/probe-author.md` | MISSING |
| I4 slice-to-fit | `tests/unit/test_slice_to_fit.py` (+ slice_to_fit code) | MISSING |
| I6 policies | `schemas/policies.schema.json`, `.mini-ork/policies.yaml` | MISSING |
| I7 convergence-audit | `convergence_audit` / `gap_finder` code | MISSING |
| I8 guarded loop | `mini_ork/orchestration/guarded_loop.py` | MISSING |
| I9 shared contracts | `mini_ork/specdir/speckit.py` | MISSING |

**G2 — No IDE surface renders any SDD mechanism.** `mini_ork/ide_pages/__init__.py`
registers no `sdd` (nor probe-validity / evidence-ledger / policies) page, so the
Zed-fork UI cannot draw the mechanisms even where they exist. The closest surface
is `mini_ork/ide_pages/run.py` exposing `context-pack.v2.json` as an audit file.

**G3 — Landed SDD mechanisms are default-OFF / shadow (spec-vs-shipped gap).**
The 9 epics that landed pass their gates but most do not change behaviour:
`MO_PROBE_VALIDITY=shadow`, and `MO_EVIDENCE_LEDGER` / `MO_KICKOFF_GUARD` are
unset (code default shadow). Only `MO_CONTEXT_V2` and `MO_REVIEW_ROUND_AWARE`
are `on`. No n≥30 A/B has flipped them — so "implemented" ≠ "live".

**G4 — Untracked-deliverable loss in the primary.** Root-caused: `stash@{0}`
("preserve-main-changes-before-p1-2-checkpointer-worktree") held the P2 tracked
files AND, in its `^3` tree, the two untracked ones — i.e. an agent ran
`git stash -u` in the primary. Any in-flight run's untracked output can be
stranded the same way. No guard exists.

## (c) Blocked on

- **User decision, not a technical block.** All three open items
  (`sdd-ide-page`, `sdd-missing-epics`, `guard-primary-stash-u`) need the user to
  pick priority; no credentials, infra, or dependency is blocking them.
- **This session owns no uncommitted work** — its one deliverable is committed
  and pushed (`0792dc43` → `origin/main`).
