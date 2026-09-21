"""Unit tests for the paired-contrast attribution module (harness_contrast).

Hermetic: no DB, no lane, no network, no live runs. The numbered cases follow
the kickoff contract — the lane control is mechanical (empty lane and mixed
lanes refuse with ``ValueError``), an all-agree contrast is reported as
*unresolved* rather than as a win, and an empty set reports ``None`` rates
rather than ``0.0``.
"""
from __future__ import annotations

import json

import pytest

from mini_ork.cli import harness_contrast as cli
from mini_ork.learning import harness_contrast


def _row(probe: str, a: bool, b: bool, lane: str = "glm") -> dict:
    return {"probe": probe, "lane": lane, "a": a, "b": b}


def test_1_all_agree_is_unresolved_not_a_win():
    """Every row agrees → the null result is reported as unresolved."""
    rows = [
        _row("p1", True, True),
        _row("p2", True, True),
        _row("p3", False, False),
        _row("p4", False, False),
    ]
    report = harness_contrast.attribute(rows)
    assert report["n"] == 4
    assert report["discordant"] == 0
    assert report["resolved"] is False
    assert report["a_rate"] == report["b_rate"]
    assert report["delta"] == 0.0
    assert all(r["kind"] in ("agree-pass", "agree-fail") for r in report["rows"])
    assert [r["delta"] for r in report["rows"]] == [0, 0, 0, 0]


def test_2_discordant_rows_are_attributed_exactly():
    rows = [
        _row("p1", False, True),
        _row("p2", False, True),
        _row("p3", True, False),
        _row("p4", True, True),
    ]
    report = harness_contrast.attribute(rows)
    assert report["b_only"] == 2
    assert report["a_only"] == 1
    assert report["discordant"] == 3
    assert report["resolved"] is True
    assert report["delta"] == (report["b_pass"] - report["a_pass"]) / 4
    assert report["delta"] == 0.25


def test_3_mixed_lane_raises_naming_probe_and_both_lanes():
    rows = [
        _row("p1", True, True),
        _row("p2", True, False, lane="kimi"),
        _row("p3", False, False),
    ]
    with pytest.raises(ValueError) as excinfo:
        harness_contrast.attribute(rows)
    message = str(excinfo.value)
    assert "p2" in message
    assert "glm" in message
    assert "kimi" in message


def test_4_empty_lane_raises():
    rows = [_row("p1", True, True), _row("p2", True, False, lane="")]
    with pytest.raises(ValueError):
        harness_contrast.attribute(rows)


def test_5_empty_set_is_none_never_zero():
    report = harness_contrast.attribute([])
    assert report["n"] == 0
    assert report["a_rate"] is None
    assert report["b_rate"] is None
    assert report["delta"] is None
    assert report["resolved"] is False
    assert report["rows"] == []


def test_6_per_row_kind_and_delta_are_exact():
    rows = [
        _row("agree_pass", True, True),
        _row("agree_fail", False, False),
        _row("a_only", True, False),
        _row("b_only", False, True),
    ]
    echoed = harness_contrast.attribute(rows)["rows"]
    assert [r["kind"] for r in echoed] == [
        "agree-pass", "agree-fail", "a-only", "b-only",
    ]
    assert [r["delta"] for r in echoed] == [0, 0, -1, 1]
    assert [r["probe"] for r in echoed] == [
        "agree_pass", "agree_fail", "a_only", "b_only",
    ]


def test_7_summarize_markers_and_none_rendering():
    unresolved = harness_contrast.summarize(
        harness_contrast.attribute([_row("p1", True, True), _row("p2", False, False)])
    )
    assert "unresolved (no discordant pairs)" in unresolved

    attributed = harness_contrast.summarize(
        harness_contrast.attribute([
            _row("p1", False, True),
            _row("p2", False, True),
            _row("p3", True, True),
            _row("p4", True, False),
        ])
    )
    assert "+0.250" in attributed

    empty = harness_contrast.summarize(harness_contrast.attribute([]))
    assert "-" in empty


def test_8_cli_registered_and_reports_without_gating(tmp_path, capsys):
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY

    assert "harness-contrast" in SUBCOMMAND_REGISTRY

    good = tmp_path / "rows.json"
    good.write_text(json.dumps([
        _row("p1", False, True),
        _row("p2", False, True),
        _row("p3", False, False),
        _row("p4", True, True),
    ]))
    assert cli.main(["--json", str(good)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delta"] == 0.5
    assert payload["b_only"] == 2
    assert payload["resolved"] is True

    # A contaminated set is reported to stderr and still exits 0.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([_row("p1", True, True), _row("p2", True, False, lane="kimi")]))
    assert cli.main([str(bad)]) == 0
    assert "mixed" in capsys.readouterr().err

    # A malformed file is reported to stderr and still exits 0.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert cli.main([str(broken)]) == 0
    assert capsys.readouterr().err
