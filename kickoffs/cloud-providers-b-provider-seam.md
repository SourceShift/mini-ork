# cloud-providers B — Node-provider seam in RemoteWorkspace (+ `static` provider)

Part of `docs/architecture/cloud-sandbox-providers.md` (epic **B**). Depends on
the merged contract `mini_ork/remote/providers/base.py` and on epic A2 (the
remote marker reaper now lives in `backends/remote.py`). Single deliverable:
`--placement remote` obtains its node through a node provider, so a provider
can *create* a node per run and *destroy* it at run end. Today's static nodes
become the `static` provider, and the default path is byte-identical.

## Goal

With `provider: <name>` in the environment profile (or `MO_NODE_PROVIDER`),
the remote workspace:
- acquires the run's node from that provider on `up()`;
- talks to it over the unchanged node-agent protocol, adding the provider's
  auth headers to every request;
- shares it across every process of the run;
- has it destroyed at run end, on `kill_run`, and by `sandbox-gc`.

With no provider configured, nothing changes.

## Why (traced 2026-10-13)

- `RemoteWorkspace._factory` (`backends/remote.py` ~line 1702) resolves a
  node eagerly through `select_node` (explicit `node=` → `MO_NODE_URL` →
  `config/nodes.yaml`). Nothing can create a node on demand or delete one.
- Teardown paths build a fresh workspace just to tear down:
  - `workspace_session.release_remote_session` (`_factory(env=…)` then
    `down()`)
  - the remote marker reaper used by `kill_run` (A2)

  A provider that created a node inside the constructor would create a new
  sandbox *during teardown*.
- `RemoteWorkspace._request` (~line 444) is the single HTTP call site. It
  adds only `Authorization`. Daytona's preview proxy additionally requires
  `x-daytona-preview-token`.
- `RemoteWorkspace._token()` prefers `os.environ[self._token_env]`. For a
  provider-minted node, an ambient `MO_NODE_TOKEN` (set for some static node)
  would override the node's real token.
- `nodes doctor` (`cli/nodes.py` `_checks`) probes `/v1/health` before
  `up()`, so a node that does not exist yet fails check 1.

## Requirements

1. **`mini_ork/remote/providers/static.py`** (new): `StaticProvider` with
   `name = "static"`.
   - `create(...)` returns the node `select_node` would pick, using the same
     env the factory uses today. The profile's `node:` maps to `MO_NODE`. The
     result is a `NodeEndpoint(handle="", provider="static", …)` carrying
     `token_env`.
   - `attach` returns `None`. `release` is a no-op. `reap` returns `[]`.
   - Registered at import via `register_node_provider("static", StaticProvider)`.
     The catalog entry already exists in `base._LAZY_PROVIDERS`.
2. **Profile fields** (`mini_ork/remote/environments.py` + `schemas/environment.schema.json`):
   - `provider` (string, default `""`) and `provider_options` (object,
     free-form, default `{}`; each provider validates its own keys) on
     `Environment`, parsed in `_to_environment`.
   - `to_dict` emits them **only when set**, so the default-profile
     byte-equality test stays green.
   - Schema: add both properties, plus an optional `resources.disk` (number or
     string).
3. **Provider-aware factory** (`backends/remote.py` `_factory`):
   - `name = provider_name_for(profile, src)`.
   - If `name == "static"`, or an explicit `node=` kwarg was passed: today's
     code path, unchanged.
   - Otherwise construct `RemoteWorkspace` with a placeholder `_NodeRef`
     (empty `url`) plus new kwargs `node_provider=name` and `provider_env=dict(src)`.
     **Construction never contacts the provider.** The `image` requirement is
     waived when the provider path is taken. The provider chooses the image
     from the profile; pass `image=profile.image or src.get("MO_SANDBOX_IMAGE")
     or "provider-default"`.
4. **Lazy node resolution** (`RemoteWorkspace`):
   - `_NodeRef` gains `headers: Mapping[str, str] = {}` (a NamedTuple field
     with a default, so existing 4-arg constructions keep working).
     `_request` adds every header to every request.
   - New public `ensure_node(*, create: bool) -> bool`:
     - no-op `True` when `self._node.url` is set
     - `create=True` → `acquire_run_node(get_node_provider(name), run_id=…,
       run_dir=self._run_dir, profile=self._profile, env=…,
       engine_root=self._engine_root)`
     - `create=False` → `attach_run_node(self._run_dir, env)`, returning
       `False` when nothing is recorded

     On success it swaps in a `_NodeRef` built from the `NodeEndpoint`
     (headers included) and marks the token as provider-owned. A
     `ProviderUnavailableError` becomes `RemoteUnavailableError` (the
     existing `remote_unavailable` failure class).
   - `up()` calls `self._setup_step("provision", lambda:
     self.ensure_node(create=True))` first, when `node_provider` is set.
   - `_request` calls `ensure_node(create=False)` when the url is empty. If
     that returns `False` it raises `RemoteUnavailableError("no <provider>
     node recorded for run <id>")`, except that `down()` treats "no node" as
     nothing to delete and returns quietly.
   - `_token()`: for a provider-owned token, return the endpoint's token and
     never read `os.environ`.
