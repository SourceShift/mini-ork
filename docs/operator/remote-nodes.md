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