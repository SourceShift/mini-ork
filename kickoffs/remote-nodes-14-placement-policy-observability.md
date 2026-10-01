# remote-nodes-14 — Placement policy and observability

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
**D5** (the what-runs-where table) and "Configuration → Run selection".
Depends on epics 03, 07, 08 and 11, which together make a remote node correct.
This epic exposes it as one switch.

## Goal

`mini-ork run <recipe> <kickoff> --placement remote --env <name>` runs the DAG
with the D5 routing:
- every CLI-spawning dispatch and every check runs on the environment's node
- in-process logic, tool-less HTTP lanes and the publisher stay local

The operator can see where each node ran. A node host is never oversubscribed.

## Why

- Isolation is selected today by two low-level env vars, `MO_SANDBOX_SCOPE`
  and `MO_SANDBOX_BACKEND` (`dispatch/providers.py:941-961`
  `_select_workspace`). There is no run-level concept of placement, and no
  rule for which lanes should stay local.
- `run` parses only `--deadline` before the recipe and kickoff positionals
  (`mini_ork/cli/main.py:517-540`).
- A run on a remote node is otherwise indistinguishable in the event stream
  and the UI.

## Requirements

1. **CLI.** Add `--placement local|remote` and `--env <name>` to
   `_run_lifecycle_impl` (`cli/main.py:517`), parsed the same way as
   `--deadline`.
   - Persist both into `run_profile.json` (`placement`, `environment`) next to
     the epic-01 `roots`.
   - Env equivalents: `MO_PLACEMENT`, `MO_NODE_ENV`. The flag wins over env.
   - `--placement remote` without a resolvable environment exits 2 with the
     list of available profiles.
   - Default `local` gives byte-for-byte today's behaviour.
2. **Routing.** Extend `_select_workspace(requested, env)` with placement
   precedence:
   1. an explicit request
   2. `placement=remote` → `remote`, **unless** the lane kind is in the
      local-only set `{openai-chat, uhp}`, which are tool-less HTTP lanes
      with no tree access
   3. today's scope/backend rule

   The lane kind must reach `_select_workspace`. Pass it from `dispatch_model`
   (`providers.py:964`) rather than re-deriving it. Add a role override
   `MO_PLACEMENT_LOCAL_ROLES=planner,...` for operators who want planning on
   the laptop; it is documented as a trade-off, because a planner on
   `anthropic-compat` then reads the local tree.
3. **Session binding.** Under `placement=remote`, the run session (epic 02)
   uses the profile's node, image (prepared, epic 12), resources and network.
   Provisioning starts at run start, not lazily at the first remote node, so
   setup failures surface before any LLM spend. Emit the `remote.setup.step`
   sequence (epic 12) before the first node.
4. **Observability.**
   - Every node's start event carries `placement: local|remote` and, when
     remote, `node_host` + `session_id`.
   - A run summary event `remote.run.summary` reports `{node_host,
     sync_up_bytes, sync_down_bytes, remote_wall_s, setup_s}`.
   - `GET /api/v1/runs/{id}` (`web/routes/run_detail.py`) returns `placement`,
     `environment` and `node_host` so the UI can render a badge. Adding the
     badge to the run header component is in scope if it is ≤30 lines;
     otherwise expose the API only.
5. **Concurrency cap.**
   - Before session up, check the node's `max_sessions` (from `nodes.yaml`,
     epic 06) against the node-agent's live `sessions` count (`/v1/health`).
   - When full, wait with backoff up to `MO_REMOTE_QUEUE_WAIT_S` (default
     600), emitting `remote.queue.wait`, then fail `remote_unavailable`.
   - The scheduler's `MO_SCHED_MAX_PARALLEL` still governs how many runs
     start at all.

## Files in scope (touch ONLY these)

- `mini_ork/cli/main.py` (`_run_lifecycle_impl` flags + profile persist)
- `mini_ork/dispatch/providers.py` (`_select_workspace` + lane kind plumbing)
- `mini_ork/cli/execute.py` (run-start provisioning + node event fields)
- `mini_ork/web/routes/run_detail.py` (placement fields)
- `mini_ork/runtime/workspace_session.py` (profile-driven session, cap check)
- `tests/unit/test_placement_policy.py` (new)

## Out of scope

- Per-recipe placement declarations in `workflow.yaml`. Placement is runtime
  config; recipes stay placement-free (a cloud-swarm non-goal).
- Multi-node fan-out of one run's parallel nodes across several hosts. One run
  maps to one node host in v1.

## Verification command

```bash
python3.11 -m pytest -q tests/unit/test_placement_policy.py tests/unit -k "select_workspace or placement or run_lifecycle" && make lint
```

## Acceptance

- `_select_workspace` truth-table test covering:
  - `placement × lane kind × explicit request × MO_PLACEMENT_LOCAL_ROLES`
  - `openai-chat` stays `host` under remote
  - an `anthropic-compat` reviewer goes `remote`
  - placement unset → today's results exactly
- `run --placement remote --env missing` exits 2 and lists profiles.
- A run against the in-process node-agent shows `remote.setup.step` events
  before the first node's start event. Every implementer and verifier event
  carries `placement=remote`, and the classify/transform nodes carry `local`.
- With `max_sessions: 1` and one live session, a second run waits, emits
  `remote.queue.wait` and fails `remote_unavailable` after a test-sized wait.
