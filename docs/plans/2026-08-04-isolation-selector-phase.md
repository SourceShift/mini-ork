# Plan — Isolation selector (SE-3 Phase, §8 item 5 / RQ5 / SC3)

**Created:** 2026-08-04
**Status:** PLAN — awaiting a sequencing decision (see §6) before any build
**Origin:** `internal-docs/research/2026-08-04-harness-engine-standardization.md`
§5 RQ5, §7 SC3, §8 item 5. Follows A.3 (merged `64de9f2e`).

## 1. Goal

Re-express `local` / `docker` / `microVM` isolation as a single swappable
`workspace=` axis on the harness engine — so a run can spawn the **coding-agent
CLI itself** inside a container / microVM, not only route its bash tool-exec into
one. Mirror OpenHands' `Conversation(agent, workspace=...)` factory, where
isolation is a config axis on the execution object, not a parallel subsystem.

SC3 (the success criterion this closes): *"isolation is one swappable selector on
the engine, chosen per-run, not a separate opt-in subsystem."*

## 2. The crux (what the current-state map surfaced)

Dispatch and the Workspace subsystem are **two disconnected worlds today**:

| | Harness-CLI spawn | Tool-exec sandbox |
|---|---|---|
| Entry | `core.dispatch()` → `subprocess.Popen` (`core.py:84-97`) | `Workspace.exec()` (`sandbox.py:49`) |
| Where it runs | **always the host** (`cwd=request.cwd`) | host / docker / microvm |
| Verb shape | argv (no shell), **stdin** prompt, **separated** stdout+stderr, timeout→pgid-kill→rc=124, session_id parse, `start_new_session=True` (TTY-sever, A.1) | `(cmd, cwd, timeout) -> (rc, merged_output)` — **no stdin, merged streams, no handle** |
| Sole consumer | every node dispatch | `run_minimal()` only (`agent/minimal.py`) |

`MO_SANDBOX_SCOPE="agent"` ("route the CLI") is **named** (`agent_workspace.py:63`)
but has **zero consumers** — the concept is stubbed, unimplemented.

**Decisive fact:** the `Workspace.exec` verb, designed for tool-exec, *cannot*
host the harness-CLI spawn. It has no stdin channel and merges the two streams
the dispatch path must keep apart. So the isolation selector is **not** "wrap
`up()`/`down()` around today's `Popen`" — it requires a spawn-shaped verb on the
isolation boundary, plus re-establishing the A.1 TTY-sever / pgid-kill semantics
*inside* a container (docker/microvm own their own PID namespace, so the host's
`start_new_session` no longer reaches the CLI).

## 3. Anchors (verified live on main `475a907f`)

- `core.dispatch` spawn seam — `mini_ork/dispatch/core.py:61-162` (Popen at :84).
- `dispatch_model` + A.3 engine seam — `mini_ork/dispatch/providers.py`:
  `engine_of` (:1086), `ENGINE_COMMAND_BUILDERS` (:1115),
  `register_engine_command_builder` (:1128), `EXECUTABLE_MODELS` (:26, only `codex`),
  `MODEL_DISPATCH_BACKENDS` (per-lane spawn override, e.g. codex wrapper).
- `Workspace` Protocol — `mini_ork/runtime/sandbox.py:40-67`; `LocalWorkspace`
  (:70-110); factory `get_workspace` / `register_workspace_backend`.
- Backends — `runtime/backends/docker.py` (`DockerWorkspace`),
  `runtime/backends/microvm.py` (`MicrovmWorkspace`, default-preferred).
- Selector — `resolve_agent_workspace(node_cwd, *, env, drive_root)`
  (`runtime/agent_workspace.py:73-109`); `sandbox_scope()` (:63, `tool`|`agent`),
  `sandbox_backend()` (:57). Env surface: `MO_SANDBOX_BACKEND`,
  `MO_SANDBOX_SCOPE`, `MO_SANDBOX_IMAGE`, `MO_SHARED_DRIVE_ROOT`,
  `MO_SANDBOX_CPU`, `MO_SANDBOX_MEMORY`.

## 4. The central design fork (needs 3-subagent consensus before build)

**How does the harness-CLI spawn meet the isolation boundary?**

- **(a) Extend the `Workspace` Protocol** with a spawn verb —
  `spawn(argv, *, stdin, timeout, env, cwd) -> (rc, stdout, stderr, session_id?)`.
  `LocalWorkspace.spawn` = today's `Popen` verbatim; `DockerWorkspace.spawn` =
  `docker exec -i` with stdin piped + streams separated; `MicrovmWorkspace.spawn`
  = SDK equivalent. `core.dispatch` routes through `workspace.spawn(...)` when a
  workspace is present, else today's `Popen`.
  - *Pro:* one isolation abstraction owns both tool-exec and CLI-spawn;
    symmetrical with OpenHands.
  - *Con:* grows the "deliberately tiny" Protocol; every backend must implement a
    stdin-streaming spawn; the A.1 TTY-sever + pgid-kill must be re-established
    *inside* the container.

