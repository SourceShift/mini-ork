"""CW-POR must fail CLOSED: an unavailable check cannot license a promote.

``_cw_por_compute`` used to answer ``'default_passed'`` when the checker could
not be imported and ``'indeterminate_default_passed'`` when it raised, and
``mo_promote_synthesis_gate`` blocked only on ``'failed'``. All three statuses
therefore fell through to the structural condition and permitted the promote —
so deleting the checker, breaking its import, or feeding it a panel with no
ground truth silently *approved* authority capture instead of catching it.

Only an affirmative ``passed`` clears the condition now. These tests pin each
non-``passed`` path and the pre-existing fail-open names, so the escape hatch
cannot come back by rename.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import promotion_gate as pg  # noqa: E402

# The statuses that used to mean "the check did not run, carry on".
_FAIL_OPEN_STATUSES = {"default_passed", "indeterminate_default_passed"}

_HEALTHY_VOTERS = [
    {"voter_id": "c1", "vote": "approve", "confidence": 0.90,
     "ground_truth_match": True},
    {"voter_id": "c2", "vote": "approve", "confidence": 0.85,
     "ground_truth_match": True},
    {"voter_id": "w1", "vote": "reject", "confidence": 0.60,
     "ground_truth_match": False},
]

# Every voter's ground_truth_match is None: the panel voted, but nothing in the
# input says who was right, so CW-POR has no signal to compute from.
_NO_GROUND_TRUTH_VOTERS = [
    {"voter_id": "v1", "vote": "approve", "confidence": 0.90},
    {"voter_id": "v2", "vote": "reject", "confidence": 0.80},
]


def _write_verdict(tmp_path: Path, name: str, payload: dict) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _gate_ready_payload(tmp_path: Path, voters: list[dict]) -> str:
    """A verdict file that clears every condition EXCEPT authority capture.

    panel_score well above threshold and all three structural signals present,
    so the CW-POR condition is the only thing that can decide the outcome.
    """
    return _write_verdict(tmp_path, "verdict.json", {
        "panel_score": 92.0,
        "voters": voters,
        "structural": {"citation_density_per_lens": 5.2,
                       "file_coverage_delta": 3, "finding_cardinality": 11},
    })


def _block_cw_por_import(monkeypatch) -> None:
    """Make ``from mini_ork.gates import cw_por`` raise, whatever ran before.

    Blanking the ``sys.modules`` slot is not enough on its own: once the real
    submodule has been imported (any earlier test that imports it — including
    ``test_compute_error_does_not_pass`` below), the import machinery leaves it
    cached as an *attribute* of the parent package, and ``from X import Y``
    hits that attribute before ever consulting ``sys.modules``. So an
    ``None``-in-``sys.modules`` monkeypatch passes when this test runs first
    and silently fails to block when it runs third. Dropping the attribute too
    is what makes the test order-independent.
    """
    import mini_ork.gates

    monkeypatch.delattr(mini_ork.gates, "cw_por", raising=False)
    monkeypatch.setitem(sys.modules, "mini_ork.gates.cw_por", None)


# ── The three fail-closed statuses ───────────────────────────────────────────


def test_import_failure_does_not_pass(tmp_path, monkeypatch):
    """A missing CW-POR implementation must not read as a passed check."""
    _block_cw_por_import(monkeypatch)
    vf = _write_verdict(tmp_path, "v.json", {"voters": _HEALTHY_VOTERS})

    value, status = pg._cw_por_compute(vf)

    assert status == "unavailable"
    assert status not in _FAIL_OPEN_STATUSES
    assert value == "null"


def test_compute_error_does_not_pass(tmp_path, monkeypatch):
    """The checker raising must not read as a passed check."""
    from mini_ork.gates import cw_por

    def _boom(*_a, **_k):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(cw_por, "compute_cw_por", _boom)
    vf = _write_verdict(tmp_path, "v.json", {"voters": _HEALTHY_VOTERS})

    value, status = pg._cw_por_compute(vf)

    assert status == "error"
    assert status not in _FAIL_OPEN_STATUSES
    assert value == "null"


def test_indeterminate_verdict_does_not_pass(tmp_path):
    """A panel with no ground truth is unmeasured capture, not absent capture."""
    vf = _write_verdict(tmp_path, "v.json", {"voters": _NO_GROUND_TRUTH_VOTERS})

    value, status = pg._cw_por_compute(vf)

    assert status == "indeterminate"
    assert status not in _FAIL_OPEN_STATUSES
    assert value == "null"


def test_healthy_panel_passes(tmp_path):
    """The one status that clears the condition is still reachable."""
    vf = _write_verdict(tmp_path, "v.json", {"voters": _HEALTHY_VOTERS})

    _value, status = pg._cw_por_compute(vf)

    assert status == "passed"


def test_authority_capture_still_fails(tmp_path):
    """The check's original purpose is intact: capture is caught as 'failed'."""
    vf = _write_verdict(tmp_path, "v.json", {"voters": [
        {"voter_id": "w1", "vote": "reject", "confidence": 0.95,
         "ground_truth_match": False},
        {"voter_id": "w2", "vote": "reject", "confidence": 0.90,
         "ground_truth_match": False},
        {"voter_id": "c1", "vote": "approve", "confidence": 0.60,
         "ground_truth_match": True},
    ]})

    _value, status = pg._cw_por_compute(vf)

    assert status == "failed"


