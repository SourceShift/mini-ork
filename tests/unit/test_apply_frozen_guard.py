"""The apply loop refuses a frozen target and stays inside the recipe's prompts.

Kickoff rsi-t1-kernel-hole-2-apply-guard. Hermetic temp engine root + temp
sqlite, no network. The engine root carries no ``config/kernel-frozen.yaml``, so
``enforced_frozen_paths`` falls back to ``FROZEN_PATHS_DEFAULT`` — the
``mini_ork/recovery/auto_repair.py`` fixture target is on that floor.
"""

import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

import mini_ork
import mini_ork.cli.apply as ap

REPO = Path(mini_ork.__file__).resolve().parents[1]

# Copied from tests/unit/test_cli_apply_py.py (the suite whose env contract this
# module shares), plus the operator token the apply loop must ignore.
_APPLY_ENV = [
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT",
    "MO_APPLY_ENABLED", "MO_APPLY_DRY_RUN", "MO_APPLY_SCORER",
    "MO_APPLY_MODE",
    "MO_APPLY_NONREGRESSION_DELTA", "MO_APPLY_MIN_EXAMPLES",
    "MO_APPLY_REGRESSION_TOLERANCE", "MO_APPLY_PERTASK_JSON",
    "MO_APPLY_MOCK_BASELINE", "MO_APPLY_MOCK_DELTA",
    "MO_APPLY_FORCE_REGRESSION",
    "MO_AUTO_APPLY", "MO_AUTO_APPLY_MAX_TARGETS",
    "MO_APPLY_PROBE_MAX_TASKS", "MO_APPLY_PROBE_BUDGET_USD",
    "MO_APPLY_PROBE_TIMEOUT_S",
    "MO_ALLOW_FROZEN_EDIT",
]


