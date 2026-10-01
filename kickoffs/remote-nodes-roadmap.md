# Remote node execution — roadmap (local control plane, cloud nodes)

Ingest: `mini-ork epics ingest kickoffs/remote-nodes-roadmap.md`. Each epic id
below matches `kickoffs/<id>.md`, so `scheduler.resolve_kickoff` finds the
hand-written kickoff with no `split` step.

## Goal and design (context)

Run every DAG node that spawns an agent CLI or touches the target tree on a
remote VM, while the executor, `state.db`, run dir, UI, steering and publisher
stay on the laptop. Design, decisions D1–D8, the sync protocol, the node-agent
API and the provider choice are all in `docs/architecture/remote-nodes.md`.
Every kickoff cites it by section, so read it first.

**Definition of done for the whole set:** epic 15's E2E passes against a node-agent
with no host mounts (simulated remote) AND against a real Hetzner CX33. In both,
a `code-fix` run with `--placement remote` lands its diff in the local checkout,
runs its verifier remotely, and leaks no host path.

## Delivery constraints (context)

- **Default stays `local`, byte-for-byte.** Every seam is dead code when
  `MO_PLACEMENT` and `MO_SANDBOX_BACKEND` are unset (A1 delivery-safety
  constraints, `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`). Each kickoff
  carries a default-parity test.
- **No silent fallback.** An unknown backend, an unreachable node or an
  unmapped host path fails loud. It never quietly runs on the host.
- **Additive deps only.** FastAPI and uvicorn are already deps. Cloud SDKs, if
  any, import lazily.
- **Conformance (cloud-swarm P6).** The `remote` backend passes the same
  Workspace suite as `local` and `docker`.
- **New subcommands** must be added to `_NATIVE_MODULE_SUBS`
  (`mini_ork/cli/main.py:40`), and the exact-set guard
  `tests/unit/test_native_dispatch_py.py` must be updated in the same epic.

## Dispatch notes (context)

- Each epic is one `framework-edit` dispatch (mini-ork self-edit). Export
  `MO_ALLOW_FRAMEWORK_CWD=1` and `MO_STATIC_RECIPE_PLAN=1`, and pin a gate
  python: `MINI_ORK_TEST_CMD="python3.11 -m pytest -q <epic tests>"`.
- framework-edit never emits `verdict.json`, so judge each epic by reviewer
  pass + its kickoff's verification command run by hand + `git diff` against
  the kickoff.
- Epics 01, 02 and 04 have no dependencies and can run in parallel
  (`MO_SCHED_MAX_PARALLEL=3`). 05 starts as soon as 04 lands.
- Order is not strict beyond the deps below. The scheduler unblocks as deps
  resolve.

## Pin run roots once per run (id: remote-nodes-01-pin-run-roots)
- recipe: framework-edit
- max attempts: 2

Resolve target/run/home/engine roots once at run start into a `RunRoots`
record persisted in `run_profile.json`. This replaces the `os.environ`
`MO_TARGET_CWD` handoff, which lets the baseline snapshot a different tree than
the implementer edits.

## Run-scoped workspace session (id: remote-nodes-02-run-workspace-session)
- recipe: framework-edit
- max attempts: 2

Replace per-spawn `ws.up()`/`ws.down()` (`dispatch/core.py:270-274`) with one
session per run, reused across nodes, retries and fallback lanes. It is torn
down at run end, on SIGTERM and on `kill_run`.

## Path map and portable transport argv (id: remote-nodes-03-path-map-portable-argv)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-01-pin-run-roots

One `PathMap` seam in `_spawn_in_workspace` translates cwd, argv, env, prompt
text and MCP config to the D4 roots. Python transports become
`python3 -m mini_ork.dispatch.<mod>` against the engine root. An unmapped host
path fails loud.

## Agent node image and engine layout (id: remote-nodes-04-agent-image-engine-layout)
- recipe: framework-edit
- max attempts: 2

`docker/agent-node/Dockerfile` contains python3.11, git, node 22, the claude
CLI, opencode and ripgrep. Add a smoke script and define the `/opt/mini-ork`
engine mount convention.

