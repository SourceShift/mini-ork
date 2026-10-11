"""Manage the MCP server registry and audit what a workflow grants.

Nodes reach MCP servers by name; this command owns the other half of that
reference — the connection definition and its auth — and can tell you, before a
run, whether the servers a recipe grants will actually resolve.

Metadata-only, like ``mini-ork providers``: values enter through a hidden
prompt or standard input and land in the owner-only secret store. The registry
file itself holds ``${NAME}`` references, so it stays committable.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

from mini_ork.dispatch.mcp_registry import (
    McpRegistryError,
    load_registry,
    node_mcp_grants,
    operator_registry_path,
    registry_paths,
    resolve_grant,
    write_entry,
    remove_entry,
)
from mini_ork.dispatch.mcp_proxy import run_proxy
from mini_ork.dispatch.vault import vault_environment
from mini_ork.dispatch.secrets import (
    SecretStoreError,
    read_secret_exports,
    secret_store_path,
    write_secret_exports,
)


_USAGE = """Usage:
  mini-ork mcp list [--json]
  mini-ork mcp add <name> (--url <url> | --command <cmd>) [options]
  mini-ork mcp rm <name>
  mini-ork mcp check [--workflow <workflow.yaml>]
  mini-ork mcp launch <name>
  mini-ork mcp proxy <name>

Options for `add` (repeatable):
  --header K=V     HTTP header, e.g. --header 'Authorization=Bearer ${ARXIV_TOKEN}'
  --arg V          stdio server argument
  --env K=V        stdio server environment variable
  --secret NAME    prompt for NAME's value and store it owner-only (0600)
  --replace        overwrite an existing entry

`check` resolves every MCP grant a workflow declares and reports the ones that
would fail at dispatch time — an unset ${NAME}, a disabled entry, a server no
registry defines.

