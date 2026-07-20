"""Python port of ``bin/mini-ork-bugs`` — operator CLI for the bug-report channel.

Strangler-fig parity port of the 42-line bash dispatcher. The bash script
stays in place (strangler-fig KEEP invariant per the migration kickoff);
this module gives Python callers an in-process target and gives the parity
test a stable surface to byte-diff against the live bash subprocess.

Pipeline map (bash subcommand → Python dispatch):
  sweep                 → bug_report_sweep      (delegates to mini_ork.observability.bug_report)
  list                  → bug_report_list       (delegates)
  show <id>             → bug_report_show       (delegates)
  prioritize [--top N]  → bug_report_prioritize (delegates)
  promote --top N       → bug_report_promote    (delegates)
  help|--help|-h        → _usage()              (hand-mirrored literal)
  <other>               → stderr msg + _usage + exit 2

Env resolution (mirrors bash bin/mini-ork-bugs lines 19-23):
  * ``MINI_ORK_ROOT``  — repo root for promote kickoff files; default is the
    parent of the ``mini_ork`` package, resolved via ``Path.resolve()`` which
    canonicalizes symlinks (mirrors bash ``readlink -f``).
  * ``MINI_ORK_HOME``  — runs root for sweep; default ``$MINI_ORK_ROOT/.mini-ork``.
  * ``MINI_ORK_DB``    — state.db path; default ``$MINI_ORK_HOME/state.db``.

Output semantics:
  * ``sweep`` / ``promote`` return a count integer from the peer; the
    dispatcher writes ``"<n>\\n"`` to stdout, matching bash's ``print(N)``.
  * ``list`` / ``prioritize`` / ``show`` forward peer stdout byte-for-byte.
  * ``help`` / ``--help`` / ``-h`` write ``_USAGE_BLOCK`` to stdout.
  * Unknown subcommand writes ``Unknown subcommand: <x>\\n`` to stderr,
    then ``_USAGE_BLOCK`` to stdout, then exits 2 (matches bash case `*`).

Parity is enforced by ``tests/unit/test_mini_ork_bugs_py.py`` (>=6 cases
that drive the LIVE bash subprocess against a temp DB seeded by
``db/init.sh`` and diff stdout/stderr/exit-code against the Python port
byte-for-byte; floats 1e-6 on confidence; epochs 1-second tolerance).

Bash source-of-truth: ``bin/mini-ork-bugs`` lines 2-16 (extracted by bash
``_usage()`` via ``sed -n '2,16p' "$0" | sed 's/^# \\{0,1\\}//'``). The
literal below is hand-mirrored; ``test_help_parity`` re-verifies it on
every CI run so any drift in the bash docblock is caught immediately.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from mini_ork.observability.bug_report import (
    bug_report_list,
    bug_report_prioritize,
    bug_report_promote,
    bug_report_show,
    bug_report_sweep,
)

__all__ = [
    "main",
    "_usage",
    "_resolve_root",
    "_ensure_env",
]

# Hand-mirror of `sed -n '2,16p' bin/mini-ork-bugs | sed 's/^# \{0,1\}//'`
# (696 bytes; last two chars are the \n that terminates line 15 plus the
# empty line 16). Drift from the bash source breaks `test_help_parity`.
_USAGE_BLOCK = (
    "mini-ork bugs — operator UI for the per-agent bug reporting channel.\n"
    "\n"
    "Subcommands:\n"
    "  sweep [--since EPOCH] [--all]\n"
    "      Pick up noticed_bugs.jsonl files from every run dir, upsert into\n"
    "      the bug_reports table. Called by `mini-ork reflect` automatically;\n"
    "      run manually to force a fresh sweep.\n"
    "\n"
    "  list                                 50 most-recent bug_reports rows\n"
    "  show <id>                            full detail of one row\n"
    "  prioritize [--top N]                 ranked view\n"
    "  promote --top N                      take top-N open bugs, create epics\n"
    "                                       + per-epic kickoffs, flip status to\n"
    "                                       'queued_as_epic'.\n"
    "\n"
)


def _resolve_root() -> Path:
    """Return ``MINI_ORK_ROOT`` as the bash dispatcher would compute it.

    Mirrors bash bin/mini-ork-bugs:19:
        MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"

    Resolution order:
      1. ``$MINI_ORK_ROOT`` env var (already resolved by the caller).
      2. Parent of the ``mini_ork`` package (``mini_ork/cli/bugs.py``
         → ``mini_ork/cli`` → ``mini_ork`` → REPO). ``Path.resolve()``
         canonicalizes symlinks, mirroring ``readlink -f``.
    """
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return Path(env_root).resolve()
    # mini_ork/cli/bugs.py → mini_ork/cli → mini_ork → REPO
    return Path(__file__).resolve().parent.parent.parent


def _ensure_env() -> Path:
    """Mirror bash lines 19-23: derive + export MINI_ORK_ROOT/HOME/DB.

    Honors env overrides (caller may pass MINI_ORK_HOME or MINI_ORK_DB to
    redirect the dispatcher at a temp DB, exactly the test-fixture
    pattern used by ``test_bug_report_py.py``). Defaults follow bash:
      MINI_ORK_HOME = $MINI_ORK_ROOT/.mini-ork
      MINI_ORK_DB   = $MINI_ORK_HOME/state.db
    """
    root = _resolve_root()
    os.environ["MINI_ORK_ROOT"] = str(root)
    os.environ.setdefault("MINI_ORK_HOME", str(root / ".mini-ork"))
    os.environ.setdefault("MINI_ORK_DB", str(Path(os.environ["MINI_ORK_HOME"]) / "state.db"))
    return root


def _usage() -> str:
    """Return the help text the bash ``_usage()`` would emit."""
    return _USAGE_BLOCK


def _parse_top(rest: list[str], default: int) -> int:
    """Mirror bash's inline ``--top N`` parsing for `prioritize`/`promote`.

    The bash library parses ``--top N`` itself inside ``bug_report_prioritize``
    (and ``bug_report_promote``); the Python peer port's
    ``bug_report_prioritize`` takes ``top`` as a kwarg, so the dispatcher
    has to bridge the difference. Unknown flags are ignored (matches
    bash's ``*) shift ;;`` default branch).
    """
    top = default
    i = 0
    while i < len(rest):
        if rest[i] == "--top" and i + 1 < len(rest):
            try:
                top = int(rest[i + 1])
            except ValueError:
                top = default
            i += 2
            continue
        i += 1
    return top


def main(argv: list[str]) -> int:
    """Dispatch ``argv`` to the peer port, mirroring bash bin/mini-ork-bugs.

    Args:
        argv: Positional args starting with the subcommand. ``argv[0]`` is
              the subcommand (``sweep`` / ``list`` / ``show`` / ``prioritize``
              / ``promote`` / ``help|--help|-h``). Defaults to ``"help"`` when
              empty, matching bash's ``sub="${1:-help}"``.

    Returns:
        Process exit code: 0 on success, 2 on unknown subcommand or
        missing ``show`` id (matches bash).
    """
    _ensure_env()

    sub = argv[0] if argv else "help"
    rest = argv[1:]

    if sub == "sweep":
        sys.stdout.write(f"{bug_report_sweep(*rest)}\n")
        return 0
    if sub == "list":
        sys.stdout.write(bug_report_list())
        return 0
    if sub == "show":
        if not rest:
            sys.stderr.write("id required\n")
            return 2
        sys.stdout.write(bug_report_show(int(rest[0])))
        return 0
    if sub == "prioritize":
        top = _parse_top(rest, default=10)
        sys.stdout.write(bug_report_prioritize(top=top))
        return 0
    if sub == "promote":
        sys.stdout.write(f"{bug_report_promote(*rest)}\n")
        return 0
    if sub in ("help", "--help", "-h"):
        sys.stdout.write(_usage())
        return 0

    sys.stderr.write(f"Unknown subcommand: {sub}\n")
    sys.stdout.write(_usage())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))