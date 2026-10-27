"""Parse the sectioned log written by the static-check verifier.

The verifier writes one section per check::

    [no-test-gaming] diff adds no test-harness detection ...
      mini_ork/cli/execute_handlers.py:1715: pytest-env-probe: ...
      FAIL

A section that passed ends with ``ok`` — and may still contain error-shaped
lines from a command that failed by design (a forward apply-check on an
already-applied patch, for example). Selecting feedback lines by their text
alone therefore surfaces the wrong check; :func:`failing_sections` selects by
the section's own verdict instead. The revise (``mini_ork.cli.execute``) and
auto-repair (``mini_ork.recovery.auto_repair``) feedback builders both lead
with this, so a one-line failure reaches the implementer by name.

Stdlib only — this is read inside the executor's hot path.
"""
from __future__ import annotations

import re

# ``[<check-id>] <description>`` at column zero. Indented bracketed text is a
# body line, not a new section.
SECTION_RE = re.compile(r"^\[([A-Za-z0-9_.:-]+)\]\s")

_FAIL_MARKER = "FAIL"

# A returned line is truncated to this many characters plus an ellipsis; the
# total returned text stops before it would exceed ``_MAX_TOTAL_CHARS``.
_MAX_LINE_CHARS = 400
_ELLIPSIS = "…"
_MAX_TOTAL_CHARS = 8_000


def _read_lines(log_path: str) -> list[str]:
    """Lines of *log_path*, or ``[]`` when it is missing or unreadable."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except (OSError, ValueError):
        return []


def _last_non_blank(section: list[str]) -> int | None:
    """Index of the section's last line that has non-whitespace content."""
    for idx in range(len(section) - 1, -1, -1):
        if section[idx].strip():
            return idx
    return None


def _verdict_index(section: list[str]) -> int | None:
    """Index of the section's verdict line, or ``None`` when it has none.

    The static-check verifier ends every section with its verdict, so only the
    last non-blank line decides. A bare ``FAIL`` echoed earlier in the body is
    command output, not a verdict.
    """
    idx = _last_non_blank(section)
    if idx is None or section[idx].strip() != _FAIL_MARKER:
        return None
    return idx


def _failed_bodies(lines: list[str]) -> list[tuple[str, list[str]]]:
    """``(check_id, section_lines)`` for every section whose verdict is FAIL.

    ``section_lines[0]`` is the header; a preamble before the first header is
    not part of any section.
    """
    sections: list[tuple[str, list[str]]] = []
    current_id: str | None = None
    current: list[str] = []
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            if current_id is not None:
                sections.append((current_id, current))
            current_id = match.group(1)
            current = [line]
        elif current_id is not None:
            current.append(line)
    if current_id is not None:
        sections.append((current_id, current))

    failed: list[tuple[str, list[str]]] = []
    for check_id, section in sections:
        if _verdict_index(section) is None:
            continue
        failed.append((check_id, section))
    return failed


def _section_lines(section: list[str], cap: int) -> list[str]:
    """Header plus the section's body tail, within *cap* lines.

    The verdict line and anything after it are dropped. The header counts
    toward *cap*; when body lines are dropped a single omission marker is
    inserted right after the header, and one more body line is dropped so the
    marker does not push the section past its cap.
    """
    header = section[0].rstrip()
    verdict_idx = _verdict_index(section)
    body = section[1 : verdict_idx]  # drop the verdict and trailing blanks
    budget = cap - 1  # the header counts toward the cap
    if budget <= 0:
        return [header]
    if len(body) <= budget:
        return [header] + [line.rstrip() for line in body]
    # One slot of the tail budget goes to the omission marker.
    keep = budget - 1
    kept = body[-keep:] if keep else []
    omitted = len(body) - len(kept)
    return (
        [header, f"  {_ELLIPSIS} ({omitted} earlier lines omitted)"]
        + [line.rstrip() for line in kept]
    )


def _truncate(line: str) -> str:
    """Cap a line at ``_MAX_LINE_CHARS`` characters, marking the cut."""
    if len(line) > _MAX_LINE_CHARS:
        return line[:_MAX_LINE_CHARS] + _ELLIPSIS
    return line


def failing_sections(
    log_path: str,
    names: list[str] | None = None,
    *,
    max_lines_per_check: int = 20,
    max_lines: int = 60,
) -> list[str]:
    """Header + body lines of each FAILED section, in file order.

    A section is failed only when its last non-blank line is ``FAIL``. The
    verdict line itself is dropped, the section keeps the *tail* of its body
    (the finding lines the verifier prints just before the verdict), and each
    returned line is truncated to 400 characters plus ``…``. ``names`` filters
    on check id; when it matches no failed section the unfiltered failed
    sections are returned instead, so a verifier that names its checks
    differently in JSON and in the log does not lose the failures.
    ``max_lines_per_check`` counts the header (a section yields at most that
    many lines); ``max_lines`` caps the total across sections. A
    missing/unreadable file or a log with no failed section yields ``[]``.
    """
    if max_lines <= 0:
        return []
    selected = _failed_bodies(_read_lines(log_path))
    if names is not None:
        matching = [
            (check_id, section)
            for check_id, section in selected
            if check_id in names
        ]
        if matching:
            selected = matching

    out: list[str] = []
    total_chars = 0
    for _check_id, section in selected:
        for line in _section_lines(section, max_lines_per_check):
            line = _truncate(line)
            if total_chars + len(line) > _MAX_TOTAL_CHARS:
                return out
            out.append(line)
            total_chars += len(line)
            if len(out) >= max_lines:
                return out
    return out
