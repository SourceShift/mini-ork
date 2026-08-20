# P0 — Workspace exec protocol (no behavior change)

## Deliverable (ONE)
Introduce a minimal, backend-agnostic `Workspace` execution protocol so a later
phase can run agents/commands inside sandboxes, while the default `local`
backend preserves today's exact host-subprocess behavior. This phase adds NEW
code only and changes NO existing behavior — nothing else imports it yet.

Design context: `internal-docs/research/2026-07-30-sandbox-shared-drive-design.md`
(section 5, "P0 — Workspace protocol"). This is the seam that per-agent
sandboxes and the run-shared drive will plug into.

## Files in scope (ABSOLUTE paths — edit only these)
- /Volumes/docker-ssd/ps/mini-ork/mini_ork/runtime/sandbox.py   (NEW)
- /Volumes/docker-ssd/ps/mini-ork/tests/unit/test_sandbox_protocol.py   (NEW)

Do NOT edit any other file. Do NOT wire this into dispatch/execute yet.

## Acceptance criteria (concrete)
1. `mini_ork/runtime/sandbox.py` defines:
   - A `Workspace` protocol (typing.Protocol or ABC) with:
     `exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]`
     (merged stdout+stderr, returncode), `put(self, content: str) -> str`
     (write content to a temp path in the workspace, return the path),
     `get(self, path: str) -> str`, `up(self) -> None`, `down(self) -> None`.
   - A `LocalWorkspace` implementing `Workspace` whose `exec` matches the
     semantics of `mini_ork.runtime.contract.mo_runtime_exec` (cwd pinned per
     call, merged stdout+stderr, timeout that kills the process group). Reuse
     `mo_runtime_exec` internally rather than reimplementing the kill logic.
     `put`/`get` use ordinary local files under a temp dir; `up`/`down` are
     no-ops for local.
   - A backend registry mirroring the repo's existing SOLID pattern
     (see `register_node_handler` / `register_provider_kind`):
     `register_workspace_backend(name: str, factory)` +
     `get_workspace(backend: str = "local", **kwargs) -> Workspace`.
     `local` is registered by default. Unknown backend raises `ValueError`.
2. `mini_ork/runtime/sandbox.py` imports only Python stdlib + existing
   `mini_ork.runtime.contract`. No new third-party dependency.
3. `tests/unit/test_sandbox_protocol.py` covers:
   - `LocalWorkspace().exec("echo hi", cwd=<tmp>, timeout=10)` returns `(0, ...)`
     with `hi` in the output.
   - `put`/`get` round-trip a string.
   - a nonzero returncode command (`exit 3`) yields rc 3.
   - `get_workspace("local")` returns a `LocalWorkspace`; `get_workspace("nope")`
     raises `ValueError`.
   - `register_workspace_backend` makes a custom backend resolvable.
4. `make lint` (ruff blocking tier F+E9) is clean on the new files.
5. `python3 -m pytest -q tests/unit/test_sandbox_protocol.py` is green.

## Constraints
- Pure stdlib; no cloud SDKs in this phase.
- Match the module/docstring style of `mini_ork/runtime/engine.py` and
  `mini_ork/runtime/contract.py`.
- Fail-open and side-effect-free at import time.