# ── The gate itself: an unrun check must reject, not fall through ────────────


def test_gate_rejects_when_checker_unavailable(tmp_path, monkeypatch):
    _block_cw_por_import(monkeypatch)
    vf = _gate_ready_payload(tmp_path, _HEALTHY_VOTERS)

    decision, rc = pg.mo_promote_synthesis_gate(vf, "research_synthesis")

    assert rc == 1
    assert decision["decision"] == "rejected"
    assert decision["reason"] == "cw_por_unverified"
    assert decision["signals"]["cw_por_status"] == "unavailable"


def test_gate_rejects_when_compute_raises(tmp_path, monkeypatch):
    from mini_ork.gates import cw_por

    def _boom(*_a, **_k):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(cw_por, "compute_cw_por", _boom)
    vf = _gate_ready_payload(tmp_path, _HEALTHY_VOTERS)

    decision, rc = pg.mo_promote_synthesis_gate(vf, "research_synthesis")

    assert rc == 1
    assert decision["decision"] == "rejected"
    assert decision["reason"] == "cw_por_unverified"
    assert decision["signals"]["cw_por_status"] == "error"


def test_gate_rejects_when_no_ground_truth(tmp_path):
    """Weakest case: everything else is met, only the capture check is blind."""
    vf = _gate_ready_payload(tmp_path, _NO_GROUND_TRUTH_VOTERS)

    decision, rc = pg.mo_promote_synthesis_gate(vf, "research_synthesis")

    assert rc == 1
    assert decision["decision"] == "rejected"
    assert decision["reason"] == "cw_por_unverified"
    assert decision["signals"]["cw_por_status"] == "indeterminate"


def test_gate_still_passes_a_healthy_panel(tmp_path):
    """Fail-closed must not mean fail-always: the honest path still approves."""
    vf = _gate_ready_payload(tmp_path, _HEALTHY_VOTERS)

    decision, rc = pg.mo_promote_synthesis_gate(vf, "research_synthesis")

    assert rc == 0
    assert decision["reason"] == "all_conditions_met"


def test_gate_still_rejects_authority_capture(tmp_path):
    """The blocking path that already existed keeps its own reason code."""
    vf = _gate_ready_payload(tmp_path, [
        {"voter_id": "w1", "vote": "reject", "confidence": 0.95,
         "ground_truth_match": False},
        {"voter_id": "w2", "vote": "reject", "confidence": 0.90,
         "ground_truth_match": False},
        {"voter_id": "c1", "vote": "approve", "confidence": 0.60,
         "ground_truth_match": True},
    ])

    decision, rc = pg.mo_promote_synthesis_gate(vf, "research_synthesis")

    assert rc == 1
    assert decision["reason"] == "authority_capture"


@pytest.mark.parametrize("status", sorted(_FAIL_OPEN_STATUSES))
def test_fail_open_names_are_gone(status):
    """The old names are not reachable from any path of _cw_por_compute."""
    import ast
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(pg._cw_por_compute))).body[0]
    assert isinstance(fn, ast.FunctionDef)
    # The docstring names the removed statuses on purpose — it records what the
    # function used to return and why that was wrong. Only literals in the
    # *body* can be returned, so the docstring is excluded rather than the
    # docstring being scrubbed of the history it exists to preserve.
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body.pop(0)

    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert status not in literals, f"{status} is still produced by _cw_por_compute"
