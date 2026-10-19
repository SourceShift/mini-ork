"""judge_validity — measure a judge lane against execution-grounded evidence.

The learning loop trusts an LLM reviewer's approve/reject as a reward, but a
reviewer is itself an instrument and can drift. This module compares each
reviewer *lane family* against what actually executed (``verifier_results``,
never the reviewer's own verdict), emits a per-lane agreement envelope, and is
consumed by :func:`mini_ork.gates.promotion_gate.promotion_evaluate` on the
promote branch.

Disciplines (mirroring the sibling guards):

  * **Unmeasured never blocks.** Missing history tables, rows without verifier
    evidence, or a lane below the overlap floor all resolve to ``admit=True``.
  * **Per lane, not global.** Wilson lower bounds are computed per lane family;
    a strong lane cannot mask a weak one.
  * **Execution side is independent.** A run "passed" only when a
    ``verifier_results`` row says ``verdict='pass'`` and is not operator-marked
    ``is_false_positive``. The judge's own verdict is the *other* half of the
    pair, never the ground truth.
  * **Tighten-only floor.** ``MO_JUDGE_VALIDITY_MIN`` may raise the 0.80 floor,
    never lower it.

Env knobs (read at call time, never cached at import):

  ``MO_JUDGE_VALIDITY``       off by default; ``"1"``/any non-off value enables
                              the promotion consumer (``"0"``/``"off"`` = off).
  ``MO_JUDGE_VALIDITY_ROWS``  history rows to audit (default 200).
  ``MO_JUDGE_VALIDITY_MIN``   agreement floor; values below 0.80 clamp up.

Public surface is stdlib-only at import time (json/math/os/sqlite3/tempfile/
time) so the promotion gate can import it without side effects.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import time

DEFAULT_MIN_AGREEMENT = 0.80
DEFAULT_MIN_OVERLAP = 30
DEFAULT_ROWS = 200

_APPROVE_TOKENS = frozenset(
    {"approve", "approved", "pass", "success", "ok"}
)
_REJECT_TOKENS = frozenset(
    {
        "reject",
        "rejected",
        "fail",
        "failed",
        "request_changes",
        "needs_revision",
        "escalate",
    }
)

# Mirrors ``mini_ork/learning/writeback.py:_lane_family`` (copied here rather
# than importing a private helper). The kickoff adds ``deepseek`` and ``codex``
# to the family vocabulary.
_FAMILY_TOKENS = ("opus", "glm", "minimax", "deepseek", "kimi", "codex")

_OFF_VALUES = frozenset({"", "0", "off", "false", "no"})


# ── env knobs ────────────────────────────────────────────────────────────────


def enabled() -> bool:
    """True when ``MO_JUDGE_VALIDITY`` asks the promotion consumer to run.

    The guard is off by default: an unset knob must leave the promotion result
    dict byte-identical to the pre-guard shape.
    """
    raw = os.environ.get("MO_JUDGE_VALIDITY")
    if raw is None:
        return False
    return str(raw).strip().lower() not in _OFF_VALUES


def rows_limit() -> int:
    """History rows to audit (``MO_JUDGE_VALIDITY_ROWS``, default 200)."""
    raw = os.environ.get("MO_JUDGE_VALIDITY_ROWS")
    try:
        value = int(raw) if raw is not None and str(raw).strip() else DEFAULT_ROWS
    except (TypeError, ValueError):
        return DEFAULT_ROWS
    return value if value > 0 else DEFAULT_ROWS


def min_agreement() -> float:
    """The agreement floor; ``MO_JUDGE_VALIDITY_MIN`` is tighten-only.

    A value below ``DEFAULT_MIN_AGREEMENT`` is clamped *up* to the default: a
    safety floor is not a setting, so configuration can never loosen it.
    """
    raw = os.environ.get("MO_JUDGE_VALIDITY_MIN")
    if raw is None:
        return DEFAULT_MIN_AGREEMENT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_AGREEMENT
    if math.isnan(value):
        return DEFAULT_MIN_AGREEMENT
    return max(DEFAULT_MIN_AGREEMENT, value)


# ── statistics ───────────────────────────────────────────────────────────────


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a binomial proportion.

    ``n == 0`` and ``successes == 0`` both return ``0.0``. The lower bound is
    the admission statistic: it shrinks toward 0 as the sample gets small, so a
    handful of lucky agreements can never clear a high floor.
    """
    if n <= 0:
        return 0.0
    successes = max(0, min(int(successes), int(n)))
    if successes == 0:
        return 0.0
    p = successes / n
    denom = 1.0 + (z * z) / n
    center = p + (z * z) / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n))
    lower = (center - margin) / denom
    return max(0.0, min(1.0, lower))


