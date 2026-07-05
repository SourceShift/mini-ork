"""mini_ork_coord — Python port of bin/mini-ork-coord CLI dispatcher.

Faithful Python port of the thin bash wrapper at ``bin/mini-ork-coord``.
The bash wrapper sources ``lib/coord_registry.sh`` and ``lib/coord_gate.sh``
then dispatches ``argv[1]`` to one of the public coord_* / coord_gate_*
functions. This module does the same by dispatching to the already-ported
peer modules under ``mini_ork.ported`` (``coord_registry``, ``coord_gate``).

Strangler-fig: ``bin/mini-ork-coord`` stays byte-identical at git HEAD;
this port is additive and gives Python callers an in-process target while
``tests/unit/test_mini_ork_coord_py.py`` enforces byte-level parity.

Subcommand contract:
    acquire <agent> <path> <mode> [ttl_seconds]  → lease id on stdout (rc=0/4)
    release <lease_id>                            → exit 0 on success
    renew   <agent> <lease_id> [ttl_seconds]     → exit 0 on success (holder only)
    gate    <agent> <path> <mode>                 → rc=0 (advisory) / rc=11 (strict deny)
    metrics                                       → prints metrics JSON to stdout
    audit    [N]                                  → prints last N audit records

Exit codes (mirrors bash exactly):
    acquire  0 / 1 (conflict) / 2 (usage) / 4 (deadlock)
    release  0 / 1 / 2
    renew    0 / 1 / 2 / 3 (not holder)
    gate     0 / 2 (usage) / 11 (strict deny)
    metrics  0
    audit    0
    help     0
    unknown  2

argv parsing parity: bash uses ``${1:-}`` (empty-default) for missing
positional args; this module routes missing positional args through empty
strings so the rc=2 'usage' stderr path stays identical.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Iterator, Sequence

from mini_ork.ported import coord_registry as cr
from mini_ork.ported import coord_gate as cg

__all__ = [
    "cmd_acquire", "cmd_release", "cmd_renew",
    "cmd_gate", "cmd_metrics", "cmd_audit",
    "usage", "main",
]


# ── usage banner ─────────────────────────────────────────────────────────

_REPO = Path(__file__).resolve().parents[2]
_BIN = _REPO / "bin" / "mini-ork-coord"


def usage() -> str:
    """Return the usage banner byte-identical to bin/mini-ork-coord:24-47.

    Parses the heredoc body from bin/mini-ork-coord at call time. The bash
    heredoc uses ``<<'USAGE'`` (quoted delimiter → no variable expansion),
    so ``${MINI_ORK_RUN_DIR}`` / ``${MINI_ORK_HOME}`` placeholders stay
    literal in the emitted body. Tracking the heredoc at runtime guards
    against future bash-source edits silently diverging from this port.
    """
    text = _BIN.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start: int | None = None
    for i, line in enumerate(lines):
        s = line.rstrip("\n").strip()
        if s in ("cat <<'USAGE'", "cat <<USAGE"):
            start = i + 1
            break
    if start is None:
        raise RuntimeError("usage heredoc opener not found in bin/mini-ork-coord")
    end: int | None = None
    for j in range(start, len(lines)):
        if lines[j].rstrip("\n").strip() == "USAGE":
            end = j
            break
    if end is None:
        raise RuntimeError("usage heredoc closer not found in bin/mini-ork-coord")
    return "".join(lines[start:end])


# ── stream-capture helper ────────────────────────────────────────────────


@contextlib.contextmanager
def _captured_streams() -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """Swap sys.stdout and sys.stderr for StringIO buffers during the block.

    Used by the registry-backed cmd_* wrappers because
    ``mini_ork.ported.coord_registry`` writes via ``sys.stdout.write`` /
    ``sys.stderr.write`` (matches bash heredoc behaviour) rather than
    returning its outputs as a tuple. ``mini_ork.ported.coord_gate`` does
    NOT need this — its public API returns (stdout, stderr, rc) tuples.
    """
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# ── cmd_* thin wrappers ─────────────────────────────────────────────────


def cmd_acquire(agent: str, path: str, mode: str, ttl: str = ""
                 ) -> tuple[str, str, int]:
    """Acquire a path-prefix lease. Returns (stdout, stderr, rc).

    ``ttl`` empty string → fall back to COORD_REGISTRY_DEFAULT_TTL. Exit
    codes: 0 / 1 (conflict) / 2 (usage) / 4 (deadlock; stdout is JSON
    payload).
    """
    ttl_arg = ttl if ttl else None
    with _captured_streams() as (out, err):
        _, rc = cr.coord_acquire(agent, path, mode, ttl_arg)
    return out.getvalue(), err.getvalue(), rc


def cmd_release(lease_id: str) -> tuple[str, str, int]:
    """Release a lease by id. Returns (stdout, stderr, rc).

    Exit codes: 0 / 1 (malformed or unknown id) / 2 (usage).
    """
    with _captured_streams() as (out, err):
        rc = cr.coord_release(lease_id)
    return out.getvalue(), err.getvalue(), rc


def cmd_renew(agent: str, lease_id: str, ttl: str = ""
               ) -> tuple[str, str, int]:
    """Renew an active lease (holder only). Returns (stdout, stderr, rc).

    Exit codes: 0 / 1 / 2 / 3 (caller is not the current holder).
    """
    ttl_arg = ttl if ttl else None
    with _captured_streams() as (out, err):
        rc = cr.coord_renew(agent, lease_id, ttl_arg)
    return out.getvalue(), err.getvalue(), rc


def cmd_gate(agent: str, path: str, mode: str) -> tuple[str, str, int]:
    """Run PreToolUse-style coordination gate. Returns (stdout, stderr, rc).

    Pass-through: ``coord_gate_check`` already returns (stdout, stderr, rc)
    matching what bash ``coord_gate_check`` writes to its own stdout/stderr.

    Exit codes: 0 / 2 (usage) / 11 (strict deny when path in scope).
    """
    return cg.coord_gate_check(agent, path, mode)


def cmd_metrics() -> tuple[str, str, int]:
    """Return metrics JSON (default 4-key JSON on empty state)."""
    return cg.coord_gate_metrics(), "", 0


def cmd_audit(n: str = "") -> tuple[str, str, int]:
    """Return last-N audit JSON. Empty ``n`` → print all (matches bash)."""
    n_arg = int(n) if n else 0
    return cg.coord_gate_audit(n_arg), "", 0


# ── main dispatcher ──────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``argv[1:]`` to a subcommand. Mirrors bin/mini-ork-coord:49-79.

    Empty ``argv[0]`` (no subcommand) defaults to ``help``. Missing
    positional args are passed through as empty strings (matches bash
    ``${1:-}``). Empty agent/path/mode flow into the same rc=2 'usage'
    stderr the bash wrappers emit.

    Return value is the subcommand's exit code; on the unknown-subcommand
    branch returns 2 after writing ``"mini-ork-coord: unknown subcommand X"``
    + the usage banner to stderr.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "help"
    rest = args[1:]

    def _get(i: int) -> str:
        return rest[i] if i < len(rest) else ""

    if cmd == "acquire":
        out, err, rc = cmd_acquire(_get(0), _get(1), _get(2), _get(3))
    elif cmd == "release":
        out, err, rc = cmd_release(_get(0))
    elif cmd == "renew":
        out, err, rc = cmd_renew(_get(0), _get(1), _get(2))
    elif cmd == "gate":
        out, err, rc = cmd_gate(_get(0), _get(1), _get(2))
    elif cmd == "metrics":
        out, err, rc = cmd_metrics()
    elif cmd == "audit":
        out, err, rc = cmd_audit(_get(0))
    elif cmd in ("help", "-h", "--help"):
        sys.stdout.write(usage())
        return 0
    else:
        sys.stderr.write(f"mini-ork-coord: unknown subcommand {cmd}\n")
        sys.stderr.write(usage())
        return 2

    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main())
