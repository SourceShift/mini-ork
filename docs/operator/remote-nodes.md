# Agent node — image and engine layout

How to build the mini-ork agent-node container, how engines are pinned per
host, and how the node-agent (epic 05) mounts them. Subsequent epics in the
`remote-nodes` set append H2 sections under this same document.

## Agent image

A single Docker image, `mini-ork/agent-node`, runs every CLI-backed lane
(`claude`, `opencode`, optional `@openai/codex`). The image bakes only the
toolchain the agent needs; the mini-ork engine itself is mounted at runtime
so the same image stays usable across engine upgrades.

### Build

The image is built from `docker/agent-node/Dockerfile`:

```bash
# Single-arch, load into the local daemon (default dev loop):
bash docker/agent-node/build.sh

# Pin CLI versions at build time (recommended for CI / release builds):
CLAUDE_CODE_VERSION=1.0.55 \
OPENCODE_VERSION=0.6.4 \
bash docker/agent-node/build.sh --build-arg WITH_CODEX=1
```

The build script tags it as `mini-ork/agent-node:<git-sha>` and
`mini-ork/agent-node:latest`, where `<git-sha>` is the engine's current
`HEAD` (see [docs/architecture/remote-nodes.md](../architecture/remote-nodes.md)
D8 for why the image tracks engine sha). Multi-arch builds (`--multiarch`)
target `linux/amd64` and `linux/arm64` (Hetzner CAX and Oracle A1 are
arm64); they require a `docker buildx` builder named `multiarch` with QEMU
binfmt.

### Smoke

After building, `smoke.sh` runs the image, attaches the local mini-ork
checkout as the engine (read-only at `/opt/mini-ork`), and asserts the
runtime contract:

```bash
bash docker/agent-node/smoke.sh mini-ork/agent-node:latest "$PWD"
```

Checks: non-root user, `claude --version`, `opencode --version`,
`git --version`, `python3 -c "import mini_ork, yaml"`, and
`/workspace/target` writable. Each failing check prints a clear message and
exits non-zero.

### Runtime contract

- Non-root user `agent` (uid 1000). Claude Code refuses
  `bypassPermissions` when run as root, and dispatch sets it; this is not
  hygiene, it is load-bearing.
- `ENV PYTHONPATH=/opt/mini-ork PATH=/opt/mini-ork/bin:$PATH MO_REMOTE_NODE=1`
  in the image. `MO_REMOTE_NODE=1` is the flag that disables recursive child
  spawn.
- `ENTRYPOINT ["tini", "--"]` + `CMD ["sleep", "infinity"]` keeps the
  container alive for the node-agent to `docker exec` into. CLI invocations
  are launched via `exec`, not via ENTRYPOINT.
- Never bake credentials. Auth arrives per process (epic 13).

## Engine layout

Engines are pinned to a git sha so a sha mismatch between control plane and
node host is impossible to miss. On every node:

```
/srv/mini-ork/
├── engines/
│   └── <git-sha>/              # one tree per engine version
│       ├── mini_ork/            # exported from MINI_ORK_ROOT at <sha>
│       ├── recipes/
│       ├── bin/
│       └── schemas/
└── runs/
    └── <run_id>/
        ├── target/             # rw → /workspace/target
        ├── run/                # rw → /workspace/run
        ├── home/               # rw → /workspace/home (agent HOME)
        └── mo-home/            # ro  → /workspace/mo-home
```

The node-agent (epic 05) mounts the engine matching the control plane's
read-only at `/opt/mini-ork`. On a sha mismatch, it triggers an automatic
engine upload — a tarball of `MINI_ORK_ROOT` HEAD at the new sha — so skew
is never silent (D8).

To stage a new engine version on a node:

```bash
# From the mini-ork engine checkout, at the desired sha:
git checkout <git-sha>
git archive --format=tar HEAD | \
    ssh node-host "sudo tar -x -C /srv/mini-ork/engines/$(git rev-parse --short=12 HEAD)"
```

Per-run dirs under `/srv/mini-ork/runs/<run_id>/` are mounted at
`/workspace/{target,run,home,mo-home}` per D4 in
`docs/architecture/remote-nodes.md`. Mount modes: target/run/home are rw,
mo-home is ro.
## Running a node-agent