def agreement(
    rows: list[tuple[bool, bool]],
    *,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
) -> dict:
    """Agreement envelope for ``(judge_approve, execution_pass)`` pairs.

    Below ``min_overlap`` the lane is *unmeasured*: ``accuracy`` and friends are
    ``None`` and ``admit`` is ``True``. Measured lanes admit when the Wilson
    lower bound clears the (tighten-only) agreement floor.
    """
    n = len(rows)
    if n < min_overlap:
        return {
            "measured": False,
            "n": n,
            "accuracy": None,
            "false_approve": None,
            "false_reject": None,
            "accuracy_lower": None,
            "min_overlap": min_overlap,
            "admit": True,
            "reason": f"insufficient overlap (n={n})",
        }

    agree = sum(1 for judge_approve, execution_pass in rows
                if judge_approve == execution_pass)
    false_approve = sum(
        1 for judge_approve, execution_pass in rows
        if judge_approve and not execution_pass
    )
    false_reject = sum(
        1 for judge_approve, execution_pass in rows
        if not judge_approve and execution_pass
    )
    accuracy = agree / n
    lower = wilson_lower(agree, n)
    floor = min_agreement()
    admit = lower >= floor
    if admit:
        reason = (
            f"accuracy_lower={lower:.3f} >= min_agreement={floor:.2f} (n={n})"
        )
    else:
        reason = (
            f"accuracy_lower={lower:.3f} < min_agreement={floor:.2f} "
            f"(accuracy={accuracy:.3f}, n={n})"
        )
    return {
        "measured": True,
        "n": n,
        "accuracy": accuracy,
        "false_approve": false_approve / n,
        "false_reject": false_reject / n,
        "accuracy_lower": lower,
        "min_overlap": min_overlap,
        "admit": admit,
        "reason": reason,
    }


# ── audit ────────────────────────────────────────────────────────────────────


def _lane_family(agent_version_id: object) -> str:
    """Family token for a lane, mirroring writeback's helper.

    No token (or no agent version) lands in ``"_unknown"`` so those rows are
    still measured as a lane rather than silently dropped.
    """
    if not agent_version_id:
        return "_unknown"
    av = str(agent_version_id).lower()
    for token in _FAMILY_TOKENS:
        if token in av:
            return token
    return "_unknown"


def _judge_side(verdict: object) -> bool | None:
    """Map ``reviewer_verdict`` to approve (True) / reject (False) / skip."""
    if verdict is None:
        return None
    token = str(verdict).strip().lower()
    if token in _APPROVE_TOKENS:
        return True
    if token in _REJECT_TOKENS:
        return False
    return None


