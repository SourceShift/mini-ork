# Close the backend→frontend feature gap for the OpenHands agent-canvas UI

## Problem

The OpenHands agent-canvas frontend (`ui/`) currently boots against a thin
9-endpoint agent-server compatibility shim and exposes almost NONE of mini-ork's
native capabilities. The mini-ork backend serves ~60 endpoints across 15 route
modules (runs, dispatch, learning loop, trajectory, PTY, fleet, projects, …). A
hand-written spec (`specs/openhands-native-surface.spec.md`) proposes a hybrid
surface but was authored by a single reader and almost certainly misses
capabilities. Before we implement, we need three uncorrelated readers to prove
NOTHING is missed.

## Definition of Done

The recipe produces:

1. Three lens gap reports under `${MINI_ORK_RUN_DIR}/lens-*.md`:
   - **glm** — exhaustive endpoint enumeration across all 15 route modules
     (≥ 25 rows, each `file:line`, marked in-spec / in-ui / GAP).
   - **minimax** — data-contract → FE-surface mapping (≥ 15 rows), flagging
     streaming/binary data with no renderable home today.
   - **opus** — architectural subsystem & user-flow gaps (~1500 words, all 8
     clusters, ≥ 6 recommended new FRs).
2. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md`: a backend→FE coverage
   matrix by subsystem with consensus markers, the missing functional
   requirements, and a phase-assignment recommendation.
3. The synthesis publishes to `specs/openhands-native-surface.coverage.md`
   (a sibling of the spec — it does NOT overwrite the hand-written spec).

## Files in scope

Read-only. The lenses read these; nothing here is modified.

- **Backend routes** (the surface to catalogue), all under `mini_ork/web/routes/`:
  `agent_server.py`, `dispatch.py`, `learning.py`, `run_detail.py`,
  `stream.py`, `pty.py`, `control.py`, `fleet.py`, `trajectory.py`,
  `traceotter.py`, `projects.py`, `idea_tree.py`, `artifacts.py`,
  `recovery.py`, `fingerprint.py`.
- **Current spec**: `specs/openhands-native-surface.spec.md`.
- **Frontend**: `ui/` (React Router 7 + Vite SPA; the agent-server compat shim
  that boots it lives in `mini_ork/web/routes/agent_server.py`).

## Scope

- Target repo: this mini-ork checkout (self-analysis — set
  `MO_ALLOW_FRAMEWORK_CWD=1`).
- Depth: 3 parallel lenses + 1 synthesis. Read-only.
- Budget: ≤ $20 (per the task_class cost model; raise the per-run cap for
  this run since three file-reading lenses exceed the default $0.50).
- Output: analysis only; no source file under `mini_ork/`, `ui/`, or `recipes/`
  is modified. The implementation is a SEPARATE follow-up run.

## Success Criteria

- All 3 lens reports exist, are non-empty, and cite ≥ 1 `file:line` each.
- Synthesis cross-references all 3 lenses and has consensus markers.
- `verifiers/lens-completeness.py` returns `pass=true`.
- Every one of the 15 route modules appears in at least the glm lens report.

## Proof of success

This run has succeeded when the published coverage doc exists and contains the
coverage matrix. Command that proves it:

```
test -f specs/openhands-native-surface.coverage.md && grep -q "coverage matrix" specs/openhands-native-surface.coverage.md && echo PASS
```

## Non-goals

- Do NOT modify any source file (read-only audit).
- Do NOT implement FE components — that is the follow-up implementation run.
- Do NOT audit dependencies (React, Vite, HeroUI, the OpenHands protocol) —
  only the mini-ork surface and its coverage.
