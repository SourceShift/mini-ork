# Session gaps — `d5471234` (per-run orchestrator made always-visible on the run Overview)

Session: `d5471234-9015-4d4d-a85e-9e5353821fde` (this session)
Domain: frontend · Audited: 2026-10-16 · Method: every status re-verified against git refs
and file existence this turn, never against the recorded value. The two refs that matter
today are `origin/main` = `63fc80a0` and local `main` = `beca73ef`.

## Task table

| id | description | recorded | VERIFIED | evidence |
|---|---|---|---|---|
| `orch-always-visible` | User ask *"do based on rec always visible"* → option A: an unconditional Orchestrator section on the run Overview tab | done | **DONE** | `9ff1fea8` (`mini_ork/ide_pages/run.py`) + `64251073` (`tests/unit/test_ide_pages_run.py`); both `git merge-base --is-ancestor <c> origin/main` → YES, and both also in local `main`; `git show origin/main:mini_ork/ide_pages/run.py \| grep -c 'def orchestrator()'` → 1 |
| `ide-rebuild` | User ask *"make sure to rebuild the IDE too"* | done | **DONE** | `script/mini-ork-build` exit 0, `script/mini-ork-app` relinked the binary; a live instance is up (`pgrep` → pid 24204, `/Volumes/docker-ssd/ps/zed-mini-ork/target/mini-ork IDE.app/Contents/MacOS/zed`) |
| `ff-local-main` | From the earlier *"do both"*: bring primary local `main` level with `origin/main` | blocked (diverged) | **RESOLVED — no longer a blocker** | remote `main` advanced to `63fc80a0`; local `main` = `beca73ef` is **0 behind**, 1 ahead. `git rev-list --count main..origin/main` → 0; `git merge-base --is-ancestor origin/main main` → YES |
| `push-ui-919178b7` | Recorded as a stranded `mini-ork-ui` commit needing a push | declined (not mine) | **NON-ISSUE** | `919178b7` is an *ancestor* of mini-ork-ui HEAD `9154ef4` (`merge-base --is-ancestor` → YES), and `9154ef4` is on mini-ork-ui's `origin/main` and pinned by mini-ork commit `63fc80a0`. Nothing stranded |
| `monitor-runs` | Governing instruction: watch the spawned runs, fix mini-ork where a defect shows | in_progress | **no-op this window** | The one surface defect monitoring surfaced (the per-run orchestrator being invisible) is `orch-always-visible` above and is fixed. Not a task that "completes" |

Carried from an earlier session and **deliberately not started** (user-deferred; not this
session's assignments, listed only so this audit is not read as omitting them):
`memory-compact` (MEMORY.md compaction), `adrs-materialization`, `reward-g-saturation`,
`$CONTEXT_FILE` prompt cleanup ("fold it into the next recipe-prompt touch").

## Missing features & gaps

**G1 — Two different things are both called "the orchestrator".**
What I shipped is the *per-run decision core* (`mini_ork/orchestration/runner_orchestrator.py`),
a row on the run's Overview tab. The peer session `f6e1ed90` audits an *orchestrator rail page*
with 8 tabs (`board page orch --tab <t>`). Different objects, one word. The user's question
"where is the orchestrator?" is ambiguous by construction, and the two surfaces will keep
being confused until they are named apart in the UI.

**G2 — "Armed" can over-promise.**
The section's predicate is `runner_orchestrator.enabled()` alone. But the wiring exists only on
the dependency-graph `_parallel` path (`execute.py:1163`); on `_parallel` call sites that were
left unwired the layer is *enabled but structurally cannot act*. Those runs still print
"Armed — no interventions this run", which says the layer *chose* to stay quiet. It should
distinguish **wired + armed** from **enabled but unwired**.

**G3 — The snapshot worktree is now dead weight.**
`mini-ork-worktrees/_ide-engine` was created so the IDE would render the section while the
primary checkout was still stale. The primary now has the commits (`64251073` is an ancestor of
`main`), so the snapshot is unnecessary. It has no `.ownership` line, but it is a leftover
detached worktree and the live IDE instance was launched against it. Cleanup owed.

**G4 — Per-run, capped at 6 events; no cross-run view.**
The section reads `LIMIT 6` events for the current run. There is still no view of orchestrator
activity *across* runs — the earlier evidence that the orchestrator had fired only
`orchestrator.mutate|19` + `orchestrator.mutate.proposed|19` came from a raw DB query, not the UI.

**G5 — The verification cited here is partly inherited.**
This turn I verified the *commits* are on the shipped refs and the worktree/file exist. I did
**not** re-run `pytest tests/unit/test_ide_pages_run.py` or `make lint` this turn. The
"tests green, lint clean" claims are from the delivery turn and are attested, not re-witnessed.

## Blocked on

1. **The user, for the pixel half.** The section is drawn in a GPUI window; no tool call can
   assert the rendered row. Launch the IDE, open the run, Overview, and confirm the
   `Orchestrator — armed` section sits under "Why? — evidence".
2. **The owner, to push local `main`.** Local `main` carries one unpushed commit that is not
   mine (`beca73ef`, a peer's `docs(audit)` commit). Pushing it, and the reconciliation around
   it, belong to whoever owns `main`.
3. **A decision on the leftover scaffolding** — remove `mini-ork-worktrees/_ide-engine` and
   relaunch the IDE against the primary engine (or keep both, deliberately).
