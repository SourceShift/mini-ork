"""The MCP server registry: named connection definitions, resolved per grant.

A recipe grants MCP servers to a node by name (``tools.mcp`` in
``workflow.yaml`` or the ``mcp_servers`` capability envelope). The name is a
*reference*; this module owns the other half — the connection definition, its
auth, and the rule that a token lives in the secret store rather than in the
file that names it.

Shape. Entries accept either the editor-side shape Zed writes
(``context_servers``: ``source: stdio|http`` with ``headers`` / ``oauth``) or
the transport shape the ``claude`` CLI consumes (``command`` / ``type: http``),
so an operator can paste a Zed entry unchanged and the registry translates it.
Normalization happens here, at grant time, so the emitted config is always the
CLI shape.

Auth. Values of the form ``${NAME}`` (or ``${NAME:-fallback}``) are expanded
from the secret store (see ``mini_ork.dispatch.secrets``) overlaid with the
run's runtime environment. An unresolvable placeholder is an error, not an
empty string: a server granted with a silently-empty ``Authorization`` header
fails at call time, deep inside a node, where the cause is invisible. Resolve
loudly here instead.

Source order (first definition of a name wins): the run's own
``<run_dir>/config/mcp_servers.json``, then the operator's
``$MINI_ORK_HOME/config/mcp_servers.json``, then the repo's
``.claude/mcp_servers.json`` and ``mcp/steering.json``. The run-scoped file is
what makes a boundary run reproducible: it pins the servers that run used
without editing anything shared.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from .secrets import SecretStoreError, expand_secret_references


class McpRegistryError(ValueError):
    """A registry entry cannot be resolved into a runnable MCP server."""


# Keys the editor shape adds that the CLI either renames or has no concept of.
# The CLI would treat a stray ``source`` as an unknown stdio server (no
# ``command``) and fail to spawn, which is why these are dropped rather than
# passed through.
_EDITOR_ONLY_KEYS = frozenset({"source", "enabled", "remote", "oauth", "settings"})

# Zed's per-server tool-call timeout. The CLI has no equivalent knob; keeping
# it would be an unknown key in the spawn config.
_CLI_UNKNOWN_KEYS = frozenset({"timeout", "description"})


def registry_paths(env: Mapping[str, str] | None = None) -> list[Path]:
    """Registry files in precedence order (first definition of a name wins)."""
    e = os.environ if env is None else env
    paths: list[Path] = []
    run_dir = str(e.get("MINI_ORK_RUN_DIR") or "").strip()
    home = str(e.get("MINI_ORK_HOME") or "").strip()
    root = str(e.get("MINI_ORK_ROOT") or "").strip()
    if run_dir:
        paths.append(Path(run_dir) / "config" / "mcp_servers.json")
    if home:
        paths.append(Path(home) / "config" / "mcp_servers.json")
    if root:
        paths.append(Path(root) / ".claude" / "mcp_servers.json")
        paths.append(Path(root) / "mcp" / "steering.json")
    return paths


def operator_registry_path(env: Mapping[str, str] | None = None) -> Path:
    """The one file ``mini-ork mcp add|rm`` writes.

    Only the operator home is writable: the repo files are committed templates
    and the run file is a run's own frozen snapshot.
    """
    e = os.environ if env is None else env
    home = str(e.get("MINI_ORK_HOME") or ".mini-ork").strip()
    return Path(home) / "config" / "mcp_servers.json"


def _read_servers(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A malformed registry is worth a warning, not a dead run: the node's
        # grant falls back to the no-op stub below, which is loud in the DAG.
        sys.stderr.write(f"[mcp] ignoring unreadable registry {path}: {exc}\n")
        return {}
    if not isinstance(data, dict):
        sys.stderr.write(f"[mcp] ignoring registry {path}: top level is not an object\n")
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        # Bare ``{name: {...}}`` (no ``mcpServers`` wrapper) is accepted so a
        # hand-written file does not need the envelope.
        servers = data
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def load_registry(env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Merge every registry source, earliest source winning per name."""
    merged: dict[str, dict] = {}
    for path in registry_paths(env):
        for name, entry in _read_servers(path).items():
            merged.setdefault(name, entry)
    return merged


def node_mcp_grants(node: Mapping[str, object]) -> tuple[str, ...]:
    """The MCP server names one workflow node grants, deduplicated in grant order.

    Reads both grant surfaces — the ``tools.mcp`` block and the ``mcp_servers``
    envelope — because a server reachable through either one still has to
    resolve, and a check that read only one would pass a workflow that fails.
    Lives here rather than in the CLI so ``mini-ork mcp check`` and the IDE's
    node inspector resolve a node's grants through one implementation.
    """
    servers: list[str] = []
    tools = node.get("tools")
    if isinstance(tools, Mapping):
        for token in str(tools.get("mcp") or "").strip("[]").split(","):
            if token.strip().strip("'\""):
                servers.append(token.strip().strip("'\""))
    declared = node.get("mcp_servers") or []
    if isinstance(declared, str):
        declared = declared.split(",")
    if isinstance(declared, list):
        servers.extend(str(item).strip() for item in declared if str(item).strip())
    return tuple(dict.fromkeys(servers))


# ── placeholder resolution ───────────────────────────────────────────────────


