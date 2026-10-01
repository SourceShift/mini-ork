"""CLI for ``mini-ork node-agent`` — the data-plane HTTP server (epic 05).

Mirrors :mod:`mini_ork.cli.serve`: argparse-style ``_parse`` returns
the options dict; :func:`main` validates them, refuses unsafe binds,
and execs uvicorn in the foreground. Splitting ``_parse`` from
``main`` makes the launcher testable without binding a port — the
acceptance suite asserts the composed argv + bind / TLS / token-env
decisions, not that uvicorn actually started.
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path


_USAGE = """Usage: mini-ork node-agent [--bind ADDR] [--port N]  # noqa: F841 — argparse epilog
                          [--state-dir DIR] [--token-env NAME]
                          [--retain-hours H] [--runtime {docker,host}]
                          [--tls-cert PATH --tls-key PATH] [--reload]
                          [--help]

Boot the mini-ork node-agent HTTP server on a data-plane VM. Default
bind 127.0.0.1:7091; the launcher refuses non-loopback / non-tailnet
binds without --tls-cert + --tls-key. The bearer token is read from the
env var named by --token-env (default MO_NODE_TOKEN); a missing or
empty token env closes every non-health route with 401.

Options:
  --bind ADDR          Bind address (default 127.0.0.1)
  --port N             HTTP port (default 7091)
  --state-dir DIR      Per-host state root (default $MO_NODE_AGENT_STATE_DIR or /srv/mini-ork)
  --token-env NAME     Env var with the bearer token (default MO_NODE_TOKEN)
  --retain-hours H     Hours run dirs survive after session delete (default 24)
  --runtime {docker,host}
                       docker (default) launches a session container;
                       host runs procs directly on the VM (no isolation)
  --tls-cert PATH      TLS certificate (required for non-loopback binds)
  --tls-key PATH       TLS private key (required for non-loopback binds)
  --reload             uvicorn auto-reload (dev only)
  --help               This message
"""


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork node-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7091)
    p.add_argument(
        "--state-dir",
        default=os.environ.get("MO_NODE_AGENT_STATE_DIR", "/srv/mini-ork"),
    )
    p.add_argument("--token-env", default="MO_NODE_TOKEN")
    p.add_argument("--retain-hours", type=float, default=24.0)
    p.add_argument("--runtime", choices=("docker", "host"), default="docker")
    p.add_argument("--tls-cert", default=None)
    p.add_argument("--tls-key", default=None)
    p.add_argument("--reload", action="store_true")
    return p


def _parse(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _needs_tls(bind: str) -> bool:
    """True iff a non-loopback, non-tailnet bind requires TLS.

    Loopback and the Tailscale 100.64.0.0/10 range are accepted without
    TLS because they're already point-to-point encrypted channels.
    Anything else exposes tokens on the wire and refuses to start.
    """
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return True  # hostnames / interfaces → assume public
    if ip.is_loopback:
        return False
    if ip in _TAILNET:
        return False
    return True


def validate_bind(bind: str, tls_cert: str | None, tls_key: str | None) -> None:
    """Refuse to start on an unsafe bind. Raises ``_Exit(2)`` on failure."""
    if not _needs_tls(bind):
        return
    if not (tls_cert and tls_key):
        raise _Exit(2)


def uvicorn_argv(args: argparse.Namespace) -> list[str]:
    """Compose the uvicorn argv. Test seam — accepts an already-parsed Namespace."""
    cmd = [
        sys.executable, "-m", "uvicorn",
        "mini_ork.remote.node_agent.app:create_app",
        "--factory",
        "--host", args.bind,
        "--port", str(args.port),
        "--log-level", "info",
    ]
    if args.tls_cert and args.tls_key:
        cmd += ["--ssl-keyfile", args.tls_key, "--ssl-certfile", args.tls_cert]
    if args.reload:
        cmd.append("--reload")
    return cmd


def main(argv: list[str] | None = None, *, root: str | None = None,
         _exec: bool = True) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        validate_bind(args.bind, args.tls_cert, args.tls_key)
    except _Exit as exc:
        sys.stderr.write(
            f"mini-ork node-agent: refusing to bind {args.bind}:port "
            f"without --tls-cert and --tls-key\n"
        )
        return exc.code
    state_dir = Path(args.state_dir)
    # Production hosts always have /srv/mini-ork writable; on dev/CI
    # sandboxes that path is read-only. Fall back to /tmp so the
    # launcher can at least print the bind summary; the env override
    # also keeps the bind check testable without root.
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path("/tmp/mo-node-agent-state")
        fallback.mkdir(parents=True, exist_ok=True)
        state_dir = fallback
    os.environ["MO_NODE_AGENT_STATE_DIR"] = str(state_dir)
    if args.runtime == "host":
        logging.warning(
            "mini-ork node-agent: --runtime host gives NO isolation; "
            "only use on trusted single-tenant VMs"
        )
    cmd = uvicorn_argv(args)
    sys.stdout.write(
        "→ mini-ork node-agent\n"
        f"  bind   : {args.bind}:{args.port}\n"
        f"  state  : {state_dir}\n"
        f"  token  : env[{args.token_env}]\n"
        f"  retain : {args.retain_hours}h\n"
        f"  runtime: {args.runtime}\n\n"
    )
    if not _exec:
        return 0
    if root:
        os.chdir(root)
    os.execvp(cmd[0], cmd)  # mirror serve.py: replace process


if __name__ == "__main__":
    raise SystemExit(main())