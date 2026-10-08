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
import re
from typing import Any

__all__ = [
    "normalize_zero_questions",
    "declares_verification_command",
    "normalize_kickoff_complete",
]

_NORMALIZED_MARKER = "needs_answers->ready (0 questions: nothing to answer)"

# A kickoff may state how success is proven, as one of:
#   ## Verification commands          (heading)
#   **Verification Command:** `...`   (bold-labelled line)
#   Verification Command: `...`       (plain label)
#   # Proof of success
#   ## How we will be verified
# Any of these means the run_profile has nothing genuine left to ask about —
# the three standard questions (success criteria, scope, proof) are all
# answerable from the kickoff itself. Leading ``#`` and ``**`` decoration is
# tolerated so a bolded label counts the same as a heading.
_VERIFICATION_DECL_RE = re.compile(
    r"^[ \t]{0,3}\*{0,2}[ \t]*#{0,6}[ \t]*\*{0,2}[ \t]*"
    r"(?:verification commands?|proof of success|how (?:we|this) will be verified)\b",
    re.IGNORECASE | re.MULTILINE,
)

_DEFERRED_MARKER = (
    "kickoff declares a verification command — profile questions deferred, "
    "not interrogated (the kickoff is self-sufficient)"
)


def declares_verification_command(kickoff_text: str) -> bool:
    """True when the kickoff itself states how the run's success is proven.

    Deliberately precise: only an explicit ``Verification Command(s)`` /
    ``Proof of success`` heading or the same words as a plain label. Mentioning
    a test command in prose does NOT count — a false negative just keeps today's
    behaviour (one LLM interrogation), while a false positive would let an
    incomplete kickoff through the profile gate.
    """
    return bool(_VERIFICATION_DECL_RE.search(kickoff_text or ""))


def normalize_kickoff_complete(profile_path: str) -> str:
    """Ready a profile whose kickoff already declares its verification command.

    Mirrors :func:`normalize_zero_questions`' contract (empty path / malformed
    JSON / write failure all return without raising; every other key is
    preserved), but the trigger is the kickoff, not a zero-length question list:
    the outstanding questions are MOVED to ``deferred_questions`` rather than
    dropped, the confidence floor is lifted, and the reason is recorded in
    ``profile_status_normalized``.
    """
    if not profile_path or not os.path.isfile(profile_path):
        return ""

    try:
        with open(profile_path, encoding="utf-8") as f:
            profile: dict[str, Any] = json.load(f)
    except Exception:
        return ""

    status = str(profile.get("profile_status") or "")
    if status != "needs_answers":
        return status

    questions = profile.get("human_questions") or []
    deferred = profile.get("deferred_questions") or []
    if isinstance(deferred, list):
        deferred = [*deferred, *questions]
    profile["profile_status"] = "ready"
    profile["human_questions"] = []
    profile["deferred_questions"] = deferred
    try:
        current = float(profile.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        current = 0.0
    profile["confidence"] = max(current, 0.9)
    profile["profile_status_normalized"] = _DEFERRED_MARKER
    try:
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
            f.write("\n")
    except Exception:
        return status

    return "ready"


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
