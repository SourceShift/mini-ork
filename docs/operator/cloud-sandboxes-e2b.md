# Running agents in E2B sandboxes

`provider: e2b` runs a run's agent nodes in an [E2B](https://e2b.dev) sandbox,
a Firecracker microVM. It works the same way as the Daytona provider
([`cloud-sandboxes-daytona.md`](cloud-sandboxes-daytona.md)): one sandbox per
run, a node-agent booted inside it from your engine, the ordinary remote-node
protocol on top, and the sandbox deleted when the run ends. The shared machinery is
`mini_ork/remote/providers/sandbox_node.py`; the E2B adapter is
`mini_ork/remote/providers/e2b.py`.

## Quick start

```bash
pip install 'mini-ork[e2b]'
echo "export E2B_API_KEY='e2b_…'" >> "$MINI_ORK_HOME/config/secrets.local.sh"
cp config/environments/e2b.yaml.example "$MINI_ORK_HOME/config/environments/e2b.yaml"

PYTHONPATH=. python3 scripts/smoke_sandbox_node.py --provider e2b   # one sandbox, ~35 s
mini-ork nodes doctor --env e2b --recipe code-fix
mini-ork run code-fix <kickoff> --placement remote --env e2b
```

## What differs from Daytona

| | E2B | Daytona |
|---|---|---|
| Isolation | Firecracker microVM | container (VM classes available) |
| Port auth | traffic access token (`e2b-traffic-access-token`), public traffic off | per-sandbox preview token |
| Idempotency | `mo.run` metadata | unique sandbox name `mo-<run_id>` |
| Lifetime | sandbox `timeout`: **1 h on Hobby**, 24 h on Pro | `ttl_minutes`, enforced server-side |
| Default image | `base`: Python 3.11, git, Node 20, ~0.5 GiB RAM, no agent CLI | Python 3.14, git, Node 25, `claude`, `opencode` |
| CPU / memory | set by the template | profile `resources` (image path) |
| Egress allowlist | not wired yet (fails closed) | `domain_allow_list` |

## Templates

The `base` template is enough for the smoke and for `nodes doctor`. Real runs
want a template with the agent CLI, your test tooling and more memory. Build one
with the E2B CLI and name it in `provider_options.template`. A setup script on
`base` works too, but it reinstalls on every run.

## Verified

- 2026-10-13: `mini-ork run code-fix <kickoff> --placement remote --env e2b`
  (run-1791917474-996). The planner, implementer, typecheck, pytest (replay +
  mutation adequacy) and reviewer ran in an E2B microVM on the `base` template
  with the example profile's `setup`. The fix was published as a commit in the
  local checkout, the sandbox was killed at run end, and no secret reached the
  run dir. E2B needed no E2B-specific fix: everything came from the shared
  sandbox-node base.
- `nodes doctor --env e2b --recipe code-fix`: 10/10, with the lanes answering
  from inside the microVM.

- 2026-10-13: `scripts/smoke_sandbox_node.py --provider e2b` passed 7/7: create
  + boot 23.6 s, health through the traffic-token proxy, engine pre-staged,
  bearer 404/401, user site untouched, attach, release.
