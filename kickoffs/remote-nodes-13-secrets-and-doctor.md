# remote-nodes-13 — Secrets injection and `mini-ork nodes doctor`

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D6** (secrets resolved locally, injected per process, never at rest
remotely) and the security posture. Depends on epic 06 (remote backend) and
epic 12 (profiles list the allowed secret names).

## Goal

1. A remote spawn receives **exactly** the secrets its lane needs. The names
   must be permitted by the run's environment profile. The values exist only
   in the remote process's env: never on disk, never in node-agent logs, and
   never in stored or streamed output.
2. One command tells the operator whether a node and environment can really
   run the configured lanes, and how to fix whatever is missing.

## Why (traced 2026-10-01)

- The container allowlist forwards whole families: `ANTHROPIC_*`, `CLAUDE_*`,
  `OPENAI_*`, `CODEX_*` and any `*_API_KEY` (`runtime/backends/_workspace_env.py:51-75`).
- The secret store is exported into the whole process tree
  (`mini_ork/cli/main.py:797-814`).
- `core.dispatch` re-seeds env from a full snapshot (`dispatch/core.py:294`),
  so an ambient `ANTHROPIC_API_KEY` survives even when a lane "unsets" it
  (`providers.py:518`, `:1042`).

  Sending that to a cloud VM would ship every key to every node.
- `anthropic`-native lanes depend on the operator's ambient Claude Code login
  (`providers.py:43-45`, `:277-280`). On macOS that login lives in the
  Keychain and cannot travel. A remote node needs `CLAUDE_CODE_OAUTH_TOKEN`
  (from `claude setup-token`) or `ANTHROPIC_API_KEY`.
- There is no preflight. Lane credential failures are notoriously opaque:
  - an invalid key on an `anthropic-compat` lane hangs the full timeout with
    in=0/out=0 tokens
  - a fallback chain reports only its last lane's error

## Requirements

1. **Per-lane secret scoping.** Add `mini_ork/remote/secrets_scope.py` with:
   - `lane_secret_names(spec: ProviderSpec) -> set[str]`. These are the env
     keys the provider builder put into `spec.env` that match the secret
     pattern (`*_KEY`, `*_TOKEN`, `*_SECRET`, `ANTHROPIC_AUTH_TOKEN`), plus
     `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` for `anthropic`-native
     lanes.
   - `scoped_env(spec, profile, *, store) -> dict`, which resolves values
     from the local secret store (`mini_ork/dispatch/secrets.py`).

   The `profile.secrets` field must name a lane's secret. If it does not,
   raise `SecretNotPermittedError(lane, name)` with the profile path in the
   message. Under `remote`, `_spawn_in_workspace` builds the sandbox env as
   follows:
   1. the mapped non-secret env (epic 03)
   2. **plus** `scoped_env(...)`
   3. and nothing from the ambient allowlist families

   `docker`/`local` keep the allowlist behaviour unchanged.
2. **Node-agent handling** (`mini_ork/remote/node_agent/procs.py`):
   - Env values are held in memory only (keys only in `<pid>.json`, epic 05).
   - Every buffered stdout/stderr chunk is redacted by replacing each secret
     value of length ≥ 8 with `***` before it is written to disk or streamed.
   - The node-agent's own logs never contain request bodies.
   - The client also redacts chunks before writing the live file (epic 09),
     as defense in depth.
