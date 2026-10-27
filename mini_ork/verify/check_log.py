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


def _read_lines(log_path: str) -> list[str]:
    """Lines of *log_path*, or ``[]`` when it is missing or unreadable."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except (OSError, ValueError):
        return []


def _failed_bodies(lines: list[str]) -> list[tuple[str, list[str]]]:
    """``(check_id, section_lines)`` for every section whose body says FAIL.

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
        if not any(line.strip() == _FAIL_MARKER for line in section[1:]):
            continue
        failed.append((check_id, section))
    return failed


def failing_sections(
    log_path: str,
    names: list[str] | None = None,
    *,
    max_lines_per_check: int = 20,
    max_lines: int = 60,
) -> list[str]:
    """Header + body lines of each FAILED section, in file order.

    The ``FAIL`` marker itself is dropped and every line is ``rstrip``ped.
    ``max_lines_per_check`` counts the header (a section yields at most that
    many lines); ``max_lines`` caps the total across sections. ``names``
    filters on check id. A missing/unreadable file or a log with no failed
    section yields ``[]``.
    """
    selected = _failed_bodies(_read_lines(log_path))
    out: list[str] = []
    for check_id, section in selected:
        if names is not None and check_id not in names:
            continue
        kept = [section[0].rstrip()]
        for line in section[1:]:
            if line.strip() == _FAIL_MARKER:
                continue
            kept.append(line.rstrip())
        kept = kept[:max_lines_per_check]
        out.extend(kept[: max(0, max_lines - len(out))])
        if len(out) >= max_lines:
            break
    return out
