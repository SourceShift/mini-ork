"""``board page lanes · past decisions`` — ContextNest capsule on the Lanes tab.

The kickoff (rank 8 — "What did I already decide for this kind of work?") asks
the Lanes tab to render ContextNest capsule decision and failure atoms from
the last 14 days, scoped to the project, each opening its session. The page
goes through :func:`mini_ork.ide_pages._cn.render_cached_section` (two cached
slots, one per query — lane and budget), and the parser tolerates bullets
with or without the ``[kind xN]`` bracket prefix.

The capsule endpoint serves text/markdown in the kickoff's verified live
shape — ``# Prompt Context``, a window line, then ``## Risks`` /
``## Decisions`` / ``## Failures`` / ``## Verifications`` sections of
``- …`` bullets. Tests use synthetic data in exactly that shape, never
invented JSON and never real transcript content.

The tests reach :func:`mini_ork.cn_client.capsule` through the same module
seam :mod:`tests.unit.test_cn_ui_coordination` uses for ``_coord_request``
— monkeypatching the symbol lets one fake serve both cached-section slots
without spinning up a real ContextNest, and keeps the test hermetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import lanes

REPO = Path(__file__).resolve().parents[2]


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    from mini_ork.stores import migrate as mig

    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _project(home: Path) -> str:
    """The project root (the parent of the .mini-ork home)."""
    return str(home.parent.resolve())


def _section(page: dict[str, Any], title: str) -> dict[str, Any]:
    for s in page["sections"]:
        if s["title"] == title:
            return s
    raise AssertionError(
        f"no section with title {title!r}; got {[s['title'] for s in page['sections']]}"
    )


def _make_capsule(body: str):
    """A fake ``cn_client.capsule`` that returns ``body`` for every query —
    the lane and budget slots read the same synthetic markdown, which is
    the right shape for these tests since the page treats both queries
    as the same kind of feed (decisions/failures bullets)."""
    def _fake(query: str = "", since: str = "14d", project: str = "") -> str:
        return body
    return _fake


def _enable_capsule(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Wire both ``capsule`` and ``available`` so the page's CN-up path runs."""
    monkeypatch.setattr("mini_ork.cn_client.capsule", _make_capsule(body))
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)


# ── section rendering ───────────────────────────────────────────────────────


def test_past_decisions_renders_decisions_and_failures_with_session(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic ``## Decisions`` and ``## Failures`` bullets in the verified
    capsule shape render as rows; the canonical run id drives ``S.open_run``
    on each row, and the ``[kind xN]`` bracket prefix is stripped
    defensively."""
    project = _project(home)
    md = (
        "# Prompt Context\n\n"
        "## Risks\n"
        f"- foreign risk at /other/repo/x.py run-1792000000-xxxx\n\n"
        "## Decisions\n"
        f"- [decision_made x1] use sqlite at {project}/mini_ork/foo.py run-1792000000-aaaa\n"
        f"- retry the cache route at {project}/mini_ork/bar.py:12 run-1792000000-bbbb\n\n"
        "## Failures\n"
        f"- [failure_recorded x2] budget cap tripped at {project}/mini_ork/baz.py run-1792000000-cccc\n\n"
        "## Verifications\n"
        f"- (none)\n"
    )
    _enable_capsule(monkeypatch, md)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert section["type"] == "table"
    rows = section["rows"]
    # The risks section is not requested; the verifications section is not a
    # decision/failure either. Only the two Decisions + one Failures row remain.
    assert len(rows) == 3
    kinds = [r["cells"][0]["t"] for r in rows]
    assert kinds == ["Decisions", "Decisions", "Failures"]
    # Each row carries S.open_run wired to the canonical run id; the bracket
    # prefix was stripped, the run id was extracted, and the path was kept.
    expected_run_ids = [
        "run-1792000000-aaaa", "run-1792000000-bbbb", "run-1792000000-cccc"]
    for row, rid in zip(rows, expected_run_ids):
        assert row["do"] == {"run": rid, "title": ""}
    # The "atom" column carries the bullet body with the bracket prefix
    # stripped off.
    assert "use sqlite at" in rows[0]["cells"][1]["t"]
    assert "[decision_made" not in rows[0]["cells"][1]["t"]
    # ``file:line`` paths are kept; the trailing ``:12`` is treated as text.
    assert "bar.py:12" in rows[1]["cells"][2]["t"]


def test_past_decisions_drops_out_of_project_and_pathless_bullets(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bullets without a recognizable path and bullets whose path sits outside
    the project are both dropped, mirroring
    :func:`mini_ork.cn_client.filter_by_project_path`'s fail-closed rule:
    items with no path inside the project must not leak in."""
    project = _project(home)
    md = (
        "# Prompt Context\n\n"
        "## Decisions\n"
        f"- in-project: {project}/mini_ork/ok.py run-1792000000-aaaa\n"
        f"- foreign: /other/repo/x.py run-1792000000-bbbb\n"
        "- pathless: just a decision run-1792000000-cccc\n"
        f"- in-project trailing slash: {project}/mini_ork/also.py run-1792000000-dddd\n"
    )
    _enable_capsule(monkeypatch, md)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]
    # Only the two in-project bullets survive; the foreign and the pathless
    # are dropped (the pathless one would render with a no-action row, but
    # the kickoff's fail-closed rule requires it be filtered out).
    assert len(rows) == 2
    run_ids = {r["do"]["run"] for r in rows}
    assert run_ids == {"run-1792000000-aaaa", "run-1792000000-dddd"}