3. **Egress allowlist feed.** The hosts of the lane base URLs
   (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` or the provider default) are
   added to the session proxy allowlist when the profile's network is
   `allowlist` (epic 12).
4. **Subcommand `mini-ork nodes`**: `ls`, `ping <node>` and `doctor [--env
   <name>] [--recipe <r>] [--no-llm]`. Register it in `_NATIVE_MODULE_SUBS`
   (`cli/main.py:40`) and update the exact-set guard
   (`tests/unit/test_native_dispatch_py.py`) in the same change. `doctor`
   prints an ordered checklist with a fix hint per failure:
   1. node reachable
   2. token accepted
   3. engine sha present or uploaded
   4. image prepared
   5. session up
   6. `claude`/`opencode`/`python3 -c "import mini_ork"` inside
   7. **per-lane auth smoke**: for each lane the recipe's roles resolve to
      (via the existing lane resolution), one ~20-token prompt inside the
      session with the scoped env, each with a 30 s timeout so the
      bad-key-hangs failure is reported as "auth: no response in 30 s — check
      the key", not a hang
   8. network level effective
   9. session down

   `--no-llm` skips step 7. Exit code 0 means all checks passed.
5. Docs: append "Credentials for remote nodes" to `docs/operator/remote-nodes.md`.
   It covers `claude setup-token`, which secrets go in the profile, and the
   "keys never at rest remotely" guarantee and its limits: a malicious agent
   process can still read its own env.

## Files in scope (touch ONLY these)

- `mini_ork/remote/secrets_scope.py` (new)
- `mini_ork/dispatch/core.py` (`_spawn_in_workspace` remote env assembly only)
- `mini_ork/remote/node_agent/procs.py` (redaction)
- `mini_ork/cli/nodes.py` (new) + `mini_ork/cli/main.py` (`_NATIVE_MODULE_SUBS` entry)
- `tests/unit/test_native_dispatch_py.py` (exact-set guard), `tests/unit/test_secrets_scope.py` (new), `tests/unit/test_nodes_doctor.py` (new)

- `mini_ork/runtime/backends/remote.py` — `RemoteWorkspace._session_payload` only
  (requirement 3: the lane hosts join `allow_domains` there)

## Notes from epics 10 and 14 (read before implementing)

These seams already exist on main. Reuse them; do not re-implement them.

- `RemoteWorkspace.up()` already emits `remote.setup.step` for `health`,
  `engine`, `image_prepare`, `session_up`, `initial_sync` and `post_sync`, and
  it enforces `max_sessions`. Doctor steps 1–5 and 9 drive the real workspace
  (`mini_ork.runtime.backends.remote._factory(env=...)`, then `up()` and
  `down()`) and report from those step events. Do not hand-roll the HTTP
  calls.
- `--env <profile>` binds node, image, resources and network in `_factory`
  (`MO_NODE_ENV` → `environments.load_profile`). `doctor --env` reuses that
  path.
- A registry node's token is read from its own `token_env` (`Node.token_env`,
  passed to the workspace). Never assume `MO_NODE_TOKEN`.
- Lane kind: `providers._lane_kind(model)` reads the registry. The lane alias
  resolves via `llm_dispatch.resolve_lane_family`.
- `dispatch_node` publishes `MO_NODE_ID`, `MO_NODE_ROLE`, `MO_NODE_ATTEMPT` and
  `MO_INPUT_HASH` per node. These are not secrets, and the remote env assembly
  must keep them, because the reattach key and the placement roles depend on
  them. `MO_NODE_TOKEN*` must stay stripped (`isolated_env`).
- Producer check: epics 02, 03, 05, 09, 10, 11 and 14 each shipped code that
  READ a value nothing in production WROTE. For every env var, kwarg or field
  the new code reads, name its production producer in the commit message.

## Out of scope

- A credential *proxy*, where keys never enter the sandbox at all. That is a
  hardening follow-up once v1 works.
- Git or GitHub credentials. They are never sent; push stays local (D1).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_secrets_scope.py tests/unit/test_nodes_doctor.py tests/unit/test_native_dispatch_py.py && make lint
```

## Acceptance

- A remote spawn for a `glm` `anthropic-compat` lane carries only that lane's
  `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`. Ambient `OPENAI_API_KEY`
  and `ANTHROPIC_API_KEY` in the control-plane env are absent (captured-request
  assertion).
- A lane secret missing from `profile.secrets` raises
  `SecretNotPermittedError` before any network call.
- A proc that echoes its token stores and streams `***`. The `.out` file on
  the node-agent never contains the value.
- `doctor --no-llm` against the in-process node-agent passes all non-LLM
  checks. With a fake lane that never answers, step 7 reports the 30 s auth
  hint and exits non-zero.

## Review bar (added after epics 02–04 were reviewed)

Each earlier epic passed its gate and still failed review for the same reason:
the acceptance tests that drive the REAL seam were never written, so unwired or
broken code looked green. This epic is rejected unless:

- every Acceptance bullet above has its own test, and that test exercises the
  production path (the executor / dispatch / CLI entrypoint), not only the new
  module in isolation;
- every new public function or class has at least one production caller
  (`git grep` for it outside its own module and tests);
- nothing is gated only by a skip: a daemon- or network-gated test must run in
  this environment (Docker is available via colima; only `/Volumes/docker-ssd`
  is bind-visible), or the epic says why it cannot;
- the default path (feature env unset) is proven byte-identical by a test.