The node-agent is the data-plane service on the remote VM. It holds one
session (container + `/srv/mini-ork/runs/<run_id>/…` dirs) per run and runs
each agent spawn as a detached process whose output is buffered to disk, so a
dropped connection re-attaches from a byte offset.

```bash
export MO_NODE_TOKEN="$(openssl rand -hex 32)"     # the control plane needs the same value
mini-ork node-agent --bind 100.x.y.z --port 7091   # tailnet address; loopback is the default
curl -s http://100.x.y.z:7091/v1/health            # the only route that needs no token
```

- **Auth:** every route except `/v1/health` requires
  `Authorization: Bearer $MO_NODE_TOKEN` (`--token-env` names a different
  variable).
- **Bind policy:** loopback and tailnet (`100.64.0.0/10`) binds start as-is.
  Any other address requires `--tls-cert` and `--tls-key`, or the launcher
  refuses to start.
- **Runtimes:** `--runtime docker` (default) runs each session in a container
  from the agent image (see "Agent image" above). `--runtime host` runs procs
  directly on the VM. That mode has no isolation and exists for tests and
  trusted single-tenant boxes.
- **State and retention:** session dirs live under `/srv/mini-ork` (falling
  back to `/tmp/mo-node-agent-state` when that path is not writable). A
  deleted session keeps its dirs for `--retain-hours` (default 24) before the
  background reaper removes them and any leftover `mo.sandbox=1` container.
- **Env and secrets:** a spawn request carries env values. They reach the
  process through `docker exec -e KEY` and the CLI's own environment, so they
  never appear in argv. Only the keys are written to `.procs/<pid>.json`.

## Selecting the remote backend

The `Workspace` backend resolver (`mini_ork.runtime.agent_workspace`) learns
a fourth selector, `remote`, alongside the existing `local`, `docker`, and
`microvm`. With `MO_SANDBOX_BACKEND=remote` (or `resolve_spawn_workspace("remote", …)`),
mini-ork drives the node-agent over HTTP instead of forking a local subprocess or
launching a container on the control-plane host. Picking it is a config change;
no caller has to branch on the backend.

### What you set

The runtime needs three things to pick a node:

1. **The node URL and bearer token.** Two equivalent shapes:
   - One-off, bypass the registry entirely:

       ```bash
       export MO_NODE_URL="http://100.64.0.5:7091"
       export MO_NODE_TOKEN="$(pass show node-agent/staging-a)"
       ```
   - Named, looked up in the registry:

       ```bash
       # config/nodes.yaml (template at $MINI_ORK_ROOT, live at $MINI_ORK_HOME):
       nodes:
         staging-a:
           url: http://100.64.0.5:7091
           token_env: MO_NODE_TOKEN_STAGING_A    # name only — value lives in env
           max_sessions: 4
         # …export MO_NODE_TOKEN_STAGING_A="$(pass show node-agent/staging-a)"
         export MO_NODE=staging-a
       ```

   The live file at `$MINI_ORK_HOME/config/nodes.yaml` MERGES PER NODE over the
   template (a live entry with the same name keeps the template's URL and only
   overrides the fields it sets; a live entry with a NEW name is appended).
   This avoids the "tabs of providers.yaml drops lanes" trap — the committed
   template's defaults keep flowing through.

2. **The session image.** Required by `POST /v1/sessions`. Either pass
   `image=` to the resolver / factory, or set `MO_SANDBOX_IMAGE`. Same env var
   the docker backend already reads.

3. **A run id.** Either `MINI_ORK_RUN_ID` (the harness already sets it) or the
   resolver generates one.

A working `config/nodes.yaml.example` ships under `$MINI_ORK_ROOT/config/` —
copy it to `$MINI_ORK_HOME/config/nodes.yaml` and edit. Tokens live in env only.

### What you observe

- One `RemoteWorkspace.up()` per run — `POST /v1/sessions` is idempotent on
  `run_id`, so a re-used session is returned, not recreated.
- An engine sha mismatch triggers a one-shot `PUT /v1/engines/{sha}` (a git
  bundle of `$MINI_ORK_ROOT` HEAD). A second `up()` on the same sha uploads
  nothing. A dirty engine tree refuses unless `MO_REMOTE_ALLOW_DIRTY_ENGINE=1`
  — a sha must name an exact tree.
