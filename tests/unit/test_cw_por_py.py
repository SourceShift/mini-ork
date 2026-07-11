"""Parity gate: ``mini_ork.ported.cw_por.compute_cw_por`` vs
``lib/cw_por.sh:mo_compute_cw_por``.

Every case invokes the LIVE bash function in a fresh subprocess — the bash
output is the control; the test never hardcodes an expected JSON. The
Python port must match the bash output byte-for-byte (floats within 1e-6,
rationale strings identical, structural dict equality).

Six parity cases:

  (a) clean panel — 4 voters, correct votes dominate with higher
      confidence → ``verdict=panel_healthy``, ``cw_por < threshold``
  (b) captured panel — wrong voters dominate adoption with higher
      confidence → ``verdict=authority_capture_suspected``,
      ``cw_por > threshold``
  (c) indeterminate — all voters have ``ground_truth_match=None`` →
      ``cw_por`` is ``None``, ``verdict=indeterminate``,
      ``n_with_ground_truth=0``
  (d) ``MO_CW_POR_THRESHOLD`` env override — set env to 0.05 in both
      subprocess AND Python (``os.environ`` patching via monkeypatch),
      both must honor it
  (e) malformed JSON (missing ``.voters[]``) — bash returns rc=2 + stderr
      JSON error, Python raises ``ValueError``; both sides must agree on
      this failure mode (subprocess rc vs raised exception)
  (f) missing verdict file — bash returns rc=2 + stderr error, Python
      raises ``FileNotFoundError``

Strangler-fig co-existence: ``lib/cw_por.sh`` is byte-identical before
and after this test exists. The test only WRITES to ``tmp_path`` fixture
files and READS from ``lib/cw_por.sh``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import cw_por as cwp  # noqa: E402

SH = REPO / "lib" / "cw_por.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _bash(env: dict[str, str], body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh bash that has sourced lib/cw_por.sh.

    The bash function ``mo_compute_cw_por`` is the production API; the
    heredoc-python3 inside it is internal. We invoke the OUTER function
    so the control matches what callers actually use.
    """
    wrapper = f'. "{SH}"\n{body}\n'
    full_env = {**os.environ, **env}
    return subprocess.run(
        ["bash", "-c", wrapper], env=full_env, capture_output=True, text=True,
    )


def _bash_compute(verdict_file: str,
                  extra_env: dict[str, str] | None = None
                  ) -> subprocess.CompletedProcess:
    """Invoke ``mo_compute_cw_por <verdict_file>`` in a fresh bash."""
    env = dict(extra_env) if extra_env else {}
    # Strip MO_CW_POR_THRESHOLD unless the caller explicitly set it, so
    # tests don't inherit ambient thresholds from the developer shell.
    env.setdefault("MO_CW_POR_THRESHOLD", "0.3")
    return _bash(env, f'mo_compute_cw_por "{verdict_file}"')


def _compare_payloads(py: dict, bash: dict, *,
                      float_tol: float = 1e-6) -> None:
    """Structural equality gate. Compares dict shapes, string fields
    verbatim, and floats within ``float_tol``. The bash output goes
    through json.loads first to normalize (since bash also prints JSON)."""
    assert set(py.keys()) == set(bash.keys()), (
        f"key mismatch: py={sorted(py.keys())} bash={sorted(bash.keys())}"
    )
    for k, v in py.items():
        bv = bash[k]
        if isinstance(v, float) or isinstance(bv, float):
            assert abs(float(v) - float(bv)) <= float_tol, (
                f"float mismatch at {k!r}: py={v!r} bash={bv!r}"
            )
        elif v is None or bv is None:
            assert v is None and bv is None, (
                f"None mismatch at {k!r}: py={v!r} bash={bv!r}"
            )
        else:
            assert v == bv, f"field {k!r}: py={v!r} bash={bv!r}"


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

    r_bash = _bash_compute(str(panel))
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode} stderr={r_bash.stderr!r}"
    bash_payload = json.loads(r_bash.stdout)

    _compare_payloads(py_payload, bash_payload)
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

    r_bash = _bash_compute(str(panel))
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode} stderr={r_bash.stderr!r}"
    bash_payload = json.loads(r_bash.stdout)

    _compare_payloads(py_payload, bash_payload)
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

    r_bash = _bash_compute(str(panel))
    assert r_bash.returncode == 0, (
        f"bash rc={r_bash.returncode} stderr={r_bash.stderr!r}"
    )
    bash_payload = json.loads(r_bash.stdout)

    # Indeterminate branch has a DIFFERENT key set from the normal branch —
    # compare shape, not the union.
    assert set(py_payload.keys()) == set(bash_payload.keys())
    assert py_payload["cw_por"] is None
    assert bash_payload["cw_por"] is None
    assert py_payload["verdict"] == "indeterminate"
    assert bash_payload["verdict"] == "indeterminate"
    assert py_payload["n_with_ground_truth"] == 0
    assert py_payload["n_voters"] == 3
    # Rationale string is verbatim equal (no float involvement here).
    assert py_payload["rationale"] == bash_payload["rationale"]
    # Normal-branch keys must NOT leak into indeterminate output.
    for k in ("n_correct", "n_wrong", "n_pairs_evaluated", "adopted_vote"):
        assert k not in py_payload
        assert k not in bash_payload


