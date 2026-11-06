"""`validate` requires a Done-When section to name numbered, observable criteria.

"Done When: the feature works" is not checkable, so the verifier node falls back
to a reviewer's opinion — the vacuous-spec failure mode the spec-driven-delivery
plan documents. The check is a warning by default and an error only on request
(`MO_KICKOFF_INVARIANTS=error`), so the rule arrives as a ratchet rather than as
a wall on the day it ships.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.cli.validate import Findings, _check_kickoff, _numbered_criteria


UNNUMBERED = """# Fix the retry loop

## Goal

Retries stop after the budget.

## Done When

The retry loop respects the budget and tests pass.
"""

NUMBERED = """# Fix the retry loop

## Goal

Retries stop after the budget.

## Done When

1. `pytest tests/unit/test_retry.py` exits 0.
2. Three consecutive failures raise `RetryBudgetExhausted`.
"""


def test_section_detection_counts_numbered_items():
    assert _numbered_criteria(NUMBERED) == (1, 2)
    assert _numbered_criteria(UNNUMBERED) == (1, 0)


def test_nested_subsection_items_still_count():
    text = UNNUMBERED + "\n### Evidence\n\n1. the log shows three attempts\n"
    assert _numbered_criteria(text) == (1, 1)


def test_only_done_when_sections_are_read():
    text = "## Goal\n\n1. something\n\n## Notes\n\n1. something else\n"
    assert _numbered_criteria(text) == (0, 0)


def _validate(path: Path, monkeypatch, mode: str = "warn") -> Findings:
    monkeypatch.setenv("MO_KICKOFF_INVARIANTS", mode)
    findings = Findings()
    _check_kickoff(str(path), findings)
    return findings


def test_unnumbered_done_when_warns(tmp_path, monkeypatch):
    p = tmp_path / "kickoff.md"
    p.write_text(UNNUMBERED)
    findings = _validate(p, monkeypatch)
    assert findings.warnings == 1
    assert findings.errors == 0


def test_numbered_done_when_is_clean(tmp_path, monkeypatch):
    p = tmp_path / "kickoff.md"
    p.write_text(NUMBERED)
    findings = _validate(p, monkeypatch)
    assert findings.warnings == 0 and findings.errors == 0


def test_mode_error_promotes_the_warning(tmp_path, monkeypatch):
    p = tmp_path / "kickoff.md"
    p.write_text(UNNUMBERED)
    findings = _validate(p, monkeypatch, mode="error")
    assert findings.errors == 1


def test_mode_off_silences_it(tmp_path, monkeypatch):
    p = tmp_path / "kickoff.md"
    p.write_text(UNNUMBERED)
    findings = _validate(p, monkeypatch, mode="off")
    assert findings.warnings == 0 and findings.errors == 0


@pytest.mark.parametrize("value", ["", "yes", "ERROR ", "strict"])
def test_unknown_mode_falls_back_to_warn(tmp_path, monkeypatch, value):
    p = tmp_path / "kickoff.md"
    p.write_text(UNNUMBERED)
    monkeypatch.setenv("MO_KICKOFF_INVARIANTS", value)
    findings = Findings()
    _check_kickoff(str(p), findings)
    assert findings.warnings == (0 if value.strip().lower() == "error" else 1)


def test_missing_kickoff_is_not_an_error(tmp_path, monkeypatch):
    findings = _validate(tmp_path / "absent.md", monkeypatch)
    assert findings.errors == 0 and findings.warnings == 0
