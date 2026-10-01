# remote-nodes-12 — Environment profiles

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
"Configuration" section. Depends on epic 05 (node-agent). The equivalent of
Claude Code's cloud environments: a saved, named description of where and how
a run's nodes execute.

## Goal

A run selects a named environment. The environment decides:
- which node host is used
- which image, with a setup script **cached** as an image layer so later
  sessions start warm
- which non-secret env vars are set
- which secret *names* the run may use (resolved in epic 13)
- which network level applies
- the resources

Provisioning progress is visible as events, so a client can render a live
setup checklist.

## Why

- Today an isolated run is configured by loose env vars
  (`MO_SANDBOX_IMAGE`/`_CPU`/`_MEMORY`, `runtime/backends/docker.py:284-299`).
  There is no named profile, no setup step and no network policy: `docker run`
  sets no `--network` (`docker.py:117-140`).
- A target repo's tests need its toolchain. Installing that toolchain on every
  session would cost minutes per run. Caching it by hash makes the second run
  fast.
- `/api/agent-profiles` and `/api/workspaces` return empty placeholders
  (`web/routes/agent_server.py:246-256`, `:358-367`). This epic gives them
  real data to serve later; serving them is out of scope here.

## Requirements

1. **Schema and loader.**
   - Add `schemas/environment.schema.json` and `mini_ork/remote/environments.py`.
   - Load `config/environments/<name>.yaml` from `MINI_ORK_HOME/config`
     (shadow), falling back to `MINI_ORK_ROOT/config` (template). Merge per
     key, not by file replacement. Validate against the schema; an unknown
     key is an error.
   - Fields:
     - `node` (a name from `nodes.yaml`, epic 06)
     - `image`
     - `setup` (shell script string) and `setup_timeout_s` (default 900)
     - `env` (map; reject any key that looks like a secret: `*_KEY`,
       `*_TOKEN`, `*_SECRET`, `*PASSWORD*` — those belong in `secrets`)
     - `secrets` (list of names)
     - `network`: `full` or `allowlist`
     - `allow_domains` (list)
     - `resources`: `{cpus, memory}`
   - Ship `config/environments/default.yaml.example`.
2. **Setup cache, node-agent side.** `POST /v1/images/prepare {base_image,
   setup, platform}` works as follows:
   - Compute `key = sha256(base_image_digest + setup + platform)`.
   - If `mo-env:<key>` exists, return it.
   - Otherwise run the base image as root with `/workspace/target` empty.
     Run `setup` with `bash -euo pipefail` under the timeout, and stream its
     output as proc output (epic 05 procs).
   - On success, `docker commit` → `mo-env:<key>`. On failure, return the
     log tail and leave no tag.
   - `POST /v1/sessions` accepts `image=mo-env:<key>`.

   Setup that needs the repo (`pip install -e /workspace/target`) is
   expressed as a separate `post_sync` script. It runs after the initial
   tree upload, every session, and is not cached. Document the difference.
3. **Network levels, node-agent side.**
   - `full` uses the default bridge.
   - `allowlist`:
     - Create an internal Docker network `mo-int-<session>` with no egress,
       plus an egress proxy container (tinyproxy with a domain allowlist file)
       attached to both that network and the bridge.
     - Set `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` in the session env.
     - The proxy allows only `allow_domains` plus the lane endpoints the run
       needs. Epic 13 supplies those; until then the allowlist is
       `allow_domains` only.
   - Session down tears down both the network and the proxy.
4. **Provisioning events.** The client emits `remote.setup.step` events `{step:
   health|engine|image_prepare|session_up|initial_sync|post_sync, status:
   start|ok|fail, ms, detail}` in order. These are the data for a live
   "setting up" checklist.
5. **Resources.** `cpus`/`memory` map to `docker run --cpus/--memory` on the
   session container.

## Files in scope (touch ONLY these)

- `schemas/environment.schema.json` (new)
- `mini_ork/remote/environments.py` (new)
- `mini_ork/remote/node_agent/images.py` (new) + `sessions.py` (network, resources, image)
- `config/environments/default.yaml.example` (new)
- `tests/unit/test_environments.py` (new)
- `docs/operator/remote-nodes.md` (append "Environment profiles")

## Out of scope

- Secret resolution and injection (epic 13). This epic only validates names.
- The `--env` CLI flag and placement routing (epic 14).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_environments.py tests/unit/test_node_agent.py && make lint
```

## Acceptance

- Loader cases:
  - A shadow file overriding only `resources` keeps the template's `image`
    and `setup`.
  - An `env` key `OPENAI_API_KEY` is rejected with a "put it in secrets"
    message.
  - An unknown field fails schema validation.
- Setup cache, run with a fake Docker runner: the same `(image, setup)`
  prepares once, and the second call returns the cached tag without running
  setup. A failing setup leaves no tag and returns the log tail.
- `allowlist` creates the internal network and the proxy, and the session env
  carries the proxy vars. Session down removes both (fake-runner argv
  assertions). Daemon-gated live test: `curl https://example.com` fails and
  `curl https://pypi.org` succeeds when only pypi is allowed.
- The provisioning events arrive in the documented order on a successful up.

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
