"""Learnings tab — the ``Proposed lessons`` section and its Promote/Reject actions.

A pattern is proposed to a run only through trace membership: one of the run's
own ``execution_traces`` ids must appear in the pattern's
``member_item_ids_json``. The section is IDE spec 2 only, and every broken
source (missing table, unreadable member JSON) costs the section, never the tab.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from mini_ork.ide_pages import build_page

# `tests/` has no `__init__.py`; load the sibling fixture module by path (same
# dance as test_ide_pages_run_learnings).
_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))
from test_ide_pages_run import RUN, _seed, home as _home  # noqa: E402

# Re-export the imported fixture under its canonical name for pytest.
home = _home


def _dict_members(*trace_ids: str) -> str:
    return json.dumps([{"item_table": "execution_traces", "item_id": t}
                       for t in trace_ids])


def _bare_members(*trace_ids: str) -> str:
    return json.dumps(list(trace_ids))


def _patterns(h: Path, rows: list[tuple]) -> None:
    """rows: (pattern_id, cluster_label, member_item_ids_json, strength, status, lesson)."""
    con = sqlite3.connect(h / "state.db")
    con.executemany(
        "INSERT INTO emergent_patterns (pattern_id, cluster_label, member_item_ids_json, "
        "feature_set_json, strength_score, status, detected_at, lesson_text) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(pid, label, members, "[]", strength, status, 0, lesson)
         for pid, label, members, strength, status, lesson in rows])
    con.commit()
    con.close()


def _titles(page: dict) -> list[str]:
    return [str(s.get("title") or "") for s in page.get("sections") or []]


def _find(page: dict, prefix: str) -> dict | None:
    for sec in page.get("sections") or []:
        if str(sec.get("title") or "").startswith(prefix):
            return sec
    return None


def _rows(sec: dict | None) -> list[dict]:
    if not sec:
        return []
    return [r for r in (sec.get("items") or []) if isinstance(r, dict)]


def _acts(sec: dict | None) -> list[dict]:
    out: list[dict] = []
    for r in _rows(sec):
        out.extend(a for a in (r.get("acts") or []) if isinstance(a, dict))
    return out


def _page(h: Path) -> dict:
    return build_page(h, "run", "learnings", {"run": RUN})


def test_member_run_shows_promote_and_reject(home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-1", "cluster: a", _dict_members("tr-x"), 7.0,
                      "proposed", "Prefer a worktree over the shared checkout")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    sec = _find(_page(home), "Proposed lessons")
    assert sec is not None, _titles(_page(home))
    assert sec["title"] == "Proposed lessons · 1"
    row = _rows(sec)[0]
    assert row["t"] == "Prefer a worktree over the shared checkout"
    assert row["sub"].startswith("pat-1 · proposed · strength")
    promote = [a for a in _acts(sec) if a.get("label") == "Promote"][0]
    do = promote["do"]
    assert do["cli"] == ["lessons", "promote", "pat-1"]
    assert do["confirm"] == "Promote lesson pat-1? Future runs receive it."
    assert do["done"] == "Lesson promoted"
    reject = [a for a in _acts(sec)
              if (a.get("do") or {}).get("cli") == ["lessons", "forget", "pat-1"]][0]
    assert reject["do"]["confirm"] == "Forget lesson pat-1? Future runs stop receiving it."
    assert reject["do"]["done"] == "Lesson forgotten"


def test_bare_trace_id_members_count_too(home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-b", "cluster: b", _bare_members("tr-x"), 5.0,
                      "proposed", "Bare id members count")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    sec = _find(_page(home), "Proposed lessons")
    assert sec is not None
    assert _rows(sec)[0]["t"] == "Bare id members count"


def test_non_member_run_gets_no_section(home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-o", "cluster: o", _dict_members("tr-other"), 5.0,
                      "proposed", "Another run's lesson")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = _page(home)
    assert _find(page, "Proposed lessons") is None, _titles(page)


def test_row_without_authored_lesson_has_no_promote(home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-n", "cluster: n", _dict_members("tr-x"), 2.0,
                      "proposed", "")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    sec = _find(_page(home), "Proposed lessons")
    assert sec is not None
    assert _rows(sec)[0]["t"] == "cluster: n"
    assert not [a for a in _acts(sec)
                if (a.get("do") or {}).get("cli") == ["lessons", "promote", "pat-n"]]
    # Reject needs no lesson text, so it stays available.
    assert [a for a in _acts(sec)
            if (a.get("do") or {}).get("cli") == ["lessons", "forget", "pat-n"]]


def test_unreadable_member_json_costs_the_section_not_the_page(
        home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-broken", "cluster: z", '{"unterminated', 4.0,
                      "proposed", "Broken members lesson")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = _page(home)
    assert page.get("ok") is True
    titles = _titles(page)
    assert "Injected into prompts" in titles and "Written back" in titles, titles
    assert _find(page, "Proposed lessons") is None
    for sec in page.get("sections") or []:
        for row in _rows(sec):
            assert "pat-broken" not in str(row.get("sub") or "")


def test_missing_pattern_table_costs_only_the_section(home: Path, monkeypatch) -> None:
    _seed(home)
    con = sqlite3.connect(home / "state.db")
    con.execute("DROP TABLE emergent_patterns")
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = _page(home)
    assert page.get("ok") is True
    titles = _titles(page)
    assert "Injected into prompts" in titles and "Written back" in titles, titles


def test_injected_used_unused_marking_unchanged(home: Path, monkeypatch) -> None:
    run_dir = _seed(home)
    _patterns(home, [("pat-1", "cluster: a", _dict_members("tr-x"), 7.0,
                      "proposed", "A proposed lesson")])
    (run_dir / "learned").mkdir()
    (run_dir / "learned" / "worker.json").write_text(json.dumps(
        {"node_id": "worker", "injected": True,
         "sources": [{"id": "c1", "kind": "law"}]}))
    (run_dir / "context-pack.v2.json").write_text(json.dumps(
        {"constraints": [{"id": "c1", "kind": "law", "text": "Be kind"},
                         {"id": "c2", "kind": "law", "text": "Be calm"}]}))
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = _page(home)
    cons = _find(page, "Constraints (injected)")
    assert cons is not None, _titles(page)
    subs = [str(r.get("sub") or "") for r in _rows(cons)]
    assert "c1 · used" in subs and "c2 · unused" in subs, subs
    assert _find(page, "Proposed lessons") is not None


def test_level_1_gets_no_section_and_no_actions(home: Path, monkeypatch) -> None:
    _seed(home)
    _patterns(home, [("pat-1", "cluster: a", _dict_members("tr-x"), 7.0,
                      "proposed", "An authored lesson")])
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "1")
    page = _page(home)
    assert _find(page, "Proposed lessons") is None, _titles(page)
    for sec in page.get("sections") or []:
        for act in _acts(sec):
            cli = (act.get("do") or {}).get("cli")
            assert cli != ["lessons", "promote", "pat-1"]
            assert cli != ["lessons", "forget", "pat-1"]