def _execution_side(con: sqlite3.Connection, run_id: object) -> bool | None:
    """Ground truth for a run: any genuine pass row means pass.

    ``is_false_positive=1`` marks an operator-rejected pass, so it does not
    count. No verifier rows at all means the run is unmeasured (``None``).
    """
    rows = con.execute(
        "SELECT verdict, is_false_positive FROM verifier_results "
        "WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    for verdict, is_false_positive in rows:
        if str(verdict or "").strip().lower() != "pass":
            continue
        try:
            flagged = int(is_false_positive or 0)
        except (TypeError, ValueError):
            flagged = 0
        if flagged == 0:
            return True
    return False


def _no_history() -> dict:
    return {"rows": 0, "lanes": {}, "admit": True, "reason": "no history"}


def audit(db_path: str, *, limit: int = DEFAULT_ROWS) -> dict:
    """Audit stored history, one agreement envelope per judge lane family.

    Judge rows come from ``execution_traces.reviewer_verdict``; the execution
    side comes from ``verifier_results`` for the same ``run_id``. Unknown
    verdicts and runs with no verifier rows are skipped, never guessed.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_ROWS
    if limit <= 0:
        return _no_history()

    con = sqlite3.connect(db_path)
    try:
        present = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('execution_traces', 'verifier_results')"
            )
        }
        if not {"execution_traces", "verifier_results"} <= present:
            return _no_history()

        traces = con.execute(
            "SELECT run_id, reviewer_verdict, agent_version_id "
            "FROM execution_traces "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()

        by_lane: dict[str, list[tuple[bool, bool]]] = {}
        rows = 0
        for run_id, verdict, agent_version_id in traces:
            judge_approve = _judge_side(verdict)
            if judge_approve is None:
                continue  # NULL/empty/unknown verdict -> skip the row
            execution_pass = _execution_side(con, run_id)
            if execution_pass is None:
                continue  # no verifier evidence -> unmeasured, skip the row
            by_lane.setdefault(
                _lane_family(agent_version_id), []
            ).append((judge_approve, execution_pass))
            rows += 1
    finally:
        con.close()

    if not by_lane:
        return _no_history()

    lanes: dict[str, dict] = {}
    for lane_name in sorted(by_lane):
        pairs = by_lane[lane_name]
        envelope = agreement(pairs)
        if not envelope["measured"]:
            # The lane stays UNMEASURED for admission (measured=False,
            # accuracy_lower=None, admit=True) — but the raw observed rates are
            # still reported, so an operator can see *why* a lane has not
            # accumulated evidence. No envelope is written for an unmeasured
            # lane (write_envelopes filters on "measured").
            n = len(pairs)
            agree = sum(1 for judge_approve, execution_pass in pairs
                        if judge_approve == execution_pass)
            envelope["accuracy"] = agree / n if n else None
            envelope["false_approve"] = (
                sum(1 for judge_approve, execution_pass in pairs
                    if judge_approve and not execution_pass) / n
                if n else None
            )
            envelope["false_reject"] = (
                sum(1 for judge_approve, execution_pass in pairs
                    if not judge_approve and execution_pass) / n
                if n else None
            )
        lanes[lane_name] = envelope
    failing = [
        lane for lane in sorted(lanes)
        if lanes[lane]["measured"] and not lanes[lane]["admit"]
    ]
    floor = min_agreement()
    if failing:
        first = failing[0]
        lane = lanes[first]
        admit = False
        reason = (
            f"lane {first}: accuracy_lower={lane['accuracy_lower']:.3f} < "
            f"min_agreement={floor:.2f} "
            f"(accuracy={lane['accuracy']:.3f}, n={lane['n']})"
        )
    else:
        admit = True
        measured = [lane for lane in sorted(lanes) if lanes[lane]["measured"]]
        if measured:
            reason = (
                f"all {len(measured)} measured lane(s) meet "
                f"min_agreement={floor:.2f}"
            )
        else:
            reason = "no measured lanes (insufficient overlap)"

    return {
        "rows": rows,
        "lanes": lanes,
        "admit": admit,
        "reason": reason,
        "audited_at": int(time.time()),
    }


# ── envelopes ────────────────────────────────────────────────────────────────


def write_envelopes(db_path: str, result: dict) -> list[str]:
    """Write one ``<lane>.json`` sidecar per measured lane, atomically.

    Unmeasured lanes never get an envelope: a sidecar is evidence, and a fake
    or partial one would be trusted by later readers. Returns the written
    paths (empty when nothing was measured).
    """
    lanes = result.get("lanes") or {}
    measured = {
        name: envelope
        for name, envelope in lanes.items()
        if envelope.get("measured")
    }
    if not measured:
        return []

    out_dir = os.path.join(
        os.path.dirname(os.path.abspath(db_path)), "judge-validity"
    )
    os.makedirs(out_dir, exist_ok=True)
    audited_at = result.get("audited_at")
    if not isinstance(audited_at, int):
        audited_at = int(time.time())

    paths: list[str] = []
    for name in sorted(measured):
        payload = {"lane": name}
        payload.update(measured[name])
        payload["audited_at"] = audited_at
        final_path = os.path.join(out_dir, f"{name}.json")
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".json", dir=out_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, final_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        paths.append(final_path)
    return paths


# ── promotion consumer ───────────────────────────────────────────────────────


def promotion_check(db_path: str) -> dict | None:
    """Run the audit for the promotion gate.

    Returns ``None`` when the guard is off or the audit is unmeasured (no
    measured lane); otherwise returns the audit dict including ``admit``. This
    is the production producer — the audit runs here, not only in tests.
    """
    if not enabled():
        return None
    result = audit(db_path, limit=rows_limit())
    if not any(lane.get("measured") for lane in result.get("lanes", {}).values()):
        return None
    return result
