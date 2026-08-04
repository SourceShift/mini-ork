"""``mini-ork sandbox-gc`` — sweep leaked sandbox instances (sandbox P4a).

Thin CLI over :mod:`mini_ork.runtime.sandbox_reaper`: parse ``--backend``,
``--max-age``, and ``--dry-run``, call :func:`reap_sandboxes`, and print a
one-line-per-backend summary. The reaper is fail-open, so this command runs to
``exit 0`` on any host — with Docker present it removes over-age
``mo.sandbox=1`` containers; with no Docker it prints a zero summary and never
errors.

Dispatched as ``python -m mini_ork.cli.sandbox_gc`` by the subcommand registry
(see ``mini_ork/cli/main.py``), so it exposes both a ``main(rest, root)`` entry
(the registry contract) and a ``__main__`` guard (the subprocess it spawns).
"""
from __future__ import annotations

import argparse
import os
import sys

from mini_ork.runtime.sandbox_reaper import (
    DEFAULT_MAX_AGE_S,
    reap_sandboxes,
)


def _default_max_age() -> int:
    raw = (os.environ.get("MO_SANDBOX_MAX_AGE") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_S


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mini-ork sandbox-gc",
        description="Reap leaked mini-ork sandbox instances older than a TTL.",
    )
    p.add_argument(
        "--backend",
        choices=("all", "docker", "microvm"),
        default="all",
        help="which backend(s) to sweep (default: all)",
    )
    p.add_argument(
        "--max-age",
        type=int,
        default=_default_max_age(),
        metavar="SECONDS",
        help=(
            "reap instances older than this many seconds "
            f"(default: {DEFAULT_MAX_AGE_S}, env MO_SANDBOX_MAX_AGE)"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be reaped without removing anything",
    )
    return p


def main(rest: list[str], root: str) -> int:
    del root  # TTL reaping is state-decoupled; the run root is not needed.
    args = _build_parser().parse_args(rest)
    reaped = reap_sandboxes(
        backend=args.backend,
        max_age_s=args.max_age,
        dry_run=args.dry_run,
    )
    for backend, ids in reaped.items():
        if args.dry_run:
            joined = ", ".join(ids)
            suffix = f" ({joined})" if ids else ""
            print(f"{backend}: dry-run: would reap {len(ids)}{suffix}")
        else:
            joined = ", ".join(ids)
            suffix = f" ({joined})" if ids else ""
            print(f"{backend}: reaped {len(ids)}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", os.getcwd())))
