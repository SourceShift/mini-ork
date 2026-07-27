"""Standalone unit tests for ``mini_ork.gates.honest_ci_gate``.

Replaces the bash-parity gate (against ``lib/honest_ci_gate.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs the LIVE bash subprocess
(``bash -c '. "$LIB" && mo_compute_finding_cis ...'``) — it asserts the
port's behaviour directly. The expected values below are the semantic
contract the bash side used to pin (CI statistics from first principles,
verdicts, rc semantics, env knobs), now asserted on the port's output.

CI semantics pinned here (from the port's documented contract):

    n           number of numeric lens votes
    mean        statistics.fmean of the votes
    sd          sample stddev (n-1 divisor)
    sem         sd / sqrt(n)
    ci_low/high mean +/- t_{conf, n-1} * sem
    ci_width    ci_high - ci_low

with t_critical(df=3, conf=0.95)=3.182 and conf>0.95 → 2.576.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import honest_ci_gate as hci

_TOL = 1e-4  # port rounds CI fields to 4dp


def _write_findings(tmp_path: Path, name: str, findings: list) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"findings": findings}))
    return p


def _expected_ci(votes: list[float], conf: float = 0.95) -> dict:
    """First-principles CI for a vote vector (the formula the port
    documents), for comparison at 4dp rounding."""
    n = len(votes)
    m = statistics.fmean(votes)
    sd = statistics.stdev(votes)
    sem = sd / math.sqrt(n)
    tc = hci.t_critical(n - 1, conf)
    half = tc * sem
    return {
        "n": n,
        "mean": round(m, 4),
        "sd": round(sd, 4),
        "sem": round(sem, 4),
        "ci_low": round(m - half, 4),
        "ci_high": round(m + half, 4),
        "ci_width": round(2 * half, 4),
        "confidence": conf,
        "t_critical": round(tc, 4),
    }


# ---------------------------------------------------------------------------
# compute_finding_cis (4 cases)
# ---------------------------------------------------------------------------

def test_compute_finding_cis_tight_panel(tmp_path):
    """All four lenses agree per finding → sd=0 → zero-width CI pinned at
    the vote value; df=3 t_critical=3.182 at the default conf=0.95."""
    votes = {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}
    findings = [
        {"id": "F-001", "title": "Auth retry storm", "lens_votes": dict(votes)},
        {"id": "F-002", "title": "Cache key collision",
         "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
        {"id": "F-003", "title": "Null cursor crash",
         "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}},
    ]
    in_path = _write_findings(tmp_path, "tight", findings)
    py_out = tmp_path / "tight-py.json"

    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))
    assert rc_py == 0

    actual = json.loads(py_out.read_text())
    assert [f["id"] for f in actual["findings"]] == ["F-001", "F-002", "F-003"]
    for f, value in zip(actual["findings"], (2.0, 3.0, 1.0)):
        ci = f["confidence_interval"]
        assert ci["n"] == 4
        assert ci["mean"] == value
        assert ci["sd"] == 0.0
        assert ci["sem"] == 0.0
        assert ci["ci_low"] == ci["ci_high"] == value
        assert ci["ci_width"] == 0.0
        assert ci["t_critical"] == 3.182
        assert ci["confidence"] == 0.95


def test_compute_finding_cis_split_panel(tmp_path):
    """Split votes → positive-width CIs matching the first-principles
    formula at 4dp rounding."""
    raw = [
        ("F-001", [0, 3, 0, 3]),
        ("F-002", [1, 4, 0, 5]),
        ("F-003", [2, 2, 2, 2]),
    ]
    findings = [
        {"id": fid, "title": fid,
         "lens_votes": dict(zip(("glm", "kimi", "codex", "minimax"), votes))}
        for fid, votes in raw
    ]
    in_path = _write_findings(tmp_path, "split", findings)
    py_out = tmp_path / "split-py.json"

    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))
    assert rc_py == 0

    actual = json.loads(py_out.read_text())
    for f, (fid, votes) in zip(actual["findings"], raw):
        assert f["confidence_interval"] == _expected_ci([float(v) for v in votes])


def test_single_vote_zero_width(tmp_path):
    findings = [
        {"id": "F-001", "title": "Lone vote",
         "lens_votes": {"glm": 2}},
    ]
    in_path = _write_findings(tmp_path, "single", findings)
    py_out = tmp_path / "single-py.json"

    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))
    assert rc_py == 0
    actual = json.loads(py_out.read_text())

    ci = actual["findings"][0]["confidence_interval"]
    assert ci["n"] == 1
    assert ci["ci_width"] == 0.0
    assert ci["ci_low"] == ci["ci_high"] == 2.0
    assert ci["note"] == "single_vote_zero_width_is_misleading"


def test_no_numeric_votes(tmp_path):
    findings = [
        {"id": "F-001", "title": "Empty votes", "lens_votes": {}},
    ]
    in_path = _write_findings(tmp_path, "nonum", findings)
    py_out = tmp_path / "nonum-py.json"

    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))
    assert rc_py == 0
    actual = json.loads(py_out.read_text())

    ci = actual["findings"][0]["confidence_interval"]
    assert ci["n"] == 0
    assert ci["mean"] is None and ci["sd"] is None and ci["sem"] is None
    assert ci["ci_low"] is None and ci["ci_high"] is None
    assert ci["ci_width"] is None
    assert ci["note"] == "no_numeric_votes"


# ---------------------------------------------------------------------------
# check_ci_widths (3 cases)
# ---------------------------------------------------------------------------

def test_check_ci_widths_within_band(tmp_path):
    findings = [
        {"id": "F-001", "title": "Auth retry storm",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
        {"id": "F-002", "title": "Cache key collision",
         "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
        {"id": "F-003", "title": "Null cursor crash",
         "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}},
    ]
    in_path = _write_findings(tmp_path, "tight", findings)
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_py["verdict"] == "ci_within_band" and rc_py == 0
    assert v_py["reason"] == "ok"
    # All zero-width → nothing trips the 2.0 ceiling.
    assert v_py["wide_count"] == 0
    assert v_py["total"] == 3
    assert v_py["wide_ratio"] == 0.0
    assert v_py["ci_width_ceiling"] == 2.0
    assert v_py["wide_ratio_ceiling"] == 0.3


def test_check_ci_widths_too_wide(tmp_path):
    findings = [
        {"id": "F-001", "title": "Race condition",
         "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
        {"id": "F-002", "title": "Stale cache read",
         "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
        {"id": "F-003", "title": "Missing escape",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
    ]
    in_path = _write_findings(tmp_path, "split", findings)
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_py["verdict"] == "CI_TOO_WIDE" and rc_py == 1
    assert v_py["reason"] == "wide_cis"
    # F-001 (width≈5.51) and F-002 (width≈7.57) trip the 2.0 ceiling;
    # F-003 is zero-width. 2/3 = 0.6667 > 0.3 ratio ceiling.
    assert v_py["wide_count"] == 2
    assert v_py["total"] == 3
    assert abs(v_py["wide_ratio"] - 2 / 3) <= _TOL


def test_missing_input_indeterminate(tmp_path):
    py_dir = tmp_path / "py"
    py_dir.mkdir()

    bogus = tmp_path / "does-not-exist.json"
    v_py, rc_py = hci.check_ci_widths(str(bogus), str(py_dir))

    assert v_py["verdict"] == "indeterminate" and rc_py == 0
    assert v_py["reason"] == "missing_input"
    assert v_py["wide_count"] == 0
    assert v_py["total"] == 0
    assert v_py["wide_ratio"] is None
    # Early-return shape: NO augmented_path key.
    assert "augmented_path" not in v_py


# ---------------------------------------------------------------------------
# Env knobs (1 case: custom confidence + custom ceiling)
# ---------------------------------------------------------------------------

def test_custom_confidence_and_ceiling(tmp_path, monkeypatch):
    findings = [
        {"id": "F-001", "title": "Tight votes",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
        {"id": "F-002", "title": "Wider votes",
         "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
        {"id": "F-003", "title": "Tighter votes",
         "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}},
    ]
    in_path = _write_findings(tmp_path, "custom", findings)
    monkeypatch.setenv("MO_CI_CONFIDENCE", "0.99")
    monkeypatch.setenv("MO_CI_WIDTH_CEILING", "0.5")

    # 1) compute_finding_cis: t_critical flips from 3.182 (df=3, conf=0.95)
    #    to 2.576 (conf=0.99 asymptote).
    py_out = tmp_path / "custom-py.json"
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))
    assert rc_py == 0
    actual = json.loads(py_out.read_text())

    for ci in actual["findings"]:
        assert ci["confidence_interval"]["t_critical"] == 2.576, ci
        assert ci["confidence_interval"]["confidence"] == 0.99

    # 2) check_ci_widths: ceiling flips to 0.5; the default wide-ratio
    #    ceiling 0.3 still holds -> F-002 (ci_width ~ 4.461 at conf=0.99)
    #    trips the CI_TOO_WIDE branch.
    py_dir = tmp_path / "py2"
    py_dir.mkdir()

    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_py["verdict"] == "CI_TOO_WIDE"
    assert rc_py == 1
    assert v_py["ci_width_ceiling"] == 0.5
    assert v_py["wide_count"] == 1  # only F-002 is wide at conf=0.99
    assert v_py["total"] == 3
