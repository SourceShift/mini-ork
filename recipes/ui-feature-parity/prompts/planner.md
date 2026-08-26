# Planner — UI feature-parity gap audit

You are the planner for a read-only gap audit. The goal: guarantee that the
OpenHands agent-canvas frontend (`ui/`) can be made to expose EVERY capability
the mini-ork backend provides, so the downstream implementation stage misses
nothing.

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

Produce a short plan (≤500 words, markdown) that confirms:

1. **Inputs each lens reads** (already listed in the kickoff): the backend
   route surface under `mini_ork/web/routes/*.py`, the current spec
   `specs/openhands-native-surface.spec.md`, and the frontend `ui/`.
2. **Definition of a "capability"** for this audit: a distinct backend
   behaviour a user could act on or observe — an HTTP endpoint, an SSE stream,
   a websocket/PTY channel, a produced artifact, or a CLI-reachable action.
3. **Definition of a "gap"**: a capability that is (a) present in the routes but
   (b) NOT covered by a functional requirement in the current spec, OR not
   surfaced anywhere in `ui/`. Every gap must carry a `file:line` anchor.
4. **Coverage targets**: each lens must cover ALL 15 route modules named in the
   kickoff and report a minimum gap count (glm ≥ 25 endpoint-level rows,
   minimax ≥ 15 data-contract rows, opus ≥ 6 surface-level flows).
5. **Synthesis rules**: how the synthesizer deduplicates overlapping gaps across
   lenses and emits the backend→FE coverage matrix + missing FRs.
6. **Output binding**: final synthesis → `specs/openhands-native-surface.coverage.md`.

Keep it tight — the lenses already know their stance. Your job is to confirm
scope, tighten thresholds, and bind the output path.

Output to `${MINI_ORK_RUN_DIR}/plan.md`. Markdown, ≤500 words.
