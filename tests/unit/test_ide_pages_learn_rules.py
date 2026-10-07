"""IDE page ``learn`` → Rules: what agents are told, and control over it.

Seeds a temp home + DB (the verification command runs with ``MINI_ORK_HOME`` /
``MINI_ORK_DB`` unset, so the fixture must build its own) and asserts the
"Your rules" table, the "Learned rules (verified)" table and detail, the
read-only preview, the empty states, and the tab order.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli.prefs_cmd import build_preview
from mini_ork.ide_pages import build_page, learn
from mini_ork.memory import preferences
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

# Longer than any list-cell truncation, so "appears unabridged" is meaningful.
_LONG_LESSON = ("When a pattern cluster has no authored lesson, treat it as a "
                "frequency count and never inject it as guidance. ") * 6
_KICKOFF = "# Rules Preview Run\n\ntask_class: code_fix\n\n## Files in scope\n\n- `mini_ork/x.py`\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_DB", str(h / "state.db"))
    return h


def _conn(home: Path) -> sqlite3.Connection:
    return sqlite3.connect(home / "state.db")


def _seed_pref(home: Path, key: str, value: str, *, scope: str = "global",
               target: str = "") -> None:
    preferences.set_pref(key, value, scope=scope, target=target,
                         db=str(home / "state.db"))


def _seed_injection(home: Path, run_id: str, node_id: str, kind: str, source_id: str) -> None:
    con = _conn(home)
    con.execute("INSERT INTO lesson_injections (run_id, node_id, source_kind, source_id, ts) "
                "VALUES (?,?,?,?,?)", (run_id, node_id, kind, source_id, int(time.time())))
    con.commit()
    con.close()


def _seed_pattern(home: Path, pid: str, status: str, lesson: str, *,
                  members: list[dict] | None = None, strength: float = 0.9) -> None:
    con = _conn(home)
    con.execute("INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
                "feature_set_json, strength_score, status, detected_at, lesson_text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, f"cluster: {pid}", json.dumps(members or []), "[]", strength, status,
                 int(time.time()), lesson))
    con.commit()
    con.close()


def _seed_run(home: Path, run_id: str, task_class: str, kickoff: Path, *,
              created: int | None = None) -> None:
    now = int(created if created is not None else time.time())
    con = _conn(home)
    con.execute("INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, task_class, "code-fix", str(kickoff), "published", now, now))
    con.commit()
    con.close()


def _seed_trace(home: Path, trace_id: str, run_id: str) -> None:
    con = _conn(home)
    con.execute("INSERT INTO execution_traces (trace_id, run_id, task_class, status, created_at) "
                "VALUES (?,?,'code_fix','success',?)", (trace_id, run_id, int(time.time())))
    con.commit()
    con.close()


def _table_rows(home: Path, table: str) -> list[tuple]:
    """Every row of ``table`` in stable order — the before/after snapshot a
    "render wrote nothing" assertion compares."""
    con = _conn(home)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608 — test-owned table names
    finally:
        con.close()


def _ledger_outcomes(home: Path) -> list[str]:
    con = _conn(home)
    try:
        return [r[0] for r in con.execute(
            "SELECT outcome FROM semantic_memory_uses ORDER BY rowid").fetchall()]
    finally:
        con.close()


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


# ── tab wiring ──────────────────────────────────────────────────────────────

def test_tab_order_places_rules_after_your_code(home: Path) -> None:
    page = build_page(home, "learn", "rules", {})
    assert page["ok"]
    assert [(t["key"], t["label"]) for t in page["tabs"]] == [
        ("code", "Your code"), ("rules", "Rules"), ("overview", "Overview"),
        ("lessons", "Lessons"), ("memory", "Memory"), ("improve", "Self-improve")]
    assert page["tab"] == "rules"
    assert page["errors"] == {}
    json.dumps(page)


# ── Your rules ──────────────────────────────────────────────────────────────

def test_your_rules_lists_applies_to_and_remove(home: Path) -> None:
    _seed_pref(home, "rr-global", "Keep summaries short")
    _seed_pref(home, "rr-task", "Run the unit tests", scope="task_class", target="code_fix")
    _seed_pref(home, "rr-path", "Run the ide page tests", scope="path",
               target="mini_ork/ide_pages/**")
    # A ledger row: the global rule was given to a node twice this week.
    _seed_injection(home, "run-1", "implementer", "preference", "pref:global::rr-global")
    _seed_injection(home, "run-2", "implementer", "preference", "pref:global::rr-global")

    table = _section(learn.build(home, "rules", {}), "Your rules")
    assert [c["t"] for c in table["rows"][0]["cells"]][0] == "Keep summaries short"
    by_key = {r["cells"][0]["t"]: r for r in table["rows"]}
    assert by_key["Keep summaries short"]["cells"][1]["t"] == "everyone"
    assert by_key["Run the unit tests"]["cells"][1]["t"] == "task: code_fix"
    assert by_key["Run the ide page tests"]["cells"][1]["t"] == "files: mini_ork/ide_pages/**"
    # Injection counts: 2× for the given rule, "not yet" for the others.
    assert by_key["Keep summaries short"]["cells"][2]["t"].startswith("2×")
    assert by_key["Run the unit tests"]["cells"][2]["t"] == "not yet"
    # Remove action carries the exact CLI call + a confirm.
    remove = by_key["Keep summaries short"]["do"]
    assert remove["cli"] == ["prefs", "rm", "rr-global", "--scope", "global", "--target", ""]
    assert "confirm" in remove
    # "Add a rule" section action opens a thread.
    assert table["actions"][0]["do"]["thread"].startswith("I want to add a rule")


def test_your_rules_empty_state(home: Path) -> None:
    table = _section(learn.build(home, "rules", {}), "Your rules")
    assert "No rules yet" in table["rows"][0]["cells"][0]["t"]


# ── Learned rules (verified) ────────────────────────────────────────────────

def test_learned_rules_table(home: Path) -> None:
    _seed_pattern(home, "p1", "approved", "Never inject an unlesson-ed cluster",
                  members=[{"item_table": "execution_traces", "item_id": "t1"},
                           {"item_table": "execution_traces", "item_id": "t2"}])
    _seed_pattern(home, "p2", "proposed", "Still waiting for the gate")
    _seed_trace(home, "t1", "run-a")
    _seed_trace(home, "t2", "run-b")          # 2 distinct runs → "seen in 2"
    _seed_injection(home, "run-a", "reviewer", "pattern", "p1")

    table = _section(learn.build(home, "rules", {}), "Learned rules (verified)")
    assert [c["t"] for c in table["rows"][0]["cells"]] == [
        "Never inject an unlesson-ed cluster", "2", table["rows"][0]["cells"][2]["t"]]
    assert table["rows"][0]["do"] == {"set": {"rule": "p1"}}
    assert "1×" in table["rows"][0]["cells"][2]["t"]
    # The proposed row is NOT a rule; it is only counted as waiting.
    assert "1 more are waiting" in table["note"]


def test_learned_rules_empty_state(home: Path) -> None:
    section = _section(learn.build(home, "rules", {}), "Learned rules (verified)")
    assert section["type"] == "list"
    assert "No verified lessons" in section["items"][0]["t"]


def test_learned_rule_detail_shows_full_lesson_and_actions(home: Path) -> None:
    _seed_pattern(home, "p1", "approved", _LONG_LESSON,
                  members=[{"item_table": "execution_traces", "item_id": "t1"}])
    _seed_run(home, "run-a", "code_fix", home.parent / "kickoff.md")
    (home.parent / "kickoff.md").write_text(_KICKOFF, encoding="utf-8")
    _seed_trace(home, "t1", "run-a")
    _seed_injection(home, "run-a", "reviewer", "pattern", "p1")

    page = learn.build(home, "rules", {"rule": "p1"})
    detail = _section(page, "Rule · p1")
    assert detail["type"] == "markdown"
    assert _LONG_LESSON.strip() in detail["text"]      # full lesson, unabridged
    assert "Seen in 1 independent run(s)" in detail["text"]

    labels = [a["label"] for a in detail["actions"]]
    assert "Forget" in labels and "Make it mine" in labels and "Close" in labels
    by_label = {a["label"]: a for a in detail["actions"]}
    assert by_label["Forget"]["do"]["cli"] == ["lessons", "forget", "p1"]
    assert "confirm" in by_label["Forget"]["do"]
    mine = by_label["Make it mine"]["do"]
    assert mine["cli"][:2] == ["prefs", "set"]
    assert mine["cli"][2] == "learned-p1"
    assert mine["cli"][3].startswith("When a pattern cluster")
    assert mine["cli"][4:] == ["--scope", "global"]


def test_learned_rule_detail_missing(home: Path) -> None:
    section = _section(learn.build(home, "rules", {"rule": "nope"}), "Rule")
    assert "no longer exists" in section["items"][0]["t"]


# ── Preview ─────────────────────────────────────────────────────────────────

def test_preview_equals_build_preview(home: Path) -> None:
    kick = home.parent / "kickoff.md"
    kick.write_text(_KICKOFF, encoding="utf-8")
    _seed_run(home, "run-a", "code_fix", kick)
    _seed_pref(home, "rr-global", "Keep summaries short")

    page = learn.build(home, "rules", {"preview": "run-a", "node": "implementer"})
    # The run chip is marked on.
    chips = _section(page, "Pick a run")
    on = [c for c in chips["items"] if c["on"]]
    assert len(on) == 1 and on[0]["t"] == "Rules Preview Run"
    # The node chip is marked on.
    nodes = _section(page, "Pick a node")
    assert [c["t"] for c in nodes["items"]] == ["implementer", "reviewer", "researcher"]
    assert [c["t"] for c in nodes["items"] if c["on"]] == ["implementer"]

    block, _sources, _tc, _paths = build_preview(_KICKOFF, "code_fix", "implementer")
    markdown = _section(page, "implementer for: Rules Preview Run")
    assert markdown["text"] == block.strip()
    assert "Keep summaries short" in markdown["text"]


def test_preview_render_writes_nothing(home: Path) -> None:
    """A preview render must leave the store byte-identical (kickoff §3
    "Read-only: no ledger writes", §4 "The page never writes").

    The semantic channel is the write path: rendering it sweeps finished runs
    (flipping a pending retrieval to win/loss) and re-mirrors every approved
    pattern (refreshing the mirror's ``created_at``). Masking
    ``MINI_ORK_RUN_ID`` only suppresses the retrieval row; the sweep and the
    upsert both still fire. Both must be suppressed for a preview, so this test
    snapshots both tables and asserts nothing moved — and that the block still
    renders, i.e. read-only did not simply blank the channel.
    """
    from mini_ork import memory as semantic

    _seed_pattern(home, "p1", "approved", "Never inject an unlesson-ed cluster")
    mid = semantic.upsert("Never inject an unlesson-ed cluster", scope="code_fix",
                          key="p1", db_path=str(home / "state.db"))

    # A retrieval still pending for a run that has already finished: a sweep
    # would resolve it, and a preview must not be the thing that does.
    con = _conn(home)
    con.execute("INSERT INTO semantic_memory_uses "
                "(memory_id, scope, run_id, retrieved_at, outcome) VALUES (?,?,?,?,?)",
                (mid, "code_fix", "run-a", int(time.time()), "pending"))
    con.commit()
    con.close()
    _seed_trace(home, "t-done", "run-a")        # success → that run is over

    kick = home.parent / "kickoff.md"
    kick.write_text(_KICKOFF, encoding="utf-8")
    _seed_run(home, "run-a", "code_fix", kick)

    before = (_table_rows(home, "semantic_memory"),
              _table_rows(home, "semantic_memory_uses"))
    assert _ledger_outcomes(home) == ["pending"], "fixture must start pending"

    page = learn.build(home, "rules", {"preview": "run-a", "node": "implementer"})
    text = _section(page, "implementer for: Rules Preview Run")["text"]

    assert "Never inject an unlesson-ed cluster" in text, "the block still renders"
    assert (_table_rows(home, "semantic_memory"),
            _table_rows(home, "semantic_memory_uses")) == before, (
        "a preview render wrote to the store")


def test_preview_chips_skip_unreadable_kickoffs_then_cap(home: Path) -> None:
    """Chips offer only runs whose kickoff reads; the 8-cap counts those.

    A run whose ``kickoff_path`` points at a file that is gone yields an empty
    preview block, so it must not be offered — and the cap must be applied
    *after* that filter, or the newest runs would crowd out readable older ones.
    """
    from mini_ork.ide_pages.learn.rules import _PREVIEW_RUNS

    kick = home.parent / "kickoff.md"
    kick.write_text(_KICKOFF, encoding="utf-8")
    base = int(time.time())
    # Newest 3 runs point at kickoff files that do not exist.
    for i in range(3):
        _seed_run(home, f"gone-{i}", "code_fix", home.parent / f"missing-{i}.md",
                  created=base + 100 - i)
    # Older runs all have a readable kickoff — more than the 8-chip cap.
    for i in range(_PREVIEW_RUNS + 2):
        _seed_run(home, f"ok-{i}", "code_fix", kick, created=base - i)

    chips = _section(learn.build(home, "rules", {}), "Pick a run")
    ids = [c["do"]["set"]["preview"] for c in chips["items"]]
    assert all(not rid.startswith("gone-") for rid in ids), ids
    assert len(ids) == _PREVIEW_RUNS, ids      # enough readable runs to fill the cap
    assert ids[0] == "ok-0", ids               # newest readable first


def test_preview_without_a_selection_shows_hint(home: Path) -> None:
    section = _section(learn.build(home, "rules", {}), "What will an agent be told?")
    assert section["type"] == "list"
    assert "Pick a run" in section["items"][0]["t"]
