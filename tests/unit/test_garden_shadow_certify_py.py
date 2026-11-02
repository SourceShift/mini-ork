"""garden must say whether the shadow certify verdict ever fires (P6).

The default code-fix path pays for the two veto terms and obeys neither. That is
a cost leak only if the terms never block, and nobody can tell without counting
`would_block` across run directories — so the count belongs in `garden`, not in
a script each operator writes once and loses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import garden  # noqa: E402


def _run(home: Path, name: str, **doc) -> None:
    d = home / "runs" / name
    d.mkdir(parents=True)
    (d / "certify_terms.json").write_text(json.dumps(doc), encoding="utf-8")


def _infos(home: Path, capsys) -> list[str]:
    """One string per finding: the message plus the `Fix:` line under it."""
    findings = garden.Findings()
    garden._check_shadow_certify(str(home), findings)
    out: list[str] = []
    for line in capsys.readouterr().err.splitlines():
        if line.startswith("[info]"):
            out.append(line)
        elif line.startswith(" " * 10) and out:
            out[-1] += "\n" + line.strip()
    return out


def test_no_reports_is_silent(tmp_path, capsys):
    assert _infos(tmp_path, capsys) == []


def test_never_firing_points_at_the_budget(tmp_path, capsys):
    _run(tmp_path, "r1", status="ran", mode="shadow", would_block=False)
    _run(tmp_path, "r2", status="ran", mode="shadow", would_block=False)
    (text,) = _infos(tmp_path, capsys)
    assert "ran 2 time(s) and never blocked" in text
    assert "MO_ASSAY_RELATIONS_K" in text


def test_firing_reports_how_many_published_anyway(tmp_path, capsys):
    _run(tmp_path, "r1", status="ran", mode="shadow", would_block=True)
    _run(tmp_path, "r2", status="ran", mode="shadow", would_block=False)
    (text,) = _infos(tmp_path, capsys)
    assert "would have blocked 1 of 2 run(s)" in text
    assert "(1 published anyway)" in text
    assert "MO_CODEFIX_CERTIFY=gate" in text


def test_gate_mode_blocks_are_not_called_anymore(tmp_path, capsys):
    _run(tmp_path, "r1", status="ran", mode="gate", would_block=True)
    (text,) = _infos(tmp_path, capsys)
    assert "published anyway" not in text


def test_skipped_terms_report_that_nothing_was_measured(tmp_path, capsys):
    _run(tmp_path, "r1", status="skipped", mode="shadow", would_block=False)
    (text,) = _infos(tmp_path, capsys)
    assert "measured nothing" in text


def test_corrupt_reports_do_not_crash(tmp_path, capsys):
    d = tmp_path / "runs" / "bad"
    d.mkdir(parents=True)
    (d / "certify_terms.json").write_text("{not json", encoding="utf-8")
    assert _infos(tmp_path, capsys) == []


def test_findings_never_count_as_warnings(tmp_path):
    _run(tmp_path, "r1", status="ran", mode="shadow", would_block=True)
    findings = garden.Findings()
    garden._check_shadow_certify(str(tmp_path), findings)
    assert findings.warnings == 0
    assert findings.errors == 0
