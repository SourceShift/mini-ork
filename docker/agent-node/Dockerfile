# syntax=docker/dockerfile:1.7
#
# mini-ork agent-node image.
#
# One image, any engine version: the engine (mini-ork source) is mounted
# read-only at /opt/mini-ork by the node-agent (D8 in
# docs/architecture/remote-nodes.md). This Dockerfile bakes only the
# toolchain the agent nodes need — no engine code, no secrets.
#
# Build (single arch, load into the local daemon):
#   docker buildx build --load -t mini-ork/agent-node:latest \
#     --build-arg CLAUDE_CODE_VERSION=<x.y.z> \
#     --build-arg OPENCODE_VERSION=<x.y.z> .
#
# Multi-arch (requires a running buildx builder named `multiarch`):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t mini-ork/agent-node:<sha> -t mini-ork/agent-node:latest \
#     --build-arg CLAUDE_CODE_VERSION=<x.y.z> \
#     --build-arg OPENCODE_VERSION=<x.y.z> --push .

ARG DEBIAN_BASE=debian:bookworm-slim
FROM ${DEBIAN_BASE}

# --- pinned toolchain versions (overridable at build time) ------------------
# Default values are stable-but-loose; CI / release builds pin these via
# `docker build --build-arg CLAUDE_CODE_VERSION=<exact>` so the image is
# reproducible. With-Codex is off by default: enable with --build-arg WITH_CODEX=1.
ARG CLAUDE_CODE_VERSION=latest
ARG OPENCODE_VERSION=latest
ARG WITH_CODEX=0
ARG CODEX_VERSION=latest

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- system packages ---------------------------------------------------------
# `build-essential` is intentional: some node_modules + Python deps compile
# native extensions (e.g. cryptography wheels fall back to source on unusual
# arches). The layer is large but stable.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        openssh-client \
        python3 \
        python3-pip \
        ripgrep \
        tini \
        build-essential \
 && rm -rf /var/lib/apt/lists/*

# --- Python 3.11 from the deadsnakes-style backports -------------------------
# `debian:bookworm-slim` ships python3.11 by default; this RUN keeps the
# `python3` -> python3.11 mapping explicit so a future base bump to
# `debian:trixie-slim` (python3.12) gets caught here, not silently shipped.
RUN if ! command -v python3.11 >/dev/null 2>&1; then \
        apt-get update \
      && apt-get install -y --no-install-recommends python3.11 python3.11-venv \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python3.11

# --- Node 22 LTS via NodeSource ---------------------------------------------
# The agent CLIs (`claude`, `opencode`, `@openai/codex`) are npm packages and
# must run on a current LTS line. Pinning the Node major keeps glibc /
# prebuilt-binary expectations stable across image rebuilds.
ARG NODE_MAJOR=22
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" \
        | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && node --version \
 && npm --version

# --- CLI tools: claude-code, opencode, (optional) codex ---------------------
# Globally installed so `/opt/mini-ork/bin` (engine mount) does not have to
# shadow them. The agent shells out to these bare names per
# `mini_ork/dispatch/providers.py`.
RUN npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
 && if [ "${WITH_CODEX}" = "1" ]; then \
        npm install -g "@openai/codex@${CODEX_VERSION}"; \
    fi \
 && npm cache clean --force \
 && which claude \
 && which opencode

# --- mini-ork base Python deps (matches pyproject.toml [project].dependencies)
# Non-editable on purpose: the engine mount is read-only. An editable install
# would point at /opt/mini-ork and break the first time the engine tree is
# replaced with a new sha.
COPY pyproject.toml /tmp/mini-ork-pyproject/pyproject.toml
RUN pip install --no-cache-dir \
        "PyYAML>=6.0" \
        "jsonschema>=4.0"

# --- non-root user ----------------------------------------------------------
# Claude Code refuses `bypassPermissions` when run as root, and dispatch sets it
# (`mini_ork/dispatch/providers.py:479`). So the agent must run as a non-root
# user. uid 1000 is the standard unprivileged user on Debian-based images.
RUN groupadd --gid 1000 agent \
 && useradd  --uid 1000 --gid agent --shell /bin/bash --create-home agent

# --- workspace layout --------------------------------------------------------
# /workspace/target is the run target repo (D4 row 1 in
# docs/architecture/remote-nodes.md). /workspace/home is the agent's HOME
# (claude / opencode / codex drop their session caches there). The engine
# itself is mounted at /opt/mini-ork by the node-agent at session start;
# the image does not pre-create it.
RUN mkdir -p /workspace/target /workspace/home \
 && chown -R agent:agent /workspace

ENV HOME=/workspace/home \
    WORKDIR=/workspace/target

# --- runtime environment -----------------------------------------------------
# PYTHONPATH lets `import mini_ork` resolve from the mounted engine at
# /opt/mini-ork without a per-session pip step. MO_REMOTE_NODE=1 is the flag
# that disables recursive child spawn — load-bearing for the security posture
# on remote nodes (docs/architecture/remote-nodes.md, security posture block).
ENV PYTHONPATH=/opt/mini-ork \
    PATH=/opt/mini-ork/bin:$PATH \
    MO_REMOTE_NODE=1

WORKDIR /workspace/target
USER agent:agent

# --- entrypoint --------------------------------------------------------------
# `docker run` with no command invokes `tini -- sleep infinity`, which keeps
# the container alive for the node-agent to `docker exec` into. The actual
# CLI invocations (claude, opencode, codex) are launched via `docker exec`,
# not by ENTRYPOINT — see smoke.sh for the contract.
ENTRYPOINT ["tini", "--"]
CMD ["sleep", "infinity"]