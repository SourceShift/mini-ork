# Decision — isolation-selector spawn seam (SE-3 §8 item 5 / RQ5 / SC3)

**Date:** 2026-08-04
**Status:** SPLIT → returned to user (sequencing is the user's call; consensus is advisory)
**Plan:** `docs/plans/2026-08-04-isolation-selector-phase.md`
**Consensus run:** 3 role-differentiated subagents (Builder · Reviewer · Future-Maintainer), model sonnet, parallel.

## Problem

Build the isolation selector: let a run spawn the harness CLI *itself* inside
docker/microvm (`scope=agent`), not just its tool-exec. Crux: `Workspace.exec`
(`sandbox.py:49`, `(cmd,cwd,timeout)->(rc,merged)`, no stdin) cannot host the
dispatch spawn (`core.py:84`, argv + stdin prompt + separated stdout/stderr +
pgid-kill timeout + session_id + TTY-sever). A new spawn-shaped verb is needed on
the isolation boundary. The user pre-selected "build the full selector now"
(lane B) before this consensus ran.

## Options

- **(a)** Extend the `Workspace` Protocol with `spawn(argv,*,stdin,timeout,env,cwd)`.
- **(b)** Parallel `spawn` seam on the harness engine (A.3 registry).
- **(c)** Thin-plumb only: `workspace=` axis on `DispatchRequest`, host path
  unchanged, docker/microvm = pinned `NotImplementedError`, TTY ratchet test.
- **(hybrid)** Engine owns the spawn *contract*; delegates container transport to
  a thin `Workspace.spawn` primitive per backend.

## Votes (verbatim)

### Builder → `other-hybrid-delegate`
- **LOAD-BEARING RISK:** If the full container-launch mechanics go inside the
  engine seam (pure b), DockerWorkspace/MicrovmWorkspace already own container
  lifecycle + stdin-capable exec — the plumbing gets rewritten a second time and
  the two copies drift on the one invariant that kills headless runs: TTY-sever +
  process-group reap inside the container.
- **REASONING:** (a) pure distorts a deliberately-minimal Protocol whose merged-
  stream, no-stdin `exec` is right for tool-exec. (b) pure duplicates container
  launch and creates two reap-invariant code paths that drift. The hybrid threads
  the needle: engine seam owns the spawn CONTRACT (argv, stdin pipe, separate
  stdout/stderr, session_id, rc=124), and for scope=agent DELEGATES transport to a
  single thin `Workspace.spawn` primitive per backend (not the full `exec`).
  Local.spawn = today's Popen verbatim (zero regression); Docker.spawn =
  `docker exec -i` with no `-t` (no TTY); Microvm.spawn = SDK equivalent. One
  place to enforce the TTY-sever test; one container-lifecycle owner; no Protocol
  bloat beyond one verb.
- **ALTERNATIVE-IF-WRONG:** If the hybrid is too much surface to ship safely, drop
  to (b) and accept duplicate container logic, protected by a shared test fixture
  firing the TTY-sever assertion against BOTH paths.

### Reviewer → `other-thin-plumb-first`
- **LOAD-BEARING RISK:** Both (a) and (b) built now derive the spawn-verb shape
  from exactly one live adapter (claude CLI) — a premature abstraction that rots or
  gets rewritten when a second real adapter (openhands-sdk / codex-in-container)
  arrives.
- **REASONING:** The correctness lens is not just "does TTY-sever hold" but "will
  the abstraction boundary hold without a regression seam." A spawn verb added now
  but implemented by a single concrete adapter (host) with docker/microvm stubs is
  exactly the drift surface (b) warns about, added to the Protocol surface (a)
  would distort. The §5 ratchet (a test proving scope=agent cannot inherit
  `/dev/tty`) is fully achievable with (c): add `workspace` to `DispatchRequest`,
  keep Popen when `workspace="host"`, pin docker/microvm as `NotImplementedError`,
  write the TTY ratchet against the host default. Establishes the invariant-under-
  test with zero new abstraction to rot; defer the verb shape to Phase B.
- **ALTERNATIVE-IF-WRONG:** If Phase B is confirmed NO-GO first, prefer (a) with
  `Workspace.spawn` as an *optional* Protocol extension (default raises
  `NotImplementedError`) over (b), reusing the container lifecycle Docker already
  owns rather than duplicating on the engine seam.

### Future-Maintainer → `other-c-thin-plumb-first`
- **LOAD-BEARING RISK:** Committing to a full spawn-verb shape against only one
  real adapter before the Phase-B openhands-sdk go/no-go bakes in a premature
  abstraction the second engine will duplicate, reshape, or fight — locking the
  maintainer into an interface with no derived validation.
- **REASONING:** The highest danger isn't picking wrong between (a)/(b) today — it
  is picking ANY shape before there are two adapters to derive from. If Phase B is
  GO, openhands brings `Conversation(agent, workspace=)` as a second data point and
  both become derivable; if NO-GO, mini-ork owns isolation and can make the call
  with the full picture. (c) establishes `workspace: str = "host"` on
  `DispatchRequest`, threads it, raises pinned `NotImplementedError` for
  scope=agent, preserves the A.1 ratchet — zero implementation cost, zero interface
  commitment, cheapest to reshape (one enum vs a coordinated multi-backend
  refactor under pressure).
- **ALTERNATIVE-IF-WRONG:** If (c) stalls Phase B or NO-GO lands first, default to
  (b) — engine owns its spawn contract, delegates container mechanics via a minimal
  internal primitive — keeping the tool-exec Workspace Protocol tight.

## Synthesis

**2/3 thin-plumb-first (c); 1/3 build-now-as-hybrid.** Neither pure (a) nor pure
(b) got a vote — the "deliberately-tiny Protocol distortion" (a) and the
"duplicate container logic + drift" (b) cons were disqualifying for every lens.

The split is fundamentally about **sequencing**, which the user already decided
("build now", lane B), *before* seeing this consensus. The consensus is new,
decision-relevant evidence that build-now is premature under the "derive the
interface from two adapters" constraint (the same constraint the A.3 follow-up
todo `docs/todos/20260804-1258-...` documents). So the outcome returns to the
user rather than auto-shipping the majority.

