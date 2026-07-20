"""Python port of ``lib/citation_verifier_mechanical.sh::mo_check_citations``.

Recall-floor + wireheading check on synthesized documents. Extracts every
``path:LINE`` (or ``path:START-END``) citation, verifies each resolves to
a real file with the cited line range in bounds, computes a coverage ratio,
and gates on a recall floor. Mirrors the bash semantics byte-stable so the
parity test in ``tests/unit/test_citation_verifier_mechanical_py.py`` can
diff live-bash vs ported-Python output at 1e-6 float tolerance.

Public API:
    ``check_citations(doc, report_dir=None, root=None, floor=None, min_count=None)``
    returns ``(verdict_dict, rc)``. Same dict shape bash emits on stdout; same
    rc semantics (``rc=0`` when gate cannot measure or coverage >= floor;
    ``rc=1`` when ``CITATION_UNDERCOVERED`` triggers).

Env knobs honored (overridable via kwargs):
    ``MO_CITATION_COVERAGE_FLOOR``  default 0.8
    ``MO_CITATION_MIN_COUNT``       default 3
    ``MINI_ORK_ROOT``               default = parent of ``lib/`` (this module's
                                    parent's parent)
    ``MINI_ORK_RUN_DIR``            used as ``report_dir`` fallback

The bash-only ``mo_grounded_rejection`` side-effect from ``gates_common.sh``
is intentionally NOT replicated here — Python callers wire rejection through
their own substrate.
"""
from __future__ import annotations

import os
import re
from typing import Any


# Regex lifted verbatim from the bash heredoc — left boundary excludes word
# chars and slashes; path allows letters/digits/._-/; extension whitelist
# filters out arxiv-style "arxiv:NUMBER" false-positives; right boundary
# excludes word chars.
CITATION_PATTERN = re.compile(
    r"(?<![\w/])"
    r"((?:[A-Za-z0-9_.\-/]+)"
    r"\.(?:py|ts|tsx|js|jsx|sh|bash|yaml|yml|"
    r"json|toml|md|rst|sql|go|rs|c|h|cc|cpp|hpp|"
    r"java|kt|swift|rb|php|html|css|scss|conf|"
    r"ini|cfg))"
    r":(\d+)(?:-(\d+))?"
    r"(?![\w])"
)


def _default_root() -> str:
    """Mirror bash: ``cd $(dirname BASH_SOURCE)/.. && pwd`` resolves to the
    parent of ``lib/`` where this module's ancestors live."""
    # mini_ork/gates/citation_verifier_mechanical.py → parents[2] = repo root
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return repo_root


def _extract_citations(text: str) -> list[tuple[str, int, int]]:
    """Return ``[(path, start, end), ...]`` with duplicates on
    ``(path, start, end)`` collapsed in first-seen order."""
    raw: list[tuple[str, int, int]] = []
    for m in CITATION_PATTERN.finditer(text):
        path, start, end = m.group(1), m.group(2), m.group(3)
        raw.append((path, int(start), int(end) if end else int(start)))
    seen: set[tuple[str, int, int]] = set()
    out: list[tuple[str, int, int]] = []
    for path, start, end in raw:
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        out.append((path, start, end))
    return out


def _count_lines(path: str, cap: int) -> int:
    """Count lines in ``path`` up to ``cap`` (early-exit when cap reached) —
    matches bash's short-circuit byte-count."""
    line_count = 0
    with open(path, "rb") as fh:
        for _ in fh:
            line_count += 1
            if line_count >= cap:
                break
    return line_count


def _check_one(path: str, start: int, end: int, root: str) -> tuple[bool, str]:
    """Validate one citation. Returns ``(ok, reason)`` — same reason strings
    bash emits so parity is byte-stable."""
    if start < 1 or end < start:
        return False, "bad_line_range"
    resolved = path if os.path.isabs(path) else os.path.join(root, path)
    if not os.path.isfile(resolved):
        return False, "file_missing"
    try:
        line_count = _count_lines(resolved, end)
    except Exception as exc:
        return False, f"read_error:{exc}"
    if end > line_count:
        return False, f"line_out_of_bounds:{line_count}"
    return True, "ok"


