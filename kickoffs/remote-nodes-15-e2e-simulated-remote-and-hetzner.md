# remote-nodes-15 — E2E proof: simulated remote + Hetzner

Part of `kickoffs/remote-nodes-roadmap.md`. Design: `docs/architecture/remote-nodes.md`
"Test of done" and "Test provider". Depends on epics 10, 13 and 14, which
transitively means all of 01–14. This epic proves the whole set and ships the
cheap-cloud test path.

## Goal

Two proofs, plus the scripts to repeat them:

1. **Simulated remote (CI-able).** The node-agent runs inside a Docker
   container that shares **no filesystem** with the laptop. A `code-fix` run
   with `--placement remote` must complete with:
   - the diff landed in the local checkout
   - the verifier executed remotely
   - zero host paths in any remote argv, env or cwd
   - `kill_run` leaving nothing running remotely
2. **Real cloud.** The same run against a Hetzner CX33 VM brought up and torn
   down by one script, reached over Tailscale.

## Why

Every earlier epic is verified in isolation, mostly against an in-process
node-agent with `--runtime host`. That runtime shares the laptop's filesystem,
so it cannot catch a host-path leak or a missing sync. This is the class of
bug the S1 live probe caught: unit tests stayed green through the
`_RUN_CONTRACT_KEYS` double filter. Only a node with a genuinely separate
filesystem proves D1/D4 hold.

## Requirements

1. **Fixture**: `tests/fixtures/remote_e2e_repo/`, a tiny Python package with
   one failing test (an off-by-one in `calc.py`). Add a `code-fix` kickoff
   with an explicit "Files in scope" block. Headless kickoffs without a scope
   block get `needs_answers` from the classifier and hard-block the planner.
2. **Deterministic agent for the plumbing tier**: `tests/fixtures/bin/mo-fake-agent`.
   - It accepts the claude CLI flags mini-ork passes, reads the prompt from
     stdin, applies the known one-line fix to the target in its cwd, and
     prints a claude-shaped JSON result envelope with usage.
   - The test environment profile points the implementer lane's command at it
     through a run-scoped `providers.yaml` override. No LLM spend, so it runs
     in CI with Docker.
3. **Simulated-remote harness** `tests/integration/test_remote_nodes_e2e.py`
   (daemon-gated):
   - Build `docker/agent-node` (epic 04).
   - Start a "node host" container from the same image running `mini-ork
     node-agent --runtime host --bind 0.0.0.0:7091` with a test TLS cert. It
     runs with **no `-v` mounts** and gets its engine via `PUT /v1/engines`.
   - Write a temp `nodes.yaml` and environment profile.
   - Run `mini-ork run code-fix <fixture kickoff> --placement remote --env e2e`
     from a temp copy of the fixture repo.

   Assertions:
   - run status completed
   - local `git diff` shows exactly the fix
   - a `remote.check` event with rc=0 for the test verifier
   - the `remote.sync.*` events present
   - the node-agent's audit log (see item 4) contains no value starting with
     the laptop's `$HOME`, `/Users`, `/Volumes` or `/private`
   - `kill_run` test: a second run with a sleeping fake agent is killed, the
     node-agent then reports zero live procs, and its session is deleted
4. **Audit log**: the node-agent appends one JSON line per proc to
   `<state>/audit.jsonl` with `{argv, cwd, env_keys, image, session}`. Env
   **values** are never logged. The leakage assertion and operators both read
   it.
5. **Live tier** (`MO_REMOTE_LIVE=1`): the same harness with a real cheap lane
   (`MO_E2E_LANE`, for example a minimax/glm `anthropic-compat` lane) instead
   of `mo-fake-agent`. It needs the lane secret in the local secret store and
   in the profile's `secrets`. It reports cost from `llm_calls`.
6. **Hetzner path**:
   - `deploy/node-agent/cloud-init.yaml` installs docker, python3.11 and
     tailscale (`TS_AUTHKEY` templated in at create time, never committed),
     and creates a `mini-ork` user and the `/srv/mini-ork` layout.
   - `deploy/node-agent/mini-ork-node-agent.service` is a systemd unit bound
     to the tailnet IP, with the token from `/etc/mini-ork/node-agent.env`
     (mode 0600).
   - `scripts/remote_node_hetzner.sh up|down|status`:
     - `up` runs `hcloud server create --type ${HCLOUD_TYPE:-cx33}
       --image ubuntu-24.04 --user-data-from-file …`. It waits for tailscale,
       ships a git bundle of `MINI_ORK_ROOT` HEAD, `pip install`s it, builds
       the agent image on the VM, generates a token, starts the unit, then
       prints the `nodes.yaml` snippet and the hourly price.
     - `down` destroys the server; it prompts unless `--yes`.
     - `status` lists servers labelled `mo-node=1`.

     It needs `HCLOUD_TOKEN` and `TS_AUTHKEY` from the operator's env.
7. **Runbook**: append "Run nodes on a cheap cloud VM" to
   `docs/operator/remote-nodes.md`, covering:
   - prerequisites
   - `up`, `nodes doctor --env hetzner`, running the fixture, then a real
     kickoff
   - teardown, with cost notes: CX33 €0.0136/h excl. VAT (after Hetzner's
     2026-06-15 increase); IPv6-only plus Tailscale avoids the IPv4 charge;
     Hetzner cloud has no nested virtualization, so the node uses Docker, not
     microVM
   - Oracle Always Free (A1, 2 OCPU / 12 GB, arm64) as the $0 alternative

## Files in scope (touch ONLY these)

- `tests/fixtures/remote_e2e_repo/**`, `tests/fixtures/bin/mo-fake-agent` (new)
- `tests/integration/test_remote_nodes_e2e.py` (new)
- `mini_ork/remote/node_agent/procs.py` (audit log only)
- `deploy/node-agent/cloud-init.yaml`, `deploy/node-agent/mini-ork-node-agent.service` (new)
- `scripts/remote_node_hetzner.sh` (new)
- `docs/operator/remote-nodes.md` (append runbook)

## Out of scope

- Managed-sandbox backends (E2B, Modal, Daytona). Each is a later epic behind
  the same conformance suite.
- Running the live tier in CI. It costs money and needs secrets; it is
  operator-run.

## Verification command

```bash
python3.11 -m pytest -q tests/integration/test_remote_nodes_e2e.py   # daemon-gated plumbing tier
MO_REMOTE_LIVE=1 python3.11 -m pytest -q tests/integration/test_remote_nodes_e2e.py -k live   # operator, optional
bash -n scripts/remote_node_hetzner.sh && make lint
```

## Acceptance

- The plumbing tier passes on a machine with Docker. A deliberately
  introduced host-path leak (a test-only monkeypatch that skips `PathMap.text`)
  makes the leakage assertion fail. This proves the audit catches what the
  unit suite cannot.
- `kill_run` leaves zero live procs and no session on the node host.
- The operator transcript of `scripts/remote_node_hetzner.sh up` → `mini-ork nodes doctor --env hetzner`
  → the fixture run → `down` is pasted into the runbook as the reference run,
  with its wall time and cost.

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
