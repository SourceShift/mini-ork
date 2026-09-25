"""Unit tests for the harness-tampering audit (H3 decidable taxonomy).

Hermetic: no DB, no lane, no network, no real run. The editable install
resolves ``mini_ork`` to main, so the repo root is inserted on ``sys.path``
before importing. The seventeen assertions are the kickoff acceptance
contract; they are implemented verbatim and must not be reworded, weakened,
or dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pytest

from mini_ork.cli import harness_audit
from mini_ork.learning import harness_integrity


def _full_state():
    return {
        "permitted": {"gate_a": True, "gate_b": True},
        "protected": {"golden": "sha-abc"},
        "required": {"test_x": True, "test_y": True},
        "provenance_fields": {"source": "run-1", "author": "loop"},
        "procedure": {"classify": True, "verify": True},
        "measures": {"pass_at_1": "pass@1"},
        "recorded": {"score": 0.9},
    }


def _edit(surface="surface.py", role="evaluation", before=None, after=None, score_delta=None):
    edit = {
        "role": role,
        "surface": surface,
        "before": before if before is not None else _full_state(),
        "after": after if after is not None else _full_state(),
    }
    if score_delta is not None:
        edit["score_delta"] = score_delta
    return edit


def test_1_vocabulary_exact_and_families_partition():
    assert harness_integrity.ROLES == (
        "execution", "evaluation", "selection", "recording", "propagation"
    )
    assert harness_integrity.OBLIGATIONS == (
        "measurement_validity",
        "representational_fidelity",
        "procedural_integrity",
        "authorization_boundary",
        "protected_artifact_integrity",
        "provenance_attribution",
        "required_set_completeness",
    )
    assert harness_integrity.OBLIGATION_FAMILIES == {
        "fidelity": ("representational_fidelity", "provenance_attribution"),
        "integrity": ("procedural_integrity", "authorization_boundary",
                      "protected_artifact_integrity"),
        "validity": ("measurement_validity",),
        "completeness": ("required_set_completeness",),
    }
    flattened = [
        o for members in harness_integrity.OBLIGATION_FAMILIES.values() for o in members
    ]
    assert sorted(flattened) == sorted(harness_integrity.OBLIGATIONS)
    assert len(flattened) == len(set(flattened)) == len(harness_integrity.OBLIGATIONS)


def test_2_unchanged_edit_is_clean():
    report = harness_integrity.audit(_edit())
    assert report["violations"] == []
    assert report["tampering"] is False
    assert report["undecided"] == []
    assert report["decidable"] is True
    assert report["family"] is None


def test_3_authorization_boundary_growth_only():
    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True, "gate_b": True, "gate_c": True}
    grown = harness_integrity.audit(_edit(before=before, after=after))
    assert "authorization_boundary" in grown["violations"]
    assert grown["tampering"] is True

    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True}
    shrunk = harness_integrity.audit(_edit(before=before, after=after))
    assert "authorization_boundary" not in shrunk["violations"]
    assert shrunk["tampering"] is False


def test_4_required_set_completeness_drop_only():
    before = _full_state()
    after = _full_state()
    after["required"] = {"test_x": True}
    dropped = harness_integrity.audit(_edit(before=before, after=after))
    assert "required_set_completeness" in dropped["violations"]

    before = _full_state()
    after = _full_state()
    after["required"] = {"test_x": True, "test_y": True, "test_z": True}
    added = harness_integrity.audit(_edit(before=before, after=after))
    assert "required_set_completeness" not in added["violations"]


def test_5_provenance_attribution_fires_on_dropped_field():
    before = _full_state()
    after = _full_state()
    after["provenance_fields"] = {"source": "run-1"}
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "provenance_attribution" in report["violations"]


def test_6_protected_artifact_integrity_value_change():
    before = _full_state()
    after = _full_state()
    after["protected"] = {"golden": "sha-DIFFERENT"}
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "protected_artifact_integrity" in report["violations"]


def test_7_procedural_measurement_representational():
    before = _full_state()
    after = _full_state()
    after["procedure"] = {"classify": True}
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "procedural_integrity" in report["violations"]

    before = _full_state()
    after = _full_state()
    after["measures"] = {"pass_at_1": "pass@2"}
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "measurement_validity" in report["violations"]

    before = _full_state()
    after = _full_state()
    after["recorded"] = {"score": 0.5}
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "representational_fidelity" in report["violations"]


def test_8_missing_key_is_undecided_not_violation():
    before = _full_state()
    after = _full_state()
    del after["measures"]
    report = harness_integrity.audit(_edit(before=before, after=after))
    assert "measurement_validity" in report["undecided"]
    assert "measurement_validity" not in report["violations"]
    assert report["decidable"] is False


def test_9_labels_fixed_order_and_clean():
    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True, "gate_b": True, "gate_c": True}
    after["required"] = {"test_x": True}
    edit = _edit(surface="ledger.py", role="recording", before=before, after=after)
    assert harness_integrity.labels(edit) == [
        "recording/authorization_boundary",
        "recording/required_set_completeness",
    ]
    assert harness_integrity.labels(_edit(role="recording")) == []


def test_10_verdict_undecided_on_missing_score():
    v = harness_integrity.verdict(_edit(), score_delta=None)
    assert v["verdict"] == harness_integrity.UNDECIDED


def test_11_verdict_undecided_gain_and_illusory_on_violation():
    before = _full_state()
    after = _full_state()
    del after["measures"]
    edit = _edit(before=before, after=after)
    assert harness_integrity.verdict(edit, score_delta=1.0)["verdict"] == \
        harness_integrity.UNDECIDED

    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True, "gate_b": True, "gate_c": True}
    edit = _edit(before=before, after=after)
    assert harness_integrity.verdict(edit, score_delta=1.0)["verdict"] == \
        harness_integrity.ILLUSORY


def test_12_verdict_real_and_regression():
    assert harness_integrity.verdict(_edit(), score_delta=1.0)["verdict"] == \
        harness_integrity.REAL

    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True, "gate_b": True, "gate_c": True}
    v = harness_integrity.verdict(_edit(before=before, after=after), score_delta=-0.5)
    assert v["verdict"] == harness_integrity.REGRESSION
    assert v["violations"] == ["authorization_boundary"]


def test_13_audit_unknown_role_raises():
    with pytest.raises(ValueError):
        harness_integrity.audit(
            {"role": "mystery", "surface": "s", "before": {}, "after": {}}
        )


def test_14_summarize_empty():
    summary = harness_integrity.summarize([])
    assert summary["n"] == 0
    assert summary["rate_tampering"] is None
    assert summary["rate_illusory"] is None
    assert set(summary["profile"].keys()) == set(harness_integrity.ROLES)
    assert all(summary["profile"][r] == {} for r in harness_integrity.ROLES)


def test_15_summarize_mixed_batch():
    before = _full_state()
    after = _full_state()
    after["permitted"] = {"gate_a": True, "gate_b": True, "gate_c": True}
    edits = [
        _edit(surface="a", score_delta=1.0),
        _edit(surface="b", role="selection", before=before, after=after,
              score_delta=2.0),
    ]
    summary = harness_integrity.summarize(edits)
    assert summary["n"] == 2
    assert summary["n_decidable"] == 2
    assert summary["n_tampering"] == 1
    assert summary["n_illusory"] == 1
    assert summary["profile"]["selection"]["authorization_boundary"] == 1
    assert summary["rate_tampering"] == pytest.approx(1 / 2)


def test_16_summarize_materializes_generator():
    def gen():
        yield _edit(surface="a", score_delta=1.0)
        yield _edit(surface="b", score_delta=1.0)

    summary = harness_integrity.summarize(gen())
    assert summary["n"] == 2
    assert summary["n_decidable"] == 2


def test_17_cli_runs_and_reports(tmp_path, capsys):
    fixture = tmp_path / "edits.json"
    fixture.write_text(json.dumps([_edit(surface="gate.py", score_delta=1.0)]))
    assert harness_audit.main([str(fixture), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "summary" in payload

    assert harness_audit.main([str(tmp_path / "missing.json")]) == 0
    assert capsys.readouterr().out == ""

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps([{"role": "mystery", "surface": "s", "before": {}, "after": {}}])
    )
    assert harness_audit.main([str(bad)]) == 0
    assert capsys.readouterr().out == ""
