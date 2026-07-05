"""Python port of ``bin/mini-ork-rollback`` — thin CLI over
``mini_ork.ported.version_registry.rollback``.

Strangler-fig parity port. The bash script is a thin wrapper that parses
two positional args (``kind`` and ``name``), validates them, then delegates
to ``version_rollback`` from ``lib/version_registry.sh``. The Python port
mirrors that role: it re-parses the same arguments, applies the same
validation, and delegates to the already-ported
``mini_ork.ported.version_registry.rollback``. Argument parsing, exit
codes, help text, and stderr routing all match the bash source
byte-for-byte so the parity test (``tests/unit/test_mini_ork_rollback_py.py``)
can compare live bash invocations against this module on the same
temp DB.

Public surface (mirrors bash functions / arg flow):

    help_text()              → bash `_usage` heredoc block
    rollback(kind, name,
             db=None, now=None)
                              → version_registry.rollback (delegates)
    main(argv=None)          → CLI dispatcher (exit codes mirror bash)

Exit code mapping (mirrors bash exactly):

    0  success — JSON of the now-stable previous version on stdout.
    2  usage error — bash emitted `_usage` to stderr (invalid kind,
       missing name, or wrong arg count). Python mirrors by writing
       HELP_TEXT to stderr.
    1  rollback failure — ``version_rollback`` itself failed (no stable
       found, or stable has no ``previous_stable_version``). The
       bash heredoc's ``sys.exit(1)`` propagates through the function
       call and ``set -e`` exits the script with 1. Python catches the
       ValueError raised by the Python port of ``version_registry.rollback``
       and emits ``str(e)`` to stderr (matches bash's stderr verbatim).

Environment honored:

    MINI_ORK_DB        — state.db path (used when ``db=None``).
    MINI_ORK_HOME      — base dir for the default DB path
                         (only when ``db=None`` AND ``MINI_ORK_DB`` unset).
    TZ                 — pinned in parity tests to ``UTC`` so any
                         ``datetime(..., 'localtime')`` comparison
                         doesn't drift. The rollback SQL itself does
                         not format datetimes — only ``int(time.time())``
                         epoch seconds — but TZ is still propagated
                         for consistency with other parity tests.

Parity is enforced by ``tests/unit/test_mini_ork_rollback_py.py``
(>=6 cases that drive the LIVE ``bin/mini-ork-rollback`` bash subprocess
against a temp DB seeded by ``db/init.sh`` and compare stdout / stderr /
exit codes against this module).
"""
from __future__ import annotations

import sys

from mini_ork.ported import version_registry

__all__ = ["help_text", "rollback", "main"]


# ─────────────────────────────────────────────────────────────────────────────
# Help text — verbatim copy of bash's `cat <<'EOF' … EOF` block in _usage().
#
# Bash's heredoc emits the body lines (each terminated with "\n") plus the
# newline of the line that precedes the EOF terminator. The body has no
# trailing blank line after "Show this help", so the emitted output ends
# with exactly one "\n" (the newline of "  --help, -h       Show this help").
# sys.stdout.write(HELP_TEXT) emits the same bytes.
# ─────────────────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "Usage: mini-ork rollback <workflow|agent> <name>\n"
    "\n"
    "Roll back a workflow or agent to its previous stable version.\n"
    "\n"
    "Arguments:\n"
    "  workflow|agent   Registry kind to roll back\n"
    "  name             Workflow or agent name\n"
    "\n"
    "Options:\n"
    "  --help, -h       Show this help\n"
)


def help_text() -> str:
    """Return the bash `_usage` heredoc body verbatim."""
    return HELP_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# Thin wrapper — delegates to version_registry.rollback.
# ─────────────────────────────────────────────────────────────────────────────
def rollback(kind: str, name: str, db: str | None = None,
             now: int | None = None) -> str:
    """Roll back the named ``kind/name`` to its previous stable version.

    Thin wrapper over :func:`mini_ork.ported.version_registry.rollback`.
    The ``db`` and ``now`` kwargs are forwarded unchanged. The ``now``
    injection is provided so the parity test can pin both bash and
    Python invocations to a deterministic timestamp when needed; bash
    has no equivalent hook, so the test normally runs both calls within
    the same wall-clock second and tolerates ±1 s drift in
    ``promoted_at``.

    Returns the now-current version JSON string (no trailing newline;
    the caller — bash or this module's ``main`` — decides how to emit
    it). Raises ``ValueError`` on the same conditions as
    ``version_registry.rollback``:

      * ``version_rollback: no stable version found for {kind}/{name}``
      * ``version_rollback: no previous stable version recorded for {version_id}``
    """
    return version_registry.rollback(kind, name, db=db, now=now)


# ─────────────────────────────────────────────────────────────────────────────
# CLI dispatcher — mirrors bash's arg-parsing and exit-code flow exactly.
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """CLI dispatcher. Returns the exit code.

    Mirrors ``bin/mini-ork-rollback`` byte-for-byte:

    1. ``--help`` / ``-h`` → ``_usage`` to **stdout**, exit 0.
    2. invalid ``kind`` (not "workflow" / "agent") → ``_usage`` to
       **stderr**, exit 2.
    3. missing ``name`` OR ``$# -ne 2`` → ``_usage`` to **stderr**,
       exit 2.
    4. ``ValueError`` from ``version_registry.rollback`` → ``str(e)``
       (followed by ``\\n``) to **stderr**, exit 1. This matches
       bash's `print(..., file=sys.stderr); sys.exit(1)` from the
       heredoc inside ``version_rollback``.
    5. success → ``version_registry.rollback`` JSON to **stdout**
       followed by ``\\n`` (bash's ``print(...)`` adds ``\\n``), exit 0.
    """
    if argv is None:
        argv = sys.argv[1:]

    # ── (1) --help / -h ────────────────────────────────────────────────────
    # bash: if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then _usage; exit 0
    if argv and argv[0] in ("--help", "-h"):
        sys.stdout.write(HELP_TEXT)
        return 0

    # bash: kind="${1:-}"; name="${2:-}"
    kind = argv[0] if len(argv) >= 1 else ""
    name = argv[1] if len(argv) >= 2 else ""

    # ── (2) invalid kind ───────────────────────────────────────────────────
    # bash: if [[ "$kind" != "workflow" && "$kind" != "agent" ]]; then
    #          _usage >&2; exit 2
    if kind not in ("workflow", "agent"):
        sys.stderr.write(HELP_TEXT)
        return 2

    # ── (3) missing name or wrong arg count ────────────────────────────────
    # bash: if [[ -z "$name" || $# -ne 2 ]]; then _usage >&2; exit 2
    if not name or len(argv) != 2:
        sys.stderr.write(HELP_TEXT)
        return 2

    # ── (4) ValueError from rollback → mirror bash's stderr + rc=1 ─────────
    try:
        result = rollback(kind, name)
    except ValueError as e:
        # bash: `print(f"...", file=sys.stderr)` → "<msg>\n"
        sys.stderr.write(str(e) + "\n")
        return 1

    # ── (5) success — JSON to stdout + "\n" (bash's print adds \n) ────────
    # version_registry.rollback returns the JSON WITHOUT a trailing
    # newline; bash's print(...) adds one. Mirror exactly.
    sys.stdout.write(result + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())