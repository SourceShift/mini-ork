"""Parity gate: mini_ork.context_assembler vs lib/context_assembler.sh (core).

Byte-for-byte comparison of the two prompt blocks (failure modes, prior runs)
against the LIVE bash emitters, and structural equality of the ContextPack
(volatile keys assembled_at/tokens_estimated stripped) on the same seeded DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import context_assembler as ca  # noqa: E402
from mini_ork import trace_store  # noqa: E402

CA_SH = REPO / "lib" / "context_assembler.sh"


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    now = int(time.time())
    grads = [
        ("g1", "auth.middleware", "tests skipped silently", "run pytest -x", "e", 0.9, now, "code-fix"),
        ("g2", "workflow.gate", "framework-internal lesson", "fix gate", "e", 0.8, now, "code-fix"),
        ("g3", "db.migration", "cross-class lesson", "always backup", "e", 0.95, now, "__cross_class__"),
        ("g4", "lowconf.target", "below floor", "ignore", "e", 0.3, now, "code-fix"),
    ]
    con.executemany(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change,"
        " evidence, confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)", grads)
    con.commit()
    con.close()
    for i, (run, status) in enumerate([("r1", "success"), ("r1", "success"),
                                       ("r2", "failure"), ("r2", "success")]):
        trace_store.trace_write(
            {"trace_id": f"t{i}", "run_id": run, "task_class": "code-fix",
             "status": status, "cost_usd": 0.5, "duration_ms": 2000,
             "agent_version_id": "minimax"}, db=dbp)
    return dbp


def _bash_fn(db, fn, *args, extra_env=None):
    r = subprocess.run(
        ["bash", "-c", f'. "{CA_SH}" && {fn} "$@"', "_", *args],
        env={**os.environ, "MINI_ORK_DB": db, "MINI_ORK_ROOT": str(REPO),
             **(extra_env or {})},
        capture_output=True, text=True)
    return r.stdout.rstrip("\n")


def test_failure_modes_md_parity(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    py = ca.failure_modes_md("code-fix", 5, db=db)
    bash = _bash_fn(db, "context_failure_modes_md", "code-fix", "5")
    assert py == bash
    assert "auth.middleware" in py and "lowconf.target" not in py


def test_failure_modes_project_scope_filter(db, monkeypatch, tmp_path):
    # Foreign MO_TARGET_CWD strips framework-internal targets (workflow.*).
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    py = ca.failure_modes_md("code-fix", 5, db=db)
    bash = _bash_fn(db, "context_failure_modes_md", "code-fix", "5",
                    extra_env={"MO_TARGET_CWD": str(tmp_path)})
    assert py == bash
    assert "workflow.gate" not in py and "auth.middleware" in py


def test_prior_runs_md_parity(db, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    py = ca.prior_runs_md("code-fix", 5, db=db)
    bash = _bash_fn(db, "context_prior_runs_md", "code-fix", "5")
    assert py == bash
    assert "r1: success" in py and "1/2 nodes failed" in py


def test_context_assemble_parity(db, tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix", "goal": "fix auth tests"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    py_pack = ca.context_assemble(str(brief), "implementer", db=db)
    bash_out = _bash_fn(db, "context_assemble", str(brief), "implementer")
    bash_pack = json.loads(bash_out)
    for volatile in ("assembled_at", "tokens_estimated"):
        py_pack.pop(volatile, None)
        bash_pack.pop(volatile, None)
    assert py_pack == bash_pack


def _seed_emergent(db, rows):
    """rows: (pattern_id, cluster_label, features_list, strength, status)."""
    con = sqlite3.connect(db)
    now = int(time.time())
    for pid, label, feats, strength, status in rows:
        con.execute(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label, "
            "member_item_ids_json, feature_set_json, strength_score, "
            "suggested_meta_adr, status, detected_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, label, "[]", json.dumps(feats), strength,
             "meta-adr text", status, now))
    con.commit()
    con.close()


def test_verified_emergent_md_readback(db, monkeypatch):
    """failure_modes_md surfaces judge-gate 'approved' emergent patterns and
    hides 'proposed' ones; bash and python md output stay byte-identical."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    _seed_emergent(db, [
        ("emg-ok",  "empty verifier output means silent failure",
         ["verifier_addition"], 7.0, "approved"),
        ("emg-raw", "unverified confabulated self-diagnosis",
         ["adr"], 9.0, "proposed"),
    ])
    py = ca.failure_modes_md("code-fix", 5, db=db)
    bash = _bash_fn(db, "context_failure_modes_md", "code-fix", "5")
    assert py == bash
    assert "Verified emergent patterns" in py
    assert "empty verifier output means silent failure" in py
    # The 'proposed' (unverified) pattern must NOT reach the prompt.
    assert "confabulated" not in py


def test_verified_emergent_optout_and_json(db, monkeypatch):
    """MO_EMERGENT_INJECT=0 suppresses the block; JSON pack carries approved rows
    under verified_emergent_patterns with bash/python parity."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    _seed_emergent(db, [
        ("emg-ok", "cross-run lesson", ["verifier_addition"], 6.0, "approved"),
    ])
    # opt-out hides the md block on both sides
    monkeypatch.setenv("MO_EMERGENT_INJECT", "0")
    py_off = ca.failure_modes_md("code-fix", 5, db=db)
    bash_off = _bash_fn(db, "context_failure_modes_md", "code-fix", "5",
                        extra_env={"MO_EMERGENT_INJECT": "0"})
    assert py_off == bash_off
    assert "Verified emergent patterns" not in py_off
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)

    # JSON path: verified_emergent_patterns populated + parity.
    import tempfile
    brief = os.path.join(tempfile.mkdtemp(), "brief.json")
    with open(brief, "w") as f:
        f.write(json.dumps({"task_class": "code-fix", "goal": "x"}))
    pack = ca.context_assemble(brief, "implementer", db=db)
    bash_out = _bash_fn(db, "context_assemble", brief, "implementer")
    bpack = json.loads(bash_out)
    for v in ("assembled_at", "tokens_estimated"):
        pack.pop(v, None); bpack.pop(v, None)
    assert pack == bpack
    ids = [e["cite"] for e in pack["verified_emergent_patterns"]]
    assert "emergent_patterns/emg-ok" in ids


def test_truncation_budget(db, tmp_path, monkeypatch):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"task_class": "code-fix"}))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_CTX_BUDGET_TOKENS", "120")
    pack = ca.context_assemble(str(brief), "implementer", db=db)
    monkeypatch.delenv("MINI_ORK_CTX_BUDGET_TOKENS")
    assert pack.get("_truncated") is True
    assert "_truncation_summary" in pack