5. **Markers and teardown:**
   - `workspace_session._node_locator`: for a provider-backed workspace, also
     record `"provider"` and `"handle"`. Never a token or a header value.
   - `workspace_session.release_remote_session`: after the existing `ws.down()`,
     call `release_run_node(run_dir, env)` (best-effort). That destroys the
     provider's node; static nodes have no record, so it is a no-op.
   - The remote marker reaper (A2's `_reap_remote_marker` in
     `backends/remote.py`): when the marker names a non-static `provider`,
     build the workspace through the provider path (empty url, lazy attach
     via the run-dir record) instead of the marker's url/token_env. Then
     `kill_all` + `down()` as today, then `release_run_node(run_dir, env)`.
6. **`nodes doctor`** (`cli/nodes.py`):
   - when the workspace has a `node_provider`, add check `0. provision`
     (`ws.ensure_node(create=True)`, report the provider and handle) before
     check 1, and call `release_run_node(run_dir, env)` in the `finally`;
   - check 9's "session down" stays as is;
   - `where` is read after provisioning.

## Files in scope (touch ONLY these)

- `mini_ork/remote/providers/static.py` (new)
- `mini_ork/remote/environments.py`, `schemas/environment.schema.json`
- `mini_ork/runtime/backends/remote.py` (`_NodeRef`, `__init__` kwargs, `_request`, `_token`, `up`, `down`, `ensure_node`, `_factory`, `_reap_remote_marker`)
- `mini_ork/runtime/workspace_session.py` (`_node_locator`, `release_remote_session`)
- `mini_ork/cli/nodes.py` (`doctor`/`_checks` provision + release only)
- `tests/unit/test_remote_provider_seam.py` (new), `tests/unit/test_environments.py` (extend)

## Out of scope

- Any real cloud provider (Daytona is epic C, built separately against this
  seam). Tests use a fake provider registered via `register_node_provider`.
- `sandbox_reaper.py` / `sandbox-gc` (A2 already sweeps node providers).

## Run rules

- Never rebase, merge, reset or stash during the run. The diff is the tree delta.
- `python3.11 -m pytest`. For CLI reads in a worktree use
  `MINI_ORK_ROOT=$PWD PYTHONPATH=$PWD python3.11 -m mini_ork.cli.main …`.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_remote_provider_seam.py tests/unit/test_environments.py \
  tests/unit/test_remote_workspace.py tests/unit/test_remote_live_and_kill.py \
  tests/unit/test_remote_reattach.py tests/unit/test_remote_tree_sync_e2e.py \
  tests/unit/test_remote_node_hardening.py tests/unit/test_workspace_session.py \
  tests/unit/test_nodes_doctor.py tests/unit/test_node_providers_contract.py \
  tests/unit/test_workspace_conformance.py tests/unit/test_sandbox_teardown_registry.py
ruff check mini_ork/remote mini_ork/runtime mini_ork/cli/nodes.py
```

## Acceptance (each bullet has its own test in `test_remote_provider_seam.py`)

The fake provider's `create` starts the **in-process node-agent fixture** that
`test_remote_live_and_kill.py` / `test_nodes_doctor.py` already use. It
returns its URL with `headers={"x-fake-provider": "h"}` and a freshly minted
token. Real HTTP, no cloud.

- **End to end through the seam:** with a profile `provider: fake`,
  `get_run_session(run_id, "remote", env=…)` provisions exactly one node,
  syncs the target tree, and runs a spawn. Every request the node-agent
  receives carries `x-fake-provider: h` (assert on the server side). The
  run-dir record names the handle.
- **One node per run:** a second `_factory(env=…)` + `up()` in the same run
  dir attaches. `create` was called once.
- **Construction never provisions:** `_factory(env=…)` and
  `release_remote_session` on a run with no record call neither `create` nor
  `attach`.
- **Run end destroys the node:** `release_remote_session` deletes the node
  session on the node-agent, then calls the provider's `release(handle)` and
  removes the record.
- **`kill_run` destroys the node:** for a provider-backed run, `kill_run`
  (the real entrypoint) kills the procs, deletes the session and releases the
  node.
- **Token isolation:** with an ambient `MO_NODE_TOKEN=wrong` in `os.environ`,
  a provider-backed workspace still authenticates with the provider's token.
- **Failure class:** a provider whose `create` raises
  `ProviderUnavailableError` makes `up()` raise `RemoteUnavailableError`, and
  the executor's provisioning path reports `failure_class=remote_unavailable`.
- **`nodes doctor --env <fake profile>`** passes 0–9 and leaves neither a
  session nor a recorded node behind.
- **Default path byte-identical:** with no provider anywhere, `_factory`
  builds the same `RemoteWorkspace` as before (same node, token_env, image
  requirement). Every pre-existing test in the verification command passes
  unmodified. `Environment.to_dict()` for the shipped default profile is
  unchanged.

## Review bar

Rejected unless every Acceptance bullet drives the production entrypoints
(`get_run_session`, `release_remote_session`, `kill_run`, `nodes doctor`)
against the real in-process node-agent, not mocks of `RemoteWorkspace`. No
token or header value may appear in any file the run writes (assert by
grepping the run dir in the end-to-end test). Every new public symbol must
have a production caller.
