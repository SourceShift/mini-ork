"""Contract tests for the coverage-aware active-eval selector (H9).

Hermetic: no DB, no lane, no network, no real run. The editable install
resolves ``mini_ork`` to main, so insert the repo root on ``sys.path`` before
importing.
"""
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.learning import active_eval as ae


def test_proxy_rate_none_over_empty():
    assert ae.proxy_rate([]) is None
    assert ae.proxy_rate([{"id": "a"}, {"id": "b"}]) is None
    assert ae.proxy_rate([{"id": "a", "proxy_failed": None}]) is None
    assert ae.proxy_rate([
        {"id": "a", "proxy_failed": True},
        {"id": "b", "proxy_failed": False},
    ]) == 0.5
    assert ae.proxy_rate([
        {"id": "a", "proxy_failed": True},
        {"id": "b"},
    ]) == 1.0


def test_target_rate_none_when_unmeasured():
    assert ae.target_rate([]) is None
    assert ae.target_rate([{"id": "a", "proxy_failed": True}]) is None
    assert ae.target_rate([
        {"id": "a", "target_failed": True},
        {"id": "b", "target_failed": False},
    ]) == 0.5


def test_paired_exact():
    rows = [
        {"id": "a", "proxy_failed": True, "target_failed": False},
        {"id": "b", "proxy_failed": False},
        {"id": "c", "target_failed": True},
        {"id": "d", "proxy_failed": None, "target_failed": True},
        {"id": "e", "proxy_failed": True, "target_failed": None},
        {"id": "f", "proxy_failed": True, "target_failed": True},
    ]
    got = ae.paired(rows)
    assert [r["id"] for r in got] == ["a", "f"]


def test_transfer_residual_needs_min_paired():
    rows = [
        {"id": "a", "proxy_failed": True, "target_failed": False},
        {"id": "b", "proxy_failed": False, "target_failed": False},
    ]
    assert len(ae.paired(rows)) < ae.MIN_PAIRED
    assert ae.transfer_residual(rows) is None


def test_transfer_residual_mean():
    rows = [
        {"id": "a", "proxy_failed": True, "target_failed": False},   # -1.0
        {"id": "b", "proxy_failed": False, "target_failed": False},  # 0.0
        {"id": "c", "proxy_failed": True, "target_failed": False},   # -1.0
        {"id": "d", "proxy_failed": False, "target_failed": False},  # 0.0
    ]
    assert ae.transfer_residual(rows) == pytest.approx(-0.5)


def test_transfer_residual_positive_when_proxy_underreports():
    rows = [
        {"id": "a", "proxy_failed": False, "target_failed": True},
        {"id": "b", "proxy_failed": False, "target_failed": True},
        {"id": "c", "proxy_failed": False, "target_failed": True},
        {"id": "d", "proxy_failed": False, "target_failed": True},
    ]
    assert ae.transfer_residual(rows) == pytest.approx(1.0)
    assert ae.transfer_residual(rows) > 0.0


def test_predicted_risk_none_and_clamp():
    assert ae.predicted_risk({"id": "a", "proxy_failed": True}, None) is None
    assert ae.predicted_risk({"id": "a"}, 0.0) is None
    assert ae.predicted_risk({"id": "a", "proxy_failed": True}, 0.5) == pytest.approx(1.0)
    assert ae.predicted_risk({"id": "a", "proxy_failed": True}, 2.0) == pytest.approx(1.0)
    assert ae.predicted_risk({"id": "a", "proxy_failed": False}, -1.0) == pytest.approx(0.0)


def test_coverage_gain_none_and_floor():
    assert ae.coverage_gain({"id": "a", "support": 0.5}, []) is None
    assert ae.coverage_gain({"id": "a", "modes": ["x"]}, []) is None
    assert ae.coverage_gain(
        {"id": "a", "modes": ["x"], "support": ae.SUPPORT_FLOOR}, []
    ) == 0.0
    assert ae.coverage_gain(
        {"id": "a", "modes": ["x"], "support": 0.05}, []
    ) == 0.0
    assert ae.coverage_gain(
        {"id": "a", "modes": ["x", "y"], "support": 0.5}, []
    ) == 2.0


def test_coverage_gain_absent_from_every_selected():
    selected = [
        {"id": "s1", "modes": ["x"]},
        {"id": "s2", "modes": ["y"]},
    ]
    cand = {"id": "c", "modes": ["x", "y", "z"], "support": 0.5}
    assert ae.coverage_gain(cand, selected) == 1.0
    assert ae.coverage_gain(cand, [selected[0]]) == 2.0