def _write_report(
    report_path: str,
    rows: list[tuple[str, int, int, bool, str]],
) -> None:
    """Best-effort TSV report. Mirrors bash: header + rows, silently swallow
    errors (audit aid only)."""
    try:
        parent = os.path.dirname(report_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("path\tlines\tverdict\treason\n")
            lines = []
            for path, start, end, ok, reason in rows:
                range_text = f"{start}-{end}" if end != start else str(start)
                lines.append(f"{path}\t{range_text}\t{'PASS' if ok else 'FAIL'}\t{reason}")
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass  # audit aid only — bash's exact posture


def check_citations(
    doc: str | None,
    report_dir: str | None = None,
    root: str | None = None,
    floor: float | None = None,
    min_count: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Python equivalent of ``mo_check_citations <doc> [<report_dir>]``.

    Returns ``(verdict_dict, rc)``. ``verdict_dict`` has the same keys and
    rationale strings bash emits to stdout; ``rc`` matches the bash
    function's return code (``0`` = gate does not fire or cannot measure,
    ``1`` = ``CITATION_UNDERCOVERED``).
    """
    if floor is None:
        floor = float(os.environ.get("MO_CITATION_COVERAGE_FLOOR", "0.8"))
    if min_count is None:
        min_count = int(os.environ.get("MO_CITATION_MIN_COUNT", "3"))
    if root is None:
        env_root = os.environ.get("MINI_ORK_ROOT")
        root = env_root if env_root else _default_root()
    if report_dir is None:
        report_dir = os.environ.get("MINI_ORK_RUN_DIR", ".")

    # Shell-level early return: missing/empty doc — matches bash printf path
    # that OMITS ``report_path`` from the dict. Rc=0.
    if not doc or not os.path.isfile(doc):
        return {
            "verdict": "indeterminate",
            "reason": "missing_document",
            "coverage": None,
            "coverage_floor": floor,
            "total_citations": 0,
            "valid_citations": 0,
            "invalid_citations": 0,
            "unique_files": 0,
            "rationale": "document path missing or not a file; cannot measure",
        }, 0

    # Shell-level early return: root not a directory — same dict shape as
    # missing_document (no report_path). Rc=0.
    if not os.path.isdir(root):
        return {
            "verdict": "indeterminate",
            "reason": "missing_root",
            "coverage": None,
            "coverage_floor": floor,
            "total_citations": 0,
            "valid_citations": 0,
            "invalid_citations": 0,
            "unique_files": 0,
            "rationale": "MINI_ORK_ROOT not a directory; cannot resolve citations",
        }, 0

    report_path = os.path.join(report_dir, "citation-report.tsv")
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass  # mkdir is best-effort in bash too

    # Read doc — on error, emit-style missing_document (WITH report_path).
    # Distinct from the shell-level early return above.
    try:
        text = open(doc, encoding="utf-8").read()
    except Exception as exc:
        return {
            "verdict": "indeterminate",
            "reason": "missing_document",
            "coverage": None,
            "coverage_floor": floor,
            "total_citations": 0,
            "valid_citations": 0,
            "invalid_citations": 0,
            "unique_files": 0,
            "report_path": report_path,
            "rationale": f"could not read {doc}: {exc}",
        }, 0

    citations = _extract_citations(text)
    total = len(citations)

    if total < min_count:
        reason = "no_citations_found" if total == 0 else "insufficient_citations"
        return {
            "verdict": "indeterminate",
            "reason": reason,
            "coverage": None,
            "coverage_floor": floor,
            "total_citations": total,
            "valid_citations": 0,
            "invalid_citations": 0,
            "unique_files": 0,
            "report_path": report_path,
            "rationale": f"found {total} citations; need >= {min_count} for the gate to engage",
        }, 0

    valid = 0
    invalid = 0
    rows: list[tuple[str, int, int, bool, str]] = []
    unique_files: set[str] = set()
    for path, start, end in citations:
        unique_files.add(path)
        ok, reason_text = _check_one(path, start, end, root)
        if ok:
            valid += 1
        else:
            invalid += 1
        rows.append((path, start, end, ok, reason_text))

    _write_report(report_path, rows)

    coverage = valid / total if total else 0.0

    if coverage < floor:
        return {
            "verdict": "CITATION_UNDERCOVERED",
            "reason": "low_coverage",
            "coverage": round(coverage, 4),
            "coverage_floor": floor,
            "total_citations": total,
            "valid_citations": valid,
            "invalid_citations": invalid,
            "unique_files": len(unique_files),
            "report_path": report_path,
            "rationale": (
                f"citation coverage {coverage:.1%} < floor {floor:.0%} "
                f"({invalid} of {total} citations failed to resolve); "
                f"document is not safely grounded"
            ),
        }, 1

    return {
        "verdict": "citations_covered",
        "reason": "ok",
        "coverage": round(coverage, 4),
        "coverage_floor": floor,
        "total_citations": total,
        "valid_citations": valid,
        "invalid_citations": invalid,
        "unique_files": len(unique_files),
        "report_path": report_path,
        "rationale": (
            f"citation coverage {coverage:.1%} >= floor {floor:.0%} "
            f"across {total} citations spanning {len(unique_files)} unique files"
        ),
    }, 0