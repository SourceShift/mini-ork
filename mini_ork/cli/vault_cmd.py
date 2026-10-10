"""``mini-ork vault`` — report which secret sources are wired, and where a key lives.

Two questions this answers before a run instead of inside one:

* *Is my vault configured at all?* — ``vault status`` lists the backend chain
  in precedence order with each one's readiness. The default chain (environment
  then the local 0600 file) needs no setup, so "ready" here is the common case.
* *Which backend will serve ``ARXIV_API_TOKEN``?* — ``vault which`` walks the
  chain and names the first source that has the key.

Neither reads a value out. A command that prints secrets is a command that ends
up in a shell history file and a screen-share, so the answer is always a name and
a location, never the secret. ``mini-ork mcp check`` is the companion command
that verifies the *grants* resolve.
"""

from __future__ import annotations

import os
import sys

from mini_ork.dispatch.vault import (
    EnvBackend,
    FileBackend,
    VaultError,
    vault_config_path,
    vault_environment,
)

_USAGE = """Usage:
  mini-ork vault status [--json]
  mini-ork vault which <NAME>

`status` lists the backend chain in precedence order and whether each can serve
values. `which` names the first backend that holds NAME, without printing it.

Backends are declared in $MINI_ORK_HOME/config/vault.yaml (see
config/vault.example.yaml). With no such file the chain is: environment, then
the owner-only secrets.local.sh — no server, no daemon, no network.
"""


def _backend_kind(backend: object) -> str:
    """A short, stable label for a backend instance."""
    if isinstance(backend, EnvBackend):
        return "environment"
    if isinstance(backend, FileBackend):
        return "file (0600)"
    return getattr(backend, "name", "unknown")


def _cmd_status(as_json: bool) -> int:
    path = vault_config_path()
    try:
        environment = vault_environment()
    except VaultError as exc:
        # An unreadable or mistyped vault.yaml is the one case a status command
        # must not paper over: it means every lookup will fail.
        print(f"mini-ork vault: {exc}", file=sys.stderr)
        return 2
    statuses = environment.status()
    if as_json:
        import json

        print(json.dumps(
            {
                "config": str(path) if path.is_file() else None,
                "backends": [
                    {"backend": _backend_kind(environment.backends[index]),
                     "ready": status.ready,
                     "detail": status.detail,
                     "source": status.source}
                    for index, status in enumerate(statuses)
                ],
            },
            indent=2,
        ))
        return 0 if all(status.ready for status in statuses) else 2
    print("=== mini-ork vault ===")
    print(f"Config: {path.resolve() if path.is_file() else 'not present (default chain)'}")
    print(f"{'#':<3} {'Backend':<16} {'Ready':<6} Detail")
    for index, status in enumerate(statuses):
        backend = environment.backends[index]
        print(f"{index:<3} {_backend_kind(backend):<16} "
              f"{'yes' if status.ready else 'NO':<6} {status.detail}")
        if status.source:
            print(f"{'':<3} {'':<16} {'':<6} {status.source}")
    return 0 if all(status.ready for status in statuses) else 2


def _cmd_which(name: str) -> int:
    try:
        environment = vault_environment()
    except VaultError as exc:
        print(f"mini-ork vault: {exc}", file=sys.stderr)
        return 2
    if not name:
        print("mini-ork vault which: NAME is required", file=sys.stderr)
        return 2
    # Resolve through the same chain the dispatcher uses, so a name reported
    # here is a name that will actually resolve at dispatch time.
    for backend in environment.backends:
        try:
            found = backend.get(name) is not None
        except VaultError as exc:
            print(f"{name}: {_backend_kind(backend)} — {exc}", file=sys.stderr)
            return 2
        if found:
            print(f"{name}: {_backend_kind(backend)} — {backend.describe()}")
            return 0
    print(f"{name}: not found in any backend ({environment.describe()})")
    return 1


def main(argv: list[str] | None = None, *, root: str | None = None) -> int:
    """Run ``mini-ork vault`` without ever printing a secret value."""
    del root  # the chain is resolved from MINI_ORK_HOME in the environment
    rest = list(sys.argv[1:] if argv is None else argv)
    if not rest or rest[0] in {"help", "--help", "-h"}:
        sys.stdout.write(_USAGE)
        return 0
    action, args = rest[0], rest[1:]
    try:
        if action == "status":
            return _cmd_status("--json" in args)
        if action == "which":
            return _cmd_which(args[0] if args else "")
        raise ValueError(f"unknown vault action: {action}")
    except (VaultError, ValueError) as exc:
        print(f"mini-ork vault: {exc}", file=sys.stderr)
        sys.stdout.write(_USAGE)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], root=os.environ.get("MINI_ORK_ROOT", "")))
