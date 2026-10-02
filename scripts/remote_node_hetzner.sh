#!/usr/bin/env bash
# remote_node_hetzner.sh — a mini-ork node host on a cheap Hetzner Cloud VM.
#
#   up       create the VM, join it to your tailnet, install mini-ork + the
#            agent image, start the node-agent, print the config to paste
#   down     destroy the VM(s) labelled mo-node=1 (asks first; --yes skips)
#   status   list them
#
# Needs: hcloud, tailscale (this machine on the same tailnet), ssh, git, openssl.
# Env:   HCLOUD_TOKEN    Hetzner Cloud API token (project-scoped)
#        TS_AUTHKEY      Tailscale auth key (one-off/ephemeral recommended)
#        HCLOUD_SSH_KEY  name of an SSH key already in the Hetzner project
# Optional: HCLOUD_TYPE (cx33), HCLOUD_IMAGE (ubuntu-24.04), HCLOUD_LOCATION
#        (fsn1), HCLOUD_SERVER_NAME (mo-node-1), HCLOUD_IPV4=1 (keep a public
#        IPv4; default is IPv6-only — the tailnet is how you reach the node).
#
# Nothing secret is written to this repo. The rendered cloud-init (with the
# Tailscale key) lives in a 0600 temp file that is removed on exit; the node
# token is generated here, written to the VM's /etc/mini-ork (0600) and printed
# once for your shell.
set -euo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LABEL="mo-node=1"
NAME="${HCLOUD_SERVER_NAME:-mo-node-1}"
TYPE="${HCLOUD_TYPE:-cx33}"
IMAGE="${HCLOUD_IMAGE:-ubuntu-24.04}"
LOCATION="${HCLOUD_LOCATION:-fsn1}"
PORT=7091

die() { echo "remote_node_hetzner: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found on PATH"; }
need_env() { [ -n "${!1:-}" ] || die "$1 is not set"; }

servers() {
    hcloud server list --selector "$LABEL" -o noheader -o columns=id,name,status,ipv6 2>/dev/null || true
}