def _tree(root):
    """Every path under root, relative — proves no sidecar was written."""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def _select(db_path, table):
    """All rows of ``table`` as dicts; [] when the table does not exist."""
    con = sqlite3.connect(str(db_path))
    try:
        info = list(con.execute("PRAGMA table_info(%s)" % table))
        if not info:
            return []
        names = [r[1] for r in info]
        rows = list(con.execute("SELECT %s FROM %s" % (", ".join(names), table)))
        return [dict(zip(names, r)) for r in rows]
    finally:
        con.close()


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    for var in _APPLY_ENV:
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "engine"
    frozen = root / "mini_ork" / "recovery" / "auto_repair.py"
    ok = root / "recipes" / "demo" / "prompts" / "ok.md"
    frozen.parent.mkdir(parents=True)
    ok.parent.mkdir(parents=True)
    (root / "recipes" / "demo" / "verifiers").mkdir(parents=True)
    frozen.write_text("FROZEN = 1\n")
    ok.write_text("# ok\n")
    (root / "recipes" / "demo" / "verifiers" / "x.md").write_text("# x\n")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    dbp = tmp_path / "state.db"
    con = sqlite3.connect(str(dbp))
    try:
        con.executescript(
            (REPO / "db" / "migrations" / "0036_safety_events.sql").read_text()
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setenv("MINI_ORK_DB", str(dbp))
    ap._SCHEMA_INIT = False
    yield types.SimpleNamespace(
        root=root, db=dbp, frozen=frozen, ok=ok,
        recipe_dir=root / "recipes" / "demo",
    )
    ap._SCHEMA_INIT = False


# ── _frozen_target ──────────────────────────────────────────────────────────

def test_frozen_target_floor_hit(engine):
    assert ap._frozen_target(str(engine.frozen)) == [
        "mini_ork/recovery/auto_repair.py"
    ]


def test_frozen_target_nonfrozen_inside_tree(engine):
    assert ap._frozen_target(str(engine.ok)) == []


def test_frozen_target_outside_engine_tree(engine, tmp_path):
    assert ap._frozen_target(str(tmp_path / "elsewhere.md")) == []
    assert ap._frozen_target(str(tmp_path)) == []


def test_frozen_target_empty(engine):
    assert ap._frozen_target("") == []


def test_frozen_target_symlink_to_frozen(engine):
    evil = engine.recipe_dir / "prompts" / "evil.md"
    evil.symlink_to(engine.frozen)
    assert ap._frozen_target(str(evil)) == ["mini_ork/recovery/auto_repair.py"]


def test_frozen_target_hardlink_outside_tree(engine, tmp_path):
    # Review fix: a hardlink is the frozen file under another name, even
    # outside the engine tree; writing through it edits the frozen file.
    link = tmp_path / "hardlinked.py"
    os.link(engine.frozen, link)
    assert ap._frozen_target(str(link)) == ["mini_ork/recovery/auto_repair.py"]


def test_frozen_target_hardlink_inside_prompts(engine):
    link = engine.recipe_dir / "prompts" / "hl.md"
    os.link(engine.frozen, link)
    assert ap._frozen_target(str(link)) == ["mini_ork/recovery/auto_repair.py"]


def test_frozen_target_case_variant(engine):
    # Review fix: on a case-insensitive filesystem (macOS APFS) a case variant
    # names the frozen file itself; a string match alone would miss it.
    variant = engine.root / "MINI_ORK" / "Recovery" / "AUTO_REPAIR.py"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem: the variant is a different path")
    assert ap._frozen_target(str(variant)) == ["mini_ork/recovery/auto_repair.py"]


def test_frozen_target_new_file_is_not_frozen(engine):
    assert ap._frozen_target(str(engine.recipe_dir / "prompts" / "new.md")) == []


# ── _record_frozen_refusal ──────────────────────────────────────────────────

def test_record_frozen_refusal_never_raises(engine, monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert ap._record_frozen_refusal(["mini_ork/recovery/auto_repair.py"]) is False
    assert ap._record_frozen_refusal(
        ["mini_ork/recovery/auto_repair.py"],
        db=str(tmp_path / "no-such-dir" / "state.db"),
    ) is False


def test_record_frozen_refusal_emits_event(engine):
    assert ap._record_frozen_refusal(
        ["mini_ork/recovery/auto_repair.py"], candidate_id="cand-9",
        db=str(engine.db)) is True
    rows = _select(engine.db, "safety_events")
    assert len(rows) == 1
    assert rows[0]["tripwire_id"] == "frozen-kernel-edit"
    assert rows[0]["severity"] == "high"
    assert json.loads(rows[0]["evidence_json"]) == {
        "run_id": "",
        "paths": ["mini_ork/recovery/auto_repair.py"],
        "boundary": "apply",
        "candidate_id": "cand-9",
    }


# ── apply_mutation ──────────────────────────────────────────────────────────

def test_apply_mutation_refuses_frozen(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    before = _tree(engine.root)
    assert ap.apply_mutation(
        "cand-1", str(engine.frozen), "NEW-DIRECTIVE",
        db=str(engine.db)) == ""
    assert engine.frozen.read_text() == "FROZEN = 1\n"
    assert _tree(engine.root) == before
    assert _select(engine.db, "version_registry") == []


def test_apply_mutation_refusal_records_event(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    ap.apply_mutation("cand-1", str(engine.frozen), "NEW-DIRECTIVE",
                      db=str(engine.db))
    rows = _select(engine.db, "safety_events")
    assert len(rows) == 1
    ev = json.loads(rows[0]["evidence_json"])
    assert ev["candidate_id"] == "cand-1"
    assert ev["boundary"] == "apply"


def test_token_does_not_unlock(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    monkeypatch.setenv("MO_ALLOW_FROZEN_EDIT", "1")
    assert ap.apply_mutation(
        "cand-1", str(engine.frozen), "NEW-DIRECTIVE",
        db=str(engine.db)) == ""
    assert engine.frozen.read_text() == "FROZEN = 1\n"


def test_apply_mutation_symlink_target_refused(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    evil = engine.recipe_dir / "prompts" / "evil.md"
    evil.symlink_to(engine.frozen)
    assert ap.apply_mutation(
        "cand-1", str(evil), "NEW-DIRECTIVE", db=str(engine.db)) == ""
    assert engine.frozen.read_text() == "FROZEN = 1\n"


def test_ordinary_target_still_lands(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    ap.apply_mutation("cand-2", str(engine.ok), "NEW-DIRECTIVE",
                      db=str(engine.db))
    assert "NEW-DIRECTIVE" in engine.ok.read_text()
    assert _select(engine.db, "safety_events") == []


def test_no_db_table_still_refused(engine, monkeypatch, tmp_path):
    monkeypatch.setenv("MO_APPLY_ENABLED", "1")
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    monkeypatch.setenv("MINI_ORK_DB", str(empty))
    assert ap.apply_mutation(
        "cand-1", str(engine.frozen), "NEW-DIRECTIVE") == ""
    assert engine.frozen.read_text() == "FROZEN = 1\n"


# ── apply_run ───────────────────────────────────────────────────────────────

def test_apply_run_refuses_before_picking(engine):
    assert ap.apply_run(
        "demo", "prompt_file", "x", str(engine.frozen),
        db=str(engine.db)) == 0
    rows = _select(engine.db, "apply_attempts")
    assert len(rows) == 1
    assert rows[0]["decision"] == "rejected"
    assert rows[0]["source_kind"] == "none"
    assert rows[0]["rationale"].startswith(
        "frozen path: mini_ork/recovery/auto_repair.py")
    events = _select(engine.db, "safety_events")
    assert len(events) == 1
    assert events[0]["tripwire_id"] == "frozen-kernel-edit"
    assert events[0]["severity"] == "high"
    ev = json.loads(events[0]["evidence_json"])
    assert ev["boundary"] == "apply"
    assert ev["paths"] == ["mini_ork/recovery/auto_repair.py"]
    assert engine.frozen.read_text() == "FROZEN = 1\n"


def test_apply_run_refusal_stdout(engine, capsys):
    ap.apply_run("demo", "prompt_file", "x", str(engine.frozen),
                 db=str(engine.db))
    out = capsys.readouterr().out.splitlines()
    payload = [ln for ln in out if ln.startswith("{") and "frozen_path" in ln]
    assert len(payload) == 1
    assert json.loads(payload[0]) == {
        "decision": "rejected",
        "task_class": "demo",
        "target": "x",
        "reason": "frozen_path",
    }
    idx = out.index(payload[0])
    assert idx > 0
    assert out[idx - 1].strip()


def test_apply_run_refuses_even_in_dry_run(engine, monkeypatch):
    monkeypatch.setenv("MO_APPLY_DRY_RUN", "1")
    assert ap.apply_run(
        "demo", "prompt_file", "x", str(engine.frozen),
        db=str(engine.db)) == 0
    rows = _select(engine.db, "apply_attempts")
    assert [r["decision"] for r in rows] == ["rejected"]
    assert len(_select(engine.db, "safety_events")) == 1


# ── target confinement ──────────────────────────────────────────────────────

def test_inside_helper(engine):
    rd = str(engine.recipe_dir)
    prompts = os.path.join(rd, "prompts")
    assert ap._inside(os.path.join(prompts, "ok.md"), prompts)
    assert not ap._inside(os.path.join(rd, "verifiers", "x.md"), prompts)
    assert not ap._inside(rd, rd)


def test_prompt_file_for_prompts_branch_confined(engine):
    rd = str(engine.recipe_dir)
    got = ap._prompt_file_for(rd, "prompts/ok.md")
    assert got.replace(os.sep, "/").endswith("recipes/demo/prompts/ok.md")
    assert ap._prompt_file_for(rd, "prompts/../verifiers/x.md") == ""


def test_prompt_file_for_agent_branch_confined(engine):
    rd = str(engine.recipe_dir)
    got = ap._prompt_file_for(rd, "agent.ok.prompt")
    assert got.endswith("prompts/ok.md")
    assert ap._prompt_file_for(rd, "agent.../verifiers/x.prompt") == ""


def test_harness_branch_confined(engine):
    rd = engine.recipe_dir
    (engine.root / "escape.md").write_text("# escape\n")
    (rd / "workflow.yaml").write_text(
        "nodes:\n"
        "  - name: ok\n"
        "    prompt_ref: prompts/ok.md\n"
        "  - name: evil\n"
        "    prompt_ref: ../../escape.md\n"
    )
    got = ap._harness_target_file_for(str(rd), "harness.demo.ok")
    assert got.replace(os.sep, "/").endswith("recipes/demo/prompts/ok.md")
    assert (engine.root / "escape.md").exists()
    assert ap._harness_target_file_for(str(rd), "harness.demo.evil") == ""
