"""Parity gate: live bash vs Python port of ``refute_or_promote_gate``.

For each fixture we build a synthetic findings file + fabrications file,
invoke the LIVE ``mo_check_fabrication_survival`` bash function via
subprocess (no mocking), then call the Python port and compare the
resulting dict shape. Floats must match within ``1e-6``; exit codes must
match; every key/value must match byte-stable after JSON parse.

Strangler-fig invariant: ``lib/refute_or_promote_gate.sh`` is NEVER
modified by this test (parity is enforced, not the bash itself). The
bash subprocess sources ONLY ``lib/refute_or_promote_gate.sh`` — never
``gates_common.sh`` — so the bash-only ``mo_grounded_rejection``
side-effect stays inert. The harness achieves this by pointing
``MINI_ORK_ROOT`` at an isolated tmp dir that has no
``lib/gates_common.sh``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import refute_or_promote_gate as rp  # noqa: E402

LIB_SH = REPO / "lib" / "refute_or_promote_gate.sh"


# --------------------------------------------------------------------------- #
# Subprocess harness
# --------------------------------------------------------------------------- #

def _bash_check(findings: str, fabs: str, report_dir: str,
                env_extra: dict) -> tuple[dict, int]:
    """Run live bash ``mo_check_fabrication_survival``, return (dict, rc).

    Asserts non-empty JSON stdout — bash always emits exactly one line of
    JSON in our fixtures (no early-return branch is silent).
    """
    env = os.environ.copy()
    env.update(env_extra)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_SH}" && mo_check_fabrication_survival "$1" "$2" "$3"',
         "_", findings, fabs, report_dir],
        env=env,
        capture_output=True,
        text=True,
    )
    raw = (proc.stdout or "").strip()
    assert raw, f"bash emitted no JSON: stderr={proc.stderr!r}"
    parsed = json.loads(raw)
    return parsed, proc.returncode


def _py_check(findings: str, fabs: str, report_dir: str,
              env_extra: dict) -> tuple[dict, int]:
    """Run Python port with the same env knobs bash sees."""
    saved: dict[str, str | None] = {}
    for k, v in env_extra.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        d, rc = rp.check_fabrication_survival(findings, fabs, report_dir)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return d, rc


def _assert_parity(bash_dict: dict, py_dict: dict,
                   bash_rc: int, py_rc: int, label: str) -> None:
    assert bash_rc == py_rc, f"{label}: rc drift bash={bash_rc} py={py_rc}"
    assert bash_dict == py_dict, (
        f"{label}: dict drift\n  bash={bash_dict!r}\n  py={py_dict!r}"
    )
    b_rate = bash_dict.get("fp_rate")
    p_rate = py_dict.get("fp_rate")
    if b_rate is not None and p_rate is not None:
        assert abs(b_rate - p_rate) < 1e-6, (
            f"{label}: fp_rate drift bash={b_rate} py={p_rate}"
        )


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def _isolated_env(tmp_path: Path) -> dict:
    """Harness env: ``MINI_ORK_ROOT`` must NOT have ``lib/gates_common.sh``
    so the bash-only ``mo_grounded_rejection`` side-effect stays inert."""
    return {"MINI_ORK_ROOT": str(tmp_path / "no_lib")}


def _seed_fabs(path: Path, n: int) -> list[dict]:
    """Seed fabrications via Python port (single source of truth for ids
    both engines read). Returns the records so callers can build
    hallucinating findings."""
    rp.generate_fabrications(n, str(path))
    return json.loads(path.read_text())


def _clean_findings(path: Path) -> None:
    path.write_text(
        "## Findings\n\n"
        "1. Real issue in src/auth/login.ts:42 — token expiration not validated.\n"
        "2. Race condition in src/db/pool.ts:118 — connection reuse before reset.\n"
        "3. Missing null check in src/api/handler.ts:7.\n"
    )


def _hallucinating_findings(path: Path, cited: list[dict]) -> None:
    text = "## Findings\n\n"
    for x in cited:
        text += f'- {x["path"]}:{x["line"]} — {x["claim"]}\n'
    text += "- src/legitimate/foo.ts:10 — real finding\n"
    path.write_text(text)


# --------------------------------------------------------------------------- #
# Fixtures (>=6 per kickoff)
# --------------------------------------------------------------------------- #

def test_01_clean_validator(tmp_path):
    """5 fabrications, findings cite 0 of them → validator_grounded/0/0/rc=0."""
    fabs = tmp_path / "fab.json"
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    _seed_fabs(fabs, 5)
    _clean_findings(findings)
    env = _isolated_env(tmp_path)
    bd, brc = _bash_check(str(findings), str(fabs), str(report), env)
    pd, prc = _py_check(str(findings), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "01_clean")
    assert brc == 0
    assert bd["verdict"] == "validator_grounded"
    assert bd["reason"] == "ok"
    assert bd["fp_count"] == 0
    assert bd["fp_total"] == 5
    assert bd["fp_rate"] == 0.0


def test_02_hallucinating_3_of_5(tmp_path):
    """Findings cite first 3 of 5 → REFUTE_FAILED / fp_rate=0.6 / rc=1."""
    fabs = tmp_path / "fab.json"
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    recs = _seed_fabs(fabs, 5)
    _hallucinating_findings(findings, recs[:3])
    env = _isolated_env(tmp_path)
    bd, brc = _bash_check(str(findings), str(fabs), str(report), env)
    pd, prc = _py_check(str(findings), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "02_hallucinating")
    assert brc == 1
    assert bd["verdict"] == "REFUTE_FAILED"
    assert bd["reason"] == "high_fp_survival"
    assert bd["fp_count"] == 3
    assert bd["fp_total"] == 5
    assert abs(bd["fp_rate"] - 0.6) < 1e-6


def test_03_missing_findings(tmp_path):
    """Missing findings file → indeterminate/missing_inputs/no report_path/rc=0."""
    fabs = tmp_path / "fab.json"
    report = tmp_path / "report"
    report.mkdir()
    _seed_fabs(fabs, 5)
    missing = tmp_path / "does-not-exist.md"
    env = _isolated_env(tmp_path)
    bd, brc = _bash_check(str(missing), str(fabs), str(report), env)
    pd, prc = _py_check(str(missing), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "03_missing_findings")
    assert brc == 0
    assert bd["verdict"] == "indeterminate"
    assert bd["reason"] == "missing_inputs"
    assert "report_path" not in bd
    assert "report_path" not in pd
    # Rationale must match the bash printf string verbatim.
    assert bd["rationale"] == (
        "findings_path or fabrications_json missing; cannot measure"
    )


def test_04_missing_fabrications(tmp_path):
    """Missing fabrications file → same shape as (3)."""
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    _clean_findings(findings)
    missing = tmp_path / "no-fabs.json"
    env = _isolated_env(tmp_path)
    bd, brc = _bash_check(str(findings), str(missing), str(report), env)
    pd, prc = _py_check(str(findings), str(missing), str(report), env)
    _assert_parity(bd, pd, brc, prc, "04_missing_fabs")
    assert brc == 0
    assert bd["verdict"] == "indeterminate"
    assert bd["reason"] == "missing_inputs"
    assert "report_path" not in bd
    assert "report_path" not in pd


def test_05_empty_fabrications(tmp_path):
    """Empty fabrications array → indeterminate/missing_inputs WITH report_path/rc=0."""
    fabs = tmp_path / "fab.json"
    fabs.write_text("[]")
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    _clean_findings(findings)
    env = _isolated_env(tmp_path)
    bd, brc = _bash_check(str(findings), str(fabs), str(report), env)
    pd, prc = _py_check(str(findings), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "05_empty_fabs")
    assert brc == 0
    assert bd["verdict"] == "indeterminate"
    assert bd["reason"] == "missing_inputs"
    assert "report_path" in bd
    assert bd["rationale"] == "fabrications_json must be a non-empty array"


def test_06_boundary_one_of_ten_at_default_ceiling(tmp_path):
    """1 of 10 cited at default ceiling 0.1 — exactly at ceiling, must NOT
    trigger (rate > ceiling is strict)."""
    fabs = tmp_path / "fab.json"
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    recs = _seed_fabs(fabs, 10)
    _hallucinating_findings(findings, recs[:1])
    env = _isolated_env(tmp_path)
    # Default ceiling 0.1 — leave env untouched so MO_REFUTE_FP_CEILING
    # is unset on both engines.
    env.pop("MO_REFUTE_FP_CEILING", None)
    bd, brc = _bash_check(str(findings), str(fabs), str(report), env)
    pd, prc = _py_check(str(findings), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "06_boundary")
    assert brc == 0
    assert bd["verdict"] == "validator_grounded"
    assert bd["fp_count"] == 1
    assert bd["fp_total"] == 10
    assert abs(bd["fp_rate"] - 0.1) < 1e-6
    assert abs(bd["fp_ceiling"] - 0.1) < 1e-6


def test_07_custom_ceiling_override(tmp_path):
    """2 of 5 cited = 0.4 < 0.5 ceiling → validator_grounded."""
    fabs = tmp_path / "fab.json"
    findings = tmp_path / "findings.md"
    report = tmp_path / "report"
    report.mkdir()
    recs = _seed_fabs(fabs, 5)
    _hallucinating_findings(findings, recs[:2])
    env = _isolated_env(tmp_path)
    env["MO_REFUTE_FP_CEILING"] = "0.5"
    bd, brc = _bash_check(str(findings), str(fabs), str(report), env)
    pd, prc = _py_check(str(findings), str(fabs), str(report), env)
    _assert_parity(bd, pd, brc, prc, "07_custom_ceiling")
    assert brc == 0
    assert bd["verdict"] == "validator_grounded"
    assert bd["fp_count"] == 2
    assert bd["fp_total"] == 5
    assert abs(bd["fp_rate"] - 0.4) < 1e-6
    assert abs(bd["fp_ceiling"] - 0.5) < 1e-6


# --------------------------------------------------------------------------- #
# Generation parity — schema/formula/template, NOT exact ids (random)
# --------------------------------------------------------------------------- #

def test_08_generation_parity(tmp_path):
    """Both engines generate the same schema, line formula, claim template
    rotation, path template, and id format. Exact id strings differ
    because ``secrets.token_hex(4)[:7]`` is per-process."""
    py_fabs = tmp_path / "py_fabs.json"
    bash_fabs = tmp_path / "bash_fabs.json"

    rp.generate_fabrications(5, str(py_fabs))

    env = os.environ.copy()
    env["MINI_ORK_ROOT"] = str(tmp_path / "no_lib")
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_SH}" && mo_generate_fabrications 5 "$1"',
         "_", str(bash_fabs)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"bash generation failed rc={proc.returncode} stderr={proc.stderr}"
    )

    py_records = json.loads(py_fabs.read_text())
    bash_records = json.loads(bash_fabs.read_text())

    assert len(py_records) == len(bash_records) == 5

    # Schema
    expected_keys = {"id", "path", "line", "claim"}
    assert set(py_records[0].keys()) == expected_keys
    assert set(bash_records[0].keys()) == expected_keys

    # Claim template rotation — strip per-record id, compare templates
    for i in range(5):
        py_t = py_records[i]["claim"].replace(py_records[i]["id"], "{id}")
        bash_t = bash_records[i]["claim"].replace(bash_records[i]["id"], "{id}")
        assert py_t == bash_t, (
            f"claim template drift at i={i}: py={py_t!r} bash={bash_t!r}"
        )

    # Line formula — 30 + (i * 11) % 200
    for i in range(5):
        expected = 30 + (i * 11) % 200
        assert py_records[i]["line"] == expected, (
            f"py line drift at i={i}: got {py_records[i]['line']}"
        )
        assert bash_records[i]["line"] == expected, (
            f"bash line drift at i={i}: got {bash_records[i]['line']}"
        )

    # Path template — src/<id>/handler.ts
    for i in range(5):
        assert py_records[i]["path"] == f"src/{py_records[i]['id']}/handler.ts"
        assert bash_records[i]["path"] == f"src/{bash_records[i]['id']}/handler.ts"

    # Id format — prefix + 7 hex chars
    for rec in py_records + bash_records:
        assert rec["id"].startswith("__fabricated_")
        suffix = rec["id"][len("__fabricated_"):]
        assert len(suffix) == 7, f"id suffix wrong length: {rec['id']!r}"
        int(suffix, 16)  # valid hex


# --------------------------------------------------------------------------- #
# Strangler-fig + import smoke
# --------------------------------------------------------------------------- #

def test_bash_untouched():
    """Strangler-fig: bash source must remain byte-identical."""
    proc = subprocess.run(
        ["git", "diff", "--stat", "lib/refute_or_promote_gate.sh"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "", (
        f"bash was modified:\n{proc.stdout}\n{proc.stderr}"
    )


def test_import_and_validation():
    """Smoke: pure import + arg-validation without subprocess."""
    # count<1 raises ValueError
    try:
        rp.generate_fabrications(0, str("/tmp/foo.json"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for count=0")

    # negative count raises ValueError
    try:
        rp.generate_fabrications(-3, str("/tmp/foo.json"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for negative count")

    # empty out_path raises ValueError
    try:
        rp.generate_fabrications(3, "")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty out_path")

    # check_fabrication_survival smoke on missing inputs
    d, rc = rp.check_fabrication_survival("/no/such/file.md", "/no/such/fabs.json")
    assert rc == 0
    assert d["verdict"] == "indeterminate"
    assert d["reason"] == "missing_inputs"
    assert "report_path" not in d
    assert set(d.keys()) == {
        "verdict", "reason", "fp_count", "fp_total",
        "fp_rate", "fp_ceiling", "rationale",
    }