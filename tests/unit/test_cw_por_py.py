"""Standalone unit tests for ``mini_ork.gates.cw_por.compute_cw_por``.

Replaces the bash-parity gate (against ``lib/cw_por.sh:mo_compute_cw_por``)
as part of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs the LIVE bash function in a
subprocess — it asserts the port's behaviour directly. The expected values
below are the semantic contract the bash side used to pin (verdicts, pair
counts, threshold semantics, error modes), now asserted on the port's
output.

Six cases:

  (a) clean panel — 4 voters, correct votes dominate with higher
      confidence → ``verdict=panel_healthy``, ``cw_por < threshold``
  (b) captured panel — wrong voters dominate adoption with higher
      confidence → ``verdict=authority_capture_suspected``,
      ``cw_por > threshold``
  (c) indeterminate — all voters have ``ground_truth_match=None`` →
      ``cw_por`` is ``None``, ``verdict=indeterminate``,
      ``n_with_ground_truth=0``
  (d) threshold override — explicit ``threshold=`` kwarg and the
      ``MO_CW_POR_THRESHOLD`` env var both reach the port
  (e) malformed JSON (missing ``.voters[]``) — Python raises
      ``ValueError``
  (f) missing verdict file — Python raises ``FileNotFoundError``
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import cw_por as cwp


# ─────────────────────────────────────────────────────────────────────────────
# (a) clean panel — low CW-POR → panel_healthy
# ─────────────────────────────────────────────────────────────────────────────
def test_clean_panel_panel_healthy(tmp_path):
    panel = tmp_path / "clean.json"
    panel.write_text(json.dumps({
        "voters": [
            {"voter_id": "glm",     "vote": "approve", "confidence": 0.85,
             "ground_truth_match": True},
            {"voter_id": "kimi",    "vote": "approve", "confidence": 0.80,
             "ground_truth_match": True},
            {"voter_id": "codex",   "vote": "approve", "confidence": 0.75,
             "ground_truth_match": True},
            {"voter_id": "minimax", "vote": "reject",  "confidence": 0.30,
             "ground_truth_match": False},
        ],
    }))

    py_rc, py_payload = cwp.compute_cw_por(str(panel))
    assert py_rc == 0
    assert py_payload["verdict"] == "panel_healthy"
    assert py_payload["cw_por"] < py_payload["threshold"]
    assert py_payload["n_correct"] == 3
    assert py_payload["n_wrong"] == 1
    assert py_payload["n_pairs_evaluated"] == 3
    assert py_payload["adopted_vote"] == "approve"


# ─────────────────────────────────────────────────────────────────────────────
# (b) captured panel — high CW-POR → authority_capture_suspected
# ─────────────────────────────────────────────────────────────────────────────
def test_captured_panel_authority_capture_suspected(tmp_path):
    panel = tmp_path / "captured.json"
    panel.write_text(json.dumps({
        "voters": [
            {"voter_id": "glm",     "vote": "approve", "confidence": 0.40,
             "ground_truth_match": True},
            {"voter_id": "kimi",    "vote": "reject",  "confidence": 0.95,
             "ground_truth_match": False},
            {"voter_id": "codex",   "vote": "reject",  "confidence": 0.90,
             "ground_truth_match": False},
            {"voter_id": "minimax", "vote": "reject",  "confidence": 0.85,
             "ground_truth_match": False},
        ],
    }))

    py_rc, py_payload = cwp.compute_cw_por(str(panel))
    assert py_rc == 0
    assert py_payload["verdict"] == "authority_capture_suspected"
    assert py_payload["cw_por"] > py_payload["threshold"]
    assert py_payload["n_correct"] == 1
    assert py_payload["n_wrong"] == 3
    assert py_payload["n_pairs_evaluated"] == 3
    assert py_payload["adopted_vote"] == "reject"


# ─────────────────────────────────────────────────────────────────────────────
# (c) indeterminate — all voters ground_truth_match=None
# ─────────────────────────────────────────────────────────────────────────────
def test_indeterminate_no_ground_truth_signal(tmp_path):
    panel = tmp_path / "unknown.json"
    panel.write_text(json.dumps({
        "voters": [
            {"voter_id": "glm",  "vote": "approve", "confidence": 0.85,
             "ground_truth_match": None},
            {"voter_id": "kimi", "vote": "reject",  "confidence": 0.70,
             "ground_truth_match": None},
            {"voter_id": "codex","vote": "approve", "confidence": 0.60,
             "ground_truth_match": None},
        ],
    }))

    py_rc, py_payload = cwp.compute_cw_por(str(panel))
    assert py_rc == 0
    assert py_payload["cw_por"] is None
    assert py_payload["verdict"] == "indeterminate"
    assert py_payload["n_with_ground_truth"] == 0
    assert py_payload["n_voters"] == 3
    assert py_payload["rationale"]
    # Normal-branch keys must NOT leak into indeterminate output.
    for k in ("n_correct", "n_wrong", "n_pairs_evaluated", "adopted_vote"):
        assert k not in py_payload


# ─────────────────────────────────────────────────────────────────────────────
# (d) threshold override — kwarg AND env var both reach the port
# ─────────────────────────────────────────────────────────────────────────────
def test_threshold_env_override_honored(tmp_path):
    # A captured-panel layout (wrong voters more confident, adopted
    # vote == wrong vote) which yields cw_por > 0.
    # cw_por here: 1 pair (glm, kimi) delta=0.15, 1 pair (glm, codex)
    # delta=0.10, adopted=reject, correct_vote=approve (correct[0].vote).
    # Both deltas>0 and adopted==w.vote and adopted!=correct_vote → both
    # count as overrides. overrides=0.25, pairs=2, cw_por=0.125. So
    # threshold 0.05 → capture; threshold 0.95 → healthy.
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({
        "voters": [
            {"voter_id": "glm",     "vote": "approve", "confidence": 0.40,
             "ground_truth_match": True},
            {"voter_id": "kimi",    "vote": "reject",  "confidence": 0.55,
             "ground_truth_match": False},
            {"voter_id": "codex",   "vote": "reject",  "confidence": 0.50,
             "ground_truth_match": False},
        ],
    }))

    # Sub-case 1: threshold=0.05 → authority_capture_suspected
    py_rc, py_payload = cwp.compute_cw_por(str(panel), threshold=0.05)
    assert py_rc == 0
    assert py_payload["verdict"] == "authority_capture_suspected"
    assert py_payload["threshold"] == 0.05
    assert abs(py_payload["cw_por"] - 0.125) <= 1e-6

    # Sub-case 2: threshold=0.95 → panel_healthy (cw_por=0.125 < 0.95)
    py_rc2, py_payload2 = cwp.compute_cw_por(str(panel), threshold=0.95)
    assert py_rc2 == 0
    assert py_payload2["verdict"] == "panel_healthy"
    assert py_payload2["threshold"] == 0.95

    # Sub-case 3: env var MO_CW_POR_THRESHOLD reaches the port when no
    # explicit threshold argument is given.
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MO_CW_POR_THRESHOLD", "0.05")
    try:
        py_rc3, py_payload3 = cwp.compute_cw_por(str(panel))
    finally:
        monkey.undo()
    assert py_rc3 == 0
    assert py_payload3["verdict"] == "authority_capture_suspected"
    assert py_payload3["threshold"] == 0.05


# ─────────────────────────────────────────────────────────────────────────────
# (e) malformed — missing .voters[]
# ─────────────────────────────────────────────────────────────────────────────
def test_malformed_missing_voters_raises_value_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"verdict": "approve"}')

    with pytest.raises(ValueError):
        cwp.compute_cw_por(str(bad))

    # Also: an empty voters array must trigger the same failure mode.
    empty = tmp_path / "empty.json"
    empty.write_text('{"voters": []}')

    with pytest.raises(ValueError):
        cwp.compute_cw_por(str(empty))


# ─────────────────────────────────────────────────────────────────────────────
# (f) missing verdict file
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_file_raises_file_not_found(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert not missing.exists()

    with pytest.raises(FileNotFoundError):
        cwp.compute_cw_por(str(missing))
