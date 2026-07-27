"""Standalone unit tests for
``mini_ork.gates.krippendorff_alpha_gate.check_panel_alpha``.

Replaces the bash-parity gate (against
``lib/krippendorff_alpha_gate.sh:mo_check_panel_alpha``) as part of the
bash→Python migration: the Python port is now the sole implementation, so
its coverage no longer runs the LIVE bash function in a subprocess — it
asserts the port's behaviour directly. The expected values below are the
semantic contract the bash side used to pin (verdicts, reasons, rc
semantics, alpha values, rationale substrings), now asserted on the
port's output.

Eight cases covering every code path:

  (a) high-agreement 4-lens panel     → ``panel_calibrated`` rc=0
  (b) moderate-agreement 4-lens panel → ``panel_calibrated`` rc=0
  (c) low-agreement 4-lens panel      → ``ALPHA_ESCALATE``    rc=1
  (d) missing input file              → ``indeterminate`` ``no_panel_scores`` rc=0
  (e) ragged matrix                   → ``indeterminate`` ``no_panel_scores`` rc=0
  (f) non-numeric score entry         → ``indeterminate`` ``no_panel_scores`` rc=0
  (g) lens_count < min_lenses         → ``indeterminate`` ``insufficient_panel`` rc=0
  (h) constant-marginal panel         → ``alpha=1.0`` ``panel_calibrated`` rc=0
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import krippendorff_alpha_gate as kag


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _write_panel(run_dir: Path, lens_scores: dict) -> Path:
    """Write a ``panel-verdict.json`` under ``run_dir`` with the given
    ``lens_scores`` payload. Returns the run_dir path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "panel-verdict.json").write_text(json.dumps({"lens_scores": lens_scores}))
    return run_dir


# ─────────────────────────────────────────────────────────────────────────────
# (a) high-agreement 4-lens panel → panel_calibrated rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_high_agreement_panel_calibrated(tmp_path):
    run_dir = _write_panel(tmp_path, {
        "glm":     [8, 7, 9, 8, 7],
        "kimi":    [8, 7, 9, 8, 7],
        "codex":   [8, 7, 9, 8, 7],
        "minimax": [8, 7, 9, 8, 7],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "panel_calibrated"
    assert verdict["reason"] == "ok"
    assert verdict["alpha"] == 1.0  # constant marginals → alpha=1.0
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# (b) moderate-agreement 4-lens panel → panel_calibrated rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_moderate_agreement_panel_calibrated(tmp_path):
    run_dir = _write_panel(tmp_path, {
        "glm":     [8, 6, 9, 7, 8],
        "kimi":    [7, 7, 8, 6, 7],
        "codex":   [9, 5, 9, 8, 8],
        "minimax": [8, 6, 8, 7, 7],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "panel_calibrated"
    assert verdict["reason"] == "ok"
    assert verdict["alpha"] >= verdict["alpha_threshold"]
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# (c) low-agreement 4-lens panel → ALPHA_ESCALATE rc=1
# ─────────────────────────────────────────────────────────────────────────────
def test_low_agreement_alpha_escalate(tmp_path):
    run_dir = _write_panel(tmp_path, {
        "glm":     [1, 9, 2, 8, 3],
        "kimi":    [9, 1, 8, 2, 9],
        "codex":   [2, 8, 3, 7, 2],
        "minimax": [8, 2, 9, 3, 8],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 1
    assert verdict["verdict"] == "ALPHA_ESCALATE"
    assert verdict["reason"] == "low_alpha"
    assert verdict["alpha"] < verdict["alpha_threshold"]
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 5
    assert "alpha " in verdict["rationale"]
    assert "< threshold" in verdict["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) missing input file → indeterminate no_panel_scores rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_input_file_no_panel_scores(tmp_path):
    run_dir = tmp_path
    run_dir.mkdir(parents=True, exist_ok=True)
    # NO panel-verdict.json — gate must fall through to indeterminate.
    assert not (run_dir / "panel-verdict.json").exists()

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "indeterminate"
    assert verdict["reason"] == "no_panel_scores"
    assert verdict["alpha"] is None
    assert verdict["lens_count"] == 0
    assert verdict["item_count"] == 0
    assert "missing" in verdict["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (e) ragged matrix → indeterminate no_panel_scores rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_ragged_matrix_no_panel_scores(tmp_path):
    run_dir = _write_panel(tmp_path, {
        "glm":     [8, 6, 9],
        "kimi":    [7, 7],
        "codex":   [9],
        "minimax": [8, 6, 8, 7],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "indeterminate"
    assert verdict["reason"] == "no_panel_scores"
    assert "ragged" in verdict["rationale"]
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (f) non-numeric score entry → indeterminate no_panel_scores rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_non_numeric_score_no_panel_scores(tmp_path):
    run_dir = _write_panel(tmp_path, {
        "glm":     [8, "bad", 9, 8, 7],
        "kimi":    [8, 7, 9, 8, 7],
        "codex":   [8, 7, 9, 8, 7],
        "minimax": [8, 7, 9, 8, 7],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "indeterminate"
    assert verdict["reason"] == "no_panel_scores"
    assert "non-numeric" in verdict["rationale"]
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) lens_count < min_lenses → indeterminate insufficient_panel rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_insufficient_lenses_insufficient_panel(tmp_path):
    # Only one lens — below the default min_lenses=2.
    run_dir = _write_panel(tmp_path, {
        "glm": [8, 7, 9, 8, 7],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "indeterminate"
    assert verdict["reason"] == "insufficient_panel"
    assert verdict["alpha"] is None
    assert verdict["lens_count"] == 1
    assert verdict["item_count"] == 5
    assert "need >= " in verdict["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (h) constant-marginal panel → alpha=1.0 panel_calibrated rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_constant_marginal_alpha_one(tmp_path):
    # All lenses score identically → exp_total=0 → alpha=1.0 fallback.
    # Use 4 identical lenses across 4 items (different from (a) which
    # had 5 items; this also covers the item_count=4 path).
    run_dir = _write_panel(tmp_path, {
        "glm":     [5, 5, 5, 5],
        "kimi":    [5, 5, 5, 5],
        "codex":   [5, 5, 5, 5],
        "minimax": [5, 5, 5, 5],
    })

    verdict, rc = kag.check_panel_alpha(str(run_dir))
    assert rc == 0
    assert verdict["verdict"] == "panel_calibrated"
    assert verdict["reason"] == "ok"
    assert verdict["alpha"] == 1.0
    assert verdict["lens_count"] == 4
    assert verdict["item_count"] == 4
