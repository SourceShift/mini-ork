"""Parity gate: mini_ork.gates.coalition_gate vs lib/coalition_gate.sh.

rho is neutralised (MO_RHO_THRESHOLD=999) so the verdict is driven purely by
family collision — deterministic across bash's *measured* rho and the port's
*passed* rho. Only the rho-independent subset {verdict, reason, family_count}
is compared, never rho or the rationale string (which legitimately differ:
bash measures rho, the port is handed 0.0).

Subsumes the retired tests/unit/test_coalition_gate.sh (10 assertions across 4
behavioral groups). Every .sh assertion is present here as a case driving the
LIVE lib via a bash subprocess (_bash_verdict / _bash_full):
  - A verdict=panel_diverse / reason=ok / family_count=4  -> test_diverse_ok_parity
    (verdict) + test_diverse_reason_and_family_count_parity (reason + family_count);
  - B verdict=COALITION_ABORT / family_count=1 / reason    -> test_collision_aborts_parity
    (verdict) + test_collision_reason_and_family_count_parity (reason + family_count);
  - C advisory -> panel_diverse / reason=advisory_*        -> test_advisory_bypass_parity;
  - D single-agent -> panel_diverse / reason=single_agent_run -> test_single_agent_run_parity.
The reason + family_count fields and the advisory + single-agent branches were
the coverage the original verdict-only _bash_verdict lacked; they are added here
as live-bash cases. No production change was required — the bash verdict heredoc
and the port's coalition_verdict are byte-identical.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.gates import coalition_gate as cg

CG_SH = REPO / "lib" / "coalition_gate.sh"
AGENTS = str(REPO / "config" / "agents.yaml")


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    return dbp


def _seed(db, panel, lanes):
    for i, lane in enumerate(lanes):
        trace_store.trace_write(
            {"trace_id": f"tr-{i}-{panel}", "agent_version_id": lane,
             "task_class": "panel", "status": "success"}, db=db)


def _bash_verdict(db, panel):
    r = subprocess.run(
        ["bash", "-c", f'. "{CG_SH}" && mo_check_panel_coalition "$1" recipe', "_", panel],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db,
             "MO_RHO_THRESHOLD": "999", "MO_FAMILY_DIVERSITY_GATE": "strict"},
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])["verdict"], r.returncode
    except (ValueError, IndexError, KeyError):
        return None, r.returncode


def _bash_full(db, panel, *, mode="strict"):
    """Drive the LIVE lib and return (parsed_json_dict, rc). Same rho
    neutralisation as _bash_verdict; returns the whole payload so callers can
    assert the rho-independent .reason / .family_count fields the verdict-only
    helper drops. `mode` maps to MO_FAMILY_DIVERSITY_GATE (strict|advisory)."""
    r = subprocess.run(
        ["bash", "-c", f'. "{CG_SH}" && mo_check_panel_coalition "$1" recipe', "_", panel],
        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db,
             "MO_RHO_THRESHOLD": "999", "MO_FAMILY_DIVERSITY_GATE": mode},
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.returncode
    except (ValueError, IndexError):
        return None, r.returncode


def test_family_of_and_distribution(db):
    assert cg.family_of("minimax") == "minimax"
    assert cg.family_of("opus") == "anthropic"
    assert cg.family_of("codex_lens") == "openai"
    _seed(db, "P1", ["minimax", "minimax", "codex"])
    d = cg.family_distribution(db, "P1", AGENTS)
    assert d["lens_count"] == 3 and d["family_count"] == 2


def test_collision_aborts_parity(db):
    _seed(db, "PC", ["minimax", "minimax"])  # same family -> collision
    v_py, rc_py = cg.check_panel_coalition("PC", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    v_bash, rc_bash = _bash_verdict(db, "PC")
    assert v_py["verdict"] == "COALITION_ABORT" and rc_py == 1
    assert v_bash == "COALITION_ABORT" and rc_bash == 1


def test_diverse_ok_parity(db):
    _seed(db, "PD", ["minimax", "codex"])  # two families -> diverse
    v_py, rc_py = cg.check_panel_coalition("PD", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    v_bash, rc_bash = _bash_verdict(db, "PD")
    assert v_py["verdict"] == "panel_diverse" and rc_py == 0
    assert v_bash == "panel_diverse" and rc_bash == 0


def test_diverse_reason_and_family_count_parity(db):
    # .sh group A: 4 distinct families (glm/kimi/codex/minimax -> zhipu/moonshot/
    # openai/minimax) -> panel_diverse, reason=ok, family_count=4. Subsumes .sh
    # A2 (reason=ok) + A3 (family_count=4), which verdict-only _bash_verdict drops.
    _seed(db, "PDIV", ["glm", "kimi", "codex", "minimax"])
    v_py, rc_py = cg.check_panel_coalition("PDIV", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    v_bash, rc_bash = _bash_full(db, "PDIV")
    assert v_bash is not None, "live bash emitted no parseable JSON verdict"
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"] == "ok"
    assert v_py["family_count"] == 4 and rc_py == 0
    assert v_bash["verdict"] == "panel_diverse" and v_bash["reason"] == "ok"
    assert v_bash["family_count"] == 4 and rc_bash == 0


def test_collision_reason_and_family_count_parity(db):
    # .sh group B: 4 same-family lenses (sonnet/opus/sonnet/opus -> all anthropic)
    # -> COALITION_ABORT, family_count=1. Subsumes .sh B2 (family_count=1) + B3
    # (reason in {both, family_collision}). Under neutralised rho, high_rho is
    # always False, so reason is deterministically "family_collision" — the "both"
    # alternative only arises from an unneutralised *measured* rho (out of scope;
    # the .sh accepted family_collision as valid).
    _seed(db, "PCOL", ["sonnet", "opus", "sonnet", "opus"])
    v_py, rc_py = cg.check_panel_coalition("PCOL", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    v_bash, rc_bash = _bash_full(db, "PCOL")
    assert v_bash is not None, "live bash emitted no parseable JSON verdict"
    assert v_py["verdict"] == "COALITION_ABORT" and v_py["reason"] == "family_collision"
    assert v_py["family_count"] == 1 and rc_py == 1
    assert v_bash["verdict"] == "COALITION_ABORT" and v_bash["reason"] == "family_collision"
    assert v_bash["family_count"] == 1 and rc_bash == 1


def test_advisory_bypass_parity(db):
    # .sh group C: MO_FAMILY_DIVERSITY_GATE=advisory turns a family-collision abort
    # into panel_diverse with reason=advisory_*. Subsumes .sh C1 (verdict=
    # panel_diverse under advisory) + C2 (reason starts with advisory_).
    _seed(db, "PADV", ["sonnet", "opus", "sonnet", "opus"])  # collision
    v_py, rc_py = cg.check_panel_coalition("PADV", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0,
                                           mode="advisory")
    v_bash, rc_bash = _bash_full(db, "PADV", mode="advisory")
    assert v_bash is not None, "live bash emitted no parseable JSON verdict"
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"].startswith("advisory_")
    assert rc_py == 0
    assert v_bash["verdict"] == "panel_diverse" and v_bash["reason"].startswith("advisory_")
    assert rc_bash == 0


def test_single_agent_run_parity(db):
    # .sh group D: a single trace (fewer than 2 lenses) short-circuits to
    # panel_diverse / reason=single_agent_run before family/rho computation — a
    # distinct early-return branch neither existing parity case exercised.
    # Subsumes .sh D1 (verdict=panel_diverse) + D2 (reason=single_agent_run).
    _seed(db, "PSING", ["sonnet"])
    v_py, rc_py = cg.check_panel_coalition("PSING", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    v_bash, rc_bash = _bash_full(db, "PSING")
    assert v_bash is not None, "live bash emitted no parseable JSON verdict"
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"] == "single_agent_run"
    assert rc_py == 0
    assert v_bash["verdict"] == "panel_diverse" and v_bash["reason"] == "single_agent_run"
    assert rc_bash == 0
