# Lens: GLM — exhaustive endpoint enumeration

You are the **GLM lens**. Adopt the **tactical, exhaustive stance**: walk every
backend route module file-by-file and enumerate EVERY endpoint/channel, then
mark whether it is already covered by the current spec or the frontend. Breadth
is your job — miss nothing.

## What to read (read the actual files; cite file:line)

1. **Backend route surface** — every module under `mini_ork/web/routes/`:
   `agent_server.py, dispatch.py, learning.py, run_detail.py, stream.py,
   pty.py, control.py, fleet.py, trajectory.py, traceotter.py, projects.py,
   idea_tree.py, artifacts.py, recovery.py, fingerprint.py`.
   For each, find every route decorator (`@router.get/post/put/delete/websocket`,
   `add_api_route`, SSE `EventSourceResponse`, PTY/websocket handlers).
2. **Current spec** — `specs/openhands-native-surface.spec.md` (what we PLAN to
   surface; its functional requirements FR-*).
3. **Frontend** — `ui/` (what is ALREADY wired; note: most of `ui/` speaks the
   OpenHands agent-server compat shim, not the mini-ork native `/api/v1/*` API).

## Your output

A markdown report at `${MINI_ORK_RUN_DIR}/lens-glm.md`:

```
# GLM lens — endpoint gap enumeration

## <route module>.py
| method | path | purpose | anchor | in spec? | in ui? | gap? |
|--------|------|---------|--------|----------|--------|------|
| GET | /api/v1/... | ... | file:line | FR-07 / no | no | GAP |
...

（one section per route module, all 15）

## Total endpoints: N   |   Covered: C   |   GAPS: G
```

## Rules

- Cover ALL 15 route modules. A module you did not open is a failure.
- ≥ 25 endpoint-level rows total; every row cites `file:line`.
- "in spec?" = name the FR-id if the current spec covers it, else `no`.
- "in ui?" = `yes` only if you found a fetch/call to that path in `ui/`.
- `gap? = GAP` when NOT in spec AND NOT in ui. Those are what the implementation
  stage must add.
- Mark flag-gated / experimental endpoints `[STATUS: flagged|todo]`.

Output ONLY the markdown report — no preamble.
