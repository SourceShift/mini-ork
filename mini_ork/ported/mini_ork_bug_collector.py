"""Python port of ``bin/mini-ork-bug-collector`` — auto-dispatched side-issue scanner.

Strangler-fig parity port of the bash dispatcher. The bash
``bin/mini-ork-bug-collector`` stays in place (strangler-fig KEEP
invariant per the migration kickoff); this module gives Python
callers an in-process surface and gives the parity test a stable
target to byte-diff against the live bash subprocess.

Source-of-truth: ``bin/mini-ork-bug-collector`` lines 2-32 (extracted
by bash ``--help`` via ``sed -n '2,32p'`` then stripping a leading
hash-space or bare-hash from each line).
The literal ``_USAGE_BLOCK`` below is hand-mirrored; ``test_help_parity``
re-verifies it on every CI run so any drift in the bash docblock is
caught immediately.

Pipeline map (bash branch → Python dispatch):
  --mode off                          → return 0 (bash line 50)
  --mode heuristic  [--output-file X] → _resolve_scan_targets → heuristic_scan
  --mode llm                          → stderr stub + return 0 (lines 162-164)
  unknown mode                        → stderr msg + return 0 (lines 167-170)
  --help / -h                         → print _USAGE_BLOCK to stdout, return 0

Env resolution (mirrors bash lines 27-32):
  * ``MINI_ORK_ROOT``        — repo root; default = parent of ``mini_ork`` package
                                (mirrors ``readlink -f`` canonicalization).
  * ``MINI_ORK_HOME``        — runs root; default ``$MINI_ORK_ROOT/.mini-ork``.
  * ``MINI_ORK_RUN_DIR``     — per-run sink dir; default ``/tmp``.
  * ``MO_BUG_COLLECTOR_MODE`` — default mode (bash line 34).

Output semantics:
  * Heuristic path: appends one JSONL row per finding to
    ``${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl`` (separators=(",", ":"),
    utf-8, trailing ``\\n``). Mirrors bash heredoc exactly.
  * Always writes ``f"{emitted}\\n"`` to stderr on the heuristic
    path (mirrors bash ``print(emitted, file=sys.stderr)``).
  * ``--mode llm`` writes ``"  [bug-collector] llm mode not yet
    implemented; use --mode heuristic\\n"`` to stderr (mirrors bash
    echo >&2 at line 163).
  * Unknown mode writes ``f"bug-collector: unknown mode '<mode>'\\n"``
    to stderr (mirrors bash line 168).
  * Returns 0 in every branch — bash always exits 0 (line 173).

Parity is enforced by ``tests/unit/test_mini_ork_bug_collector_py.py``
(8 cases that drive the LIVE bash subprocess against a sandbox run-dir
and diff the resulting ``noticed_bugs.jsonl`` + stderr + exit-code
against the Python port byte-for-byte; floats 1e-6 on confidence).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

__all__ = [
    "main",
    "heuristic_scan",
    "_resolve_scan_targets",
    "_usage",
    "_parse_args",
    "_ensure_env",
    "_resolve_root",
]


# Hand-mirror of `sed -n '2,32p' bin/mini-ork-bug-collector | sed 's/^# \{0,1\}//'`.
# Lines 2-23 are the docblock (each `# `-prefixed). Lines 24-32 are the bash
# shell setup (no `#` prefix, so sed leaves them unchanged). The lone `#`
# separator lines on 7, 15, 22 collapse to empty strings after sed strips
# the leading `#`. Drift from the bash source breaks `test_help_parity`.
_USAGE_BLOCK = (
    "mini-ork bug-collector — auto-dispatched after each node completes by\n"
    "bin/mini-ork-execute. Scans the just-finished agent's output for\n"
    "side-issues the agent noticed but did NOT fix, and emits one\n"
    "noticed_bugs.jsonl row per finding so bin/mini-ork-reflect's sweep can\n"
    "pick them up.\n"
    "\n"
    "Modes:\n"
    "  --mode heuristic   (default) — regex-scan output for known markers.\n"
    "                     Free; emits low-medium confidence.\n"
    "  --mode llm         — dispatch a kimi_lens (or MO_BUG_COLLECTOR_LANE)\n"
    "                     reviewer to read the output and synthesize bugs.\n"
    "                     Higher confidence, costs ~$0.02-0.05/node.\n"
    "  --mode off         — no-op (used by harness env to disable inline).\n"
    "\n"
    "Args:\n"
    "  --node-id        trace_id or node identifier (for provenance)\n"
    "  --node-type      planner|implementer|reviewer|researcher|verifier|... agent role\n"
    "  --output-file    path to the agent's primary output (md/json/log)\n"
    "  --status         success|failure\n"
    "  --task-class     classification for the originating run\n"
    "\n"
    "Returns 0 always (best-effort; never fails the caller's run).\n"
    "\n"
    "set -uo pipefail\n"
    "\n"
    'MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"\n'
    "export MINI_ORK_ROOT\n"
    'MINI_ORK_HOME="${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}"\n'
    "export MINI_ORK_HOME\n"
    'STATE_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"\n'
    "export STATE_DB\n"
)


def _resolve_root() -> Path:
    """Return ``MINI_ORK_ROOT`` as bash would compute it.

    Mirrors bash bin/mini-ork-bug-collector:27:
        MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"

    Resolution order:
      1. ``$MINI_ORK_ROOT`` env var (already resolved by the caller).
      2. Parent of the ``mini_ork`` package. ``Path.resolve()``
         canonicalizes symlinks, mirroring ``readlink -f``.
    """
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return Path(env_root).resolve()
    # mini_ork/ported/mini_ork_bug_collector.py → ported → mini_ork → REPO
    return Path(__file__).resolve().parent.parent.parent


def _ensure_env() -> Path:
    """Mirror bash lines 27-32: derive + export MINI_ORK_ROOT/HOME.

    Does NOT export ``MINI_ORK_DB`` — the bug-collector never touches
    the bug_reports table (only writes noticed_bugs.jsonl). The bash
    script exports STATE_DB (line 31-32) but the parity test does not
    observe it, so the port omits it to avoid writing an unused var.
    """
    root = _resolve_root()
    os.environ["MINI_ORK_ROOT"] = str(root)
    os.environ.setdefault("MINI_ORK_HOME", str(root / ".mini-ork"))
    return root


def _resolve_scan_targets(output_file: str) -> list[str]:
    """Return ordered list of existing target paths to scan.

    Mirrors bash lines 58-72. Order is preserved (not sorted): the
    primary ``output_file`` first, then its ``.tool-summary`` sidecar,
    then its ``.stdout.md`` sidecar. Missing files are silently
    omitted — only the files that actually exist on disk are returned.
    """
    targets: list[str] = []
    if output_file and os.path.isfile(output_file):
        targets.append(output_file)
    if output_file:
        side1 = output_file + ".tool-summary"
        if os.path.isfile(side1):
            targets.append(side1)
        side2 = output_file + ".stdout.md"
        if os.path.isfile(side2):
            targets.append(side2)
    return targets


# Each entry: (regex, severity, confidence, scope_extractor).
# Mirrors bash lines 94-110 verbatim. Patterns are anchored to LINE
# START (re.M) and bounded by newline to avoid matching across
# sentences. They allow periods inside the captured span so file
# extensions like 'lib/foo.sh' don't break the match.
_PATTERNS = [
    (re.compile(r"^[^\n]*\b(?:noticed|observed|saw)\b[^\n]{0,200}?\bbut\b[^\n]{0,200}", re.I | re.M),
     "medium", 0.70, "agent-noticed"),
    (re.compile(r"^[^\n]*\bout of scope but[^\n]{0,200}", re.I | re.M),
     "medium", 0.75, "out-of-scope-but"),
    (re.compile(r"^[^\n]*\b(?:should fix|needs to be fixed|known(?: to be)? broken|bug in|defect in)[^\n]{0,200}", re.I | re.M),
     "high", 0.80, "explicit-bug-mention"),
    (re.compile(r"^[^\n]*\b(?:deferred|deferring|postponing|leaving for later)\b[^\n]{0,200}", re.I | re.M),
     "medium", 0.65, "deferred-fix"),
    (re.compile(r"^[^\n]*\b(?:TODO|FIXME|HACK|XXX|KLUDGE)\s*:?[^\n]{0,200}", re.I | re.M),
     "low", 0.55, "todo-marker"),
    (re.compile(r"^[^\n]*\b(?:assertion|invariant) (?:fails|violated|broken)[^\n]{0,200}", re.I | re.M),
     "high", 0.78, "broken-invariant"),
]
_ONLY_ON_FAILURE = frozenset({"broken-invariant"})


def heuristic_scan(
    targets: list[str],
    *,
    node_id: str,
    node_type: str,
    status: str,
    task_class: str,
    run_dir: str,
) -> int:
    """Scan each target with ``_PATTERNS`` and append JSONL rows to
    ``${run_dir}/noticed_bugs.jsonl``.

    Mirrors bash heredoc lines 113-154 verbatim. Returns the number of
    rows emitted. Always writes ``f"{emitted}\\n"`` to stderr (mirrors
    bash ``print(emitted, file=sys.stderr)`` at line 154), even when
    emitted == 0 — so callers can distinguish "no targets" (no stderr)
    from "targets but no matches" (stderr ``0\\n``).

    The 5-row cap is enforced at three nested levels (inner regex loop,
    pattern loop, path loop) to match bash lines 147-153. Dedupe key is
    ``(scope, title.lower())`` (bash lines 131-134). Title is
    ``m.group(0).strip()[:300]`` (bash line 129). ``observed_in`` is the
    absolute path to the file the match was found in (bash line 142).
    """
    sink = os.path.join(run_dir, "noticed_bugs.jsonl")
    os.makedirs(run_dir, exist_ok=True)

    emitted = 0
    seen: set[tuple[str, str]] = set()

    with open(sink, "a", encoding="utf-8") as out:
        for p in targets:
            if emitted >= 5:
                break
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for rgx, severity, conf, scope in _PATTERNS:
                if emitted >= 5:
                    break
                if scope in _ONLY_ON_FAILURE and status != "failure":
                    continue
                for m in rgx.finditer(text):
                    if emitted >= 5:
                        break
                    title = m.group(0).strip()[:300]
                    key = (scope, title.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {
                        "agent_role": node_type or "unknown",
                        "task_class": task_class,
                        "severity": severity,
                        "title": title,
                        "description": f"Heuristic match ({scope}) in {os.path.basename(p)} produced by node {node_id} (status={status}).",
                        "suggested_fix": "",
                        "observed_in": p,
                        "confidence": conf,
                    }
                    out.write(json.dumps(row, separators=(",", ":")) + "\n")
                    emitted += 1

    # Mirrors bash line 154 `print(emitted, file=sys.stderr)` exactly.
    # Always fires on the heuristic path, even when emitted == 0.
    print(emitted, file=sys.stderr)
    return emitted


def _usage() -> str:
    """Return the help text bash ``--help`` would emit."""
    return _USAGE_BLOCK


def _parse_args(argv: list[str]) -> dict:
    """Mirror bash arg-parsing loop lines 37-48.

    Returns dict with keys: ``mode``, ``node_id``, ``node_type``,
    ``output_file``, ``status``, ``task_class``, ``help``. Default
    ``mode`` is read from ``$MO_BUG_COLLECTOR_MODE`` (bash line 34,
    falls back to ``"heuristic"``). Default ``status`` is ``"success"``
    (bash line 35). Unknown flags are silently ignored (mirrors bash's
    default ``*) shift ;;`` branch at line 46).
    """
    mode = os.environ.get("MO_BUG_COLLECTOR_MODE") or "heuristic"
    parsed = {
        "mode": mode,
        "node_id": "",
        "node_type": "",
        "output_file": "",
        "status": "success",
        "task_class": "",
        "help": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            parsed["help"] = True
            i += 1
        elif a == "--mode" and i + 1 < len(argv):
            parsed["mode"] = argv[i + 1]
            i += 2
        elif a == "--node-id" and i + 1 < len(argv):
            parsed["node_id"] = argv[i + 1]
            i += 2
        elif a == "--node-type" and i + 1 < len(argv):
            parsed["node_type"] = argv[i + 1]
            i += 2
        elif a == "--output-file" and i + 1 < len(argv):
            parsed["output_file"] = argv[i + 1]
            i += 2
        elif a == "--status" and i + 1 < len(argv):
            parsed["status"] = argv[i + 1]
            i += 2
        elif a == "--task-class" and i + 1 < len(argv):
            parsed["task_class"] = argv[i + 1]
            i += 2
        else:
            # Unknown flags ignored, mirrors bash `*) shift ;;`.
            i += 1
    return parsed


def main(argv: list[str]) -> int:
    """Dispatch ``argv`` mirroring bash bin/mini-ork-bug-collector.

    Returns 0 in EVERY branch — bash never fails its caller (line 173).
    The ``heuristic`` branch wraps ``heuristic_scan`` in a try/except so
    any IO error (e.g. unwriteable run_dir) silently exits 0 like bash
    does (the bash heredoc Python would crash but bash exits 0 anyway
    via the trailing ``exit 0``).
    """
    _ensure_env()
    parsed = _parse_args(argv)

    if parsed["help"]:
        sys.stdout.write(_usage())
        return 0

    mode = parsed["mode"]

    # Bash line 50: `[ "$mode" = "off" ] && exit 0` — no output at all.
    if mode == "off":
        return 0

    if mode == "heuristic":
        targets = _resolve_scan_targets(parsed["output_file"])
        # Bash line 77: `[ -z "$targets" ] && exit 0` — heredoc never
        # runs, so no stderr is emitted. Mirror exactly.
        if not targets:
            return 0
        run_dir = os.environ.get("MINI_ORK_RUN_DIR") or "/tmp"
        try:
            heuristic_scan(
                targets,
                node_id=parsed["node_id"],
                node_type=parsed["node_type"],
                status=parsed["status"],
                task_class=parsed["task_class"],
                run_dir=run_dir,
            )
        except Exception:
            # Mirror bash's always-exit-0 contract. Any IO error in
            # heuristic_scan (e.g. unwriteable sink) silently no-ops.
            pass
        return 0

    # Bash line 159-164: llm mode stub.
    if mode == "llm":
        sys.stderr.write("  [bug-collector] llm mode not yet implemented; use --mode heuristic\n")
        return 0

    # Bash line 167-169: unknown mode — stderr msg, then exit 0.
    sys.stderr.write(f"bug-collector: unknown mode '{mode}'\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))