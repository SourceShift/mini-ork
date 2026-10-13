# cloud-providers A1 — Workspace backend catalog (name-agnostic resolver)

Part of `docs/architecture/cloud-sandbox-providers.md` (epic **A**, first half).
Single deliverable: the Workspace resolver stops naming backends. A2 (teardown
registry) runs in parallel on disjoint files; B (node-provider seam) follows.

## Goal

Selecting a Workspace backend by name works for **any** registered or
lazily-importable backend, without `mini_ork/runtime/agent_workspace.py`
listing it. Adding a backend is one module that calls
`register_workspace_backend(<name>, factory)` at import, plus one catalog line
for a built-in.

## Why (traced 2026-10-13)

- `mini_ork/runtime/agent_workspace.py:100-128` and `:162-181` each hardcode
  `if backend in ("microvm", "docker", "remote")`, an import ladder, and a
  `remote`-only `env=` kwarg. Every new backend edits both ladders.
- A backend that is registered but not in the ladder falls to
  `get_workspace(backend, root=node_cwd)` (`:131`, `:183`). So a plugin backend
  gets *host* kwargs and the *host* cwd, never the drive root and the
  `/workspace` mount that every isolated backend needs.
- `docker`/`microvm` factories (`backends/docker.py:281`,
  `backends/microvm.py:339`) read `os.environ` directly. `remote` honors the
  resolver's scoped `env=`. Same seam, two env contracts.

## Requirements

1. **Lazy catalog in `mini_ork/runtime/sandbox.py`:**
   - `_LAZY_WORKSPACE_BACKENDS: dict[str, str]` = `{"docker":
     "mini_ork.runtime.backends.docker", "microvm":
     "mini_ork.runtime.backends.microvm", "remote":
     "mini_ork.runtime.backends.remote"}`.
   - `register_lazy_workspace_backend(name, module)`.
   - `known_workspace_backends() -> list[str]` (sorted union of the registered
     and catalog names).
   - `get_workspace(name, **kw)` imports the catalog module on first use when
     `name` is not yet registered. An import failure raises `RuntimeError`
     naming the backend and module. An unknown name still raises the existing
     `ValueError` (message unchanged apart from listing
     `known_workspace_backends()`).
   - Import-time contract unchanged: stdlib only, no catalog module imported
     at import.
2. **Name-agnostic resolvers in `mini_ork/runtime/agent_workspace.py`.** Both
   `resolve_agent_workspace` and `resolve_spawn_workspace` become:
   - unset / `""` → as today (`(None, node_cwd)`, or the `ValueError` for
     `""`/`host` in the spawn resolver)
   - `local` → as today (`get_workspace("local", root=…)`)
   - **any other name** → `get_workspace(name, image=…, drive_root=…,
     mount_path=MOUNT_PATH, env=dict(src))` and `exec_cwd = MOUNT_PATH`. This
     covers the built-ins, plugins, and unknown names (unknown → `ValueError`
     from `get_workspace`).

   Delete both `if backend in (...)` ladders and the per-backend import
   statements. The image/drive-root resolution stays as written.
3. **One env contract for factories.** The `docker` and `microvm` `_factory`
   pop an optional `env=` kwarg and use it in place of `os.environ` for every
   env read in the factory. Absent → `os.environ` (unchanged). `remote`
   already does this; leave it.
4. Update the one test that pinned the old fall-through:
   `tests/unit/test_spawn_workspace_isolation_py.py:236` asserts
   `fake_backend["kwargs"]["root"]`. Under requirement 2 a registered isolated
   backend receives `drive_root=`, so assert `kwargs["drive_root"] ==
   str(env)` and `kwargs["mount_path"] == "/workspace"`. Fix the fixture
   docstring at `:113-117` to match.

## Files in scope (touch ONLY these)

- `mini_ork/runtime/sandbox.py`
- `mini_ork/runtime/agent_workspace.py`
- `mini_ork/runtime/backends/docker.py` (`_factory` only)
- `mini_ork/runtime/backends/microvm.py` (`_factory` only)
- `tests/unit/test_spawn_workspace_isolation_py.py` (the one assertion + docstring)
- `tests/unit/test_workspace_backend_catalog.py` (new)

## Out of scope

- Reapers, `sandbox-gc`, `kill_run`, the session-id extraction (that is epic A2).
- Anything under `mini_ork/remote/` (epic B).

## Run rules

- Never rebase, merge, reset or stash during the run. The diff is the tree delta.
- Run tests with `python3.11 -m pytest`. In a worktree, `bin/mini-ork` runs MAIN.
  Use `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -m mini_ork.cli.main …`
  for any CLI read.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_workspace_backend_catalog.py \
  tests/unit/test_sandbox_protocol.py tests/unit/test_sandbox_wiring.py \
  tests/unit/test_spawn_workspace_isolation_py.py tests/unit/test_workspace_session.py \
  tests/unit/test_docker_workspace.py tests/unit/test_microvm_workspace.py \
  tests/unit/test_remote_workspace.py tests/unit/test_path_map_wiring.py \
  tests/unit/test_workspace_conformance.py
ruff check mini_ork/runtime tests/unit/test_workspace_backend_catalog.py
```

## Acceptance (each bullet has its own test in `test_workspace_backend_catalog.py`)

- A backend registered under a NEW name (`"plugbox"`, factory `**kw`) is
  selected by `MO_SANDBOX_BACKEND=plugbox` through `resolve_agent_workspace`
  and through `resolve_spawn_workspace`. It receives `image`, `drive_root`,
  `mount_path="/workspace"` and `env`, and `exec_cwd == "/workspace"`. No
  edit to `agent_workspace.py` is needed to add it.
- `register_lazy_workspace_backend("lazybox", "<tmp module>")` makes
  `get_workspace("lazybox")` import the module on first call (prove it with a
  tmp module on `sys.path`). Before that call the module is absent from
  `sys.modules`.
- A catalog entry whose module fails to import raises `RuntimeError` naming
  the backend. An unregistered, uncatalogued name raises `ValueError` whose
  message lists `known_workspace_backends()`.
- `known_workspace_backends()` ⊇ `{"local", "docker", "microvm", "remote"}`
  in a fresh interpreter in which none of the backend modules has been
  imported (subprocess check).
- **Default path byte-identical:** with `MO_SANDBOX_BACKEND` unset, a fresh
  interpreter that calls `resolve_agent_workspace("/x")` returns
  `(None, "/x")` and has imported none of `mini_ork.runtime.backends.*`.
- The `docker` factory given `env={"MO_SANDBOX_IMAGE": "img:scoped"}` and an
  `os.environ` without it builds with image `img:scoped`. With no `env=` it
  reads `os.environ` exactly as before. Same for `microvm`, with the SDK
  import monkeypatched so no microVM boots.
- `git grep -n 'in ("microvm", "docker", "remote")' mini_ork/` returns nothing.

## Review bar

Rejected unless:
- every Acceptance bullet has its own test, driving the production resolver
  and not only the new helpers;
- `register_lazy_workspace_backend` and `known_workspace_backends` have a
  production caller (`get_workspace` uses the catalog; A2 and B will consume
  `known_workspace_backends`; note that in a code comment, not a TODO);
- the full verification command is green, and the pre-existing tests in it
  pass unmodified, except the one assertion named in requirement 4.
