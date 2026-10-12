"""``is_rubric_prescreen`` must not mistake a panel verdict for a pre-screen.

Incident (2026-10-12): runs ``run-1791790859-83222--p1-5-parity-suite-and-docs--D1``
and ``…--D3`` reached the publisher with a panel verdict of APPROVE, every gate
green, and were refused with ``missing panel verdict``. Cause: the advisory rubric
pre-screen MERGES into ``panel-verdict.json`` with ``setdefault``, so its
``source: rubric-prescreen`` stamp landed on the panel's own verdict (which carries
no ``source``). ``is_rubric_prescreen`` keyed on ``source`` alone and read the
approved panel as "no panel verdict".

A *pure* pre-screen holds only ``rubric_scoring.build_panel_verdict``'s keys; the
tests below lock both sides of that boundary.
"""
from __future__ import annotations

import json
from pathlib import Path

from mini_ork.cli import publisher


def _prescreen_payload() -> dict:
    # Exactly rubric_scoring.build_panel_verdict's shape.
    return {
        "panel_score": 100.0,
        "pass": True,
        "source": "rubric-prescreen",
        "task_class": "recursive_validate_impl",
        "scale": "rubric 0-8 mapped to 0-100",
    }


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "panel-verdict.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_pure_prescreen_is_a_prescreen(tmp_path: Path) -> None:
    assert publisher.is_rubric_prescreen(str(_write(tmp_path, _prescreen_payload()))) is True


def test_prescreen_plus_its_namespaced_echo_is_still_a_prescreen(tmp_path: Path) -> None:
    """The merge also writes a nested ``prescreen`` key — that alone is still pure."""
    payload = _prescreen_payload()
    payload["prescreen"] = _prescreen_payload()
    assert publisher.is_rubric_prescreen(str(_write(tmp_path, payload))) is True


def test_a_stamped_panel_verdict_is_not_a_prescreen(tmp_path: Path) -> None:
    """The regression: panel content + the pre-screen's stale ``source`` stamp."""
    payload = _prescreen_payload()
    payload.update({
        "verdict": "APPROVE",
        "reasons": ["DoD: the sole probe passes 4/4 with matching evidence"],
        "dod_probe_results": [{"id": "P1", "panel_consensus": "pass"}],
        "hard_rule_violations": [],
        "prescreen": _prescreen_payload(),
    })
    assert publisher.is_rubric_prescreen(str(_write(tmp_path, payload))) is False


def test_a_verdict_without_the_source_stamp_is_not_a_prescreen(tmp_path: Path) -> None:
    payload = _prescreen_payload()
    payload.pop("source")
    payload["verdict"] = "APPROVE"
    assert publisher.is_rubric_prescreen(str(_write(tmp_path, payload))) is False


def test_missing_and_malformed_files_are_not_prescreens(tmp_path: Path) -> None:
    assert publisher.is_rubric_prescreen(str(tmp_path / "absent.json")) is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert publisher.is_rubric_prescreen(str(bad)) is False
