"""``mini-ork cn status`` — one screenful of ContextNest connectivity.

The ContextNest IDE panels answer "is it on, where, and how fast?" every
refresh. ``cn status`` is the operator-facing equivalent: the same fields,
emitted as JSON (or a one-line text), exit code 0 whether the server is up
or not — "reachable: no" is a valid answer, not an error. The command
intentionally never reveals the bearer token; the secure-tier key is named
so the operator knows which one to export.

Wire: ``GET /api/v1/substrate/health`` with the short ping timeout, plus
the resolved tier/base-url/token-key. Exit codes: 0 always; 2 on a usage
error (e.g. ``mini-ork cn`` with no subcommand).
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


def _probe(base: str, timeout: float) -> tuple[bool, float, int]:
    """One availability probe with the short ping timeout.

    Returns ``(reachable, latency_seconds, http_status)``. ``http_status`` is
    the wire status code, or ``0`` on a connection / timeout error. The
    probe never raises — ``cn status`` returns 0 whether the server is up or
    not."""
    url = f"{base.rstrip('/')}/api/v1/substrate/health"
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            latency = time.monotonic() - start
            return True, latency, int(r.status)
    except urllib.error.HTTPError as exc:
        latency = time.monotonic() - start
        return False, latency, int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        latency = time.monotonic() - start
        return False, latency, 0


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
        return 0

    reachable, latency, http_status = _probe(base, timeout_ping)
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
    return 0


def main(rest: list[str], root: str) -> int:
    del root
    parser = build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return int(exc.code or 2)
    if args.subcommand is None or args.subcommand == "status":
        return _cmd_status(args)
    sys.stderr.write(f"mini-ork cn: unknown subcommand {args.subcommand!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], root=os.getcwd()))
