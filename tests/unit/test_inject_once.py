"""Each learned lesson reaches a prompt exactly once (LF-R3).

Two defects contaminated the prompts and the efficacy data:

- ``execute._learned_block`` returned both the v2 block and the v1
  ``failure_modes_md`` block in the v2 arm, and both render the verified
  themes — so every lesson was injected twice into the same node prompt.
- ``plan._inject_context`` ran the v1 producers (and their UCB retrieval
  logging) before it knew the v2 block, then dropped their output — crediting
  retrievals the planner never saw.

Everything here is hermetic: a throwaway SQLite store and run directory under
``tmp_path``, the local hash embedder. The live DB under ``.mini-ork/`` is
never opened for write and no network is touched.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from mini_ork import context_assembler, context_v2
from mini_ork.cli import execute
from mini_ork.cli import plan as plan_mod

TASK_CLASS = "unit_inject_once"
TASK_CLASS_EMPTY = "unit_empty_pack"

THEME_ID_1 = "unitheme1"
THEME_ID_2 = "unitheme2"
THEME_LESSON_1 = "ZQXunit theme one 4b1d: quote the kickoff clause you check"
THEME_LESSON_2 = "ZQXunit theme two 8c2f: fail closed on a missing value"

PATTERN_LESSON_1 = "ZQXunit approved pattern 55aa: pin the ledger row you claim"
PATTERN_LESSON_2 = "ZQXunit approved pattern 77bb: render the v1 block when the pack is empty"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS lesson_themes (
         theme_id TEXT NOT NULL, lesson_text TEXT,
         n_runs INTEGER NOT NULL DEFAULT 0, task_class TEXT,
         status TEXT, kind TEXT, last_seen TEXT)""",
    """CREATE TABLE IF NOT EXISTS emergent_patterns (
         pattern_id INTEGER PRIMARY KEY, cluster_label TEXT,
         feature_set_json TEXT, strength_score REAL, lesson_text TEXT,
         task_class TEXT, status TEXT, detected_at TEXT)""",
    # The DDL mini_ork/memory/semantic.py bootstraps, so the retrieval ledger
    # can be counted on a store the semantic channel never ran against.
    """CREATE TABLE IF NOT EXISTS semantic_memory (
         id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
         text TEXT NOT NULL, embedding BLOB NOT NULL, created_at REAL NOT NULL,
         meta TEXT, uses INTEGER NOT NULL DEFAULT 0,
         wins INTEGER NOT NULL DEFAULT 0, retired_at REAL NOT NULL DEFAULT 0,
         retire_reason TEXT NOT NULL DEFAULT '', retire_evidence TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS semantic_memory_uses (
         id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
         scope TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
         task_class TEXT NOT NULL DEFAULT '', lane TEXT NOT NULL DEFAULT '',
         node_id TEXT NOT NULL DEFAULT '', retrieved_at REAL NOT NULL,
         outcome TEXT NOT NULL DEFAULT 'pending')""",
    """CREATE TABLE IF NOT EXISTS operator_steering (
         id INTEGER PRIMARY KEY, run_id TEXT, role_target TEXT, severity TEXT,
         message TEXT, source TEXT, confidence REAL, created_at INTEGER,
         expires_at INTEGER, consumed_at INTEGER)""",
)


def _seed_db(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        for ddl in _SCHEMA:
            con.execute(ddl)
        con.executemany(
            "INSERT INTO lesson_themes (theme_id, lesson_text, n_runs, task_class,"
            " status, kind, last_seen) VALUES (?,?,?,?,?,?,?)",
            [(THEME_ID_1, THEME_LESSON_1, 4, TASK_CLASS, "verified", "task",
              "2026-10-15T00:00:00Z"),
             (THEME_ID_2, THEME_LESSON_2, 2, TASK_CLASS, "verified", "task",
              "2026-10-15T00:00:00Z")])
        con.executemany(
            "INSERT INTO emergent_patterns (pattern_id, cluster_label,"
            " feature_set_json, strength_score, lesson_text, task_class, status,"
            " detected_at) VALUES (?,?,?,?,?,?,?,?)",
            [(901, f"cluster: task_class={TASK_CLASS} status=failure",
              json.dumps(["unit-feature"]), 3.5, PATTERN_LESSON_1, TASK_CLASS,
              "approved", "2026-10-15T00:00:00Z"),
             (902, f"cluster: task_class={TASK_CLASS_EMPTY} status=failure",
              json.dumps(["unit-feature"]), 2.0, PATTERN_LESSON_2, TASK_CLASS_EMPTY,
              "approved", "2026-10-15T00:00:00Z")])
        con.commit()
    finally:
        con.close()


def _seeded_pack() -> dict:
    """The planner-shaped pack a v2-arm run would have written."""
    return {
        "version": 2,
        "mode": "on",
        "run_id": "",
        "task_class": TASK_CLASS,
        "kickoff_path": "unit/kickoff.md",
        "contract": {"files_in_scope": [], "out_of_scope": [], "do_not": [],
                     "verification": []},
        "constraints": [{"id": "c:0", "kind": "do_not",
                         "text": "Do not emit unified diffs."}],
        "file_findings": [],
        "prior_attempts": [],
        "themes": [
            {"id": f"t:{THEME_ID_1}", "text": THEME_LESSON_1, "n_runs": 4},
            {"id": f"t:{THEME_ID_2}", "text": THEME_LESSON_2, "n_runs": 2},
        ],
        "item_ids": ["c:0", f"t:{THEME_ID_1}", f"t:{THEME_ID_2}"],
    }


def _write_pack(run_dir: str, pack: dict) -> None:
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, context_v2.PACK_FILENAME), "w",
              encoding="utf-8") as fh:
        json.dump(pack, fh)


