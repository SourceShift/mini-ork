"""A check that did not run must not read as a check that passed (P1).

Three seams are covered:

  1. ``artifact_contract.load_contract_ex`` reports whether a contract came from
     a real file or from the permissive fallback, and ``validate`` defers —
     never passes — when the fallback was substituted.
  2. ``recipe_fallback_plan`` records ``contract_source`` in the plan so the
     verifier can tell "no contract was ever written" from "the contract held".
  3. ``verify._write_verification_manifest`` records, per instrument,
     ``ran`` / ``skipped`` / ``shadow`` / ``not_applicable`` plus the knob that
     decided it — the additive record that makes a disabled audit visible even
     though the verdict payload is deliberately byte-stable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import verify as verify_mod  # noqa: E402
from mini_ork.gates import artifact_contract as ac  # noqa: E402
from mini_ork.planning.recipe_plan import recipe_fallback_plan  # noqa: E402


# ── 1. contract source + verdict ─────────────────────────────────────────────

def test_missing_contract_is_reported_as_default(tmp_path):
    contract, source = ac.load_contract_ex(
        "no_such_task", contracts_dir=str(tmp_path))
    assert source == "default"
    assert contract["_default"] is True
    assert contract["success_verifiers"] == []


def test_present_contract_is_reported_as_file(tmp_path):
    (tmp_path / "patchy.yaml").write_text(
        "task_class: patchy\nexpected_artifact: patch\nsuccess_verifiers: []\n",
        encoding="utf-8")
    _contract, source = ac.load_contract_ex("patchy", contracts_dir=str(tmp_path))
    assert source == "file"


def test_validate_defers_instead_of_passing_without_a_contract(tmp_path):
    artifact = tmp_path / "out.patch"
    artifact.write_text("diff --git a/x b/x\n", encoding="utf-8")
    verdict, payload = ac.validate(
        "no_such_task", str(artifact), contracts_dir=str(tmp_path))
    assert verdict == "defer"
    assert payload["reason"] == "contract_missing"
    assert payload["contract_source"] == "default"


def test_validate_still_passes_with_a_real_contract(tmp_path):
    (tmp_path / "patchy.yaml").write_text(
        "task_class: patchy\nexpected_artifact: patch\nsuccess_verifiers: []\n",
        encoding="utf-8")
    artifact = tmp_path / "out.patch"
    artifact.write_text("diff --git a/x b/x\n", encoding="utf-8")
    verdict, payload = ac.validate(
        "patchy", str(artifact), contracts_dir=str(tmp_path), mini_ork_root=str(REPO))
    assert verdict == "pass"
    assert payload["contract_source"] == "file"


def test_load_contract_keeps_its_dict_only_shape(tmp_path):
    """``load_contract`` is the historical API — callers still get a dict."""
    contract = ac.load_contract("no_such_task", contracts_dir=str(tmp_path))
    assert isinstance(contract, dict)
    assert contract["task_class"] == "no_such_task"


# ── 2. the plan records where the contract came from ────────────────────────

def _recipe(tmp_path: Path, *, with_contract: bool) -> Path:
    rdir = tmp_path / "recipes" / "demo"
    rdir.mkdir(parents=True)
    (rdir / "workflow.yaml").write_text(
        "nodes:\n  - name: worker\n    type: implementer\nedges: []\n",
        encoding="utf-8")
    if with_contract:
        (rdir / "artifact_contract.yaml").write_text(
            "task_class: demo\noutputs:\n  - artifact.md\nsuccess_verifiers: []\n",
            encoding="utf-8")
    return rdir


def test_fallback_plan_marks_a_missing_contract(tmp_path):
    _recipe(tmp_path, with_contract=False)
    rendered = recipe_fallback_plan(
        "demo", str(tmp_path / "recipes/demo/workflow.yaml"), str(tmp_path),
        str(tmp_path / "kickoff.md"))
    assert rendered
    plan = json.loads(rendered)
    assert plan["artifact_contract"]["contract_source"] == "missing"
    assert plan["artifact_contract"]["outputs"] == []


def test_fallback_plan_marks_a_present_contract(tmp_path):
    _recipe(tmp_path, with_contract=True)
    rendered = recipe_fallback_plan(
        "demo", str(tmp_path / "recipes/demo/workflow.yaml"), str(tmp_path),
        str(tmp_path / "kickoff.md"))
    assert rendered
    plan = json.loads(rendered)
    assert plan["artifact_contract"]["contract_source"] == "file"
    assert plan["artifact_contract"]["outputs"] == ["artifact.md"]


# ── 3. the verification manifest ────────────────────────────────────────────

def _manifest(tmp_path: Path, contract: dict, results: list[str] | None = None,
              task_class: str = "code_fix") -> dict:
    path = verify_mod._write_verification_manifest(
        run_dir=str(tmp_path), task_class=task_class, artifact_path="out.patch",
        verdict="pass", plan_contract=contract, results=results or [])
    assert path.endswith("verification_manifest.json")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_manifest_names_the_knob_that_disabled_the_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_SUITE_ADEQUACY", "0")
    monkeypatch.setenv("MO_CODEFIX_CERTIFY", "shadow")
    doc = _manifest(tmp_path, {"contract_source": "file",
                               "success_verifiers": ["recipes/code-fix/verifiers/test.py"]})
    by_name = {row["instrument"]: row for row in doc["instruments"]}
    assert by_name["suite_adequacy"]["status"] == "skipped"
    assert by_name["suite_adequacy"]["knob"] == "MO_SUITE_ADEQUACY=0"
    assert by_name["certify_terms"]["status"] == "shadow"
    assert by_name["artifact_contract"]["status"] == "ran"


def test_manifest_reports_adequacy_as_ran_when_the_knob_is_on(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    doc = _manifest(tmp_path, {"contract_source": "file",
                               "success_verifiers": ["recipes/code-fix/verifiers/test.py"]})
    by_name = {row["instrument"]: row for row in doc["instruments"]}
    assert by_name["suite_adequacy"]["status"] == "ran"
    assert by_name["suite_adequacy"]["knob"] == ""


def test_manifest_marks_adequacy_not_applicable_outside_code_fix(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    doc = _manifest(tmp_path, {"contract_source": "file", "success_verifiers": []},
                    task_class="blog_post")
    by_name = {row["instrument"]: row for row in doc["instruments"]}
    assert by_name["suite_adequacy"]["status"] == "not_applicable"


def test_manifest_names_deferred_gates(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    deferred = json.dumps({
        "verifier": "__gates_unmeasured__", "pass": None,
        "detail": "gate(s) did not run: mutation-adversary-gate, oracle-coalition",
        "evidence_path": "gate_registry:defer",
    })
    doc = _manifest(tmp_path, {"contract_source": "file", "success_verifiers": []},
                    results=[deferred], task_class="blog_post")
    gates = [row["instrument"] for row in doc["instruments"]
             if row["instrument"].startswith("gate:")]
    assert gates == ["gate:mutation-adversary-gate", "gate:oracle-coalition"]


def test_manifest_reports_a_missing_contract_as_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    doc = _manifest(tmp_path, {"contract_source": "missing", "outputs": []},
                    task_class="blog_post")
    by_name = {row["instrument"]: row for row in doc["instruments"]}
    assert by_name["artifact_contract"]["status"] == "skipped"


def test_manifest_is_a_noop_without_a_run_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    assert verify_mod._write_verification_manifest(
        run_dir="", task_class="x", artifact_path="", verdict="pass",
        plan_contract={}, results=[]) == ""


@pytest.mark.parametrize("verdict", ["pass", "fail", "partial", "vacuous"])
def test_manifest_records_the_verdict_it_saw(tmp_path, monkeypatch, verdict):
    monkeypatch.delenv("MO_SUITE_ADEQUACY", raising=False)
    path = verify_mod._write_verification_manifest(
        run_dir=str(tmp_path), task_class="x", artifact_path="a",
        verdict=verdict, plan_contract={}, results=[])
    assert json.loads(Path(path).read_text())["verdict"] == verdict