## Node-agent server (id: remote-nodes-05-node-agent-server)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-04-agent-image-engine-layout

Add `mini-ork node-agent`, a FastAPI service implementing the D3 API:
sessions, detached procs with disk-buffered offset streams, kill, exec, file
transfer, engines and health. Bearer-token auth, with a safe default bind.

## Remote Workspace backend + conformance suite (id: remote-nodes-06-remote-workspace-backend)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-02-run-workspace-session, remote-nodes-05-node-agent-server

`RemoteWorkspace` is registered as `remote`. It is the node-agent client and
reads the `config/nodes.yaml` registry. A shared conformance suite runs over
local, docker and remote (with an in-process node-agent).

## Target tree sync via git snapshots (id: remote-nodes-07-target-tree-sync)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-01-pin-run-roots, remote-nodes-06-remote-workspace-backend

The snapshot/bundle protocol from the spec's §Sync protocol: initial-upload
ladder, incremental sync-up and sync-down, and pre/post tree-sha checks. On
conflict it raises `sync_conflict` and never clobbers the local tree.

## Run-dir mirror (id: remote-nodes-08-run-dir-mirror)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-03-path-map-portable-argv, remote-nodes-06-remote-workspace-backend

A manifest-diff mirror of the run dir, up before and down after remote work.
Covers agent-written outputs, `*.diff`, lens files and usage sidecars.

## Live stream and cancel for remote procs (id: remote-nodes-09-live-stream-and-cancel)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-06-remote-workspace-backend

Remote stdout is teed live into the host `agent-<node>.live.jsonl`.
`stop_run`, `kill_run` and timeouts propagate to remote processes.

## Disconnect tolerance, reattach and resume (id: remote-nodes-10-disconnect-reattach-resume)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-09-live-stream-and-cancel, remote-nodes-07-target-tree-sync

`proc_id` is persisted per node attempt. The stream reconnects with offsets.
`recover` re-attaches to or harvests remote processes instead of
re-dispatching, and claude transcripts persist per run for `--resume`.

## Verifier and verify placement (id: remote-nodes-11-verifier-placement)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-07-target-tree-sync, remote-nodes-08-run-dir-mirror

Under remote placement, DAG verifier scripts (`execute.py:1260`), post-run
`success_verifiers` (`cli/verify.py:336`) and step_rules git run through the
run workspace. They sync up, never sync down the tree, and pull back verifier
JSON.

## Environment profiles (id: remote-nodes-12-environment-profiles)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-05-node-agent-server

Profiles live in `config/environments/<name>.yaml`: node, image, a setup
script cached as an image layer by hash, env, resources, and a network level
(`full` or `allowlist` via an egress proxy).

## Secrets injection and remote doctor (id: remote-nodes-13-secrets-and-doctor)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-06-remote-workspace-backend, remote-nodes-12-environment-profiles

Each spawn gets only the keys its lane needs, held in env only and redacted
remotely. Adds `mini-ork nodes ls|ping|doctor`, which runs a per-lane auth
smoke inside the session.

## Placement policy and observability (id: remote-nodes-14-placement-policy-observability)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-03-path-map-portable-argv, remote-nodes-07-target-tree-sync, remote-nodes-08-run-dir-mirror, remote-nodes-11-verifier-placement

Adds `mini-ork run … --placement remote --env <name>` and the D5 routing table
in `_select_workspace`. Emits `remote.*` run_events, shows a UI placement
badge, and enforces per-node concurrency caps.

## E2E proof: simulated remote + Hetzner (id: remote-nodes-15-e2e-simulated-remote-and-hetzner)
- recipe: framework-edit
- max attempts: 2
- depends on: remote-nodes-10-disconnect-reattach-resume, remote-nodes-13-secrets-and-doctor, remote-nodes-14-placement-policy-observability

The E2E runs the node-agent in a container with no host mounts and drives a
`code-fix` fixture run end-to-end, plus a leakage audit and a kill test. Also
ships the Hetzner cloud-init + `hcloud` up/down script and the operator
runbook.
