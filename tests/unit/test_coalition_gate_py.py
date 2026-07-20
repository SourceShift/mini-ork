"""Parity gate: mini_ork.gates.coalition_gate vs lib/coalition_gate.sh.

rho is neutralised (MO_RHO_THRESHOLD=999) so the verdict is driven purely by
family collision — deterministic across bash's measured rho and the Python port.
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
