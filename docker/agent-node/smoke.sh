#!/usr/bin/env bash
# smoke.sh — assert the agent-node image meets the kickoff contract.
#
# Usage:  smoke.sh <image> [engine_dir]
#
#   <image>       image tag to test (e.g. mini-ork/agent-node:latest)
#   [engine_dir]  path to the mini-ork engine source, mounted read-only at
#                 /opt/mini-ork so `python3 -c "import mini_ork, yaml"`
#                 resolves from a real install. Defaults to the current
#                 working directory (the build.sh convention).
#
# Exits 0 on success; non-zero on the FIRST failing check with a clear
# message on stderr ("smoke: <check>: <reason>"). Designed to be run by
# humans and by tests/unit/test_agent_node_image.py.

set -euo pipefail

IMAGE="${1:-mini-ork/agent-node:latest}"
ENGINE_DIR="${2:-${PWD}}"

if ! command -v docker >/dev/null 2>&1; then
    echo "smoke: docker CLI not found on PATH" >&2
    exit 2
fi

if [ ! -d "${ENGINE_DIR}" ]; then
    echo "smoke: engine_dir missing: ${ENGINE_DIR}" >&2
    echo "smoke: pass the engine source dir as the second arg (or run from inside it)" >&2
    exit 3
fi

# Verify pyproject.toml exists in engine_dir so we know the mount is a real
# mini-ork checkout, not some random directory.
if [ ! -f "${ENGINE_DIR}/pyproject.toml" ]; then
    echo "smoke: engine_dir is missing pyproject.toml: ${ENGINE_DIR}" >&2
    echo "smoke: pass a mini-ork engine checkout (the directory containing pyproject.toml)" >&2
    exit 4
fi

# Run a long-lived instance we can probe. tini + sleep infinity keeps the
# container up; we exec individual checks and rm at exit.
CONTAINER_NAME="smoke-agent-node-$$"
cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --mount --allow
docker run --detach --name "${CONTAINER_NAME}" \
    -v "${ENGINE_DIR}:/opt/mini-ork:ro" \
    "${IMAGE}" >/dev/null

# Wait for the container to be reachable (tini + sleep infinity should be
# ready in well under a second, but docker daemon latency on cold start can
# push past that). 10 attempts × 0.2s = 2s ceiling.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if docker exec "${CONTAINER_NAME}" true 2>/dev/null; then
        break
    fi
    sleep 0.2
done

if ! docker exec "${CONTAINER_NAME}" true 2>/dev/null; then
    echo "smoke: container failed to start (exec returned non-zero after 2s)" >&2
    docker logs "${CONTAINER_NAME}" >&2 || true
    exit 5
fi

# check "<label>" <container_exec_argv...>
check() {
    local label="$1"; shift
    if docker exec "${CONTAINER_NAME}" "$@" >/dev/null 2>&1; then
        printf '  ok  %s\n' "${label}"
    else
        echo "smoke: ${label}: FAILED" >&2
        docker exec "${CONTAINER_NAME}" "$@" 2>&1 | sed 's/^/    | /' >&2 || true
        exit 1
    fi
}

echo "smoke: ${IMAGE} (engine=${ENGINE_DIR})"

# 1. Non-root user: `id -u` must not be 0.
check "user is non-root" \
    sh -c 'test "$(id -u)" != "0"'

# 2. claude --version
check "claude --version" \
    sh -c 'claude --version'

# 3. opencode --version
check "opencode --version" \
    sh -c 'opencode --version'

# 4. git --version
check "git --version" \
    sh -c 'git --version'

# 5. python3 -c "import mini_ork, yaml" succeeds against the mounted engine.
#    PYTHONPATH=/opt/mini-ork is set in the image ENV, but `exec` does not
#    inherit the image's ENV by default in some docker versions — set it
#    explicitly.
check "python3 import mini_ork + yaml" \
    sh -c 'PYTHONPATH=/opt/mini-ork python3 -c "import mini_ork, yaml; assert mini_ork.__name__ == \"mini_ork\""'

# 7. /workspace/target is writable by the agent user.
check "/workspace/target writable" \
    sh -c 'touch /workspace/target/.smoke-write && rm -f /workspace/target/.smoke-write'

echo "smoke: all checks passed"