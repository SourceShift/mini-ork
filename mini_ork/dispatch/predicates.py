"""Structural-plausibility predicates for dispatch results (SE-3 Phase A.2).

These answer exactly one question: does a dispatch result even have the *shape*
the node expects — e.g. "is this JSON at all". The check runs at the harness
boundary (``dispatch_with_fallback`` / ``mo_llm_dispatch``) so a structurally
wrong lane result is rejected as a fallback-triggering failure *there*, next to
the spawn, instead of being written to an out_file and killed twenty minutes
later by the verifier that finally tries to parse it (the SE-3 Layer-2 failure:
a planner lane returned bad JSON, produced no usable artifact, and the run died
late with no lane fallback ever attempted).

They are deliberately NOT semantic validators. Whether the JSON carries the
right keys / satisfies the artifact schema stays ``validate_artifact``'s job.
Keeping the two apart is load-bearing: a second, drifting copy of the schema
must not grow here. A predicate answers "plausibly the right shape" (True) vs
"definitely the wrong shape" (False); when in genuine doubt it returns True
(fail-open) so a legitimate but unusual result is never dropped by an
over-eager boundary check.
"""

from __future__ import annotations

import json

from mini_ork.dispatch.models import DispatchResult

# Shape-rejection exit code. EX_DATAERR (sysexits.h): "the input data was
# incorrect in some way". The right BSD code for "the lane produced output, but
# of the wrong shape" — distinct from rc=124 (timeout) and rc=127 (spawn fail),
# so a caller / log reader can tell a shape rejection apart from a hung or
# unspawnable lane.
SHAPE_REJECT_RC = 65


def looks_like_json(result: DispatchResult) -> bool:
    """True if ``result.text`` plausibly contains a JSON object or array.

    Structural only. Tolerant of the two shapes a well-behaved model still emits
    around otherwise-valid JSON, so neither false-rejects legitimate output:

    * a ```json fenced block, and
    * a short prose preamble/epilogue around the object
      ("Here is the plan: {...}").

    Returns False only for text that is *definitely* not JSON — a prose refusal,
    a truncated fragment, an error stub, or empty output.
    """
    text = (result.text or "").strip()
    if not text:
        return False
    if _parses_as_json(text):
        return True
    unfenced = _strip_code_fence(text)
    if unfenced != text and _parses_as_json(unfenced):
        return True
    embedded = _slice_first_json(unfenced)
    return bool(embedded) and _parses_as_json(embedded)


def _parses_as_json(candidate: str) -> bool:
    try:
        json.loads(candidate)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _strip_code_fence(text: str) -> str:
    """Drop a leading ```/```json fence line and a trailing ``` fence, if any."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]  # drop the opening fence (``` or ```json)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _slice_first_json(text: str) -> str:
    """Return the widest {...} / [...] span, or "" when there is no opener."""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
    if not starts or not ends:
        return ""
    start, end = min(starts), max(ends)
    return text[start : end + 1] if end > start else ""