def expand_placeholders(
    value: str, environ: Mapping[str, str], *, server: str
) -> str:
    """Substitute ``${NAME}`` / ``${NAME:-fallback}`` from ``environ``.

    ``secrets.expand_secret_references`` owns the algorithm; the wrapper only
    re-labels its failure so a caller inspecting the registry sees the server
    name, not just the variable. Resolving here rather than at call time is the
    point: a server granted with an unset key must stop the node at grant time.
    """
    try:
        return expand_secret_references(value, environ, context=f"mcp server {server!r}")
    except SecretStoreError as exc:
        raise McpRegistryError(str(exc)) from exc


def _expand_tree(node: object, environ: Mapping[str, str], *, server: str) -> object:
    if isinstance(node, str):
        return expand_placeholders(node, environ, server=server)
    if isinstance(node, list):
        return [_expand_tree(item, environ, server=server) for item in node]
    if isinstance(node, dict):
        return {k: _expand_tree(v, environ, server=server) for k, v in node.items()}
    return node


# ── transport normalization ──────────────────────────────────────────────────


def normalize_entry(name: str, raw: Mapping[str, object]) -> dict[str, object]:
    """Translate an editor-shaped or CLI-shaped entry into the CLI's shape.

    Editor-only keys are dropped with a warning when they carry a capability
    this runtime cannot honour (``oauth``, ``settings``), so an operator who
    configured OAuth in Zed learns that mini-ork will not use it instead of
    quietly running unauthenticated.
    """
    entry = dict(raw)
    source = str(entry.get("source") or "").strip().lower()
    if source == "extension":
        raise McpRegistryError(
            f"mcp server {name!r} is extension-provided; mini-ork nodes run "
            "outside the editor, so only stdio/http entries can be granted"
        )
    if source == "oauth" or entry.get("oauth"):
        sys.stderr.write(
            f"[mcp] warning: {name!r} declares oauth; mini-ork uses "
            "static headers only — provide a pre-obtained token in headers\n"
        )
    if entry.get("settings"):
        sys.stderr.write(
            f"[mcp] warning: {name!r} is an extension settings blob; "
            "only stdio/http entries can be granted to a node\n"
        )

    explicit_type = str(entry.get("type") or "").strip().lower()
    if source == "http" or explicit_type in {"http", "sse"}:
        transport = "sse" if explicit_type == "sse" or source == "sse" else "http"
        url = str(entry.get("url") or "").strip()
        if not url:
            raise McpRegistryError(f"mcp server {name!r} is {transport} but declares no url")
        headers = entry.get("headers")
        normalized: dict[str, object] = {"type": transport, "url": url}
        if isinstance(headers, dict) and headers:
            normalized["headers"] = headers
        return normalized

    command = str(entry.get("command") or "").strip()
    if not command:
        raise McpRegistryError(
            f"mcp server {name!r} declares neither a command (stdio) nor a url "
            "(http/sse); can it be reached from a node?"
        )
    normalized = {"command": command}
    args = entry.get("args")
    if isinstance(args, list) and args:
        normalized["args"] = args
    env_block = entry.get("env")
    if isinstance(env_block, dict) and env_block:
        normalized["env"] = env_block
    return normalized


def resolve_grant(
    name: str,
    registry: Mapping[str, Mapping[str, object]],
    environ: Mapping[str, str],
) -> dict[str, object]:
    """The CLI-shaped config for one granted server, placeholders expanded."""
    raw = registry.get(name)
    if raw is None:
        # Granting an unconfigured server stays a no-op stub rather than an
        # error: ``--strict-mcp-config`` must still see a hermetic config, and
        # the DAG shows the stub output when the node calls it.
        return {"command": "echo", "args": [f"no-op: {name} not configured"]}
    if raw.get("enabled") is False:
        raise McpRegistryError(
            f"mcp server {name!r} is granted by a node but disabled in the "
            "registry; enable it or drop the grant"
        )
    stripped = {k: v for k, v in raw.items() if k not in _EDITOR_ONLY_KEYS | _CLI_UNKNOWN_KEYS}
    expanded = _expand_tree(stripped, environ, server=name)
    return normalize_entry(name, expanded)  # type: ignore[arg-type]


def scoped_config(
    granted: Iterable[str],
    registry: Mapping[str, Mapping[str, object]],
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """``{name: config}`` for exactly the granted servers, in grant order."""
    return {name: resolve_grant(name, registry, environ) for name in granted}


# ── writes (the ``mini-ork mcp`` CLI) ────────────────────────────────────────


def write_entry(name: str, entry: Mapping[str, object], env: Mapping[str, str] | None = None) -> Path:
    """Add or replace one entry in the operator registry. Returns its path."""
    path = operator_registry_path(env)
    servers = _read_servers(path)
    servers[name] = dict(entry)
    _write_servers(path, servers)
    return path


def remove_entry(name: str, env: Mapping[str, str] | None = None) -> bool:
    """Drop one entry. ``False`` when it was not defined there."""
    path = operator_registry_path(env)
    servers = _read_servers(path)
    if name not in servers:
        return False
    del servers[name]
    _write_servers(path, servers)
    return True


def _write_servers(path: Path, servers: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "_comment": [
            "MCP servers granted to mini-ork nodes by name.",
            "Secrets never go here: use ${NAME} and export NAME in"
            " $MINI_ORK_HOME/config/secrets.local.sh.",
        ],
        "mcpServers": dict(sorted(servers.items())),
    }
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise McpRegistryError(f"cannot write MCP registry {path}: {exc}") from exc