def _retrieval_rows(db_path: str, run_id: str) -> list[tuple]:
    con = sqlite3.connect(db_path)
    try:
        return list(con.execute(
            "SELECT node_id, lane, COUNT(*) FROM semantic_memory_uses"
            " WHERE run_id = ? GROUP BY node_id, lane", (run_id,)))
    finally:
        con.close()


@pytest.fixture
def env(tmp_path, monkeypatch) -> tuple[str, str]:
    """``(db_path, run_dir)``, with every reader pinned at throwaway paths."""
    db_path = str(tmp_path / "state.db")
    _seed_db(db_path)
    run_dir = str(tmp_path / "run")
    home_dir = str(tmp_path / "home")
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(home_dir, exist_ok=True)
    monkeypatch.setenv("MINI_ORK_DB", db_path)
    monkeypatch.setenv("MINI_ORK_HOME", home_dir)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "unit-run")
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    monkeypatch.setenv("MO_EMERGENT_INJECT", "1")
    monkeypatch.setenv("MO_SEMANTIC_INJECT", "1")
    for var in ("MO_TARGET_CWD", "MO_INJECT_UNVERIFIED", "MO_EMBED_PROVIDER",
                "MO_EMERGENT_INJECT_UNLESSONED", "MO_PLANNER_SHARED_CONTEXT",
                "MO_CONTEXT_V2", "MO_CONTEXT_V2_HOLDOUT"):
        monkeypatch.delenv(var, raising=False)
    return db_path, run_dir


# ── failure_modes_md include_themes ─────────────────────────────────────────

def test_include_themes_defaults_to_true_and_is_byte_identical(env):
    db = env[0]
    default = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, read_only=True)
    explicit = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, read_only=True, include_themes=True)
    assert default == explicit


def test_include_themes_false_drops_only_the_induced_section(env):
    db = env[0]
    with_themes = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, read_only=True, include_themes=True)
    without = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, read_only=True, include_themes=False)

    assert with_themes.count(THEME_LESSON_1) == 1
    assert with_themes.count(THEME_LESSON_2) == 1
    assert THEME_LESSON_1 not in without
    assert THEME_LESSON_2 not in without
    assert "--- /induced lessons ---" not in without
    # Non-theme content is untouched by the flag.
    assert without.count(PATTERN_LESSON_1) == 1

    true_lines = with_themes.splitlines()
    start = next(i for i, line in enumerate(true_lines)
                 if line.startswith("--- Lessons induced from prior runs"))
    end = next(i for i, line in enumerate(true_lines)
               if line == "--- /induced lessons ---")
    assert without.splitlines() == true_lines[:start] + true_lines[end + 1:]


# ── execute._learned_block: once per prompt, decided on the arm ─────────────

def test_v2_arm_renders_each_theme_exactly_once(env, monkeypatch):
    _db, run_dir = env
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "unit-node-v2")
    _write_pack(run_dir, _seeded_pack())

    block = execute._learned_block("", TASK_CLASS, "implementer")

    assert block.count(THEME_LESSON_1) == 1
    assert block.count(THEME_LESSON_2) == 1
    assert block.count(f"[t:{THEME_ID_1}]") == 1
    assert block.count(f"[t:{THEME_ID_2}]") == 1
    # Non-theme learned content still reaches the node once in the v2 arm.
    assert block.count(PATTERN_LESSON_1) == 1


def test_v2_arm_with_empty_pack_still_omits_the_v1_themes(env, monkeypatch):
    """The arm, not the v2 block's emptiness, decides — an empty pack is still
    the v2 arm and must not fall back to the v1 theme section."""
    _db, _run_dir = env
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "unit-node-v2-empty")

    block = execute._learned_block("", TASK_CLASS, "implementer")

    assert "Lessons induced" not in block
    assert THEME_LESSON_1 not in block
    assert THEME_LESSON_2 not in block
    assert block.count(PATTERN_LESSON_1) == 1


