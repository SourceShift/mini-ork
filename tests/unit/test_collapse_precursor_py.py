"""Contract tests for the silent-collapse precursor detector (H4).

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

from mini_ork.learning import collapse_precursor as cp


def test_vocabulary_is_contract():
    assert cp.PRECURSORS == ("anchor_entropy_contraction", "tail_coverage_erosion")
    assert cp.UNMAPPED == ("representation_drift_freezing",)


def test_anchor_entropy():
    assert cp.anchor_entropy({"anchor_pass": 1, "anchor_fail": 1}) == 1.0
    assert cp.anchor_entropy({"anchor_pass": 5, "anchor_fail": 0}) == 0.0
    assert cp.anchor_entropy({"anchor_pass": 0, "anchor_fail": 0}) is None
    assert cp.anchor_entropy({"anchor_pass": 3}) is None  # missing anchor_fail


def test_tail_coverage():
    assert cp.tail_coverage({"covered_tail": 0, "covered_total": 0}) is None
    assert cp.tail_coverage({"covered_tail": 3, "covered_total": 10}) == 0.3


def test_precursor_short_and_flat():
    short = [
        {"gen": i, "anchor_pass": 1, "anchor_fail": 1}
        for i in range(cp.MIN_GENERATIONS - 1)
    ]
    r = cp.precursor(short, "anchor_entropy_contraction")
    assert r["delta"] is None
    assert r["contracting"] is False

    flat = [
        {"gen": i, "anchor_pass": 1, "anchor_fail": 1}
        for i in range(cp.MIN_GENERATIONS)
    ]
    rf = cp.precursor(flat, "anchor_entropy_contraction")
    assert rf["contracting"] is False


def test_precursor_contracting():
    history = [
        {"gen": 0, "anchor_pass": 1, "anchor_fail": 1},  # entropy 1.0
        {"gen": 1, "anchor_pass": 1, "anchor_fail": 1},  # entropy 1.0
        {"gen": 2, "anchor_pass": 4, "anchor_fail": 0},  # entropy 0.0
        {"gen": 3, "anchor_pass": 5, "anchor_fail": 0},  # entropy 0.0
    ]
    r = cp.precursor(history, "anchor_entropy_contraction")
    assert r["first"] == 1.0
    assert r["second"] == 0.0
    assert r["delta"] == r["second"] - r["first"]
    assert r["contracting"] is True


def test_sign_test_p():
    assert cp._sign_test_p(5, 0) == pytest.approx(2 * 0.5 ** 5)
    assert cp._sign_test_p(0, 0) is None
    assert cp._sign_test_p(2, 2) == pytest.approx(1.0)


def test_precursor_unknown_name_raises():
    with pytest.raises(ValueError, match="bogus"):
        cp.precursor([], "bogus")


def test_family_p():
    one = cp.family_p([{"name": "a", "p": 0.05}])
    assert one["p_family"] is None

    three = cp.family_p([
        {"name": "a", "p": 0.02},
        {"name": "b", "p": 0.3},
        {"name": "c", "p": None},
    ])
    assert three["k"] == 2
    assert three["p_family"] == pytest.approx(1 - (1 - 0.02) * (1 - 0.3))


def test_standard_degradation_index():
    degrading = [
        {"gen": 0, "visible": 0.5},
        {"gen": 1, "visible": 0.7},
        {"gen": 2, "visible": 0.6},  # below running max 0.7
    ]
    assert cp.standard_degradation_index(degrading) == 2

    nondecreasing = [
        {"gen": 0, "visible": 0.5},
        {"gen": 1, "visible": 0.5},
        {"gen": 2, "visible": 0.6},
    ]
    assert cp.standard_degradation_index(nondecreasing) is None


def test_precursor_onset():
    turning = [
        {"gen": 0, "anchor_pass": 1, "anchor_fail": 1},  # 1.0
        {"gen": 1, "anchor_pass": 1, "anchor_fail": 1},  # 1.0
        {"gen": 2, "anchor_pass": 5, "anchor_fail": 0},  # 0.0 < 1.0
    ]
    assert cp.precursor_onset(turning, "anchor_entropy_contraction") == 2

    never = [
        {"gen": 0, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 1, "anchor_pass": 1, "anchor_fail": 1},
    ]
    assert cp.precursor_onset(never, "anchor_entropy_contraction") is None


def test_lead_time_positive_and_negative():
    early = [
        {"gen": 0, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 1, "visible": 0.6, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 2, "visible": 0.7, "anchor_pass": 5, "anchor_fail": 0},
        {"gen": 3, "visible": 0.6, "anchor_pass": 5, "anchor_fail": 0},
    ]
    lr = cp.lead_time(early)
    assert lr["lead"] > 0
    assert lr["early_warning"] is True

    late = [
        {"gen": 0, "visible": 0.7, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 1, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 2, "visible": 0.6, "anchor_pass": 5, "anchor_fail": 0},
    ]
    lr2 = cp.lead_time(late)
    assert lr2["lead"] < 0
    assert lr2["early_warning"] is False


def test_lead_time_none():
    no_degrade = [
        {"gen": 0, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 1, "visible": 0.6, "anchor_pass": 1, "anchor_fail": 1},
    ]
    lr = cp.lead_time(no_degrade)
    assert lr["lead"] is None
    assert lr["early_warning"] is False

    no_turn = [
        {"gen": 0, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1},
        {"gen": 1, "visible": 0.4, "anchor_pass": 1, "anchor_fail": 1},
    ]
    lr2 = cp.lead_time(no_turn)
    assert lr2["lead"] is None
    assert lr2["early_warning"] is False


def test_monitor_empty():
    r = cp.monitor([])
    assert r["n_generations"] == 0
    assert r["lead"]["lead"] is None
    assert r["lead"]["early_warning"] is False
    assert all(t["p"] is None for t in r["tests"])


def test_monitor_early_warning_synthesized():
    history = [
        {"gen": 0, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1,
         "covered_tail": 5, "covered_total": 10},
        {"gen": 1, "visible": 0.6, "anchor_pass": 2, "anchor_fail": 1,
         "covered_tail": 5, "covered_total": 10},
        {"gen": 2, "visible": 0.7, "anchor_pass": 3, "anchor_fail": 1,
         "covered_tail": 5, "covered_total": 10},
        {"gen": 3, "visible": 0.8, "anchor_pass": 5, "anchor_fail": 0,
         "covered_tail": 4, "covered_total": 10},
        {"gen": 4, "visible": 0.9, "anchor_pass": 6, "anchor_fail": 0,
         "covered_tail": 3, "covered_total": 10},
        {"gen": 5, "visible": 0.6, "anchor_pass": 7, "anchor_fail": 0,
         "covered_tail": 2, "covered_total": 10},
    ]
    r = cp.monitor(history)
    assert r["lead"]["early_warning"] is True
    assert r["lead"]["lead"] >= 1
    assert r["precursors_unmapped"] == ["representation_drift_freezing"]


def test_cli_main(tmp_path):
    from mini_ork.cli import collapse_precursor as cli

    fixture = tmp_path / "history.json"
    fixture.write_text(json.dumps([
        {"gen": 0, "visible": 0.5, "anchor_pass": 1, "anchor_fail": 1,
         "covered_tail": 1, "covered_total": 2},
        {"gen": 1, "visible": 0.6, "anchor_pass": 1, "anchor_fail": 1,
         "covered_tail": 1, "covered_total": 2},
        {"gen": 2, "visible": 0.7, "anchor_pass": 1, "anchor_fail": 1,
         "covered_tail": 1, "covered_total": 2},
        {"gen": 3, "visible": 0.8, "anchor_pass": 1, "anchor_fail": 1,
         "covered_tail": 1, "covered_total": 2},
    ]))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main([str(fixture), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "family" in payload

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = cli.main([str(tmp_path / "nope.json"), "--json"])
    assert rc2 == 0
    assert buf2.getvalue() == ""
