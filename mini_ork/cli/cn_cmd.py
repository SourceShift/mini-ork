"""``mini-ork cn status`` — one screenful of ContextNest connectivity.

The ContextNest IDE panels answer "is it on, where, and how fast?" every
refresh. ``cn status`` is the operator-facing equivalent: the same fields,
emitted as JSON (or a one-line text), exit code 0 whether the server is up
or not — "reachable: no" is a valid answer, not an error. The command
intentionally never reveals the bearer token; the secure-tier key is named
so the operator knows which one to export.

On the secure tier a missing token is a CONFIG error, not a "down" answer:
the command exits non-zero, names the missing KEY (never the value) on
stderr, and refuses to probe the server. Scripts and CI can therefore
distinguish "fix the secret store" (exit 5) from "server is down" (exit 0).

Wire: ``GET /api/v1/substrate/health`` with the short ping timeout, plus
the resolved tier/base-url/token-key. Exit codes: 0 always for reachability
questions; 2 on a usage error (e.g. ``mini-ork cn`` with no subcommand);
5 on a secure-tier secret-store misconfiguration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from mini_ork import cn_client
from mini_ork.dispatch.secrets import SecretStoreError

# Exit codes — kept small and named for the caller, not the implementation.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SECRET_CONFIG = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork cn",
        description="ContextNest client status",
        add_help=True,
    )
    sub = parser.add_subparsers(dest="subcommand")
    status = sub.add_parser("status", help="show tier, endpoint, reachability, latency")
    status.add_argument("--json", action="store_true",
                        help="emit JSON instead of a one-line text")
    return parser


def _probe(base: str, timeout: float, headers: dict | None = None) -> tuple[bool, float, int]:
    """One availability probe with the short ping timeout.

    Returns ``(reachable, latency_seconds, http_status)``. ``http_status`` is
    the wire status code, or ``0`` on a connection / timeout error. The
    probe never raises — ``cn status`` returns 0 whether the server is up or
    not. ``headers`` is attached to the request so the secure-tier probe
    sends the bearer token; the simple tier passes ``None`` and gets a bare
    GET.
    """
    url = f"{base.rstrip('/')}/api/v1/substrate/health"
    start = time.monotonic()
    if headers:
        req = urllib.request.Request(url, headers=headers)
    else:
        req = url
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            latency = time.monotonic() - start
            return True, latency, int(r.status)
    except urllib.error.HTTPError as exc:
        latency = time.monotonic() - start
        return False, latency, int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        latency = time.monotonic() - start
        return False, latency, 0


def _report_secret_error(exc: SecretStoreError, *, token_key: str,
                         base: str, tier: str, as_json: bool) -> int:
    """Surface a secure-tier missing-token error on stderr (key name only,
    never the value), emit a structured payload on stdout for ``--json``,
    and return a non-zero exit code. The token value is never read here
    because ``SecretStoreError`` only carries the key name."""
    # Use the resolved key name when the exception message is too generic;
    # the operator needs to know which ``export`` to add to their store.
    key = token_key or "<unset>"
    msg = f"mini-ork cn: secure tier secret store is missing key {key!r}: {exc}"
    sys.stderr.write(msg + "\n")
    if as_json:
        # Structured payload on stdout so ``--json`` callers can still parse
        # the diagnostic programmatically. ``token_key`` is the only field
        # from the misconfigured state — no value is ever present.
        payload = {
            "tier": tier,
            "endpoint": base,
            "token_key": token_key or None,
            "reachable": False,
            "latency_ms": 0,
            "http_status": 0,
            "disabled": False,
            "error": "secret_store_missing_key",
            "error_key": key,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return EXIT_SECRET_CONFIG


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = cn_client._tier_config()
    base = cfg["base_url"]
    tier = cfg["tier"]
    token_key = cfg["token_key"]
    timeout_ping = float(cfg["timeout_ping_s"])

    if os.environ.get("MO_DISABLE_CN", "0") == "1":
        payload = {
            "tier": tier,
            "endpoint": base,
            "token_key": token_key if tier == "secure" else None,
            "reachable": False,
            "latency_ms": 0,
            "http_status": 0,
            "disabled": True,
            "note": "MO_DISABLE_CN=1",
        }
        if args.json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(f"cn: {tier} · {base} · disabled (MO_DISABLE_CN=1)\n")
        return EXIT_OK

    # Resolve the bearer token BEFORE the network probe so a secure-tier
    # misconfiguration fails closed here too — a missing key is a config
    # error, not a "server is down" answer, and ``cn status`` is the one
    # place an operator runs first when something looks wrong. The
    # ``SecretStoreError`` carries the KEY name only, never the value.
    try:
        headers = cn_client._auth_headers()
    except SecretStoreError as exc:
        return _report_secret_error(exc, token_key=token_key, base=base,
                                    tier=tier, as_json=args.json)

    reachable, latency, http_status = _probe(base, timeout_ping, headers=headers or None)
    payload = {
        "tier": tier,
        "endpoint": base,
        "token_key": token_key if tier == "secure" else None,
        "reachable": reachable,
        "latency_ms": int(round(latency * 1000)),
        "http_status": http_status,
        "disabled": False,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        tag = "reachable" if reachable else "not reachable"
        sys.stdout.write(
            f"cn: {tier} · {base} · {tag} · {payload['latency_ms']} ms "
            f"(http {http_status})\n"
        )
    return EXIT_OK


def main(rest: list[str], root: str) -> int:
    del root
    parser = build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    if args.subcommand is None or args.subcommand == "status":
        return _cmd_status(args)
    sys.stderr.write(f"mini-ork cn: unknown subcommand {args.subcommand!r}\n")
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], root=os.getcwd()))
