#!/usr/bin/env bash
# build.sh — wrapper around `docker buildx build` for the agent-node image.
#
# Tags the image as:
#   mini-ork/agent-node:<short-sha>   (the engine's current HEAD — D8)
#   mini-ork/agent-node:latest        (a local-dev alias)
#
# Usage:  build.sh [--multiarch] [--push] [--no-load]
#
#   --multiarch   build for linux/amd64,linux/arm64 via buildx (requires a
#                 running buildx builder named `multiarch` with QEMU binfmt).
#                 Default is the host platform only (linux/amd64 on x86,
#                 linux/arm64 on M-series).
#   --push        push the resulting image to a registry (implies --multiarch
#                 and disables --load). Without this flag, single-arch builds
#                 are loaded into the local daemon and multi-arch builds are
#                 exported to `docker-load` tarballs under ./dist/.
#   --no-load     skip `docker load` after a multi-arch build (CI use).
#
# Build-arg passthrough: CLAUDE_CODE_VERSION, OPENCODE_VERSION, WITH_CODEX,
# CODEX_VERSION. Defaults to "latest" for the pinned CLI versions, matching
# the Dockerfile's ARG defaults. Export them in the env before invoking, or
# pass them via `--build-arg` after the flags are parsed below.

set -euo pipefail

MULTIARCH=0
PUSH=0
LOAD=1
BUILDX_FLAGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --multiarch)  MULTIARCH=1 ;;
        --push)       PUSH=1; LOAD=0 ;;
        --no-load)    LOAD=0 ;;
        --load)       LOAD=1 ;;
        --build-arg)  BUILDX_FLAGS+=("--build-arg" "$2"); shift ;;
        --build-arg=*) BUILDX_FLAGS+=("--build-arg" "${1#--build-arg=}") ;;
        -h|--help)
            sed -n '2,25p' "$0"
            exit 0
            ;;
        *)
            echo "build.sh: unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
done

if ! command -v docker >/dev/null 2>&1; then
    echo "build.sh: docker CLI not found on PATH" >&2
    exit 3
fi

# Resolve engine HEAD sha. If we're inside the engine checkout, prefer
# `git rev-parse --short HEAD` so the tag tracks the actual engine version
# (D8). If git is unavailable, fall back to a timestamp tag.
if command -v git >/dev/null 2>&1 && git rev-parse --short HEAD >/dev/null 2>&1; then
    SHORT_SHA="$(git rev-parse --short=12 HEAD)"
else
    SHORT_SHA="ts-$(date -u +%Y%m%d%H%M%S)"
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILDER_NAME="${BUILDER_NAME:-multiarch}"

if [ "${MULTIARCH}" -eq 1 ]; then
    if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
        echo "build.sh: buildx builder '${BUILDER_NAME}' not found." >&2
        echo "build.sh: create one with:" >&2
        echo "  docker buildx create --name ${BUILDER_NAME} --driver docker-container --bootstrap" >&2
        exit 4
    fi
    PLATFORM="linux/amd64,linux/arm64"
    BUILDX_CMD=(docker buildx build --builder "${BUILDER_NAME}")
else
    # The image runs on the DAEMON's architecture, not the client's: on a Mac
    # `uname -m` says arm64 (no `linux/` prefix, and not aarch64), while the
    # colima/Docker Desktop VM reports its own arch here.
    PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"
    BUILDX_CMD=(docker buildx build)
fi

# The context is the repo root (the Dockerfile COPYs pyproject.toml); the
# sibling Dockerfile.dockerignore narrows it to that one file.
BUILDX_CMD+=("-f" "${REPO_ROOT}/docker/agent-node/Dockerfile")

TAG_FLAGS=(
    "-t" "mini-ork/agent-node:${SHORT_SHA}"
    "-t" "mini-ork/agent-node:latest"
)

if [ "${PUSH}" -eq 1 ]; then
    BUILDX_CMD+=("--push" "--platform" "${PLATFORM}" "${TAG_FLAGS[@]}" "${BUILDX_FLAGS[@]}" "${REPO_ROOT}")
elif [ "${MULTIARCH}" -eq 1 ] && [ "${LOAD}" -eq 1 ]; then
    mkdir -p "${REPO_ROOT}/dist"
    OUT_TAR="${REPO_ROOT}/dist/agent-node-${SHORT_SHA}.tar"
    BUILDX_CMD+=("--platform" "${PLATFORM}" "--output" "type=docker,dest=${OUT_TAR}" "${TAG_FLAGS[@]}" "${BUILDX_FLAGS[@]}" "${REPO_ROOT}")
elif [ "${MULTIARCH}" -eq 1 ]; then
    BUILDX_CMD+=("--platform" "${PLATFORM}" "${TAG_FLAGS[@]}" "${BUILDX_FLAGS[@]}" "${REPO_ROOT}")
else
    BUILDX_CMD+=("--platform" "${PLATFORM}" "--load" "${TAG_FLAGS[@]}" "${BUILDX_FLAGS[@]}" "${REPO_ROOT}")
fi

echo "build.sh: PLATFORM=${PLATFORM}  SHORT_SHA=${SHORT_SHA}  PUSH=${PUSH}  LOAD=${LOAD}"
echo "build.sh: ${BUILDX_CMD[*]}"

"${BUILDX_CMD[@]}"

if [ "${MULTIARCH}" -eq 1 ] && [ "${LOAD}" -eq 1 ] && [ "${PUSH}" -eq 0 ]; then
    echo "build.sh: loading ${OUT_TAR} into local daemon"
    docker load -i "${OUT_TAR}"
fi

echo "build.sh: tagged mini-ork/agent-node:${SHORT_SHA} and :latest"