- Each `Workspace.exec` / `spawn` maps onto the run-scoped URL scheme the
  node-agent actually ships: `POST /v1/sessions/{run_id}/procs`,
  `GET /v1/sessions/{run_id}/procs/{pid}/stream`, `POST /v1/sessions/{run_id}/exec`,
  `PUT/GET /v1/sessions/{run_id}/files?root=run`,
  `DELETE /v1/sessions/{sid}`.
- `put` / `get` round-trip through the node-agent's tar files API. `put`
  returns an in-workspace path (`run/put-<hex>.txt`) so a follow-up `get` reads
  it back through the same root.
- `RemoteWorkspace.spawn` keeps stdout and stderr SEPARATE (matches the host
  `spawn_local` contract and the docker backend's behaviour). It returns
  `(rc, stdout, stderr)` with the conventional rc=124 on timeout and rc=127 on
  a missing argv[0].

### What you don't get

- **No silent host fallback.** When the node-agent is unreachable after
  `MO_REMOTE_HTTP_RETRIES` attempts (default 5), `RemoteWorkspace.up()` raises
  `RemoteUnavailableError`. The dispatch layer maps that exception to
  `failure_class=remote_unavailable`. There is no codepath where a transport
  failure degrades into a host subprocess. A dirty engine tree is the only
  other raise path; `MO_REMOTE_ALLOW_DIRTY_ENGINE=1` is the explicit bypass for
  test setups.
- **No automatic node selection.** If neither `MO_NODE_URL`+`MO_NODE_TOKEN`
  nor `MO_NODE` resolves to a registry entry, `select_node` raises loudly. This
  is by design — auto-picking a node would let a config typo route every
  dispatcher's run to the wrong host.
- **No env leakage.** `spawn(env=...)` filters through the same `_container_env`
  allowlist the docker and microvm backends share (`mini_ork.runtime.backends._workspace_env`).
  A non-allowlisted ambient key (e.g. `ZZ_AMBIENT_SENTINEL`) does not reach the
  child; every `_RUN_CONTRACT_KEYS` member the caller sets DOES arrive. The
  `local` backend is exempt from this filter — it is the host-parity backend
  and passes the full env.

### Verifying

The conformance suite under `tests/unit/test_workspace_conformance.py`
parameterises the same six cases across `local` and `remote` (the docker case
is daemon-gated; the in-process node-agent test uses `--runtime host` so no
daemon is required). The unit suite at `tests/unit/test_remote_workspace.py`
covers the retry policy, idempotent engine upload, dirty-tree refusal, and
default-path byte-identical proof (`MO_SANDBOX_BACKEND` unset never loads
`mini_ork.runtime.backends.remote`).

```bash
python3.11 -m pytest -q \
    tests/unit/test_workspace_conformance.py \
    tests/unit/test_remote_workspace.py \
    tests/unit/test_sandbox_protocol.py
```

## What gets synced

Your local checkout stays the source of truth. The node-agent holds a replica
at `runs/<run>/target`, and the two are synced with git at dispatch boundaries.
Every diff is computed by git (`mini_ork/remote/tree_sync.py`).

- **Which checkout:** the run's pinned target (`run_profile.json` `roots`, set
  at run start), or an explicit `MO_TARGET_CWD`. A session with neither refuses
  to start; mini-ork's own engine checkout is never used as a fallback.
- **What goes over:** the exact worktree: tracked files, uncommitted changes,
  and untracked files that `.gitignore` does not exclude.
- **Secret-named files are left out:** `.env`, `.env.*`, `*.pem`, `*.key`,
  `id_rsa*`, `id_ed25519*`, `*.tfvars`, `secrets.local.sh` and
  `.mini-ork/state.db*`. Add patterns with `MO_REMOTE_SYNC_EXCLUDE=a,b`. An
  untracked secret never leaves the laptop. A tracked one goes over at its
  committed version, never your local edits. The names left out are reported
  in a `remote.sync.excluded` event (names only, never contents).
- **First upload:** a git bundle, trying `full` (all history), then `branch`,
  then `squashed` (one commit, no history). It uses the first one under
  `MO_REMOTE_BUNDLE_MAX_MB` (default 100), and fails with the sizes if even
  `squashed` is too big. The replica checks out your HEAD, so `git status`
  there shows the same uncommitted changes as yours.
- **Before every remote spawn or check:** if your worktree changed since the
  last sync, an incremental bundle goes up.
- **After every remote agent spawn:** the replica's changes come back as
  uncommitted changes in your checkout. Your HEAD, index and stash are never
  touched. If the agent committed, you get the same content, uncommitted, plus
  a `remote.head_moved` event. Checks (verifiers) never sync back.
