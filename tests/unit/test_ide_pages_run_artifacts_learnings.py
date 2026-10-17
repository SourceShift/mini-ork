"""Run page: the Artifacts and Learnings tabs' IDE-spec-2 additions.

Covers kickoff ``run-page/rp-e10-artifacts-learnings.md``:

- the Artifacts tab leads with a full-width "Key artifacts" section, in the
  kickoff's stated order, then one table per producing node plus "Run files";
- the whole tab stays at or under a 200-file-row cap, the overflow collapsing
  into one final "+N more in the run folder" row that reveals the run dir;
- every produced/injected lesson row with an id gets a confirmed Forget button
  and an "Open in Learn" link;
- injected lessons carry a used/unused mark read off the injection ledger, or
  the section says usage was not recorded.

Hermetic: each test builds its own home, run dir and (where needed) an empty
``state.db``. No network, no real credentials, no run-local state it did not
create itself.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages.run import Node, Run, _artifacts_tab, _learnings_tab

CONFIRM = "Forget lesson {lid}? Future runs stop receiving it."
LESSONS = (("c-111", "Keep diffs small"), ("c-222", "Add tests beside fixes"),
           ("th-901", "Ship in two tiers"))


@pytest.fixture(autouse=True)
def _ide_spec_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kickoff renders these pages with MINI_ORK_IDE_SPEC=2."""
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")


def _run(tmp_path: Path, nodes: tuple[str, ...] = ("implementer", "reviewer", "planner"),
         run_id: str = "run-1792047347-00001") -> Run:
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (home / "state.db").parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE _bootstrap (x)")
    con.commit()
    con.close()
    return Run(id=run_id, home=home, run_dir=run_dir, row={}, card={},
               nodes=[Node(id=n, type=n) for n in nodes], cols=[], calls=[],
               recipe_dir=None, workspace=None)


