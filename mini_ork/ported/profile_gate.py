"""profile_gate — Python port of lib/profile_gate.sh::mo_profile_normalize_zero_questions.

Parity contract (byte-identical observable behaviour vs the live bash):
  empty / missing path                  -> returns ""  (no file write, no stderr)
  malformed JSON / read failure         -> returns ""  (no file write)
  status=='needs_answers' AND
        not human_questions            -> rewrites file (status='ready',
                                          empty human_questions,
                                          profile_status_normalized marker,
                                          ALL OTHER KEYS PRESERVED) and
                                          returns "ready"
  otherwise                            -> returns str(profile_status or "");
                                          file untouched

The bash source uses bare ``except Exception`` for both JSON-load and file-write
failures (read-only fs, partial writes, decode errors) — this port mirrors that
breadth so partial-write / read-only-fs edge cases stay parity-stable. Output
stdout is bare string (no trailing newline); parity test normalises both sides
via ``str.strip()``.
"""
from __future__ import annotations

import json
import os
from typing import Any

__all__ = ["normalize_zero_questions"]

_NORMALIZED_MARKER = "needs_answers->ready (0 questions: nothing to answer)"


def normalize_zero_questions(profile_path: str) -> str:
    """Normalize planner-profile zero-questions contradiction; return status.

    See module docstring for parity contract.
    """
    if not profile_path or not os.path.isfile(profile_path):
        return ""

    try:
        with open(profile_path, encoding="utf-8") as f:
            profile: dict[str, Any] = json.load(f)
    except Exception:
        return ""

    status = str(profile.get("profile_status") or "")
    questions = profile.get("human_questions") or []
    if status == "needs_answers" and not questions:
        profile["profile_status"] = "ready"
        profile["human_questions"] = []
        profile["profile_status_normalized"] = _NORMALIZED_MARKER
        try:
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2)
                f.write("\n")
        except Exception:
            pass
        status = "ready"

    return status
