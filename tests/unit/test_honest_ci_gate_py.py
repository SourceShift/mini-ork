"""Parity gate: mini_ork.ported.honest_ci_gate vs lib/honest_ci_gate.sh.

Every case constructs a tmp findings JSON, runs the LIVE bash subprocess
(via ``bash -c '. "$LIB" && mo_compute_finding_cis ...'``) AND the
in-process Python port, then compares the augmented findings-with-cis.json
(for ``compute_finding_cis``) and verdict JSON (for ``check_ci_widths``)
at 1e-6 float tolerance.

No mocks, no hardcoded expecteds — every expected is the live bash output.
If bash breaks, this test breaks (intentional). Mirrors the parity pattern
in test_coalition_gate_py.py / test_cost_pause_py.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import honest_ci_gate as hci  # noqa: E402

SH = REPO / "lib" / "honest_ci_gate.sh"


def _run_bash(args: list[str], **extra_env) -> subprocess.CompletedProcess:
    """Source the bash lib, then run ``args`` (a literal bash fragment)."""
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), **extra_env}
    return subprocess.run(
        ["bash", "-c", f'. "{SH}" && {args[0]}', *args[1:]],
        env=env,
        capture_output=True,
        text=True,
    )


def _bash_compute(in_path: Path, out_path: Path,
                  **extra_env) -> tuple[int, str]:
    r = _run_bash(
        ["mo_compute_finding_cis \"$1\" \"$2\"", "_", str(in_path), str(out_path)],
        **extra_env,
    )
    return r.returncode, r.stdout


def _bash_check(in_path: Path, report_dir: Path,
                **extra_env) -> tuple[dict, int]:
    r = _run_bash(
        ["mo_check_ci_widths \"$1\" \"$2\"", "_", str(in_path), str(report_dir)],
        **extra_env,
    )
    last = (r.stdout or "").strip().splitlines()
    if not last:
        raise AssertionError(
            f"bash emitted no verdict JSON line. stdout={r.stdout!r} "
            f"stderr={r.stderr!r} rc={r.returncode}"
        )
    return json.loads(last[-1]), r.returncode


def _scrub_paths(v: Any) -> Any:
    """Strip filesystem-path fields that legitimately differ between bash
    and Python tmp dirs. Augmented JSON contents (the *file*) are still
    diffed separately for compute_finding_cis cases.
    """
    if isinstance(v, dict) and "augmented_path" in v:
        v = {**v, "augmented_path": "<tmp-path-stripped>"}
    return v


def _cmp(actual: Any, expected: Any, tol: float = 1e-6, path: str = "root") -> None:
    """Recursively compare two parsed JSON values.

    Dicts: same key set, recurse on values. Lists: same length, recurse.
    Floats: abs(a-b) <= tol. Other: ==. Null matches None on both sides.
    """
    actual = _scrub_paths(actual)
    expected = _scrub_paths(expected)
    if isinstance(actual, dict) and isinstance(expected, dict):
        assert set(actual.keys()) == set(expected.keys()), (
            f"dict key mismatch at {path}: actual={sorted(actual.keys())} "
            f"expected={sorted(expected.keys())}"
        )
        for k in actual:
            _cmp(actual[k], expected[k], tol, f"{path}.{k}")
        return
    if isinstance(actual, list) and isinstance(expected, list):
        assert len(actual) == len(expected), (
            f"list len mismatch at {path}: {len(actual)} vs {len(expected)}"
        )
        for i, (a, e) in enumerate(zip(actual, expected)):
            _cmp(a, e, tol, f"{path}[{i}]")
        return
    if isinstance(actual, float) and isinstance(expected, (int, float)):
        assert abs(actual - expected) <= tol, (
            f"float drift at {path}: actual={actual!r} expected={expected!r}"
        )
        return
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        assert abs(actual - expected) <= tol, (
            f"float drift at {path}: actual={actual!r} expected={expected!r}"
        )
        return
    assert actual == expected, f"value mismatch at {path}: {actual!r} vs {expected!r}"


def _write_findings(tmp_path: Path, name: str, findings: list) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"findings": findings}))
    return p


# ---------------------------------------------------------------------------
# compute_finding_cis parity (4 cases)
# ---------------------------------------------------------------------------

def test_compute_finding_cis_tight_panel(tmp_path):
    findings = [
        {"id": "F-001", "title": "Auth retry storm",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
        {"id": "F-002", "title": "Cache key collision",
         "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
        {"id": "F-003", "title": "Null cursor crash",
         "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}},
    ]
    in_path = _write_findings(tmp_path, "tight", findings)
    bash_out = tmp_path / "tight-bash.json"
    py_out = tmp_path / "tight-py.json"

    rc_bash, _ = _bash_compute(in_path, bash_out)
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))

    assert rc_bash == 0 and rc_py == 0
    _cmp(json.loads(py_out.read_text()), json.loads(bash_out.read_text()))


def test_compute_finding_cis_split_panel(tmp_path):
    findings = [
        {"id": "F-001", "title": "Race condition",
         "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
        {"id": "F-002", "title": "Stale cache read",
         "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
        {"id": "F-003", "title": "Missing escape",
         "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
    ]
    in_path = _write_findings(tmp_path, "split", findings)
    bash_out = tmp_path / "split-bash.json"
    py_out = tmp_path / "split-py.json"

    rc_bash, _ = _bash_compute(in_path, bash_out)
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))

    assert rc_bash == 0 and rc_py == 0
    _cmp(json.loads(py_out.read_text()), json.loads(bash_out.read_text()))


def test_single_vote_zero_width(tmp_path):
    findings = [
        {"id": "F-001", "title": "Lone vote",
         "lens_votes": {"glm": 2}},
    ]
    in_path = _write_findings(tmp_path, "single", findings)
    bash_out = tmp_path / "single-bash.json"
    py_out = tmp_path / "single-py.json"

    rc_bash, _ = _bash_compute(in_path, bash_out)
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))

    assert rc_bash == 0 and rc_py == 0
    actual = json.loads(py_out.read_text())
    expected = json.loads(bash_out.read_text())
    _cmp(actual, expected)

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
    bash_out = tmp_path / "nonum-bash.json"
    py_out = tmp_path / "nonum-py.json"

    rc_bash, _ = _bash_compute(in_path, bash_out)
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))

    assert rc_bash == 0 and rc_py == 0
    actual = json.loads(py_out.read_text())
    expected = json.loads(bash_out.read_text())
    _cmp(actual, expected)

    ci = actual["findings"][0]["confidence_interval"]
    assert ci["n"] == 0
    assert ci["mean"] is None and ci["sd"] is None and ci["sem"] is None
    assert ci["ci_low"] is None and ci["ci_high"] is None
    assert ci["ci_width"] is None
    assert ci["note"] == "no_numeric_votes"


# ---------------------------------------------------------------------------
# check_ci_widths parity (3 cases)
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
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    v_bash, rc_bash = _bash_check(in_path, bash_dir)
    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_bash["verdict"] == "ci_within_band" and rc_bash == 0
    assert v_py["verdict"] == "ci_within_band" and rc_py == 0
    _cmp(v_py, v_bash)


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
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    v_bash, rc_bash = _bash_check(in_path, bash_dir)
    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_bash["verdict"] == "CI_TOO_WIDE" and rc_bash == 1
    assert v_py["verdict"] == "CI_TOO_WIDE" and rc_py == 1
    _cmp(v_py, v_bash)


def test_missing_input_indeterminate(tmp_path):
    bash_dir = tmp_path / "bash"
    py_dir = tmp_path / "py"
    bash_dir.mkdir()
    py_dir.mkdir()

    bogus = tmp_path / "does-not-exist.json"
    v_bash, rc_bash = _bash_check(bogus, bash_dir)
    v_py, rc_py = hci.check_ci_widths(str(bogus), str(py_dir))

    assert v_bash["verdict"] == "indeterminate" and rc_bash == 0
    assert v_py["verdict"] == "indeterminate" and rc_py == 0
    _cmp(v_py, v_bash)


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
    bash_out = tmp_path / "custom-bash.json"
    py_out = tmp_path / "custom-py.json"
    rc_bash, _ = _bash_compute(in_path, bash_out)
    rc_py = hci.compute_finding_cis(str(in_path), str(py_out))

    assert rc_bash == 0 and rc_py == 0
    actual = json.loads(py_out.read_text())
    expected = json.loads(bash_out.read_text())
    _cmp(actual, expected)

    for ci in actual["findings"]:
        assert ci["confidence_interval"]["t_critical"] == 2.576, ci

    # 2) check_ci_widths: ceiling flips to 0.5; default wide-ratio ceiling
    #    0.3 still holds -> F-002 (ci_width ~ 4.461 at conf=0.99) trips the
    #    CI_TOO_WIDE branch.
    bash_dir = tmp_path / "bash2"
    py_dir = tmp_path / "py2"
    bash_dir.mkdir()
    py_dir.mkdir()

    v_bash, rc_bash = _bash_check(in_path, bash_dir)
    v_py, rc_py = hci.check_ci_widths(str(in_path), str(py_dir))

    assert v_bash["verdict"] == v_py["verdict"] == "CI_TOO_WIDE"
    assert rc_bash == rc_py == 1
    _cmp(v_py, v_bash)