cmd_up() {
    need hcloud; need tailscale; need ssh; need git; need openssl; need curl
    need_env HCLOUD_TOKEN; need_env TS_AUTHKEY; need_env HCLOUD_SSH_KEY
    if hcloud server describe "$NAME" >/dev/null 2>&1; then
        die "a server named $NAME already exists (run 'down' first, or set HCLOUD_SERVER_NAME)"
    fi
    local started rendered v4 ip ssh_ token upper
    started=$(date +%s)
    rendered=$(mktemp)
    chmod 600 "$rendered"
    trap 'rm -f "$rendered"' EXIT
    sed -e "s|__TS_AUTHKEY__|${TS_AUTHKEY}|" -e "s|__NODE_NAME__|${NAME}|" \
        "$ROOT/deploy/node-agent/cloud-init.yaml" > "$rendered"
    v4=(--without-ipv4)
    [ "${HCLOUD_IPV4:-0}" = "1" ] && v4=()

    echo "==> creating $NAME ($TYPE, $IMAGE, $LOCATION)"
    hcloud server create --name "$NAME" --type "$TYPE" --image "$IMAGE" --location "$LOCATION" \
        --ssh-key "$HCLOUD_SSH_KEY" --user-data-from-file "$rendered" --label "$LABEL" "${v4[@]}" \
        >/dev/null

    echo "==> waiting for $NAME to join the tailnet"
    ip=""
    for _ in $(seq 1 120); do
        ip=$(tailscale ip -4 "$NAME" 2>/dev/null | head -1 || true)
        [ -n "$ip" ] && break
        sleep 5
    done
    [ -n "$ip" ] || die "$NAME never appeared on the tailnet (check the auth key; 'down' to clean up)"
    ssh_=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$ip")
    for _ in $(seq 1 60); do "${ssh_[@]}" true 2>/dev/null && break; sleep 5; done
    echo "==> waiting for cloud-init on $ip"
    "${ssh_[@]}" cloud-init status --wait >/dev/null || die "cloud-init failed on $NAME"

    echo "==> shipping mini-ork $(git -C "$ROOT" rev-parse --short HEAD) and building the agent image"
    git -C "$ROOT" bundle create - HEAD 2>/dev/null | "${ssh_[@]}" 'cat > /root/mini-ork.bundle'
    "${ssh_[@]}" bash -s <<'REMOTE'
set -euo pipefail
rm -rf /opt/mini-ork-node
git clone -q /root/mini-ork.bundle /opt/mini-ork-node/src
python3 -m venv /opt/mini-ork-node/venv
/opt/mini-ork-node/venv/bin/pip install -q '/opt/mini-ork-node/src[web]'
cd /opt/mini-ork-node/src && bash docker/agent-node/build.sh >/var/log/mini-ork-agent-image.log 2>&1
REMOTE

    echo "==> starting the node-agent on $ip:$PORT"
    token=$(openssl rand -hex 32)
    printf 'MO_NODE_TOKEN=%s\nMO_NODE_BIND=%s\n' "$token" "$ip" \
        | "${ssh_[@]}" 'umask 077; cat > /etc/mini-ork/node-agent.env'
    "${ssh_[@]}" 'install -m 0644 /opt/mini-ork-node/src/deploy/node-agent/mini-ork-node-agent.service \
        /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now mini-ork-node-agent'
    for _ in $(seq 1 30); do curl -fsS "http://$ip:$PORT/v1/health" >/dev/null 2>&1 && break; sleep 2; done
    curl -fsS "http://$ip:$PORT/v1/health" >/dev/null || die "node-agent not answering (journalctl -u mini-ork-node-agent on the VM)"

    upper=$(echo "$NAME" | tr 'a-z-' 'A-Z_')
    cat <<EOF

==> $NAME is up in $(( $(date +%s) - started ))s.

1. Add to \$MINI_ORK_HOME/config/nodes.yaml:

nodes:
  $NAME:
    url: http://$ip:$PORT
    token_env: MO_NODE_TOKEN_$upper
    max_sessions: 2

2. Add to \$MINI_ORK_HOME/config/environments/hetzner.yaml:

node: $NAME
image: mini-ork/agent-node:latest
network: full
secrets: []        # the api_key_env of every lane the runs use

3. In the shell you run mini-ork from (shown once; keep it out of git):

export MO_NODE_TOKEN_$upper=$token

4. Check it:   mini-ork nodes doctor --env hetzner --no-llm

Cost: $TYPE at the hourly rate on https://www.hetzner.com/cloud (CX33 was
EUR 0.0136/h excl. VAT after the 2026-06-15 increase). Run '$0 down' when done.
EOF
}

cmd_down() {
    need hcloud; need_env HCLOUD_TOKEN
    local yes=0 list id
    [ "${1:-}" = "--yes" ] && yes=1
    list=$(servers)
    if [ -z "$list" ]; then echo "no servers labelled $LABEL"; return 0; fi
    echo "$list"
    if [ "$yes" -ne 1 ]; then
        read -r -p "destroy these? [y/N] " ans
        case "$ans" in y|Y|yes|YES) ;; *) echo "aborted"; return 1 ;; esac
    fi
    while read -r id _; do
        [ -n "$id" ] && hcloud server delete "$id"
    done <<< "$list"
}

cmd_status() {
    need hcloud; need_env HCLOUD_TOKEN
    local list
    list=$(servers)
    if [ -n "$list" ]; then echo "$list"; else echo "no servers labelled $LABEL"; fi
}

case "${1:-}" in
    up)     cmd_up ;;
    down)   shift; cmd_down "$@" ;;
    status) cmd_status ;;
    *)      echo "usage: $0 {up|down [--yes]|status}" >&2; exit 2 ;;
esac
