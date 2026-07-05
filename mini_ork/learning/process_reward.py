"""Process Reward Model (PRM) heuristic — faithful Python port.

Faithful port of ``lib/process_reward.sh::prm_score_trace``. Approximates a
per-node reward in ``[0.0, 1.0]`` from observable trace signals. The bash
function remains the canonical scorer (this module co-exists via the
strangler-fig pattern) and parity is enforced by
``tests/unit/test_process_reward_parity.py`` which invokes the live bash
function and compares scores within ``1e-6``.

Weight table (mirrors ``prm_score_trace`` and ``prm_backfill`` verbatim —
do not edit one without the other):

OUTCOME-GATED (proof-integrity Goodhart fix): every term below applies ONLY
when ``status == "success"``. A FAILED trace scores 0 regardless of activity.

================  ======  ==================================================
Signal            Weight  Trigger (all require status == success)
================  ======  ==================================================
status            0.50    ``trace["status"] == "success"``
reviewer_verdict  0.30    verdict in {approve, approved, pass, success, ok}
tool_calls        0.10    ``len(json.loads(tool_calls)) > 0``     [capped]
files_read/written 0.05   ``len(json.loads(...)) > 0``           [capped]
duration_ms       0.05    ``1000 <= duration_ms <= 600000``
cost_usd          0.00    RETIRED (rewarded cost>0 — near-constant, off-thesis)
================  ======  ==================================================

So outcome + independent review carry 0.80; activity/timeliness are a small
success-only bonus (combined activity clamped at ``ACTIVITY_CAP=0.15``). A bare
success = 0.50; a fully-approved active timely success = 1.00; any failure = 0.
This sharpens the success/failure separation the GRPO group-relative advantage
learns from (was: busy failures scored ~0.25). ``MO_PRM_ACTIVITY_CAP=0`` still
disables the activity clamp for A/B comparison.

Same-family decontamination is INTENTIONALLY ABSENT: the doer's lane is
not a valid proxy for the reviewer's lane without a real ``reviewer_model``
column in ``execution_traces``. Re-introducing it here would diverge from
bash and break parity. See the FE note in ``lib/process_reward.sh``.

Public API::

    from mini_ork.learning.process_reward import score_trace

    score = score_trace({
        "status": "success",
        "tool_calls": "[{...}]",
        "files_written": "[]",
        "reviewer_verdict": "approve",
        "duration_ms": 4200,
        "cost_usd": 0.012,
    })
"""

from __future__ import annotations

import json
import os

# Outcome-dominant weights (proof-integrity Goodhart fix). Outcome (status) +
# independent review (verdict) carry 0.80; activity/timeliness are a small
# SUCCESS-ONLY bonus. A FAILED node scores 0 regardless of how busy it was.
W_STATUS = 0.50
W_TOOL = 0.10
W_FILE = 0.05
W_VERDICT = 0.30
W_DURATION = 0.05
W_COST = 0.00      # retired: rewarded cost>0 (near-constant, off-thesis)
ACTIVITY_CAP = 0.15

_VERDICT_SET = {"approve", "approved", "pass", "success", "ok"}


def _len_json(s):
    """Parse ``s`` as JSON and return ``len`` if the result is list/dict.

    Returns ``0`` for ``None``, unparseable input, or non-container values
    (e.g. JSON-encoded strings, numbers, booleans). Mirrors bash's
    ``_len_json`` inside ``prm_score_trace``.
    """
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except (TypeError, ValueError):
        return 0


def _activity_cap_enabled() -> bool:
    """Mirror bash: ``MO_PRM_ACTIVITY_CAP=0`` disables; any other value (incl.
    unparseable) leaves the default cap-on behavior intact.
    """
    raw = os.environ.get("MO_PRM_ACTIVITY_CAP", "1")
    try:
        return int(raw) != 0
    except ValueError:
        return True


def score_trace(trace: dict) -> float:
    """Compute the PRM score for a single trace row.

    ``trace`` is a dict that mirrors the ``execution_traces`` row schema
    consumed by bash ``prm_score_trace``. The fields ``tool_calls``,
    ``files_written``, ``files_read`` are expected as JSON strings (the
    SQLite ``TEXT`` representation); pass the row dict as-is.

    Returns a float in ``[0.0, 1.0]`` rounded to 4 decimal places.
    """
    score = 0.0
    status_success = (trace.get("status") or "") == "success"
    # Goodhart fix: activity, timeliness, and verdict credit apply ONLY when the
    # node actually succeeded. A busy/timely FAILURE earns nothing — failures
    # score 0, sharpening the success/failure separation the GRPO group-relative
    # advantage learns from (was: failed-but-busy scored ~0.25 from activity).
    if status_success:
        score += W_STATUS

        tool_n = _len_json(trace.get("tool_calls"))
        file_n = _len_json(trace.get("files_written")) + _len_json(trace.get("files_read"))
        activity = (W_TOOL if tool_n > 0 else 0.0) + (W_FILE if file_n > 0 else 0.0)
        if _activity_cap_enabled():
            activity = min(activity, ACTIVITY_CAP)
        score += activity

        verdict_lower = (trace.get("reviewer_verdict") or "").lower()
        if verdict_lower in _VERDICT_SET:
            score += W_VERDICT

        dur = int(trace.get("duration_ms") or 0)
        if 1000 <= dur <= 600000:
            score += W_DURATION

    return round(min(1.0, max(0.0, score)), 4)


__all__ = [
    "ACTIVITY_CAP",
    "W_COST",
    "W_DURATION",
    "W_FILE",
    "W_STATUS",
    "W_TOOL",
    "W_VERDICT",
    "score_trace",
]