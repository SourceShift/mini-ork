"""The v2 run page's Changes tab (C8).

The tab reuses the dock's changes builder (``run_dock._changes_sections``)
fail-soft and offers one ``Open as patch`` action for whichever patch artefact
the run actually left on disk. The fixtures build a hermetic ``Run`` the way
the page consumes one — tmp dirs, no DB, no network — and drive
``run._build_v2`` directly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.ide_pages import run as run_page


# A parseable git-style patch so a fixture patch doubles as a real diff source.
PATCH_TEXT = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,1 +1,2 @@\n"
    " hello\n"
    "+world\n"
)


@pytest.fixture(autouse=True)
def _ide_spec_2(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")


def _mk_run(tmp_path: Path, run_id: str = "run-fx", *, diffs=None, patches=()):
    """A hermetic ``Run``: home + run_dir + a real changed file under tmp."""
    base = tmp_path / run_id
    home = base / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    src = base / "src" / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text("hello\nworld\n", encoding="utf-8")
    if diffs is not None:
        (run_dir / "acp-diffs.json").write_text(json.dumps(diffs), encoding="utf-8")
    for name in patches:
        (run_dir / name).write_text(PATCH_TEXT, encoding="utf-8")
    card = {"id": run_id, "recipe": "code-fix", "status": "done", "steps": []}
    return run_page.Run(
        id=run_id, home=home, run_dir=run_dir, row={"recipe": "code-fix"},
        card=card, nodes=[], cols=[], calls=[], recipe_dir=None, workspace=None,
    ), src


def _acp(src: Path) -> list[dict]:
    """Diff-cache entries pointing at a file that really exists."""
    return [{"path": str(src), "added": 3, "removed": 1,
             "old_text": "", "new_text": ""}]


def _page(run, tab="changes"):
    return run_page._build_v2(run, tab, {})


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _do_paths(page) -> list[str]:
    return [d["do"]["path"] for d in _walk(page)
            if isinstance(d.get("do"), dict) and isinstance(d["do"].get("path"), str)]


def _patch_actions(page) -> list[dict]:
    return [d for d in _walk(page) if str(d.get("label") or "") == "Open as patch"]


def _file_list_sections(sections) -> list[dict]:
    return [s for s in sections
            if isinstance(s, dict) and isinstance(s.get("files"), list) and s["files"]]


# ── tab bar ──────────────────────────────────────────────────────────────────

def test_changes_tab_sits_between_checks_and_kickoff():
    keys = [k for k, _ in run_page.V2_TABS]
    assert ("changes", "Changes") in run_page.V2_TABS
    assert keys.index("checks") < keys.index("changes") < keys.index("kickoff")


def test_changes_tab_entry_carries_no_badge(tmp_path):
    run, src = _mk_run(tmp_path, diffs=_acp(tmp_path / "run-fx" / "src" / "app.py"))
    page = _page(run)
    entry = next(t for t in page["tabs"] if t.get("key") == "changes")
    assert "count" not in entry and "tone" not in entry


# ── content ──────────────────────────────────────────────────────────────────

def test_changes_tab_lists_files_with_existing_open_targets(tmp_path):
    src = tmp_path / "run-fx" / "src" / "app.py"
    run, _ = _mk_run(tmp_path, diffs=_acp(src))

    page = _page(run)
    assert page["ok"] is True and page["tab"] == "changes"
    sections = _file_list_sections(page["sections"])
    assert sections, page["sections"]
    entries = [e for s in sections for e in s["files"]]
    assert any("app.py" in str(e.get("path") or "") for e in entries), entries
    targets = {str(e["abs"]) for e in entries if e.get("abs")}
    assert targets, entries
    for target in targets:
        assert Path(target).exists(), target
    for path in _do_paths(page):
        assert Path(path).exists(), path


def test_open_as_patch_prefers_framework_edit_diff(tmp_path):
    src = tmp_path / "run-fx" / "src" / "app.py"
    run, _ = _mk_run(tmp_path, diffs=_acp(src),
                     patches=("framework-edit.diff", "diff.patch", "changes.diff"))
    acts = _patch_actions(_page(run))
    assert len(acts) == 1, acts
    assert acts[0]["do"]["path"] == str(run.run_dir / "framework-edit.diff")


def test_open_as_patch_falls_back_to_diff_patch(tmp_path):
    src = tmp_path / "run-fx" / "src" / "app.py"
    run, _ = _mk_run(tmp_path, diffs=_acp(src), patches=("diff.patch",))
    acts = _patch_actions(_page(run))
    assert len(acts) == 1, acts
    assert acts[0]["do"]["path"] == str(run.run_dir / "diff.patch")


def test_open_as_patch_falls_back_to_changes_diff(tmp_path):
    src = tmp_path / "run-fx" / "src" / "app.py"
    run, _ = _mk_run(tmp_path, diffs=_acp(src), patches=("changes.diff",))
    acts = _patch_actions(_page(run))
    assert len(acts) == 1, acts
    assert acts[0]["do"]["path"] == str(run.run_dir / "changes.diff")


def test_no_diff_yields_quiet_section_and_no_patch_action(tmp_path):
    run, _ = _mk_run(tmp_path)  # no diff cache, no patch files
    page = _page(run)
    assert page["ok"] is True and page["tab"] == "changes"
    blob = json.dumps(page["sections"])
    assert "No changes recorded" in blob, blob
    assert "Could not read this" not in blob
    assert not _file_list_sections(page["sections"])
    assert _patch_actions(page) == []


# ── fail-soft ────────────────────────────────────────────────────────────────

def test_broken_dock_builder_yields_valid_tab(tmp_path, monkeypatch):
    from mini_ork.ide_pages import run_dock

    def boom(run, errors):
        raise RuntimeError("dock builder broke")

    monkeypatch.setattr(run_dock, "_changes_sections", boom)
    run, _ = _mk_run(tmp_path,
                     diffs=_acp(tmp_path / "run-fx" / "src" / "app.py"),
                     patches=("framework-edit.diff",))
    page = run_page._build_v2(run, "changes", {})
    assert page["ok"] is True and page["tab"] == "changes"
    assert page["sections"]
    assert "No changes recorded" in json.dumps(page["sections"])
    assert not _file_list_sections(page["sections"])
    for path in _do_paths(page):
        assert Path(path).exists(), path


def test_dock_builder_errors_dict_is_threaded(tmp_path, monkeypatch):
    from mini_ork.ide_pages import run_dock

    def boom(run, files):
        raise ValueError("summary broke")

    monkeypatch.setattr(run_dock, "_changes_summary", boom)
    run, _ = _mk_run(tmp_path, diffs=_acp(tmp_path / "run-fx" / "src" / "app.py"))
    page = _page(run)
    assert page["ok"] is True and page["tab"] == "changes"
    assert "Changes" in page["errors"]
    assert "Could not read this" in json.dumps(page["sections"])
    # The sibling section still rendered the changed files.
    assert _file_list_sections(page["sections"])