def test_past_decisions_section_renders_one_liner_when_cn_down(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN down → the section renders the established one-line state from
    :func:`mini_ork.ide_pages._cn.section_status`, never silently empty."""
    def _fake_capsule(query: str = "", since: str = "14d", project: str = "") -> str:
        raise ConnectionError(f"down for {query}")
    monkeypatch.setattr("mini_ork.cn_client.capsule", _fake_capsule)
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: False)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    # The degraded path returns a list with the established one-line state.
    assert section["type"] == "list"
    items = section["items"]
    assert len(items) == 1
    item = items[0]
    # "ContextNest" + the "not running" text are the rendered pieces.
    assert "ContextNest" in (item.get("label") or "")
    assert "not running" in (item.get("text") or "").lower() or \
           "not running" in (item.get("t") or "").lower()


def test_past_decisions_section_renders_disabled_line_under_mo_disable_cn(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_DISABLE_CN=1`` short-circuits and renders the disabled line."""
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    try:
        page = lanes.build(home, "lanes", {})
        section = _section(page, "Past decisions")
        assert section["type"] == "list"
        item = section["items"][0]
        # The disabled label and the established MO_DISABLE_CN=1 hint ride
        # the same dot — ``label="ContextNest"``, ``text="off · MO_DISABLE_CN=1"``.
        assert item.get("label") == "ContextNest"
        assert "off" in item.get("text", "").lower()
        assert "MO_DISABLE_CN=1" in item.get("text", "")
    finally:
        monkeypatch.delenv("MO_DISABLE_CN", raising=False)


def test_past_decisions_section_is_only_on_lanes_tab(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kickoff asks for one 'Past decisions' section on the default
    'lanes' tab — not on routing, not on cost."""
    page_lanes = lanes.build(home, "lanes", {})
    page_routing = lanes.build(home, "routing", {})
    page_cost = lanes.build(home, "cost", {})

    assert "Past decisions" in [s["title"] for s in page_lanes["sections"]]
    assert "Past decisions" not in [s["title"] for s in page_routing["sections"]]
    assert "Past decisions" not in [s["title"] for s in page_cost["sections"]]


def test_past_decisions_empty_state_lists_dot_when_cn_up_no_items(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN up with no decisions/failures for this project: a 'No decisions
    recorded' dot replaces the table — never a silently empty table."""
    md = (
        "# Prompt Context\n\n"
        "## Decisions\n\n"
        "## Failures\n\n"
    )
    _enable_capsule(monkeypatch, md)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    assert section["type"] == "list"
    assert any("No decisions recorded" in i.get("t", "") for i in section["items"])


def test_past_decisions_section_does_not_break_page(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when ContextNest raises mid-render, the page still builds with
    ``ok: true`` and the existing sections are untouched. The kickoff
    relies on the page continuing to build for the test_ide_pages_actions
    action sweep."""
    def _fake_capsule(query: str = "", since: str = "14d", project: str = "") -> str:
        raise RuntimeError("kapow")
    monkeypatch.setattr("mini_ork.cn_client.capsule", _fake_capsule)
    monkeypatch.setattr("mini_ork.cn_client.available", lambda: True)

    page = lanes.build(home, "lanes", {})
    assert page["ok"] is True
    titles = [s["title"] for s in page["sections"]]
    assert "Lanes" in titles
    assert "Past decisions" in titles
    assert "Credentials" in titles
    assert "Bring your own provider" in titles


def test_past_decisions_section_uses_only_existing_action_kinds(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows' ``do`` actions are only :func:`S.open_run` (or ``None``);
    no new action kind is invented for the section. The
    test_ide_pages_actions action sweep walks every action on every page,
    so an unknown action kind would break it."""
    project = _project(home)
    md = (
        "# Prompt Context\n\n"
        "## Decisions\n"
        f"- decision at {project}/mini_ork/foo.py run-1792000000-aaaa\n"
        f"- decision without run id at {project}/mini_ork/bar.py\n"
    )
    _enable_capsule(monkeypatch, md)

    page = lanes.build(home, "lanes", {})
    section = _section(page, "Past decisions")
    rows = section["rows"]
    assert len(rows) == 2
    # First row has the run id — the action is S.open_run with that id.
    assert isinstance(rows[0]["do"], dict)
    assert "run" in rows[0]["do"]
    assert rows[0]["do"]["run"] == "run-1792000000-aaaa"
    # Second row has no run id — the action is None (a plain row).
    assert rows[1]["do"] is None


# ── parser unit tests (defensive coverage of the capsule bullet grammar) ──


def test_parse_capsule_atoms_strips_bracket_and_extracts_run_id_and_path() -> None:
    """The bullet parser strips ``[kind xN]`` prefixes, extracts the
    canonical run id, and finds a recognizable path even when a
    ``file:line`` suffix follows."""
    from mini_ork.ide_pages.lanes import _parse_capsule_atoms

    md = (
        "## Decisions\n"
        "- [decision_made x3] keep foo at /ps/mini-ork/foo.py run-1792000000-aaaa\n"
        "- retry /ps/mini-ork/bar.py:12 run-1792000000-bbbb\n"
    )
    atoms = _parse_capsule_atoms(md)
    assert len(atoms) == 2
    assert atoms[0]["kind"] == "Decisions"
    assert atoms[0]["run_id"] == "run-1792000000-aaaa"
    assert atoms[0]["path"] == "/ps/mini-ork/foo.py"
    # The bracket prefix was stripped from the text body.
    assert atoms[0]["text"].startswith("keep foo at")
    # Path detection tolerates a trailing ``:line`` suffix.
    assert atoms[1]["path"] == "/ps/mini-ork/bar.py:12"
    assert atoms[1]["run_id"] == "run-1792000000-bbbb"


def test_parse_capsule_atoms_ignores_other_sections() -> None:
    """Only ``## Decisions`` and ``## Failures`` produce atoms; ``## Risks``
    and ``## Verifications`` are silently skipped."""
    from mini_ork.ide_pages.lanes import _parse_capsule_atoms

    md = (
        "## Risks\n- risk /ps/mini-ork/r.py run-1792000000-aaaa\n"
        "## Decisions\n- decision /ps/mini-ork/d.py run-1792000000-bbbb\n"
        "## Failures\n- failure /ps/mini-ork/f.py run-1792000000-cccc\n"
        "## Verifications\n- verification /ps/mini-ork/v.py run-1792000000-dddd\n"
    )
    atoms = _parse_capsule_atoms(md)
    kinds = [a["kind"] for a in atoms]
    assert kinds == ["Decisions", "Failures"]


def test_parse_capsule_atoms_handles_missing_fields_defensively() -> None:
    """A bullet with no path, no run id, and no bracket prefix still
    parses without raising — the kickoff's fail-closed rule (project
    filter) drops it from the rendered list, but the parser must not
    throw on the way through."""
    from mini_ork.ide_pages.lanes import _parse_capsule_atoms

    md = "## Decisions\n- plain bullet with no path and no run id\n"
    atoms = _parse_capsule_atoms(md)
    assert len(atoms) == 1
    assert atoms[0]["path"] == ""
    assert atoms[0]["run_id"] == ""