- **(b) A parallel spawn seam on the harness engine (A.3 registry).** Put
  `engine.spawn(command, *, request, workspace)` on the `HarnessEngine` /
  `ENGINE_COMMAND_BUILDERS` seam; it consults `workspace` (host/docker/microvm)
  to decide how to launch. Tool-exec and CLI-spawn stay distinct verbs,
  co-located on the engine (the A.3 direction).
  - *Pro:* honours "derive the Protocol from the two live engines"; doesn't
    distort the tool-exec Workspace; folds into the deferred named `HarnessEngine`
    Protocol.
  - *Con:* two isolation code paths that can drift; re-implements container-launch
    logic `DockerWorkspace` already has.

- **(c) Thin-plumb-only (minimum-durable-change).** Add `workspace=` to
  `DispatchRequest` + thread it through `engine_of`/`dispatch_model`, but wire
  only the `host` backend (today's behavior). docker/microvm CLI-spawn is a
  documented, tested **stub** (raises a clear "not yet implemented for scope=agent"
  error, pinned by a test).
  - *Pro:* establishes the axis at zero risk; lets the Phase-B openhands-sdk
    go/no-go decide whether we build CLI-in-container ourselves at all.
  - *Con:* SC3 only *designed*, not *shipped* — isolated CLI spawn doesn't
    actually run yet.

## 5. Ratchet to preserve (whatever shape lands)

A.1's invariant — the harness spawn severs its controlling TTY and is reapable as
one process group — must survive isolation. For host: unchanged
(`start_new_session=True`). For docker/microvm: the container/VM is the isolation
boundary, so the equivalent is "the CLI has no host TTY and dies with the
container." Add a test that a scope=agent spawn cannot inherit a host `/dev/tty`
(the Layer-1 incident must stay closed through the new path).

## 6. The sequencing question (the genuine user decision)

Both SE-3 incident layers are already closed (A.1/A.1b + A.2). The isolation
selector is **architectural cleanup, discretionary — not firefighting.** And its
*design depends on the Phase-B openhands-sdk go/no-go*:

- If **Phase B = GO** (embed openhands-sdk as a second engine): openhands'
  `Conversation(agent, workspace=)` **already** routes local→Local vs
  docker/remote→Remote isolation for its own engine. mini-ork would then only need
  CLI-in-container for the *claude-CLI / codex* engines — and the `workspace=`
  shape gets validated against openhands' real factory (the "derive from two
  adapters" constraint the A.3 follow-up todo demands).
- If **Phase B = NO-GO**: mini-ork owns CLI-isolation for all engines itself, and
  option (a)/(b) becomes load-bearing now.

Building a full CLI-spawn-in-workspace seam **before** the Phase-B gate risks
partial waste (openhands may bring isolation for free) **and** bets an interface
shape with only one adapter to check against — the premature-abstraction failure
mode the A.3 follow-up todo (`docs/todos/20260804-1258-...`) documents *away
from*.

**Recommendation.** Ship the isolation selector as **(c) thin-plumb-only now**
(establish the axis, zero risk, tested stub for scope=agent), and sequence the
full backend build (a)/(b) *after* the Phase-B go/no-go, so it is derived from two
real adapters. This inverts the naive §8 ordering — the higher-leverage next
phase is the **Phase-B openhands-sdk go/no-go**, with (c) landing alongside it as
the axis scaffold.

## 7. If the user confirms (c) + Phase-B-first, the build steps are

1. Add `workspace: str = "host"` to `DispatchRequest` (default preserves today).
2. `engine_of` / `dispatch_model`: read `workspace`, and for `workspace == "host"`
   keep the exact `Popen` path; for docker/microvm raise a pinned
   `NotImplementedError("scope=agent CLI spawn is Phase-B-gated")`.
3. Wire `MO_SANDBOX_SCOPE=agent` → `DispatchRequest.workspace` at the CLI seam
   (`cli/llm_dispatch` / `cli/execute`), so the stubbed axis is reachable and the
   error surfaces at dispatch, not 20 min later.
4. Tests: axis default is host (parity); scope=agent+docker raises the pinned
   error (ratchet); scope=tool unchanged (regression). Preserve the A.1 TTY
   ratchet through the new field.
5. Then re-open Phase B; if GO, replace the stub with the real backend spawn
   derived from openhands' `Conversation(workspace=)` + `DockerWorkspace.spawn`.

## 8. Do NOT

- Do not route the harness-CLI spawn through `Workspace.exec` — it has no stdin
  and merges the streams dispatch must keep separate (§2).
- Do not build the docker/microvm CLI-spawn backend before the Phase-B gate
  unless the user explicitly chooses (a)/(b)-now over the recommended (c)-first.
- Do not derive the spawn verb from the roadmap prose — derive it from the two
  live engines (claude CLI, codex wrapper) plus, post-gate, openhands.
