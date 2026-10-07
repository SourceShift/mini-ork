"""Learning & memory page — a tab per question, with the operator's first
question ("is mini-ork getting better at my work?") answered by the Overview.

Tab dispatch lives in ``build()``. Each tab module exposes ``sections(home,
args, errors)`` that returns a list of guarded sections.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn import code, improve, lessons, memory, overview

TITLE = "Learning & memory"
SUB = ("Is mini-ork getting better at your work, what has it learned, "
       "and what needs you.")
TABS = [("code", "Your code"), ("overview", "Overview"), ("lessons", "Lessons"),
        ("memory", "Memory"), ("improve", "Self-improve")]
DEFAULT_TAB = TABS[0][0]

# Old tab keys → new tab key. Keep these working for any deep-linked URLs the
# IDE has bookmarked; the dispatch remap is local to this module.
_OLD_KEY_MAP = {"learnings": "lessons", "traceotter": "improve", "bugs": "overview"}


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    key = _OLD_KEY_MAP.get(tab or "", tab or "")
    if key not in {k for k, _ in TABS}:
        key = DEFAULT_TAB
    if key == "code":
        sections_out = code.sections(home, args, errors)
    elif key == "overview":
        sections_out = overview.sections(home, args, errors)
    elif key == "lessons":
        sections_out = lessons.sections(home, args, errors)
    elif key == "memory":
        sections_out = memory.sections(home, args, errors)
    else:
        sections_out = improve.sections(home, args, errors)
    return S.page("learn", TITLE, SUB, chips_=[], actions=[], tabs=TABS, tab=key,
                  args=args, sections=sections_out, errors=errors)