# Running agents in Modal Sandboxes

`provider: modal` runs a run's agent nodes in a [Modal](https://modal.com)
Sandbox (gVisor). It follows the same model as Daytona and E2B: one sandbox per
run, the node-agent booted inside it from your engine, the ordinary remote-node
protocol on top, and the sandbox terminated at run end. The adapter is
`mini_ork/remote/providers/modal_sandbox.py`, on the shared
`providers/sandbox_node.py` base.

> **Status (2026-10-13): verified offline only** (11 fake-SDK tests). The live
> acceptance, `scripts/smoke_sandbox_node.py --provider modal` followed by a full
> `code-fix` run, is pending a Modal token. Daytona and E2B passed both on the
> same base.

## Quick start

```bash
pip install 'mini-ork[modal]'
modal token new                                   # or set MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
echo "export MODAL_TOKEN_ID='ak-…'"      >> "$MINI_ORK_HOME/config/secrets.local.sh"
echo "export MODAL_TOKEN_SECRET='as-…'"  >> "$MINI_ORK_HOME/config/secrets.local.sh"
cp config/environments/modal.yaml.example "$MINI_ORK_HOME/config/environments/modal.yaml"

PYTHONPATH=. python3 scripts/smoke_sandbox_node.py --provider modal
mini-ork nodes doctor --env modal --recipe code-fix
```

## Modal specifics

| | |
|---|---|
| Auth | `create_connect_token(port=7091)`, sent as the `_modal_connect_token` cookie (the node-agent's bearer uses `Authorization`) |
| Idempotency | sandbox name `mo-<run_id>`, unique within the app (`MO_MODAL_APP`) |
| Lifetime | `timeout` (default `MO_SANDBOX_MAX_AGE`, max 24 h) |
| Image / resources | profile `image` (any registry image) and `resources` (`cpu`, `memory`) |
| Egress allowlist | refused (Modal filters by CIDR) |
| GPU | Modal's strength: a GPU sandbox profile is a natural extension |
