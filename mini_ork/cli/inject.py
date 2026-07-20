"""Python port of bin/mini-ork-inject (the operator-steering CLI wrapper).

Mirrors ``bin/mini-ork-inject``'s CLI surface exactly. The bash wrapper
sources ``lib/operator_steering.sh`` and invokes ``operator_steering_emit``;
this module delegates to ``mini_ork.steering.operator_steering.emit`` (the
already-ported peer module, 224 lines, mirrors bash's SQL sub-pipelines).
The wrapper-specific logic — ``--role`` → ``--role-target`` translation and
the ``--source`` default injection — lives here so the SQL stays one
implementation in operator_steering.py.

Exit-code contract (mirrored from bin/mini-ork-inject):
  0  message accepted; new row id printed on stdout
  1  DB unreachable / write failed (FileNotFoundError / sqlite3.OperationalError)
  2  bad arguments / missing --message / unknown flag (ValueError)

Argv surface (matches bin/mini-ork-inject exactly):
  --run-id <run-id>                        # optional → lands in global queue
  --role <planner|implementer|reviewer|verifier|any>  # → role_target
  --message "<text>"                       # REQUIRED
  --severity <info|warn|critical>          # default: info
  --source <free-form>                     # default when absent: "operator-cli"
  --confidence 0.0-1.0                     # default: 0.8
  --ttl-secs <int>                         # default: 3600
  --help | -h                              # print this docstring, exit 0
  (no args)                                # print this docstring, exit 0

Parity gate: ``tests/unit/test_mini_ork_inject_py.py`` drives the LIVE
``bin/mini-ork-inject`` subprocess against this module and diffs the rows
read back via sqlite (id/created_at/expires_at stripped; floats 1e-6).

Source paths this port tracks (kept in lockstep):
  bash: bin/mini-ork-inject
  bash: lib/operator_steering.sh (peer)
  py:   mini_ork/steering/operator_steering.py (peer)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from mini_ork.steering.operator_steering import emit as ops_emit

__all__ = ["main", "build_parser"]


_PROGRAM_BANNER = """\
mini-ork inject — emit an operator steering message into state.db so the
next context_assemble call surfaces it to the targeted agent role.

Usage:
  mini-ork inject \\
    --run-id <run-id> \\
    --role planner|implementer|reviewer|verifier|any \\
    --message "<text>" \\
    [--severity info|warn|critical]    # default: info
    [--source <free-form>]             # default: "operator-cli" (when absent)
    [--confidence 0.0-1.0]             # default: 0.8
    [--ttl-secs <int>]                 # default: 3600 (1h)

When --run-id is omitted the message lands in the global queue and
reaches the next planner dispatch of any run.

Exit codes:
  0  message accepted; row id printed on stdout
  1  DB unreachable / write failed
  2  bad arguments
"""


def build_parser() -> argparse.ArgumentParser:
    """Argparse parser mirroring bin/mini-ork-inject's flag surface.

    Mirrors flags verbatim; default values match bash; ``--role`` is
    kept as the user-facing name and translated to ``role_target``
    internally before calling ``ops.emit`` (mirrors the wrapper's
    ``NEW_ARGS+=("--role-target")`` rewrite in bash).
    """
    p = argparse.ArgumentParser(
        prog="mini-ork inject",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_PROGRAM_BANNER,
    )
    # add_help=False so we can control --help/-h ourselves (exit 0 on
    # bare --help matches bash: `if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; …; exit 0`).
    p.add_argument("--help", "-h", action="store_true",
                   help="print this banner and exit 0")
    p.add_argument("--run-id", default=None,
                   help="target mini-ork run id (omit → global queue)")
    p.add_argument("--role", dest="role_target", default="any",
                   choices=("planner", "implementer", "reviewer", "verifier", "any"),
                   help="target agent role (translated to --role-target for the peer)")
    p.add_argument("--message", default=None,
                   help="steering message text (REQUIRED)")
    p.add_argument("--severity", default="info",
                   choices=("info", "warn", "critical"),
                   help="severity tier; default: info")
    # ``source`` has NO default at the argparse layer — bash uses a
    # HAS_SOURCE flag to distinguish "user passed --source '' explicitly"
    # from "user omitted --source entirely". We mirror that with a
    # sentinel that argparse cannot fabricate (see main()).
    p.add_argument("--source", default=None,
                   help="free-form source label; if omitted, defaults to 'operator-cli' (mirrors bash)")
    p.add_argument("--confidence", type=float, default=0.8,
                   help="confidence 0.0-1.0; default: 0.8")
    p.add_argument("--ttl-secs", type=int, default=3600,
                   help="time-to-live in seconds; default: 3600 (1h)")
    return p


def _print_banner_and_exit_zero() -> None:
    sys.stdout.write(_PROGRAM_BANNER)
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors bin/mini-ork-inject.

    Returns the numeric exit code so callers (and the parity test) can
    assert it without spawning a subprocess. Raises ``ValueError`` /
    ``FileNotFoundError`` / ``sqlite3.OperationalError`` only when called
    without exception mapping; in the normal CLI path we catch them and
    map to exit codes 2 / 1 / 1 respectively.
    """
    if argv is None:
        argv = sys.argv[1:]
    # Bare invocation OR --help/-h: print banner, exit 0 (mirrors bash:
    # `if [[ $# -eq 0 || "${1:-}" == "--help" … ]]; … exit 0`).
    if not argv or argv[0] in ("--help", "-h"):
        _print_banner_and_exit_zero()
        return 0

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits with 2 on usage errors → mirror bash's exit 2.
        return int(exc.code) if exc.code is not None else 2

    if args.help:
        _print_banner_and_exit_zero()
        return 0

    # --message required. argparse accepts None when --message is omitted
    # (we set default=None to keep HAS_SOURCE semantics clean). Mirror
    # bash's `[ -n "$message" ] || { echo "… --message required" >&2; return 2; }`.
    if not args.message:
        sys.stderr.write("mini-ork-inject: --message required\n")
        sys.stderr.flush()
        return 2

    # Default-source injection mirrors bash's `if HAS_SOURCE == 0; then
    # NEW_ARGS+=(--source operator-cli)`. argparse ``default=None`` lets
    # us detect the absent case.
    source = args.source if args.source is not None else "operator-cli"

    try:
        rowid = ops_emit(
            message=args.message,
            run_id=args.run_id,
            role_target=args.role_target,
            severity=args.severity,
            source=source,
            confidence=args.confidence,
            ttl_secs=args.ttl_secs,
        )
    except ValueError as exc:
        # Validation failures (unknown flag, bad role/severity, etc.) →
        # bash returns 2. We surface the same text on stderr.
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 2
    except FileNotFoundError as exc:
        # bash returns 1 with "operator_steering_emit: state.db not found: …".
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 1
    except sqlite3.OperationalError as exc:
        # bash returns 1 on any sqlite failure inside the heredoc INSERT.
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 1

    # Success: rowid on stdout (matches bash's `operator_steering_emit …
    # || return 1` then the wrapper exits 0 with the rowid on stdout).
    sys.stdout.write(f"{rowid}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
