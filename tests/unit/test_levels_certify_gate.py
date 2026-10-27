"""The level vector honours a recorded certify veto (MO_CODEFIX_CERTIFY=gate).

RSI Tier-1 #8b. The code-fix test verifier records the two certify veto terms
under ``verifier_test.json``'s ``certify`` key. This module's consumer reads
that evidence only — never the environment: the mode recorded at verify time
decides. In ``gate`` mode a ``relations`` veto downgrades ``target`` and a
``differential`` veto downgrades ``preserve``, both PROVEN → UNVERIFIED (never
REFUTED), so the publisher abstains and the fix stays in the tree.

Pure files in ``tmp_path``; no subprocess.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.verify import levels as L
from mini_ork.verify.behavioral import PROVEN, REFUTED, UNVERIFIED

NA = L.NA


# ── shared helpers (copied from test_verify_levels.py) ──────────────────────


def _run_dir(tmp_path: Path, *, summary=None, test_payload=None, behavioral=None,
             name: str = "run") -> Path:
    rd = tmp_path / name
    rd.mkdir(exist_ok=True)
    if summary is not None:
        (rd / "implementer-summary.json").write_text(json.dumps(summary))
    if test_payload is not None:
        (rd / "verifier_test.json").write_text(test_payload)
    if behavioral is not None:
        (rd / "verifier_behavioral.json").write_text(behavioral)
    return rd


def _test_evidence(payload: dict) -> str:
    """The stderr-prefixed evidence shape: a ``[test] running:`` line, then JSON."""
    return "[test] running: x\n" + json.dumps(payload) + "\n"


# ── kickoff fixtures ────────────────────────────────────────────────────────

SUMMARY = {"status": "implemented", "files_changed": ["mod.py"]}


def _base_payload():
    """The kickoff's base payload P: target and preserve both PROVEN."""
    return {
        "verifier": "test",
        "pass": True,
        "post_rc": 0,
        "base_rc": "",
        "replay": {"overlap": ["test_mod.py::test_a"]},
    }


def _failing_payload():
    """pass False with no overlap: target REFUTED today."""
    return {
        "verifier": "test",
        "pass": False,
        "post_rc": 0,
        "base_rc": "",
        "replay": {"overlap": []},
    }


def cert(mode, verdict, vetoed_by):
    """The kickoff's cert() block builder."""
    return {
        "mode": mode,
        "status": "ran",
        "reason": "r0",
        "poc_file": "test_mod.py",
        "relations": {"verdict": verdict, "reason": "rel-reason"},
        "differential": {"verdict": None, "reason": "diff-reason"},
        "verdict": verdict,
        "vetoed_by": vetoed_by,
        "would_block": verdict == "REFUTED",
    }


def _derive(tmp_path, payload, name="run"):
    rd = _run_dir(
        tmp_path,
        summary=SUMMARY,
        test_payload=_test_evidence(payload),
        name=name,
    )
    return L.derive_levels(str(rd), "code_fix")


# ── shadow stays inert ──────────────────────────────────────────────────────


def test_shadow_never_moves_a_level(tmp_path):
    plain_v, plain_r = _derive(tmp_path, _base_payload(), name="plain")
    shadow = _base_payload()
    shadow["certify"] = cert("shadow", "REFUTED", ["relations", "differential"])
    shadow_v, shadow_r = _derive(tmp_path, shadow, name="shadow")
    assert shadow_v == plain_v
    assert shadow_r == plain_r


# ── gate vetoes ─────────────────────────────────────────────────────────────


def test_gate_relations_veto_downgrades_target(tmp_path):
    payload = _base_payload()
    payload["certify"] = cert("gate", "REFUTED", ["relations"])
    vector, reasons = _derive(tmp_path, payload)
    assert vector["target"] == UNVERIFIED
    assert (
        reasons["target"]
        == "verifier_test.json: certify veto (relations): rel-reason"
    )
    assert vector["preserve"] == PROVEN


def test_gate_differential_veto_downgrades_preserve(tmp_path):
    payload = _base_payload()
    payload["certify"] = cert("gate", "REFUTED", ["differential"])
    vector, reasons = _derive(tmp_path, payload)
    assert vector["preserve"] == UNVERIFIED
    assert reasons["preserve"].startswith(
        "verifier_test.json: certify veto (differential)"
    )
    assert vector["target"] == PROVEN


def test_gate_without_a_veto_leaves_the_vector_unchanged(tmp_path):
    plain_v, plain_r = _derive(tmp_path, _base_payload(), name="plain")
    payload = _base_payload()
    payload["certify"] = cert("gate", None, [])
    vector, reasons = _derive(tmp_path, payload, name="gate")
    assert vector == plain_v
    assert reasons == plain_r


def test_malformed_blocks_are_ignored(tmp_path):
    plain_v, plain_r = _derive(tmp_path, _base_payload(), name="plain")
    malformed = {
        "not-a-dict": "REFUTED",
        "vetoed-by-not-a-list": {
            "mode": "gate",
            "verdict": "REFUTED",
            "vetoed_by": "relations",
        },
        "bogus-entry": cert("gate", "REFUTED", ["bogus"]),
    }
    for name, block in malformed.items():
        payload = _base_payload()
        payload["certify"] = block
        vector, reasons = _derive(tmp_path, payload, name=name)
        assert vector == plain_v, name
        assert reasons == plain_r, name


def test_veto_never_touches_a_non_proven_level(tmp_path):
    payload = _failing_payload()
    payload["certify"] = cert("gate", "REFUTED", ["relations"])
    baseline = _failing_payload()
    baseline_v, baseline_r = _derive(tmp_path, baseline, name="baseline")
    assert baseline_v["target"] == REFUTED
    vector, reasons = _derive(tmp_path, payload, name="vetoed")
    assert vector["target"] == REFUTED
    assert vector == baseline_v
    assert reasons == baseline_r


# ── level_report: the publish gate ──────────────────────────────────────────


def test_level_report_abstains_on_gate_veto(tmp_path):
    plain = _base_payload()
    plain_rd = _run_dir(
        tmp_path, summary=SUMMARY, test_payload=_test_evidence(plain), name="plain"
    )
    vetoed = _base_payload()
    vetoed["certify"] = cert("gate", "REFUTED", ["relations"])
    vetoed_rd = _run_dir(
        tmp_path, summary=SUMMARY, test_payload=_test_evidence(vetoed), name="vetoed"
    )

    plain_report = L.level_report(str(plain_rd), "code_fix")
    vetoed_report = L.level_report(str(vetoed_rd), "code_fix")

    assert plain_report["levels_decision"] == "publish"
    assert plain_report["levels_ok"] is True
    assert vetoed_report["levels_decision"] == "abstain"
    assert vetoed_report["levels_ok"] is False
    assert vetoed_report["levels"]["target"] == UNVERIFIED


# ── certify_vetoes ──────────────────────────────────────────────────────────


def test_certify_vetoes_gate_refuted_lists_both():
    both = cert("gate", "REFUTED", ["relations", "differential"])
    assert L.certify_vetoes({"certify": both}) == ["relations", "differential"]
    assert L.certify_vetoes({}) == []
