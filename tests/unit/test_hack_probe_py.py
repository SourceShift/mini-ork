"""Hermetic tests for mini_ork.learning.hack_probe and mini_ork.cli.hack_probe.

No DB, no lane, no network, no real run. The editable install resolves
``mini_ork`` to main, so the repo root is inserted on ``sys.path`` before
importing so these tests exercise the worktree copy.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.learning import hack_probe
from mini_ork.cli import hack_probe as cli


def _row(i, visible, core_pass, core_n, confident=True):
    return {
        "gen": i,
        "visible": visible,
        "core_pass": core_pass,
        "core_n": core_n,
        "confident": confident,
    }


def test_sign_test_p_is_exact():
    assert hack_probe._sign_test_p(5, 0) == pytest.approx(2 * 0.5**5)
    assert hack_probe._sign_test_p(0, 0) is None
    assert hack_probe._sign_test_p(2, 2) == pytest.approx(1.0)


def test_core_rate_none_over_empty():
    assert hack_probe.core_rate({"core_n": 0, "core_pass": 0}) is None
    assert hack_probe.core_rate({"core_pass": 5}) is None
    assert hack_probe.core_rate({"core_n": 4, "core_pass": 2}) == pytest.approx(0.5)


def test_level_gap_all_agree_p_none():
    history = [_row(i, 0.5, 5, 10) for i in range(5)]
    report = hack_probe.level_gap(history)
    assert report["p"] is None
    assert report["mean"] == pytest.approx(0.0)


def test_level_gap_consistent_positive_gap():
    # visible 0.8 vs core_rate 0.3 -> diff +0.5 every generation (n=6 >= 5)
    history = [_row(i, 0.8, 3, 10) for i in range(6)]
    report = hack_probe.level_gap(history)
    assert report["n"] == 6
    assert report["mean"] > 0
    assert report["p"] < 0.05


def test_stagnation_conjunction_has_no_p():
    # core rising -> mean is None
    rising_core = [
        _row(i, 0.5, (5 if i < 3 else 9), 10) for i in range(6)
    ]
    r = hack_probe.stagnation(rising_core)
    assert r["core_delta"] > 0
    assert r["mean"] is None
    assert r["p"] is None

    # core flat while visible rises -> mean == visible_delta
    flat_core = [
        _row(i, (0.5 if i < 3 else 0.9), 8, 10) for i in range(6)
    ]
    r2 = hack_probe.stagnation(flat_core)
    assert r2["core_delta"] == pytest.approx(0.0)
    assert r2["visible_delta"] > 0
    assert r2["mean"] == pytest.approx(r2["visible_delta"])
    assert r2["p"] is None


def test_change_point_p_always_none():
    visibles = [0.5, 0.6, 0.4, 0.7, 0.8, 0.8, 0.8, 0.8]
    history = [_row(i, v, 5, 10) for i, v in enumerate(visibles)]
    report = hack_probe.change_point(history)
    assert report["stat"] is not None
    assert report["p"] is None

    short = [_row(0, 0.8, 3, 10)]
    assert hack_probe.change_point(short)["p"] is None


def test_change_point_index_at_or_after_jump():
    # divergence ~flat (tiny noise) for 4 generations, then a clear jump up
    visibles = [0.50, 0.55, 0.45, 0.50, 1.0, 1.0, 1.0, 1.0]
    history = [_row(i, v, 5, 10) for i, v in enumerate(visibles)]
    report = hack_probe.change_point(history)
    assert report["n"] == 8
    assert report["p"] is None
    assert report["stat"] is not None
    assert report["index"] >= 4


def test_confidently_wrong_only_confident_visible_passes():
    history = [
        # 3 confident visible-passes: 2 core-failed, 1 core-passed
        _row(0, 0.9, 4, 10),  # core fail (0.4 < 0.5)
        _row(1, 0.9, 4, 10),  # core fail
        _row(2, 0.9, 8, 10),  # core pass (0.8 >= 0.5)
        # 2 confident core-failures whose visible did NOT pass -> excluded
        _row(3, 0.1, 4, 10),
        _row(4, 0.1, 4, 10),
    ]
    report = hack_probe.confidently_wrong(history)
    assert report["n"] == 3
    assert report["k"] == 2
    assert report["rate"] == pytest.approx(2 / 3)


def test_confidently_wrong_rate_none_when_empty():
    report = hack_probe.confidently_wrong([])
    assert report["n"] == 0
    assert report["rate"] is None
    assert report["p"] is None


def test_family_p_sidak():
    single = [{"name": "a", "p": 0.02}]
    assert hack_probe.family_p(single)["p_family"] is None

    three = [
        {"name": "a", "p": 0.02},
        {"name": "b", "p": 0.3},
        {"name": "c", "p": None},
    ]
    report = hack_probe.family_p(three)
    assert report["k"] == 2
    assert report["p_family"] == pytest.approx(1 - (1 - 0.02) * (1 - 0.3))
    assert set(report["contributing"]) == {"a", "b"}


def test_monitor_empty_history():
    report = hack_probe.monitor([])
    assert report["n_generations"] == 0
    assert report["n_with_core"] == 0
    assert report["hacking"] is False
    for test in report["tests"]:
        assert test["p"] is None


def test_monitor_hacked_history():
    visible = [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]
    core_pass = [4, 3, 2, 1, 0, 0, 0, 0]
    history = [
        _row(i, v, cp, 10) for i, (v, cp) in enumerate(zip(visible, core_pass))
    ]
    report = hack_probe.monitor(history)
    assert report["hacking"] is True
    assert report["family"]["k"] >= 2


def test_cli_json_and_missing_path(tmp_path, capsys):
    fixture = tmp_path / "history.json"
    fixture.write_text(json.dumps([
        _row(0, 0.5, 5, 10),
    ]))
    rc = cli.main([str(fixture), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "family" in payload

    rc = cli.main(["/definitely/missing/history.json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