`launch` is not for humans: it is the command `mini-ork zed setup` puts in
Zed's settings.json in place of the server itself. Zed spawns it, it resolves
the entry against the vault, and it execs the real server — so settings.json
names a server but never holds its token.
"""


def _parse_kv(flag: str, raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise ValueError(f"{flag} expects KEY=VALUE, got {raw!r}")
    key, value = raw.split("=", 1)
    return key.strip(), value


def _cmd_list(as_json: bool) -> int:
    registry = load_registry()
    environ = vault_environment()
    rows: list[dict[str, object]] = []
    for name in sorted(registry):
        raw = registry[name]
        transport = str(raw.get("type") or raw.get("source") or ("stdio" if raw.get("command") else "http"))
        target = str(raw.get("url") or raw.get("command") or "")
        # Resolve without raising: `list` reports the breakage `check` would fail
        # on, rather than refusing to show the entry at all.
        try:
            resolve_grant(name, registry, environ)
            status = "ready"
            detail = ""
        except (McpRegistryError, SecretStoreError) as exc:
            status = "unresolved"
            detail = str(exc)
        rows.append(
            {"name": name, "transport": transport, "target": target,
             "status": status, "detail": detail}
        )
    if as_json:
        print(json.dumps({"servers": rows, "registry": str(operator_registry_path())}, indent=2))
        return 0
    print("=== mini-ork MCP registry ===")
    print(f"Registry: {operator_registry_path().resolve()}")
    for path in registry_paths():
        if path.is_file():
            print(f"  reading: {path}")
    if not rows:
        print("No servers defined. Add one with `mini-ork mcp add`.")
        return 0
    print(f"{'Name':<24} {'Transport':<10} {'Target':<44} Status")
    for row in rows:
        print(
            f"{row['name']:<24} {row['transport']:<10} "
            f"{str(row['target'])[:44]:<44} {row['status']}"
        )
        if row["detail"]:
            print(f"{'':<24} {'':<10} {'':<44} {row['detail']}")
    return 0 if all(row["status"] == "ready" for row in rows) else 2


def _cmd_add(argv: list[str]) -> int:
    if not argv:
        raise ValueError("`mcp add` requires a server name")
    name = argv[0]
    url = command = ""
    headers: dict[str, str] = {}
    args: list[str] = []
    env: dict[str, str] = {}
    secrets: list[str] = []
    replace = False
    rest = argv[1:]
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag in {"--url", "--command"}:
            index += 1
            if index >= len(rest):
                raise ValueError(f"{flag} requires a value")
            if flag == "--url":
                url = rest[index]
            else:
                command = rest[index]
        elif flag in {"--header", "--env"}:
            index += 1
            if index >= len(rest):
                raise ValueError(f"{flag} requires KEY=VALUE")
            key, value = _parse_kv(flag, rest[index])
            (headers if flag == "--header" else env)[key] = value
        elif flag == "--arg":
            index += 1
            if index >= len(rest):
                raise ValueError("--arg requires a value")
            args.append(rest[index])
        elif flag == "--secret":
            index += 1
            if index >= len(rest):
                raise ValueError("--secret requires a variable NAME")
            secrets.append(rest[index])
        elif flag == "--replace":
            replace = True
        else:
            raise ValueError(f"unknown option for `mcp add`: {flag}")
        index += 1

    if bool(url) == bool(command):
        raise ValueError("`mcp add` needs exactly one of --url (http/sse) or --command (stdio)")
    existing = load_registry()
    if name in existing and not replace:
        raise ValueError(f"mcp server {name!r} already exists; pass --replace to overwrite")

    entry: dict[str, object] = {"url": url} if url else {"command": command}
    if headers:
        entry["headers"] = headers
    if args:
        entry["args"] = args
    if env:
        entry["env"] = env

    if secrets:
        store = secret_store_path()
        stored = read_secret_exports(store)
        updates: dict[str, str] = {}
        for secret_name in secrets:
            if secret_name in os.environ or secret_name in stored:
                print(f"{secret_name}: already configured; leaving it unchanged.")
                continue
            try:
                value = getpass.getpass(f"{secret_name} (leave blank to skip): ")
            except EOFError as exc:
                raise ValueError(
                    "hidden prompt is unavailable; export the variable or write it "
                    "with `mini-ork providers configure`"
                ) from exc
            if value:
                updates[secret_name] = value
        if updates:
            write_secret_exports(updates, store)
            print(f"Stored {len(updates)} secret(s) in {store.resolve()}.")

    path = write_entry(name, entry)
    print(f"Configured MCP server {name!r} in {path.resolve()}.")
    return _cmd_list(False)


def _cmd_rm(argv: list[str]) -> int:
    if not argv:
        raise ValueError("`mcp rm` requires a server name")
    if not remove_entry(argv[0]):
        print(f"{argv[0]}: not defined in {operator_registry_path().resolve()}", file=sys.stderr)
        return 2
    print(f"Removed MCP server {argv[0]!r}.")
    return 0


def workflow_mcp_grants(workflow: str | Path) -> list[tuple[str, tuple[str, ...]]]:
    """``[(node_id, (server, ...)), ...]`` for every MCP grant in a workflow.

    Reads both grant surfaces — the ``tools.mcp`` block and the ``mcp_servers``
    envelope — because a server reachable through either one still has to
    resolve, and a check that read only one would pass a workflow that fails.
    """
    path = Path(workflow)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid workflow {path}: {exc}") from exc
    nodes = document.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError(f"invalid workflow {path}: 'nodes' must be a list")
    grants: list[tuple[str, tuple[str, ...]]] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_id = str(node.get("name") or "").strip()
        if not node_id:
            continue
        servers = node_mcp_grants(node)
        if servers:
            grants.append((node_id, servers))
    return grants


def _cmd_check(workflow: str | Path) -> int:
    registry = load_registry()
    environ = vault_environment()
    print(f"Registry: {operator_registry_path().resolve()}")
    exit_code = 0
    for node_id, servers in workflow_mcp_grants(workflow):
        for server in servers:
            if server not in registry:
                print(f"{node_id:<24} {server:<24} NO-OP (not in registry)")
                continue
            try:
                resolve_grant(server, registry, environ)
            except (McpRegistryError, SecretStoreError) as exc:
                print(f"{node_id:<24} {server:<24} FAIL {exc}")
                exit_code = 2
            else:
                print(f"{node_id:<24} {server:<24} ready")
    return exit_code


def _resolved_entry(
    name: str, environ: Mapping[str, str]
) -> tuple[dict[str, object] | None, int]:
    """The CLI-shaped config for ``name``, or ``(None, exit_code)`` with a report."""
    registry = load_registry(environ)
    if name not in registry:
        print(
            f"mini-ork mcp: {name!r} is not in the registry "
            f"({operator_registry_path(environ).resolve()})",
            file=sys.stderr,
        )
        return None, 2
    try:
        # Resolves ${NAME} through the vault, so an unset token fails HERE, on a
        # visible stderr line, rather than as an empty header inside an editor.
        return resolve_grant(name, registry, vault_environment(environ)), 0
    except (McpRegistryError, SecretStoreError) as exc:
        print(f"mini-ork mcp: {exc}", file=sys.stderr)
        return None, 2


def proxy_server(name: str, *, environ: Mapping[str, str] | None = None) -> int:
    """Bridge stdio to the URL-based MCP server ``name`` (see ``mcp_proxy``).

    The ``Authorization`` header is resolved here, from the vault, and lives
    only in this process's memory — which is what lets an editor use an
    authenticated HTTP server without the token ever reaching a settings file.
    """
    e = dict(os.environ if environ is None else environ)
    config, code = _resolved_entry(name, e)
    if config is None:
        return code

    url = str(config.get("url") or "")
    if not url:
        print(
            f"mini-ork mcp proxy: {name!r} is a stdio server; "
            f"`mini-ork mcp launch {name}` execs it directly",
            file=sys.stderr,
        )
        return 2
    raw_headers = config.get("headers")
    headers = (
        {str(k): str(v) for k, v in raw_headers.items()}
        if isinstance(raw_headers, Mapping)
        else {}
    )
    return run_proxy(url, headers)


def launch_server(name: str, *, environ: Mapping[str, str] | None = None) -> int:
    """Resolve ``name`` and replace this process with its stdio server.

    This is the whole reason the editor integration exists. Zed's
    ``settings.json`` can hold a *command* but cannot hold a secret, and it does
    not interpolate ``${NAME}``. So the entry names this shim, the shim resolves
    the entry (headers, ``env``, ``${NAME}`` from the vault) where the secret
    already lives, and ``execvp`` makes the real server the process — no wrapper
    surviving in the middle, and no signal, exit code or stdin byte touched on
    the way.

    Returns only on failure; success does not return.
    """
    e = dict(os.environ if environ is None else environ)
    config, code = _resolved_entry(name, e)
    if config is None:
        return code

    if config.get("type") in {"http", "sse"} or config.get("url"):
        # A URL server has no process to replace, so `launch` becomes the
        # bridge. The editor names one command either way, and the token still
        # never reaches settings.json.
        return proxy_server(name, environ=e)

    command = str(config.get("command") or "")
    raw_args = config.get("args")
    arguments = [str(part) for part in raw_args] if isinstance(raw_args, list) else []
    if not command:
        print(f"mini-ork mcp launch: {name!r} resolves to no command", file=sys.stderr)
        return 2

    executable = shutil.which(command)
    if executable is None:
        print(
            f"mini-ork mcp launch: {command!r} is not on PATH "
            f"(needed by mcp server {name!r}); check PATH in the launching context",
            file=sys.stderr,
        )
        return 2

    child_env = dict(e)
    entry_env = config.get("env")
    if isinstance(entry_env, Mapping):
        # The entry's env is already expanded; it is the server's own config,
        # so it wins over the ambient value of the same name.
        child_env.update({str(k): str(v) for k, v in entry_env.items()})
    os.execvpe(executable, [command, *arguments], child_env)
    return 2  # unreachable: execvpe replaces the process or raises


def _cmd_launch(argv: list[str]) -> int:
    if not argv:
        raise ValueError("`mcp launch` requires a server name")
    return launch_server(argv[0])


def main(argv: list[str] | None = None, *, root: str | Path | None = None) -> int:
    """Run ``mini-ork mcp`` without exposing credential material."""
    del root  # the registry is resolved from MINI_ORK_HOME/ROOT in the environment
    rest = list(sys.argv[1:] if argv is None else argv)
    try:
        if not rest or rest[0] in {"help", "--help", "-h"}:
            sys.stdout.write(_USAGE)
            return 0
        action, args = rest[0], rest[1:]
        if action == "list":
            return _cmd_list("--json" in args)
        if action == "add":
            return _cmd_add([token for token in args if token != "--json"])
        if action == "rm":
            return _cmd_rm(args)
        if action == "launch":
            return _cmd_launch(args)
        if action == "proxy":
            if not args:
                raise ValueError("`mcp proxy` requires a server name")
            return proxy_server(args[0])
        if action == "check":
            workflow = ""
            if "--workflow" in args:
                position = args.index("--workflow")
                if position + 1 >= len(args):
                    raise ValueError("--workflow requires a path")
                workflow = args[position + 1]
            if not workflow:
                raise ValueError("`mcp check` requires --workflow <workflow.yaml>")
            return _cmd_check(workflow)
        raise ValueError(f"unknown mcp action: {action}")
    except (McpRegistryError, SecretStoreError, ValueError) as exc:
        print(f"mini-ork mcp: {exc}", file=sys.stderr)
        # `launch` and `proxy` are spawned as stdio servers: stdout IS the
        # transport, so a usage block written there would be read as protocol.
        if not (rest and rest[0] in {"launch", "proxy"}):
            sys.stdout.write(_USAGE)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], root=os.environ.get("MINI_ORK_ROOT", "")))