**If the user holds "build now":** the reconciling shape is the Builder's
**hybrid-delegate** — it is the build-now option that best answers the majority's
rot concern, because the only new cross-cutting interface is a thin
`Workspace.spawn` primitive that mirrors today's already-proven Popen contract;
the higher-level engine/HarnessEngine shape stays thin, so a Phase-B reshape has a
small blast radius. Both dissenters' fallbacks (Reviewer: a-as-optional-extension;
Future-Maintainer: b) also land near this hybrid.

**If the user takes the majority:** ship (c) now (axis + stub + TTY ratchet) and
make the Phase-B openhands-sdk go/no-go the next substantive phase.

## Decision-risk audit

| Risk | (c) thin-plumb | hybrid build-now |
|---|---|---|
| Interface rot before 2nd adapter | none (no verb committed) | low (thin `Workspace.spawn` mirrors proven Popen) |
| A.1 TTY/pgid invariant provable | yes (host default) | yes (single test site, but must hold *inside* container) |
| Partial waste if Phase B GO | none | some (docker/microvm spawn code may be obviated by openhands RemoteConversation) |
| SC3 shipped (isolated CLI actually runs) | no (designed only) | yes |
| Cost to reshape later | one enum | one thin primitive + engine glue |

## Follow-up

Whichever lands, preserve the A.1 ratchet as a test proving a scope=agent spawn
cannot inherit a host `/dev/tty` and stays reapable. The named `HarnessEngine`
Protocol promotion remains Phase-B-gated (`docs/todos/20260804-1258-...`).
