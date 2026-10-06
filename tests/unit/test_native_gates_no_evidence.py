"""Regression: certifying oracle gates must not read ABSENT evidence as a pass.

`mini-ork gate-fuzz --hackability` (G09-T05, 2026-10-05) measured
oracle-coalition / oracle-liveness / oracle-stability at hackability 0.8: an
unknown panel_run_id / run_id falls into each backend's fail-open default
(lens_count 0 -> "not applicable"; run_unknown -> PROCEED; zero traces ->
CONTINUE) and the evaluator mapped that default to "pass". With no evidence the
gate must defer; real panels/runs keep their existing semantics.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import gate_registry as gr  # noqa: E402
from mini_ork.gates import hackability  # noqa: E402
from mini_ork.gates.native_gates import native_condition  # noqa: E402
from mini_ork.stores import migrate  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    db_path = str(home / "state.db")
    rc, _out, err = migrate.init_db(db_path, root=str(REPO))
    assert rc == 0, err
    con = sqlite3.connect(db_path)
    con.execute("INSERT OR IGNORE INTO runs (id, agent, final_verdict) VALUES (1, 'test', 'APPROVE')")
    con.commit()
    con.close()
    for k, v in {"MINI_ORK_DB": db_path, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home)}.items():
        monkeypatch.setenv(k, v)
    for k in ("MO_FAMILY_DIVERSITY_GATE", "MO_CB_DISABLE", "MO_PANEL_MIN_ROUNDS"):
        monkeypatch.delenv(k, raising=False)
    return db_path


def _gate(db, name):
    gid = gr.gate_register(db, "custom", native_condition(name))
    assert gid
    return gid


@pytest.mark.parametrize("name,ctx", [
    ("coalition", {"panel_run_id": "no-such-panel", "recipe": "r"}),
    ("liveness", {"run_id": "no-such-run"}),
    ("stability", {"panel_run_id": "no-such-panel", "current_round": 1}),
])
def test_unknown_id_defers_instead_of_passing(db, name, ctx):
    assert gr.gate_evaluate(db, _gate(db, name), json.dumps(ctx), mini_ork_root=str(REPO)) == "defer"


@pytest.mark.parametrize("name", ["coalition", "liveness", "stability"])
def test_hollow_exploits_no_longer_pass(db, name):
    """The production audit (`gate-fuzz --hackability` → audit_gate) finds no
    exploit once absent evidence defers."""
    rec = hackability.audit_gate(db, _gate(db, name), n=0)
    assert rec["exploits"] == [], rec
    assert rec["hackability"] in (0.0, None), rec
