"""Confidence-Weighted Persuasion Override Rate diagnostic — Python port of
``lib/cw_por.sh``.

Faithful port of the bash function ``mo_compute_cw_por``. Mirrors:

  * file-existence validation + ``.voters[]`` shape validation (raises
    ``FileNotFoundError`` / ``ValueError`` to mirror bash ``rc=2`` + stderr)
  * the indeterminate branch (all voters have ``ground_truth_match=None``)
  * the ``Counter.most_common(1)`` panel-adoption proxy on ``approve|reject``
    votes
  * the pairwise (correct, wrong) loop with confidence-delta overrides
  * threshold-anchored verdict + rationale templating
  * float rounding via ``round(x, 4)`` and ``{x:.3f}`` formatting

Public API mirrors the bash contract:

  ``compute_cw_por(verdict_file, threshold=None)`` → ``(rc, dict)``
    rc == 0 on success, ``dict`` is the JSON-ready payload (matches the
    bash stdout byte-for-byte at the structural level).
    rc == 2 raises ``FileNotFoundError`` (missing file) or
    ``ValueError`` (missing/empty ``.voters[]``).

The bash implementation (lib/cw_por.sh) stays in place; this port gives
Python callers an in-process target and gives the parity gate in
``tests/unit/test_cw_por_py.py`` a stable surface to compare against the
live bash subprocess.

Orthogonal to Krippendorff α (Nasser 2026 arxiv:2601.05114): α measures
agreement reliability but is BLIND to authority-capture. CW-POR (Agarwal &
Khanna 2025, arxiv:2504.00374 §3.2) is the orthogonal axis — the rate at
which the panel adopts a high-confidence wrong answer over a low-confidence
correct one, weighted by the confidence delta. A healthy panel shows low α
+ low CW-POR or high α + low CW-POR; a captured panel shows high α + high
CW-POR.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from typing import Optional

__all__ = ["compute_cw_por", "DEFAULT_THRESHOLD"]

DEFAULT_THRESHOLD: float = 0.3


def _read_threshold(explicit: Optional[float]) -> float:
    """Resolve the threshold in priority order:
    1. explicit ``threshold`` argument
    2. ``$MO_CW_POR_THRESHOLD`` env var
    3. ``DEFAULT_THRESHOLD`` (0.3)
    """
    if explicit is not None:
        return float(explicit)
    env = os.environ.get("MO_CW_POR_THRESHOLD")
    if env is not None and env != "":
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def _validate_shape(verdict_file: str) -> dict:
    """Mirror bash's jq pre-validation + python3 json.load. Returns the
    parsed dict. Raises ``FileNotFoundError`` / ``ValueError`` on the
    same conditions bash returns ``rc=2`` for."""
    if not os.path.isfile(verdict_file):
        raise FileNotFoundError(f"verdict file not found: {verdict_file}")
    try:
        with open(verdict_file) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"json parse failed: {e}") from e
    voters = data.get("voters") if isinstance(data, dict) else None
    if not isinstance(voters, list) or len(voters) < 1:
        raise ValueError("verdict file missing required .voters[] array")
    return data


def _bucket(voters: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Partition voters by ground_truth_match. Mirrors the bash bucketing:
    ``is True`` / ``is False`` / ``is None`` — strict identity checks so
    any non-bool/non-None value falls through to unknown."""
    correct = [v for v in voters if v.get("ground_truth_match") is True]
    wrong = [v for v in voters if v.get("ground_truth_match") is False]
    unknown = [v for v in voters if v.get("ground_truth_match") is None]
    return correct, wrong, unknown


def _compute(correct: list[dict], wrong: list[dict], voters: list[dict],
             threshold: float) -> dict:
    """Run the pairwise-override loop. Mirrors bash lines 108–149 exactly:
    adopted vote via Counter.most_common(1) over approve|reject; pairs
    iterate every (correct, wrong) combination; an override contributes
    ``delta`` iff ``delta > 0`` AND the panel adopted the wrong vote AND
    the adopted vote differs from the first correct voter's vote."""
    votes = [v.get("vote") for v in voters
             if v.get("vote") in ("approve", "reject")]
    adopted = Counter(votes).most_common(1)[0][0] if votes else None
    correct_vote = correct[0].get("vote") if correct else None

    pairs = 0
    overrides = 0.0
    for c in correct:
        for w in wrong:
            c_conf = float(c.get("confidence", 0.0))
            w_conf = float(w.get("confidence", 0.0))
            delta = w_conf - c_conf
            pairs += 1
            if delta > 0 and adopted == w.get("vote") \
                    and adopted != correct_vote:
                overrides += delta

    cw_por = (overrides / pairs) if pairs > 0 else 0.0

    if cw_por > threshold:
        verdict = "authority_capture_suspected"
        rationale = (
            f"CW-POR={cw_por:.3f} exceeds threshold {threshold:.3f}; "
            f"the panel adopted a more-confident wrong vote over a "
            f"less-confident correct vote across {pairs} pair(s)"
        )
    else:
        verdict = "panel_healthy"
        rationale = (
            f"CW-POR={cw_por:.3f} within threshold {threshold:.3f}; "
            f"no measurable confidence-weighted override across "
            f"{pairs} (correct, wrong) pair(s)"
        )

    return {
        "cw_por": round(cw_por, 4),
        "threshold": threshold,
        "verdict": verdict,
        "rationale": rationale,
        "n_voters": len(voters),
        "n_correct": len(correct),
        "n_wrong": len(wrong),
        "n_pairs_evaluated": pairs,
        "adopted_vote": adopted,
    }


def compute_cw_por(verdict_file: str,
                   threshold: Optional[float] = None) -> tuple[int, dict]:
    """Faithful Python port of ``mo_compute_cw_por`` from ``lib/cw_por.sh``.

    Returns ``(0, payload)`` on success — ``payload`` matches the bash
    stdout JSON dict byte-for-byte at the structural level (floats within
    1e-6 tolerance; rationale string identical; keys identical).

    Raises ``FileNotFoundError`` if the file is missing (bash rc=2 +
    ``{"error":"verdict file not found: ..."}`` on stderr).

    Raises ``ValueError`` if the JSON is unparseable, the file is missing
    the required ``.voters[]`` array, or that array is empty (bash rc=2 +
    ``{"error":"verdict file missing required .voters[] array"}`` on stderr).
    """
    threshold = _read_threshold(threshold)
    data = _validate_shape(verdict_file)
    voters = data["voters"]

    correct, wrong, unknown = _bucket(voters)

    # Indeterminate branch — mirrors bash lines 86–98 exactly. Emits the
    # 5-key payload (no n_correct / n_wrong / n_pairs_evaluated / adopted_vote).
    if not (correct or wrong) and unknown:
        payload = {
            "cw_por": None,
            "threshold": threshold,
            "verdict": "indeterminate",
            "rationale": (
                "no ground_truth_match signal on any voter — "
                "CW-POR requires benchmark fixtures or a held-out "
                "anchor corpus to compute"
            ),
            "n_voters": len(voters),
            "n_with_ground_truth": 0,
        }
        return 0, payload

    return 0, _compute(correct, wrong, voters, threshold)