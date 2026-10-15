"""``mini-ork cn`` — the operator-facing ContextNest verbs.

``cn status`` answers "is it on, where, and how fast?" every refresh. The
ContextNest IDE panels ask the same question and share the same fields. The
intentionally never reveals the bearer token; the secure-tier key is named
so the operator knows which one to export.

On the secure tier a missing token is a CONFIG error, not a "down" answer:
the command exits non-zero, names the missing KEY (never the value) on
stderr, and refuses to probe the server. Scripts and CI can therefore
distinguish "fix the secret store" (exit 5) from "server is down" (exit 0).

``cn ack-overlap <id>`` posts to ``/api/v1/coord/overlaps/<id>/ack`` through
the raise-based :func:`mini_ork.cn_client._coord_request` seam. It maps a
``CoordUnavailable`` to exit 3 and a ``CoordHTTPError`` to exit 4, so the
IDE panel can render a precise failure next to the Ack button rather than a
generic stderr trace.

Wire: ``GET /api/v1/substrate/health`` (ping), plus the resolve-at-call
seam :func:`mini_ork.cn_client._coord_request`. Exit codes: 0 on a positive
ack or reachable ``status``; 2 on a usage error; 3 when ContextNest is
unreachable; 4 on an HTTP 4xx/5xx; 5 on a secure-tier secret-store
misconfiguration.
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
EXIT_COORD_UNREACHABLE = 3
EXIT_COORD_HTTP = 4
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
    ack = sub.add_parser("ack-overlap",
                         help="POST /api/v1/coord/overlaps/<id>/ack")
    ack.add_argument("overlap_id",
                     help="the overlap id returned by /api/v1/coord/overlaps")
    ack.add_argument("--by", default=None,
                     help="principal acknowledging; defaults to $CONCORD_PRINCIPAL "
                          "or human:$USER")
    ack.add_argument("--json", action="store_true",
                     help="emit the server response as JSON")
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


def _ack_default_by() -> str:
    """The default ``--by`` for ``cn ack-overlap``: $CONCORD_PRINCIPAL else human:$USER."""
    return os.environ.get("CONCORD_PRINCIPAL") or f"human:{os.environ.get('USER', 'unknown')}"


def _cmd_ack_overlap(args: argparse.Namespace) -> int:
    """POST ``/api/v1/coord/overlaps/<id>/ack`` through the raise-based seam.

    :func:`mini_ork.cn_client._coord_request` raises ``CoordUnavailable`` on a
    connection error (mapped to :data:`EXIT_COORD_UNREACHABLE`, exit 3) and
    ``CoordHTTPError`` on a 4xx/5xx (:data:`EXIT_COORD_HTTP`, exit 4). On a
    successful ack the parsed JSON body is echoed on stdout, either as JSON
    with ``--json`` or as a one-line ``ack-overlap: <id> acknowledged`` text.
    The id is URL-escaped through :func:`mini_ork.cn_client._enc` so an overlap
    id carrying ``/`` or another reserved character does not break the path.
    """
    by = args.by if args.by else _ack_default_by()
    try:
        payload = cn_client._coord_request(
            "POST",
            f"/api/v1/coord/overlaps/{cn_client._enc(args.overlap_id)}/ack",
            {"by": by},
        )
    except cn_client.CoordUnavailable as exc:
        sys.stderr.write(
            f"mini-ork cn ack-overlap: ContextNest unavailable: {exc}\n"
        )
        return EXIT_COORD_UNREACHABLE
    except cn_client.CoordHTTPError as exc:
        sys.stderr.write(
            f"mini-ork cn ack-overlap: ContextNest returned "
            f"{exc.status}: {exc.body}\n"
        )
        return EXIT_COORD_HTTP
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        overlap_id = (
            payload.get("id") if isinstance(payload, dict) else None
        ) or args.overlap_id
        sys.stdout.write(f"ack-overlap: {overlap_id} acknowledged\n")
    return EXIT_OK


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
    if args.subcommand == "ack-overlap":
        return _cmd_ack_overlap(args)
    sys.stderr.write(f"mini-ork cn: unknown subcommand {args.subcommand!r}\n")
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], root=os.getcwd()))
