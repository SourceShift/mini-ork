# OpenBao — the cloud-scalable secure tier

mini-ork's vault has two tiers. The simple one is `env` + a `0600` file and needs
nothing but a text editor. This directory is the other one: a real secret store
whose *server* enforces which secrets an identity may read, so least privilege is
a property of the deployment rather than a promise mini-ork makes about itself.

OpenBao is the MPL-2.0 Linux Foundation fork of Vault — API-compatible, no
BUSL licence. mini-ork talks to it through the `openbao` vault backend over
KV-v2.

## Run it

```bash
docker compose -f docker/openbao/docker-compose.yml up -d
bash docker/openbao/bootstrap.sh
set -a; source docker/openbao/openbao.env; set +a
mini-ork vault status          # one live round-trip; prints the chain, never a value
```

`bootstrap.sh` is idempotent: it writes one KV-v2 path per node role
(`mini-ork/collector`, `mini-ork/reviewer`), a **read-only policy per role**, an
audit device, and one short-lived scoped token per role. Tokens go to
`openbao.env` (`0600`) — the script prints none of them, so nothing lands in your
shell history.

Point `vault.yaml` at it (see `config/vault.example.yaml`):

```yaml
backends:
  - type: env
  - type: file
  - type: openbao
    address: http://127.0.0.1:8200
    paths: [mini-ork/collector]
    token_env: OPENBAO_TOKEN
```

## What each piece is for

| Piece | Why it exists |
|---|---|
| One KV-v2 **path per role** | The unit of least privilege. A node's token is scoped to the path its work needs. |
| A **policy per role** | Enforced by OpenBao on every read. mini-ork holds no broader token, so it cannot widen the grant. |
| **Short-lived scoped tokens** (`-ttl=1h`) | A leaked token expires; it is not a permanent key. |
| The **audit device** | Records every read server-side — the cloud tier's answer to "which node resolved this secret". The vault backend never logs a value, so the record lives here. |
| **`mini-ork vault status`** | A live probe: it performs a real round-trip and reports readiness, or the exact failure, without printing a value. |

## Dev mode is a shortcut, not the deployment

`docker-compose.yml` runs `bao server -dev`: in-memory storage, unsealed, a known
root token, plain HTTP on loopback. That is what makes the secure tier *copy-paste
runnable* — you can exercise the whole path in a minute.

For real use, swap the `command` for a config with persistent storage, a seal
(KMS/HSM/auto-unseal), and TLS, and provision the same policies in a real
namespace. **The policies, the backend config, and every mini-ork side stay
identical** — which is the point of the two-tier split: start simple, grow into
the secure tier without rewriting call sites.

## Proving it

`tests/integration/test_openbao_live.py` runs against this server and asserts the
two things a mocked test cannot: a seeded value comes back over the wire, and a
role's token is **refused** at another role's path. It skips cleanly when
`OPENBAO_ADDR` is unset, so it never affects the default suite:

```bash
set -a; source docker/openbao/openbao.env; set +a
.venv/bin/python -m pytest -q tests/integration/test_openbao_live.py
```
