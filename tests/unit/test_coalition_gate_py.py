"""Standalone unit tests for ``mini_ork.gates.coalition_gate``.

Replaces the bash-parity gate (against ``lib/coalition_gate.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer runs ``lib/coalition_gate.sh`` in a subprocess —
it asserts the port's behaviour directly. rho is neutralised
(``rho_threshold=999``) so the verdict is driven purely by family collision
— deterministic for the port's *passed* rho. Only the rho-independent
fields {verdict, reason, family_count} are asserted, never rho or the
rationale string.

Subsumes the retired tests/unit/test_coalition_gate.sh (10 assertions
across 4 behavioral groups):
  - A verdict=panel_diverse / reason=ok / family_count=4  -> test_diverse_ok
    (verdict) + test_diverse_reason_and_family_count (reason + family_count);
  - B verdict=COALITION_ABORT / family_count=1 / reason    -> test_collision_aborts
    (verdict) + test_collision_reason_and_family_count (reason + family_count);
  - C advisory -> panel_diverse / reason=advisory_*        -> test_advisory_bypass;
  - D single-agent -> panel_diverse / reason=single_agent_run -> test_single_agent_run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.gates import coalition_gate as cg  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

AGENTS = str(REPO / "config" / "agents.yaml")


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp


def _seed(db, panel, lanes):
    for i, lane in enumerate(lanes):
        trace_store.trace_write(
            {"trace_id": f"tr-{i}-{panel}", "agent_version_id": lane,
             "task_class": "panel", "status": "success"}, db=db)


def test_family_of_and_distribution(db):
    assert cg.family_of("minimax") == "minimax"
    assert cg.family_of("opus") == "anthropic"
    assert cg.family_of("codex_lens") == "openai"
    _seed(db, "P1", ["minimax", "minimax", "codex"])
    d = cg.family_distribution(db, "P1", AGENTS)
    assert d["lens_count"] == 3 and d["family_count"] == 2


def test_collision_aborts(db):
    _seed(db, "PC", ["minimax", "minimax"])  # same family -> collision
    v_py, rc_py = cg.check_panel_coalition("PC", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    assert v_py["verdict"] == "COALITION_ABORT" and rc_py == 1


def test_diverse_ok(db):
    _seed(db, "PD", ["minimax", "codex"])  # two families -> diverse
    v_py, rc_py = cg.check_panel_coalition("PD", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    assert v_py["verdict"] == "panel_diverse" and rc_py == 0


def test_diverse_reason_and_family_count(db):
    # .sh group A: 4 distinct families (glm/kimi/codex/minimax -> zhipu/moonshot/
    # openai/minimax) -> panel_diverse, reason=ok, family_count=4.
    _seed(db, "PDIV", ["glm", "kimi", "codex", "minimax"])
    v_py, rc_py = cg.check_panel_coalition("PDIV", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"] == "ok"
    assert v_py["family_count"] == 4 and rc_py == 0


def test_collision_reason_and_family_count(db):
    # .sh group B: 4 same-family lenses (sonnet/opus/sonnet/opus -> all anthropic)
    # -> COALITION_ABORT, family_count=1. Under neutralised rho, high_rho is
    # always False, so reason is deterministically "family_collision".
    _seed(db, "PCOL", ["sonnet", "opus", "sonnet", "opus"])
    v_py, rc_py = cg.check_panel_coalition("PCOL", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    assert v_py["verdict"] == "COALITION_ABORT" and v_py["reason"] == "family_collision"
    assert v_py["family_count"] == 1 and rc_py == 1


def test_advisory_bypass(db):
    # .sh group C: advisory mode turns a family-collision abort
    # into panel_diverse with reason=advisory_*.
    _seed(db, "PADV", ["sonnet", "opus", "sonnet", "opus"])  # collision
    v_py, rc_py = cg.check_panel_coalition("PADV", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0,
                                           mode="advisory")
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"].startswith("advisory_")
    assert rc_py == 0


def test_single_agent_run(db):
    # .sh group D: a single trace (fewer than 2 lenses) short-circuits to
    # panel_diverse / reason=single_agent_run before family/rho computation.
    _seed(db, "PSING", ["sonnet"])
    v_py, rc_py = cg.check_panel_coalition("PSING", "recipe", rho=0.0, db=db,
                                           agents_yaml=AGENTS, rho_threshold=999.0)
    assert v_py["verdict"] == "panel_diverse" and v_py["reason"] == "single_agent_run"
    assert rc_py == 0