# ─────────────────────────────────────────────────────────────────────────────
# (d) MO_CW_POR_THRESHOLD env override
# ─────────────────────────────────────────────────────────────────────────────
def test_threshold_env_override_honored(tmp_path):
    # Build a panel whose bare-cw_por sits between 0.05 and 0.3 — a
    # clean-panel layout at confidence 0.85 / 0.80 / 0.75 / 0.30 yields
    # cw_por = 0 (no overrides; adopted=approve; correct_vote=approve),
    # which won't trip the threshold either way. We need a panel that
    # ACTUALLY produces a non-zero CW-POR to make the override observable.
    #
    # Use a captured-panel layout (wrong voters more confident, adopted
    # vote == wrong vote) which yields cw_por > 0. With default 0.3 the
    # verdict is "authority_capture_suspected". With 0.95 the verdict
    # flips to "panel_healthy". The point of this test is that the env
    # knob reaches BOTH implementations.
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
    # cw_por here: 1 pair (glm, kimi) delta=0.15, 1 pair (glm, codex)
    # delta=0.10, adopted=reject, correct_vote=approve (correct[0].vote).
    # Both deltas>0 and adopted==w.vote and adopted!=correct_vote → both
    # count as overrides. overrides=0.25, pairs=2, cw_por=0.125. So
    # threshold 0.05 → capture; threshold 0.95 → healthy.

    # Sub-case 1: threshold=0.05 → authority_capture_suspected
    py_rc, py_payload = cwp.compute_cw_por(
        str(panel), threshold=0.05,
    )
    assert py_rc == 0
    r_bash = _bash_compute(str(panel), extra_env={"MO_CW_POR_THRESHOLD": "0.05"})
    assert r_bash.returncode == 0, f"bash rc={r_bash.returncode} stderr={r_bash.stderr!r}"
    bash_payload = json.loads(r_bash.stdout)
    _compare_payloads(py_payload, bash_payload)
    assert py_payload["verdict"] == "authority_capture_suspected"
    assert py_payload["threshold"] == 0.05

    # Sub-case 2: threshold=0.95 → panel_healthy (cw_por=0.125 < 0.95)
    py_rc2, py_payload2 = cwp.compute_cw_por(
        str(panel), threshold=0.95,
    )
    assert py_rc2 == 0
    r_bash2 = _bash_compute(str(panel), extra_env={"MO_CW_POR_THRESHOLD": "0.95"})
    assert r_bash2.returncode == 0
    bash_payload2 = json.loads(r_bash2.stdout)
    _compare_payloads(py_payload2, bash_payload2)
    assert py_payload2["verdict"] == "panel_healthy"
    assert py_payload2["threshold"] == 0.95

    # Sub-case 3: bash env override reaches bash via $MO_CW_POR_THRESHOLD
    # even when Python is called WITHOUT an explicit threshold argument —
    # both sides resolve the same env var and produce the same verdict.
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MO_CW_POR_THRESHOLD", "0.05")
    try:
        py_rc3, py_payload3 = cwp.compute_cw_por(str(panel))
    finally:
        monkey.undo()
    assert py_rc3 == 0
    r_bash3 = _bash_compute(str(panel), extra_env={"MO_CW_POR_THRESHOLD": "0.05"})
    assert r_bash3.returncode == 0
    bash_payload3 = json.loads(r_bash3.stdout)
    _compare_payloads(py_payload3, bash_payload3)
    assert py_payload3["verdict"] == "authority_capture_suspected"
    assert py_payload3["threshold"] == 0.05


# ─────────────────────────────────────────────────────────────────────────────
# (e) malformed — missing .voters[]
# ─────────────────────────────────────────────────────────────────────────────
def test_malformed_missing_voters_raises_value_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"verdict": "approve"}')

    # Python: ValueError
    with pytest.raises(ValueError):
        cwp.compute_cw_por(str(bad))

    # Bash: rc=2 + stderr JSON error matching the canonical message.
    r_bash = _bash_compute(str(bad))
    assert r_bash.returncode == 2
    try:
        bash_err = json.loads(r_bash.stderr.strip())
    except json.JSONDecodeError:
        pytest.fail(f"bash stderr not JSON: {r_bash.stderr!r}")
    assert "error" in bash_err
    assert "voters" in bash_err["error"].lower()

    # Also: an empty voters array must trigger the same failure mode.
    empty = tmp_path / "empty.json"
    empty.write_text('{"voters": []}')

    with pytest.raises(ValueError):
        cwp.compute_cw_por(str(empty))

    r_bash_empty = _bash_compute(str(empty))
    assert r_bash_empty.returncode == 2
    try:
        bash_empty_err = json.loads(r_bash_empty.stderr.strip())
    except json.JSONDecodeError:
        pytest.fail(f"bash stderr not JSON: {r_bash_empty.stderr!r}")
    assert "error" in bash_empty_err


# ─────────────────────────────────────────────────────────────────────────────
# (f) missing verdict file
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_file_raises_file_not_found(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert not missing.exists()

    # Python: FileNotFoundError
    with pytest.raises(FileNotFoundError):
        cwp.compute_cw_por(str(missing))

    # Bash: rc=2 + stderr JSON error mentioning "not found".
    r_bash = _bash_compute(str(missing))
    assert r_bash.returncode == 2
    try:
        bash_err = json.loads(r_bash.stderr.strip())
    except json.JSONDecodeError:
        pytest.fail(f"bash stderr not JSON: {r_bash.stderr!r}")
    assert "error" in bash_err
    assert "not found" in bash_err["error"].lower()