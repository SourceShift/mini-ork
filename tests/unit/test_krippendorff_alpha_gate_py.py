"""Parity gate: ``mini_ork.ported.krippendorff_alpha_gate.check_panel_alpha``
vs ``lib/krippendorff_alpha_gate.sh:mo_check_panel_alpha``.

Every case invokes the LIVE bash function in a fresh subprocess — the
bash output is the control; the test never hardcodes an expected verdict.
The Python port must match the bash output byte-for-byte at the
structural level (floats within 1e-6; rationale strings identical;
verdict dict equality on every key).

Eight parity cases covering every code path in the bash heredoc:

  (a) high-agreement 4-lens panel     → ``panel_calibrated`` rc=0
  (b) moderate-agreement 4-lens panel → ``panel_calibrated`` rc=0
  (c) low-agreement 4-lens panel      → ``ALPHA_ESCALATE``    rc=1
  (d) missing input file              → ``indeterminate`` ``no_panel_scores`` rc=0
  (e) ragged matrix                   → ``indeterminate`` ``no_panel_scores`` rc=0
  (f) non-numeric score entry         → ``indeterminate`` ``no_panel_scores`` rc=0
  (g) lens_count < min_lenses         → ``indeterminate`` ``insufficient_panel`` rc=0
  (h) constant-marginal panel         → ``alpha=1.0`` ``panel_calibrated`` rc=0

Strangler-fig co-existence: ``lib/krippendorff_alpha_gate.sh`` is
byte-identical before and after this test exists. The test only WRITES
to ``tmp_path`` fixture files and READS from ``lib/krippendorff_alpha_gate.sh``.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import krippendorff_alpha_gate as kag  # noqa: E402

SH = REPO / "lib" / "krippendorff_alpha_gate.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Skip if required CLI tools are absent — matches bash's fail-open posture.
# ─────────────────────────────────────────────────────────────────────────────
pytestmark = pytest.mark.skipif(
    shutil.which("python3") is None or shutil.which("jq") is None
                  or shutil.which("bash") is None,
    reason="python3, bash, and jq required to drive the live bash subprocess",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _bash(run_dir: str, *, threshold: str | None = None,
          min_lenses: str | None = None,
          input_path: str | None = None,
          scores_tsv: str | None = None) -> subprocess.CompletedProcess:
    """Invoke ``mo_check_panel_alpha <run_dir>`` in a fresh bash that has
    sourced ``lib/krippendorff_alpha_gate.sh``.

    The bash wrapper at the end of the bash script sources itself; we
    re-source via the inner ``source`` command to get the function into
    the test shell. Env knobs are passed through to MO_ALPHA_*.
    """
    env = {
        "MINI_ORK_ROOT": str(REPO),
    }
    if threshold is not None:
        env["MO_ALPHA_THRESHOLD"] = threshold
    if min_lenses is not None:
        env["MO_ALPHA_MIN_LENSES"] = min_lenses
    if input_path is not None:
        env["MO_ALPHA_INPUT_PATH"] = input_path
    if scores_tsv is not None:
        env["MO_ALPHA_SCORES_TSV"] = scores_tsv

    wrapper = (
        f'. "{SH}"\n'
        f'mo_check_panel_alpha "{run_dir}"\n'
    )
    return subprocess.run(
        ["bash", "-c", wrapper], env={**os.environ, **env},
        capture_output=True, text=True,
    )


def _bash_verdict(run_dir: str, **kw) -> tuple[dict, int]:
    """Run the bash function and return ``(verdict_dict, rc)``.

    The bash function emits the verdict JSON on stdout (one line) and
    the exit code as the rc. We parse the JSON line for the dict.

    Raises ``RuntimeError`` if the bash subprocess returns an unexpected
    rc (anything other than 0/1) or fails to emit a parseable JSON line —
    in either case parity cannot be asserted and the test should fail
    loudly rather than silently skip.
    """
    r = _bash(run_dir, **kw)
    if r.returncode not in (0, 1):
        raise RuntimeError(
            f"bash returned unexpected rc={r.returncode}; "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
    try:
        verdict_dict = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise RuntimeError(
            f"bash stdout unparseable: stdout={r.stdout!r} stderr={r.stderr!r}"
        ) from e
    return verdict_dict, r.returncode


def _compare_payloads(py: dict, bash: dict, *,
                      float_tol: float = 1e-6) -> None:
    """Structural equality gate. Compares dict shapes, string fields
    verbatim, and floats within ``float_tol``.

    The bash output goes through ``json.loads`` first to normalize (since
    bash also prints JSON). The Python port's dict is the contract dict
    returned by ``check_panel_alpha``.
    """
    assert set(py.keys()) == set(bash.keys()), (
        f"key mismatch: py={sorted(py.keys())} bash={sorted(bash.keys())}"
    )
    for k, v in py.items():
        bv = bash[k]
        if isinstance(v, float) or isinstance(bv, float):
            assert math.isclose(float(v), float(bv), rel_tol=float_tol, abs_tol=float_tol), (
                f"float mismatch at {k!r}: py={v!r} bash={bv!r}"
            )
        elif v is None or bv is None:
            assert v is None and bv is None, (
                f"None mismatch at {k!r}: py={v!r} bash={bv!r}"
            )
        else:
            assert v == bv, f"field {k!r}: py={v!r} bash={bv!r}"


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0, f"bash rc={bash_rc} stderr={bash_verdict!r}"

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "panel_calibrated"
    assert py_verdict["reason"] == "ok"
    assert py_verdict["alpha"] == 1.0  # constant marginals → alpha=1.0
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 5


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "panel_calibrated"
    assert py_verdict["reason"] == "ok"
    assert py_verdict["alpha"] >= py_verdict["alpha_threshold"]
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 5


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 1

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 1

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "ALPHA_ESCALATE"
    assert py_verdict["reason"] == "low_alpha"
    assert py_verdict["alpha"] < py_verdict["alpha_threshold"]
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 5
    # Rationale format is identical bash <-> Python (same .3f format).
    assert "alpha " in py_verdict["rationale"]
    assert "< threshold" in py_verdict["rationale"]


# ─────────────────────────────────────────────────────────────────────────────
# (d) missing input file → indeterminate no_panel_scores rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_input_file_no_panel_scores(tmp_path):
    run_dir = tmp_path
    run_dir.mkdir(parents=True, exist_ok=True)
    # NO panel-verdict.json — gate must fall through to indeterminate.
    assert not (run_dir / "panel-verdict.json").exists()

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "indeterminate"
    assert py_verdict["reason"] == "no_panel_scores"
    assert py_verdict["alpha"] is None
    assert py_verdict["lens_count"] == 0
    assert py_verdict["item_count"] == 0
    assert "missing" in py_verdict["rationale"]


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "indeterminate"
    assert py_verdict["reason"] == "no_panel_scores"
    assert "ragged" in py_verdict["rationale"]
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 0


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "indeterminate"
    assert py_verdict["reason"] == "no_panel_scores"
    assert "non-numeric" in py_verdict["rationale"]
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) lens_count < min_lenses → indeterminate insufficient_panel rc=0
# ─────────────────────────────────────────────────────────────────────────────
def test_insufficient_lenses_insufficient_panel(tmp_path):
    # Only one lens — below the default min_lenses=2.
    run_dir = _write_panel(tmp_path, {
        "glm": [8, 7, 9, 8, 7],
    })

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "indeterminate"
    assert py_verdict["reason"] == "insufficient_panel"
    assert py_verdict["alpha"] is None
    assert py_verdict["lens_count"] == 1
    assert py_verdict["item_count"] == 5
    assert "need >= " in py_verdict["rationale"]


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

    py_verdict, py_rc = kag.check_panel_alpha(str(run_dir))
    assert py_rc == 0

    bash_verdict, bash_rc = _bash_verdict(str(run_dir))
    assert bash_rc == 0

    _compare_payloads(py_verdict, bash_verdict)
    assert py_verdict["verdict"] == "panel_calibrated"
    assert py_verdict["reason"] == "ok"
    assert py_verdict["alpha"] == 1.0
    assert py_verdict["lens_count"] == 4
    assert py_verdict["item_count"] == 4