- **Conflict:** if you edit the checkout while a remote spawn is running, the
  sync-back refuses with `SyncConflictError`, listing the paths, and your edit
  is kept. The remote result stays fetchable at `refs/mo/remote/<run>/latest`
  for a manual merge. Edits made between spawns are not conflicts; the next
  sync-up carries them over.
- **Not supported yet:** submodules travel as gitlinks and Git LFS files as
  pointers.

## Run dir on the remote side

Whatever a remote agent reads from or writes to the run dir works the same
as locally. The mirror is **boundary-time sync**: it runs BEFORE every
remote `spawn`/`exec` (push) and AFTER every one of them (pull). It is
NOT a live tee of in-progress output — epic 09 owns that.

### What the mirror does

- **Push (local → `/workspace/run`)** before each remote call:
  - Walks the local run dir, builds a `{relpath: (sha256, size, mtime)}`
    manifest, asks the node-agent for its current manifest (one call
    with an explicit `{"root": "run"}` body), diffs the two, tars the
    changed set, and `PUT`s the body to
    `PUT /v1/sessions/{run_id}/files?root=run`.
  - Captures a push-time snapshot in memory AND writes it to
    `<run_dir>/.mo-run-mirror.json` so a control-plane restart can
    resume the pull phase correctly.
- **Pull (remote → local)** after each remote call:
  - Asks the node-agent for its CURRENT manifest, diffs against the
    push-time snapshot, fetches the tar body via
    `GET /v1/sessions/{run_id}/files?root=run`, and extracts member by
    member.
  - For each changed file: if the local sha still equals the push-time
    sha, atomic temp+rename into place with a CURRENT mtime so the
    executor's "agent wins by mtime" reader at
    `cli/execute_handlers.py:499-504` keeps its meaning. If the local
    sha differs, the local copy wins and a `remote.mirror.conflict`
    event names the file.
  - Unchanged remote files are NOT rewritten (so the executor falls
    back to stdout exactly as it does locally).

### What never crosses the wire

| Excluded | Why |
|---|---|
| `execute.log` | executor-owned; huge |
| `*.pid` | executor-owned |
| `.stop-requested` | control-plane IPC |
| `.workspace-session.json` | session bookkeeping |
| `state.db*` | the only DB writer is the control plane (D5) |
| `agent-*.live.jsonl` | epic 09 owns the live tee |

These live in `mini_ork/remote/run_mirror.py:DEFAULT_EXCLUDES`; the set
is matched on basename only so a subdir cannot bypass.

### mo-home subset

`/workspace/mo-home` is the read-only mount of a curated subset of
`$MINI_ORK_HOME`. The mirror uploads it ONCE per session, inside
`up()`:

- `config/*.yaml` (providers, agents, pricing, lane policy)
- `recipes/<MO_RECIPE>/**` when `MO_RECIPE` is set (the active recipe)

NEVER uploaded (basename glob, deny-list enforced client-side before
the tar is built):

- `state.db*`
- `secrets*`
- `auth-tokens.txt`
- `*.env`

The deny-list lives in `mini_ork/remote/run_mirror.py:DENY_LIST` and is
imported by the unit test for direct assertion — the contract is the
import surface, not a private field.

### Size caps

| Env var | Default | What it caps |
|---|---|---|
| `MO_REMOTE_MIRROR_MAX_FILE_MB` | `50` | Per-file size |
| `MO_REMOTE_MIRROR_MAX_TOTAL_MB` | `500` | Per-sync total |

An over-cap file is **skipped with an event** — `remote.mirror.skipped`
naming the file — never truncated. Over-cap files are measured via
`os.stat()` before any read, so a 50 GB log cannot poison the walk.

### Events

| Event | Payload | Emitted when |
|---|---|---|
| `remote.mirror.push` | `{files, bytes, ms}` | end of a push |
| `remote.mirror.pull` | `{files, bytes, ms}` | end of a pull |
| `remote.mirror.conflict` | `{file}` | per file where local+remote both moved |
| `remote.mirror.skipped` | `{file, size}` | per over-cap file |
| `remote.mirror.mo_home` | `{files, bytes}` | once per session, in `up()` |

Emitted via `mini_ork.observability.node_events.mo_node_emit`, so the
emitter is a silent no-op when `state.db` is missing (the unit-test
path) and never breaks a run on an observability failure.

