"""A withheld publish must say why, in prose (P5).

The level vector explains itself in JSON. Whoever inherits the run reads a diff
and a tree with no commit; this file pins the four-section summary that names
the blocking level, its reason and the mutants the suite did not notice.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.verify import levels  # noqa: E402


def _write(run_dir: Path, **over) -> str:
    vector = {"applies": levels.NA, "executes": levels.PROVEN,
              "target": levels.PROVEN, "preserve": levels.UNVERIFIED,
              "contract": levels.NA}
    reasons = {"applies": "instrument n/a", "executes": "verifier_test.json: PROVEN",
               "target": "verifier_test.json: PROVEN",
               "preserve": "suite adequacy 0.31 below floor 0.60",
               "contract": "verifier_behavioral.json: absent"}
    vector.update(over.pop("vector", {}))
    reasons.update(over.pop("reasons", {}))
    return levels.write_summary(
        str(run_dir), over.pop("task_class", "code_fix"),
        vector=vector, reasons=reasons,
        required=over.pop("required", ("applies", "executes", "target", "preserve")),
        decision=over.pop("decision", "abstain"))


def test_withheld_summary_names_the_blocking_level_and_its_reason(tmp_path):
    path = _write(tmp_path)
    assert path.endswith("summary.md")
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("# Why this run was withheld")
    assert "`abstain`" in text
    assert "**preserve: UNVERIFIED**" in text
    assert "suite adequacy 0.31 below floor 0.60" in text
    assert "still in the tree for inspection" in text


def test_summary_lists_every_level_with_required_marked(tmp_path):
    text = Path(_write(tmp_path)).read_text(encoding="utf-8")
    for level in levels.LEVELS:
        assert f"| {level} |" in text
    assert "| preserve | UNVERIFIED | yes |" in text
    assert "| contract | n/a | no |" in text


def test_summary_names_the_mutant_survivors(tmp_path):
    (tmp_path / "suite_adequacy.json").write_text(json.dumps({
        "verdict": "WEAK",
        "survivors": [
            {"file": "mini_ork/x.py", "line": 42, "operator": "comparison",
             "original": "a < b", "mutated": "a <= b"},
        ],
    }), encoding="utf-8")
    text = Path(_write(tmp_path)).read_text(encoding="utf-8")
    assert "mini_ork/x.py:42" in text
    assert "comparison" in text
    assert "`a < b` → `a <= b`" in text


def test_summary_caps_the_survivor_list(tmp_path):
    survivors = [{"file": f"f{i}.py", "line": i, "operator": "op",
                  "original": "x", "mutated": "y"} for i in range(25)]
    (tmp_path / "suite_adequacy.json").write_text(
        json.dumps({"survivors": survivors}), encoding="utf-8")
    text = Path(_write(tmp_path)).read_text(encoding="utf-8")
    assert "… and 5 more in `suite_adequacy.json`" in text


def test_published_run_gets_a_neutral_heading(tmp_path):
    text = Path(_write(tmp_path, decision="publish",
                       vector={"preserve": levels.PROVEN})).read_text(encoding="utf-8")
    assert text.startswith("# Run verification summary")
    assert "What blocked the publish" not in text


def test_refuted_run_reads_as_withheld_too(tmp_path):
    text = Path(_write(tmp_path, decision="refute",
                       vector={"target": levels.REFUTED})).read_text(encoding="utf-8")
    assert "**target: REFUTED**" in text


def test_no_run_dir_writes_nothing():
    assert levels.write_summary("", "code_fix", vector={}, reasons={},
                                required=(), decision="abstain") == ""


def test_level_report_writes_the_summary(tmp_path):
    """The side effect rides the one call the executor already makes."""
    levels.level_report(str(tmp_path), "code_fix")
    assert (tmp_path / "summary.md").is_file()
