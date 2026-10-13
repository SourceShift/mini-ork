# Running agents in Daytona sandboxes

mini-ork can run a run's agent nodes in a [Daytona](https://daytona.io)
sandbox. It creates one sandbox per run, boots a node-agent inside it, sends
the agents' work there, brings their edits back into your local checkout, and
deletes the sandbox when the run ends. You don't need a VM, Docker, or an open
port on your machine.

> **Status (2026-10-13): verified end to end.** `mini-ork run code-fix <kickoff>
> --placement remote --env daytona` ran the planner, implementer, typecheck, pytest
> (base-vs-candidate replay and mutation adequacy) and reviewer in a Daytona sandbox,
> published the fix as a commit in the local checkout, and deleted the sandbox
> (run-1791915762-20011). `scripts/smoke_daytona_node.py` passes 7/7.

The design is in
[`docs/architecture/cloud-sandbox-providers.md`](../architecture/cloud-sandbox-providers.md).
In short, Daytona is a **node provider**: it decides where a node-agent comes
from. Everything after that (tree sync, the run-dir mirror, reattach,
per-lane secrets) is the ordinary remote-node machinery from
[`remote-nodes.md`](remote-nodes.md).

## Quick start (laptop, ~5 minutes)

```bash
pip install 'mini-ork[daytona]'

# 1. the API key: environment, or the 0600 secret store
echo "export DAYTONA_API_KEY='dtn_…'" >> "$MINI_ORK_HOME/config/secrets.local.sh"
mini-ork vault which DAYTONA_API_KEY        # prints where it is served from, never the value

# 2. the profile
cp config/environments/daytona.yaml.example "$MINI_ORK_HOME/config/environments/daytona.yaml"

# 3. prove the provider end to end (one sandbox, about 20 s, a few cents)
PYTHONPATH=. python3 scripts/smoke_daytona_node.py

# 4. run
mini-ork run code-fix <kickoff> --placement remote --env daytona
```

The default profile uses Daytona's default snapshot. It already has Python,
git, Node, `claude` and `opencode`. The node-agent's one missing dependency,
`fastapi`, is installed on first boot.

## Shared or production use

The **same profile file** moves you to the secure tier. No call site changes.

| | Laptop | Shared / cloud |
|---|---|---|
| `DAYTONA_API_KEY` | env or `secrets.local.sh` | a `sops`, `command` (`pass`/`op`/`infisical`) or `openbao` backend in `$MINI_ORK_HOME/config/vault.yaml` |
| Image | default snapshot + bootstrap | `provider_options.snapshot:` a snapshot with pinned CLI versions |
| Network | `full` | `network: allowlist` + `allow_domains` (→ Daytona `domain_allow_list`) |
| Leaks | TTL (6 h) + run-end delete | the same, plus `mini-ork sandbox-gc --backend daytona` on a schedule |

## What runs where

- **Your machine (control plane):**
  - `state.db`, the run dir, the UI, the publisher and all git history
    operations;
  - the Daytona API key, which never enters the sandbox.
- **The sandbox:**
  - the node-agent (`--runtime host`, bound to `127.0.0.1:7091`, reached
    through Daytona's TLS preview proxy);
  - a replica of the target tree;
  - the agent CLIs and the verifiers.

Each sandbox has three locks:

1. the Daytona preview token (per sandbox);
2. a node-agent bearer token, minted per sandbox, stored in a 0600 file inside
   it and never written to your disk;
3. the per-lane secret scoping of remote spawns.

## Lifetime and cleanup

A sandbox is deleted at run end, on `kill_run`, and by `sandbox-gc`. If all of
those are missed (the laptop died), Daytona deletes it when its `ttl_minutes`
elapses. That defaults to `MO_SANDBOX_MAX_AGE` (6 h), and Daytona enforces it
server-side, so nothing can bill forever.

Every sandbox carries these labels:
- `mo.sandbox=1`
- `mo.provider=daytona`
- `mo.run=<run_id>`
- `mo.created=<epoch>`

Its name is `mo-<run_id>`, so you can find a run's sandbox in the Daytona
dashboard.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `node provider 'daytona' needs DAYTONA_API_KEY` | no key in the env, the secret store or the vault chain |
| `the Daytona SDK is not installed` | `pip install 'mini-ork[daytona]'` |
| `Failed to create sandbox: 1 validation error for Sandbox … kvm` | SDK/API version skew for that organization's endpoint. Pin the SDK to the API's version (`X-Daytona-Api-Version` response header) |
| `node bootstrap exited 3 … deps missing` | `bootstrap: false` on an image without the node-agent deps. Enable bootstrap or bake them in |
| `node-agent … not healthy after 180s; log tail: …` | the log tail is the node-agent's own output. Debug with `MO_DAYTONA_KEEP_FAILED=1` to keep the sandbox |
| `network: allowlist needs allow_domains` | an empty allowlist would block the LLM APIs; list them |

## Lanes and test tooling on a remote node

- **Lanes must authenticate without your laptop's login.** `anthropic-compat`
  lanes (glm, deepseek_flash, …) carry their own token and work as-is. An
  `anthropic-native` lane (opus, sonnet) uses your local Claude login, which
  cannot travel: put `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or
  `ANTHROPIC_API_KEY` in the secret store and list it under `secrets:`, or keep
  that role local with `MO_PLACEMENT_LOCAL_ROLES`. Without one, the remote
  dispatch is refused up front with that message, and the fallback chain moves on.
- **Install the project's test tooling in `setup:`.** Checks run inside the
  sandbox. If the image lacks the runner (the default snapshot has no `pytest`),
  the test verifier skips and the run's `executes` level stays unproven, which
  withholds the publish.
- `nodes doctor --env daytona --recipe <recipe>` makes a ~20-token call through
  every lane the recipe uses, from inside a real sandbox, before you spend a run.

## Environment knobs

| Variable | Meaning |
|---|---|
| `DAYTONA_API_KEY` | API key (vault chain) |
| `DAYTONA_API_URL`, `DAYTONA_TARGET` | API endpoint and region (non-secret; also `provider_options.api_url` / `target`) |
| `MO_NODE_PROVIDER=daytona` | select the provider without a profile field |
| `MO_DAYTONA_SNAPSHOT` | default snapshot when the profile names none |
| `MO_SANDBOX_MAX_AGE` | TTL in seconds (sandbox `ttl_minutes` = this / 60) |
| `MO_NODE_PROVISION_WAIT_S` | how long a run's second process waits for the first to finish provisioning (default 900) |
| `MO_DAYTONA_KEEP_FAILED=1` | keep a sandbox whose boot failed, for debugging |
