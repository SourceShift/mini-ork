#!/usr/bin/env bash
# Give the dev OpenBao its shape: one KV-v2 path per node role, a read-only
# policy per role, an audit device, and one short-lived scoped token per role.
#
# What this is for: turning "mini-ork reads secrets from OpenBao" into "a node
# can read its own secrets and provably cannot read another node's". The policy
# is the enforcement; the per-role token is what carries it.
#
# Idempotent — re-running re-writes the policies, re-issues the tokens, and
# refreshes the env file. It prints no secret to the terminal: the tokens land
# in openbao.env (0600) for you to source, so they never reach shell history.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="$here/openbao.env"
compose=(docker compose -f "$here/docker-compose.yml")

bao() { "${compose[@]}" exec -T openbao bao "$@"; }

# Wait for the server to answer, then confirm it is up (dev mode is unsealed).
for _ in $(seq 1 30); do
  if bao status >/dev/null 2>&1; then break; fi
  sleep 1
done
bao status >/dev/null

# One path per role. Real values are a human's job:
#   bao kv put secret/mini-ork/collector ARXIV_API_TOKEN=...
# These demo values exist so the live integration test has something to read.
bao kv put secret/mini-ork/collector COLLECTOR_DEMO=collector-value >/dev/null
bao kv put secret/mini-ork/reviewer REVIEWER_DEMO=reviewer-value >/dev/null

write_policy() {
  local role="$1"
  bao policy write "$role" - >/dev/null <<HCL
# Read exactly this role's path and nothing else. KV-v2 keeps versions in
# data/ but resolves them through metadata/, so a read of data/ needs the
# matching metadata/ grant — omitting it is the classic "403 on a path I can
# see" mistake.
path "secret/data/mini-ork/${role}"     { capabilities = ["read"] }
path "secret/metadata/mini-ork/${role}" { capabilities = ["read"] }
HCL
}
write_policy collector
write_policy reviewer

# Audit device: every read is recorded server-side. This is the cloud tier's
# answer to "which node resolved this secret, and when" — the vault backend
# itself never logs a value, so the record lives here. Ephemeral in dev mode.
bao audit enable file file_path=/tmp/openbao-audit.log >/dev/null 2>&1 || true

umask 077
: > "$env_file"
collector_token=""
for role in collector reviewer; do
  token="$(bao token create -policy="$role" -ttl=1h -field=token)"
  if [ "$role" = collector ]; then collector_token="$token"; fi
  printf 'export OPENBAO_TOKEN_%s=%s\n' "$(echo "$role" | tr '[:lower:]' '[:upper:]')" "$token" >> "$env_file"
done
# The default role mini-ork's vault.yaml reads (token_env: OPENBAO_TOKEN).
printf 'export OPENBAO_TOKEN=%s\n' "$collector_token" >> "$env_file"
printf 'export OPENBAO_ADDR=http://127.0.0.1:8200\n' >> "$env_file"

echo "wrote $env_file (0600); no token was printed here"
echo
echo "next:"
echo "  set -a; source $env_file; set +a"
echo "  mini-ork vault status"
