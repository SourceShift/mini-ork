# remote-nodes-04 — Agent node image and engine layout

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
decisions **D4** (roots) and **D8** (engine version pinning). It has no
dependencies and can run in parallel with 01, 02 and 05.

## Goal

Ship a buildable container image in which a mini-ork agent node can run any
CLI-backed lane, plus the documented engine-mount convention the node-agent
(epic 05) relies on.

## Why

- No agent image exists. `MO_SANDBOX_IMAGE` has no default and raises if unset
  (`runtime/backends/docker.py:284-289`). Tests and demos use `alpine:latest`
  (`tests/unit/test_docker_workspace.py:27`), which contains none of the CLIs.
  The only Dockerfile in the repo is certify's `python:3.11-slim` test image
  (`mini_ork/certify/image.py:78`). `mini-ork/agent:latest` exists only as
  prose in `kickoffs/sandbox-p2-docker-workspace.md:123`.
- Transports and verifier scripts are mini-ork code: `python3 -m
  mini_ork.dispatch.*` and `recipes/*/verifiers/*.py`. Baking mini-ork into the
  image would pin the image to one engine version. Mounting the engine
  read-only at `/opt/mini-ork` keeps one image valid across engine upgrades
  (D8).
- Claude Code refuses `bypassPermissions` when run as root, and mini-ork
  dispatch sets it (`dispatch/providers.py:479`). So the image must run as a
  non-root user.

## Requirements

1. `docker/agent-node/Dockerfile`, multi-arch (`linux/amd64` and
   `linux/arm64`; Hetzner CAX and Oracle A1 are arm64):
   - Base `debian:bookworm-slim`, plus python3.11 and pip, git, curl, ca-certificates,
     ripgrep, jq, build-essential, openssh-client and tini.
   - Node 22 LTS, with globally installed and version-pinned
     `@anthropic-ai/claude-code` and `opencode-ai`, via build args
     `CLAUDE_CODE_VERSION` and `OPENCODE_VERSION`. Add `@openai/codex` behind
     build arg `WITH_CODEX=0` (off by default).
   - Python deps of mini-ork's core install (`pyproject.toml` base
     dependencies, not `[full]`), installed system-wide, so a mounted engine
     imports cleanly without a pip step at session start.
   - A non-root user `agent` (uid 1000) with `HOME=/workspace/home` and
     `WORKDIR /workspace/target`.
   - `ENV PYTHONPATH=/opt/mini-ork PATH=/opt/mini-ork/bin:$PATH MO_REMOTE_NODE=1`.
   - `ENTRYPOINT ["tini","--"]` and `CMD ["sleep","infinity"]`, matching the
     `DockerWorkspace.up()` contract (`docker.py:140`).
2. `docker/agent-node/smoke.sh <image> [engine_dir]` runs the image with the
   engine dir mounted read-only at `/opt/mini-ork`. It asserts:
   - the user is non-root
   - `claude --version`, `opencode --version`, `git --version` and
     `python3 -c "import mini_ork, yaml"` succeed
   - `/workspace/target` is writable

   It exits non-zero on the first failure and prints which check failed.
3. `docker/agent-node/build.sh`: a `docker buildx build --platform` wrapper
   that tags `mini-ork/agent-node:<git-sha>` and `:latest`, with `--load`
   locally and `--push` optional.
4. Engine layout spec, written into the operator doc:
   - On a node host, engines live at `/srv/mini-ork/engines/<git-sha>/`. Each
     is an exported tree of `MINI_ORK_ROOT` at that sha: `mini_ork/`,
     `recipes/`, `bin/`, `schemas/`.
   - The node-agent mounts the one matching the control plane read-only at
     `/opt/mini-ork`.
   - Per-run dirs live at `/srv/mini-ork/runs/<run_id>/{target,run,home,mo-home}`
     and are mounted at `/workspace/...`.
5. New operator doc `docs/operator/remote-nodes.md`, with an "Agent image" and
   an "Engine layout" section only. Later epics append their sections.

## Files in scope (touch ONLY these)

- `docker/agent-node/Dockerfile` (new)
- `docker/agent-node/smoke.sh` (new)
- `docker/agent-node/build.sh` (new)
- `docs/operator/remote-nodes.md` (new)
- `tests/unit/test_agent_node_image.py` (new)

## Out of scope

- Per-project toolchains (setup script caching is epic 12).
- Credentials in the image. **Never** bake keys; auth arrives per process
  (epic 13).

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_agent_node_image.py && make lint
# live (daemon-gated, optional): bash docker/agent-node/build.sh && bash docker/agent-node/smoke.sh mini-ork/agent-node:latest "$PWD"
```

## Acceptance

- The static test parses the Dockerfile. It asserts a non-root `USER`,
  `HOME=/workspace/home`, `WORKDIR /workspace/target`,
  `PYTHONPATH=/opt/mini-ork`, the pinned CLI version args, and a
  `tini`/`sleep infinity` entrypoint/cmd.
- The daemon-gated test (skipped without docker, using the same gating as
  `test_docker_workspace.py`) builds the image and runs `smoke.sh` with the
  repo mounted as the engine. All checks pass.
- `smoke.sh` fails with a clear message when the engine dir is missing.