def _write(run_dir: Path, name: str, text: str = "x") -> Path:
    p = run_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _rows(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    key = "rows" if section.get("type") == "table" else "items"
    return [r for r in (section.get(key) or []) if isinstance(r, dict)]


def _all_rows(tab: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for section in tab for r in _rows(section)]


def _by_title(tab: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["title"]: s for s in tab if isinstance(s, dict)}


def _do_path(row: dict[str, Any]) -> str | None:
    do = row.get("do")
    if not isinstance(do, dict):
        return None
    for key in ("path", "reveal"):
        if do.get(key) is not None:
            return str(do[key])
    return None


def _row_json(row: dict[str, Any]) -> str:
    return json.dumps(row)


def _mark(row: dict[str, Any]) -> str | None:
    """The row's used/unused marking, word-boundary precise ("unused" ≠ "used")."""
    blob = _row_json(row)
    for word in ("unused", "not used", "used"):
        if re.search(rf"\b{word}\b", blob):
            return "unused" if word != "used" else "used"
    return None


def _forget_buttons(row: dict[str, Any], lesson_id: str) -> list[dict[str, Any]]:
    out = []
    for act in row.get("acts") or []:
        do = act.get("do") if isinstance(act, dict) else None
        if isinstance(do, dict) and [str(x) for x in do.get("cli") or []] == [
                "lessons", "forget", lesson_id]:
            out.append((act, do))
    return out


def _lesson_row_for(tab: list[dict[str, Any]], text: str) -> dict[str, Any]:
    hits = [r for r in _all_rows(tab) if text in _row_json(r)]
    assert hits, f"no lesson row mentioning {text!r}"
    return hits[0]


def _v2_pack(run: Run) -> None:
    run.run_dir.joinpath("context-pack.v2.json").write_text(json.dumps({
        "constraints": [{"kind": "rule", "id": LESSONS[0][0], "text": LESSONS[0][1]},
                        {"kind": "rule", "id": LESSONS[1][0], "text": LESSONS[1][1]}],
        "themes": [{"text": LESSONS[2][1], "id": LESSONS[2][0]}],
    }), encoding="utf-8")


def _ledger(run: Run, node: str, ids: list[str]) -> None:
    """``learned/<node>.json`` in the shape ``context_v2.write_injection_record``
    writes: each source carries the pack item id the node was given."""
    learned = run.run_dir / "learned"
    learned.mkdir(exist_ok=True)
    (learned / f"{node}.json").write_text(json.dumps({
        "node_id": node, "injected": bool(ids), "reason": "",
        "sources": [{"kind": "context_v2", "id": i} for i in ids], "written_at": 1,
    }), encoding="utf-8")


# ── artifacts: key section, grouping, cap ──────────────────────────────────

def test_key_artifacts_come_first_in_the_stated_order(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run.run_dir, "review-diff.patch", "--- a\n+++ b\n")
    _write(run.run_dir, "verdict.json", '{"verdict": "pass"}')
    _write(run.run_dir, "rubric.json", '{"scale": 5}')
    _write(run.run_dir, "evidence/notes.txt", "seen")
    _write(run.run_dir, "execute.log", "ran\n")
    _write(run.run_dir, "impl-implementer.json", "{}")

    tab = _artifacts_tab(run)
    first = tab[0]
    assert first["title"] == "Key artifacts" and first["full"] is True

    order = [Path(_do_path(r)).name for r in _rows(first) if _do_path(r)]
    # review-verdict.json and retry-hint.json do not exist, so they are absent;
    # review-diff.patch wins the single diff slot.
    assert order == ["review-diff.patch", "verdict.json", "rubric.json",
                     "evidence", "execute.log"]
    # Only the files a reviewer opens first — impl-implementer.json is not one.
    assert "impl-implementer.json" not in order

    verdict_row = next(r for r in _rows(first)
                       if (_do_path(r) or "").endswith("verdict.json"))
    assert verdict_row["do"] == {"path": str(run.run_dir / "verdict.json")}
    assert f"{run.run_dir.joinpath('verdict.json').stat().st_size} B" in _row_json(verdict_row)


def test_diff_slot_prefers_review_diff_patch(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run.run_dir, "framework-edit.diff", "--- a\n+++ b\n")
    _write(run.run_dir, "review-diff.patch", "--- a\n+++ b\n")

    names = [Path(_do_path(r)).name for r in _rows(_artifacts_tab(run)[0]) if _do_path(r)]
    assert names.count("review-diff.patch") == 1
    assert "framework-edit.diff" not in names


def test_one_table_per_producing_node_then_run_files(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run.run_dir, "impl-implementer.json", "{}")
    _write(run.run_dir, "review-reviewer.jsonl", "{}\n")
    _write(run.run_dir, "verdict.json", "{}")
    _write(run.run_dir, "execute.log", "ran\n")

    by_title = _by_title(_artifacts_tab(run))
    assert set(by_title) == {"Key artifacts", "From implementer", "From reviewer", "Run files"}

    def names(title: str) -> list[str]:
        return [Path(_do_path(r)).name for r in _rows(by_title[title]) if _do_path(r)]

    assert names("From implementer") == ["impl-implementer.json"]
    assert names("From reviewer") == ["review-reviewer.jsonl"]
    assert names("Run files") == ["verdict.json", "execute.log"]
    for title in ("From implementer", "From reviewer", "Run files"):
        assert by_title[title]["head"] == ["file", "kind", "size", "from"]
    assert by_title["From implementer"]["full"] is True


def test_over_the_row_cap_collapses_into_one_more_row(tmp_path: Path) -> None:
    run = _run(tmp_path)
    for i in range(205):  # 205 run-level files + 1 node-attributed file
        _write(run.run_dir, f"f-{i:03d}.txt")
    _write(run.run_dir, "impl-implementer.json", "{}")

    tab = _artifacts_tab(run)
    rows = _all_rows(tab)
    more = [r for r in rows if "more in the run folder" in _row_json(r)]
    assert len(more) == 1
    assert more[0]["cells"][0]["t"] == "+6 more in the run folder"  # 206 - 200 drawn
    assert more[0]["do"] == {"reveal": str(run.run_dir)}

    # The collapse row closes the last table it appears in, and exactly the cap
    # number of file rows is drawn.
    owner = next(s for s in tab if more[0] in _rows(s))
    assert _rows(owner)[-1] is more[0]
    drawn = [r for r in rows if r is not more[0] and _do_path(r)
             and Path(_do_path(r)) != run.run_dir]
    assert len(drawn) == 200

    # The node-attributed file is still its own table, and did not eat the cap.
    assert _by_title(tab)["From implementer"]["rows"][0]["do"] == {
        "path": str(run.run_dir / "impl-implementer.json")}


def test_under_the_cap_draws_no_more_row(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write(run.run_dir, "verdict.json", "{}")
    _write(run.run_dir, "impl-implementer.json", "{}")

    assert not [r for r in _all_rows(_artifacts_tab(run))
                if "more in the run folder" in _row_json(r)]


def test_level_one_keeps_the_single_flat_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An older IDE (no MINI_ORK_IDE_SPEC) still gets today's one table."""
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    run = _run(tmp_path)
    _write(run.run_dir, "impl-implementer.json", "{}")
    _write(run.run_dir, "verdict.json", "{}")

    tab = _artifacts_tab(run)
    assert [s["title"] for s in tab] == ["Artifacts"]
    assert tab[0]["type"] == "table"
    assert [Path(_do_path(r)).name for r in _rows(tab[0])] == [
        "verdict.json", "impl-implementer.json"]


def test_artifacts_tab_absorbs_an_attribution_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path)
    _write(run.run_dir, "impl-implementer.json", "{}")

    def boom(rel: str, node_ids: set[str]) -> str:
        raise RuntimeError("attribution exploded")

    monkeypatch.setattr(run_page, "_from", boom)
    tab = _artifacts_tab(run)
    assert tab and any(s.get("title") == "Artifacts" for s in tab)


# ── learnings: Forget, Open in Learn, usage marking ────────────────────────

def test_injected_lesson_rows_get_forget_and_learn(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _v2_pack(run)
    _ledger(run, "planner", [LESSONS[0][0]])  # ledger present: usage IS recorded

    tab = _learnings_tab(run)
    for lid, text in LESSONS:
        row = _lesson_row_for(tab, text)
        buttons = _forget_buttons(row, lid)
        assert len(buttons) == 1, f"{lid}: {_row_json(row)}"
        act, do = buttons[0]
        assert do["confirm"] == CONFIRM.format(lid=lid)
        assert (act.get("done") or do.get("done")) == "Lesson forgotten"
        # …and every lesson row links into the Learn page's lessons tab.
        assert any((a.get("do") or {}).get("page") == "learn"
                   and (a.get("do") or {}).get("tab") == "lessons" for a in row["acts"])


def test_usage_marking_follows_the_ledger(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _v2_pack(run)
    _ledger(run, "planner", [LESSONS[0][0]])
    # The planner also CITED the theme in its plan (context_v2.acknowledgements).
    (run.run_dir / "plan.json").write_text(
        json.dumps({"context_used": [LESSONS[2][0]]}), encoding="utf-8")

    tab = _learnings_tab(run)
    assert "usage not recorded for this run" not in json.dumps(tab)
    assert _mark(_lesson_row_for(tab, LESSONS[0][1])) == "used"
    assert _mark(_lesson_row_for(tab, LESSONS[1][1])) == "unused"
    assert _mark(_lesson_row_for(tab, LESSONS[2][1])) == "used"


def test_usage_not_recorded_note_when_the_ledger_is_absent(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _v2_pack(run)  # no learned/ directory at all

    tab = _learnings_tab(run)
    assert "usage not recorded for this run" in json.dumps(tab)
    # …instead of marking rows: no used/unused word reaches any lesson row.
    for lid, text in LESSONS:
        row = _lesson_row_for(tab, text)
        assert _mark(row) is None, lid
        assert _forget_buttons(row, lid), f"{lid} lost its Forget button"


def test_produced_lesson_gets_forget_only_when_it_has_an_id(tmp_path: Path) -> None:
    run = _run(tmp_path)
    con = sqlite3.connect(run.home / "state.db")
    con.execute("CREATE TABLE gradient_records (gradient_id TEXT, signal TEXT, "
                "suggested_change TEXT, evidence TEXT, confidence REAL, target TEXT, "
                "task_class TEXT, created_at TEXT)")
    con.execute("CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY, run_id TEXT)")
    con.execute("CREATE TABLE learning_record (title TEXT, category TEXT, outcome TEXT, "
                "confidence REAL, run_id TEXT, \"rank\" INTEGER)")
    con.execute("INSERT INTO gradient_records VALUES (?,?,?,?,?,?,?,?)",
                ("gr-777", "Cut the lens fan-out under budget", "Scope cuts per lens",
                 "tr-1", 0.55, "planner", "framework_edit", "2026-10-15T01:02:03"))
    con.execute("INSERT INTO execution_traces VALUES (?,?)", ("tr-1", run.id))
    con.execute("INSERT INTO learning_record VALUES (?,?,?,?,?,?)",
                ("Scoped re-review saves a pass", "process", "verified", 0.7, run.id, 1))
    con.commit()
    con.close()

    tab = _learnings_tab(run)
    gradient_row = _lesson_row_for(tab, "Cut the lens fan-out under budget")
    _, do = _forget_buttons(gradient_row, "gr-777")[0]
    assert do["confirm"] == CONFIRM.format(lid="gr-777")

    # A produced row without a lesson/pattern id gets no Forget button at all.
    idless = _lesson_row_for(tab, "Scoped re-review saves a pass")
    assert not [a for a in idless["acts"] if a.get("label") == "Forget"]


def test_learnings_tab_absorbs_a_ledger_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path)
    _v2_pack(run)
    _ledger(run, "planner", [LESSONS[0][0]])

    def boom(run_: Run) -> list[dict[str, Any]]:
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(run_page, "_injection_ledger", boom)
    tab = _learnings_tab(run)
    # The broken ledger costs the usage facts, not the injected lesson rows.
    assert _lesson_row_for(tab, LESSONS[0][1])


def test_level_one_lesson_rows_keep_todays_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    run = _run(tmp_path)
    _v2_pack(run)
    _ledger(run, "planner", [LESSONS[0][0]])

    tab = _learnings_tab(run)
    assert not [a for r in _all_rows(tab) for a in r.get("acts") or []]
    assert "usage not recorded for this run" not in json.dumps(tab)