def test_acquisition_none_or_measured_zero():
    assert ae.acquisition(
        {"id": "a", "proxy_failed": True, "modes": ["x"], "support": 0.5}, [], None
    ) is None
    assert ae.acquisition(
        {"id": "a", "proxy_failed": True, "support": 0.5}, [], 0.0
    ) is None
    assert ae.acquisition(
        {"id": "a", "proxy_failed": True, "modes": [], "support": 0.5}, [], 0.0
    ) == 0.0
    assert ae.acquisition(
        {"id": "a", "proxy_failed": True, "modes": ["x"], "support": 0.5}, [], 0.0
    ) == pytest.approx(1.0)


def test_sign_test_p():
    assert ae._sign_test_p(5, 0) == pytest.approx(2 * 0.5 ** 5)
    assert ae._sign_test_p(0, 0) is None
    assert ae._sign_test_p(2, 2) == pytest.approx(1.0)


def test_residual_p():
    rows = [
        {"id": "a", "proxy_failed": False, "target_failed": True},
        {"id": "b", "proxy_failed": False, "target_failed": True},
        {"id": "c", "proxy_failed": False, "target_failed": True},
        {"id": "d", "proxy_failed": False, "target_failed": True},
    ]
    assert ae.residual_p(rows) == pytest.approx(2 * 0.5 ** 4)
    assert ae.residual_p([{"id": "a"}]) is None


def test_rank_refuses_without_paired_evidence():
    rows = [
        {"id": "a", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m1"]},
        {"id": "b", "proxy_failed": True, "support": 0.5, "modes": ["m2"]},
        {"id": "c", "proxy_failed": False, "support": 0.5, "modes": ["m3"]},
        {"id": "d", "proxy_failed": True, "support": 0.5, "modes": ["m4"]},
        {"id": "e", "proxy_failed": False, "support": 0.5, "modes": ["m5"]},
    ]
    assert len(ae.paired(rows)) < ae.MIN_PAIRED
    r = ae.rank(rows, budget=3)
    assert r["selected"] == []
    assert "insufficient_paired" in r["undecided"]


def test_rank_prefers_unseen_modes():
    evidence = [
        {"id": f"e{i}", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m0"]}
        for i in range(ae.MIN_PAIRED)
    ]
    rows = evidence + [
        {"id": "a1", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m_a"]},
        {"id": "a2", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m_a"]},
        {"id": "b1", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m_b"]},
    ]
    r = ae.rank(rows, budget=3)
    assert r["selected"][0] == "a1"
    # a2 shares a mode with a1; b1 does not. Greedy must prefer the unseen mode.
    assert r["selected"][1] == "b1"
    by_id = {row["id"]: row for row in rows}
    modes = {m for cid in r["selected"] for m in by_id[cid]["modes"]}
    assert modes == {"m_a", "m_b", "m0"}


def test_rank_unmeasured_exact():
    rows = [
        {"id": "e0", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m0"]},
        {"id": "e1", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m0"]},
        {"id": "e2", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m0"]},
        {"id": "e3", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": ["m0"]},
        {"id": "no_proxy", "target_failed": True, "support": 0.5, "modes": ["m1"]},
        {"id": "no_modes", "proxy_failed": True, "target_failed": True, "support": 0.5},
        {"id": "no_support", "proxy_failed": True, "target_failed": True, "modes": ["m2"]},
    ]
    r = ae.rank(rows, budget=5)
    assert r["unmeasured"] == ["no_proxy", "no_modes", "no_support"]
    assert set(r["selected"]).isdisjoint(r["unmeasured"])


def test_report_undecided_only_when_conclusive():
    proxy_only = [
        {"id": f"p{i}", "proxy_failed": bool(i % 2), "support": 0.5, "modes": ["m"]}
        for i in range(5)
    ]
    rep = ae.report(proxy_only, budget=1)
    assert rep["transfer_residual"] is None
    assert "no_transfer_measured" in rep["undecided"]
    assert rep["undecided"] != []

    paired_rows = [
        {"id": f"e{i}", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": [f"m{i}"]}
        for i in range(ae.MIN_PAIRED)
    ]
    rep2 = ae.report(paired_rows, budget=1)
    assert rep2["transfer_residual"] is not None
    assert rep2["undecided"] == []
    assert rep2["rank"]["selected"] != []


def test_cli_main(tmp_path):
    from mini_ork.cli import active_eval as cli

    fixture = tmp_path / "history.json"
    fixture.write_text(json.dumps([
        {"id": f"e{i}", "proxy_failed": True, "target_failed": True,
         "support": 0.5, "modes": [f"m{i}"]}
        for i in range(ae.MIN_PAIRED)
    ]))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main([str(fixture), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "rank" in payload

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = cli.main([str(tmp_path / "nope.json"), "--json"])
    assert rc2 == 0
    assert buf2.getvalue() == ""