def test_holdout_arm_is_byte_identical_to_include_themes_true(env, monkeypatch):
    db, _run_dir = env
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "1")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "unit-node-holdout")

    node_block = execute._learned_block("", TASK_CLASS, "implementer")

    reference = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, node_type="implementer",
        include_themes=True).strip()
    assert node_block == "\n\n" + reference + "\n"
    assert node_block.count(THEME_LESSON_1) == 1
    assert node_block.count(THEME_LESSON_2) == 1


@pytest.mark.parametrize("mode", ["shadow", "off"])
def test_non_v2_arms_are_byte_identical_to_today(env, monkeypatch, mode):
    db, _run_dir = env
    monkeypatch.setenv("MO_CONTEXT_V2", mode)
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MINI_ORK_RUN_ID", f"unit-node-{mode}")

    node_block = execute._learned_block("", TASK_CLASS, "implementer")

    reference = context_assembler.failure_modes_md(
        TASK_CLASS, 5, db=db, node_type="implementer",
        include_themes=True).strip()
    assert node_block == "\n\n" + reference + "\n"
    assert node_block.count(THEME_LESSON_1) == 1
    assert node_block.count(THEME_LESSON_2) == 1


# ── plan._inject_context: no phantom retrievals ─────────────────────────────

KICKOFF_WITH_SCOPE = (
    "# Unit kickoff\n\n"
    "## Files in scope\n"
    "- `mini_ork/cli/plan.py`\n\n"
    "## Do NOT\n"
    "- Do not emit unified diffs.\n"
)

KICKOFF_PLAIN = ("A plain kickoff with no contract sections, no bullets and "
                 "no headings.")


def test_v2_planner_with_nonempty_pack_logs_no_phantom_retrievals(
        env, monkeypatch, tmp_path):
    db, run_dir = env
    run_id = "unit-planner-v2"
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(KICKOFF_WITH_SCOPE, encoding="utf-8")
    out_file = os.path.join(run_dir, "plan.json")

    prompt = plan_mod._inject_context(
        "UNIT PROMPT", str(kickoff), TASK_CLASS, db, out_file, False)

    # The v2 block carried the themes into the prompt exactly once...
    assert prompt.count(f"[t:{THEME_ID_1}]") == 1
    assert prompt.count(THEME_LESSON_1) == 1
    # ...and the v1-only pattern block was never produced.
    assert PATTERN_LESSON_1 not in prompt

    # The UCB path never ran for the planner: no node_id='' retrieval rows.
    assert [row for row in _retrieval_rows(db, run_id) if row[0] == ""] == []

    # The ledger keeps its order and shape.
    with open(os.path.join(run_dir, "learned", "planner.json"), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["sources"][0]["kind"] == "context_v2"
    assert record["skipped_blocks"]["role_pack"] == "shared_context_off"
    assert record["skipped_blocks"]["graph_context"] == "unverified"


def test_v2_planner_with_empty_pack_still_gets_the_v1_blocks(
        env, monkeypatch, tmp_path):
    db, run_dir = env
    run_id = "unit-planner-empty"
    monkeypatch.setenv("MO_CONTEXT_V2", "on")
    monkeypatch.setenv("MO_CONTEXT_V2_HOLDOUT", "0")
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    kickoff = tmp_path / "plain-kickoff.md"
    kickoff.write_text(KICKOFF_PLAIN, encoding="utf-8")
    out_file = os.path.join(run_dir, "plan.json")

    prompt = plan_mod._inject_context(
        "UNIT PROMPT", str(kickoff), TASK_CLASS_EMPTY, db, out_file, False)

    # The v1 failure-modes block reached the planner...
    assert prompt.count(PATTERN_LESSON_2) == 1
    # ...and its retrieval was honestly logged under node_id='', so the
    # zero-row assertion above is not vacuous.
    rows = _retrieval_rows(db, run_id)
    assert any(node_id == "" and n >= 1 for node_id, _lane, n in rows)

    with open(os.path.join(run_dir, "learned", "planner.json"), encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["sources"][0]["kind"] == "failure_modes"
    assert record["skipped_blocks"]["graph_context"] == "unverified"
    assert record["skipped_blocks"]["role_pack"] == "shared_context_off"


# ── pack marker ─────────────────────────────────────────────────────────────

def test_freshly_built_pack_carries_themes_once(env):
    db = env[0]
    pack = context_v2.build(
        "unit/kickoff.md", task_class=TASK_CLASS, db=db, run_id="unit-build",
        kickoff_text=("# Unit kickoff\n\n## Files in scope\n"
                      "- `mini_ork/cli/plan.py`\n\n## Verification\n- `make test`\n"))
    assert pack.get("themes"), "expected the seeded themes in the pack"
    assert pack.get("themes_once") is True