### Locking

The mirror takes `RemoteWorkspace._session_lock` around every push and
pull. The same lock is shared with the future tree-sync path (epic 07)
so a mirror push/pull never interleaves with a tree-bundle upload on
the same session.

## Credentials for remote nodes

A remote node never receives the control plane's ambient credentials. Each
remote spawn carries exactly the secrets of the ONE lane being dispatched
(D6 in `docs/architecture/remote-nodes.md`).

### What ships to a remote spawn

- `providers.dispatch_model` knows the lane. For a remote dispatch it works
  out that lane's secrets: the secret-named keys its `providers.yaml` builder
  put into the spawn env, plus `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`
  for an `anthropic-native` lane. Values come from the local secret store
  (`secrets.local.sh`), with an exported shell variable taking precedence.
- With `--env <profile>`, every one of those secrets must be listed under the
  profile's `secrets:`. You can list either the key the spawn carries or the
  lane's `api_key_env`. An unlisted secret fails the dispatch with
  `SecretNotPermittedError`, naming the profile file, before anything is sent.
- The spawn layer then forwards only those named secrets. Every other
  secret-named key (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`) is dropped.
- Without a profile (a one-off `MO_NODE_URL` run), the lane's own secrets still
  pass and nothing else does.

Example: a `glm` lane with `kind: anthropic-compat` and `api_key_env:
GLM_API_KEY` ships `ANTHROPIC_AUTH_TOKEN` (the GLM key's value) and
`ANTHROPIC_BASE_URL`, and nothing else. The control plane's `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` and `GLM_API_KEY` itself stay local. The profile says:

```yaml
secrets: [GLM_API_KEY]
```

Git and GitHub credentials are never sent: sync and push stay local (D1).

### Claude on a node

The macOS Claude Code login lives in the Keychain and cannot be copied to a
node. Run `claude setup-token`, put the result in `secrets.local.sh` as
`CLAUDE_CODE_OAUTH_TOKEN`, and list that name in the profile's `secrets:`.
`anthropic-compat` lanes (GLM, MiniMax and other gateways) need only their
`api_key_env`.

### Egress under `network: allowlist`

The session proxy admits the profile's `allow_domains` plus the hosts of the
configured lanes' `base_url`s, and the provider defaults for lanes without one
(`api.anthropic.com` for `anthropic-native`, `api.openai.com` for
`codex-native`). Lane calls are therefore not blocked by default.

### Checking a node: `mini-ork nodes doctor`

```bash
mini-ork nodes ls                                    # registered nodes
mini-ork nodes ping hetzner-1                        # one health probe
mini-ork nodes doctor --env default --recipe code-fix
mini-ork nodes doctor --env default --lane glm --no-llm
```

`doctor` runs nine ordered checks and stops at the first failure, with a fix
hint:

1. node reachable
2. token accepted
3. engine present or uploaded
4. image prepared
5. session up (including the initial tree sync)
6. the lanes' CLIs and `import mini_ork` inside the session
7. a per-lane auth smoke
8. the network level the node applied
9. session down (verified on the node)

Step 7 sends one short prompt through the real dispatch for each lane that
`--recipe` resolves to (or each `--lane`). It uses a 30 s ceiling, so a bad
key shows up as `auth: no response in 30 s — check the key` instead of a hang.
`--no-llm` skips step 7. The exit code is 0 only when every check passed. Run
`doctor` from inside the target checkout, or set `MO_TARGET_CWD`: step 5
syncs that tree.

### Keys at rest, and the limits

- The node-agent keeps spawn env values in memory only. `.procs/<pid>.json`
  records keys, never values.
- stdout and stderr are redacted on the node before they are written or
  streamed: every value of 8 or more characters of a secret-named key becomes
  `***`. The control plane masks the live file again. Other env values (paths,
  ids) are left alone.
- Redaction is per chunk. A secret that straddles a chunk boundary, or that
  the process prints transformed (base64, split), is not masked.
- A malicious agent process can still read its own env. Scoping limits blind
  exposure (every key going to every node), not deliberate exfiltration by a
  process that runs `env | curl`. A credential proxy that keeps keys out of
  the sandbox entirely is the hardening follow-up.

## Run nodes on a cheap cloud VM

This runs a real node host on a Hetzner Cloud VM that your laptop reaches
over Tailscale, then a run against it, then tears it down.

### Cost

- **Hetzner CX33** (x86, 4 vCPU, 8 GB): EUR 0.0136/h excl. VAT after the
  2026-06-15 price increase. `up` creates the VM IPv6-only by default, which
  avoids the IPv4 charge; you reach it over the tailnet. Set `HCLOUD_IPV4=1`
  to keep a public IPv4, for example if Docker Hub pulls fail over IPv6 while
  building the agent image.
- **Oracle Cloud Always Free** (A1, arm64, 2 OCPU, 12 GB): $0. It is the
  same setup by hand: the cloud-init file works as user-data, then follow the
  steps `up` performs.
- Neither offers nested virtualization, so the node isolates runs with Docker
  containers, not microVMs.

### Prerequisites

- `hcloud`, `tailscale`, `ssh`, `git`, `openssl` and `curl` on your machine,
  and your machine on the tailnet.
- `HCLOUD_TOKEN`: a Hetzner Cloud project API token.
- `TS_AUTHKEY`: a Tailscale auth key (one-off or ephemeral).
- `HCLOUD_SSH_KEY`: the name of an SSH key already uploaded to the Hetzner
  project.
- A clean checkout: the node gets mini-ork as a git bundle of HEAD.

### Bring a node up, check it, run, tear down

```bash
scripts/remote_node_hetzner.sh up
```

`up` performs these steps, then prints the config snippets and the token:

1. Creates the VM (`cx33`, `ubuntu-24.04`, `fsn1`, label `mo-node=1`) with a
   cloud-init that installs docker, git, a Python venv and Tailscale.
2. Waits for the VM to join the tailnet and for cloud-init to finish.
3. Ships mini-ork as a git bundle of HEAD and installs it in a venv.
4. Builds the `mini-ork/agent-node` image on the VM.
5. Generates a node token into `/etc/mini-ork/node-agent.env` (root, 0600).
6. Starts the `mini-ork-node-agent` systemd unit, bound to the tailnet
   address, and checks `/v1/health`.

Paste the printed `nodes.yaml` and `environments/hetzner.yaml` snippets, and
export the printed `MO_NODE_TOKEN_<NAME>` in your shell. Then:

```bash
mini-ork nodes doctor --env hetzner --no-llm         # nine checks; exit 0 = ready
mini-ork nodes doctor --env hetzner --recipe code-fix   # adds the per-lane auth smoke

# the E2E fixture, then a real kickoff:
cp -R tests/fixtures/remote_e2e_repo /tmp/e2e && cd /tmp/e2e \
  && git init -q && git add -A && git commit -qm fixture
mini-ork run code-fix <mini-ork>/tests/fixtures/remote_e2e_kickoff.md --placement remote --env hetzner
mini-ork run code-fix path/to/kickoff.md --placement remote --env hetzner

scripts/remote_node_hetzner.sh status
scripts/remote_node_hetzner.sh down        # asks first; --yes skips the prompt
```

For a real run, list each lane's `api_key_env` under the profile's
`secrets:`. Keys are resolved on your machine and sent per process; they are
never stored on the node.

### The simulated node (no cloud account)

`tests/integration/test_remote_nodes_e2e.py` builds a node-host container
from the agent image and runs `mini-ork node-agent --runtime host` over TLS,
with no volume mounts. It then runs the same `code-fix` command against it,
with every lane pointed at the deterministic `tests/fixtures/bin/mo-fake-agent`
(no LLM spend). It checks that:

- the run is published and the only change in the local checkout is the fix;
- `remote.setup.step` events come before the first node, the implementer ran
  `placement=remote`, `remote.sync.*` events are present, and the test
  verifier ran on the node (`remote.check` rc=0);
- the node's `/srv/mini-ork/audit.jsonl` (one line per process: argv, cwd,
  env keys, never values) names no path on this machine;
- `kill_run` leaves no agent process and no session on the node.

It needs Docker and `bash docker/agent-node/build.sh`. Set `MO_REMOTE_LIVE=1
MO_E2E_LANE=<lane> MO_E2E_SECRET=<its api_key_env>` to swap the fake for a
real lane (this costs money; it reports the cost).

### Reference run

Pending: the transcript of the first `up` → `nodes doctor --env hetzner` →
fixture run → `down` belongs here, with its wall time and cost. It needs a
Hetzner account, so it was not part of the automated verification.
