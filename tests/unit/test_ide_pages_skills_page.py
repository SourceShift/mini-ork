"""``board page skills`` — the registry, a skill's body, and mined procedures."""
from __future__ import annotations

import json
import os
from pathlib import Path

from mini_ork.ide_pages import PAGES, build_page, skills

from test_ide_pages_orch import check_page


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "proj" / ".mini-ork"
    home.mkdir(parents=True)
    return home


def _skill(base: Path, name: str, description: str | None) -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    front = "---\n"
    if description is not None:
        front += f"name: {name}\ndescription: {description}\n"
    front += "---\n\n# Body\n"
    (directory / "SKILL.md").write_text(front, encoding="utf-8")
    return directory


def test_registered_in_pages() -> None:
    assert PAGES.get("skills") == "Skills"


def test_empty_registry_still_names_where_it_looked(tmp_path: Path) -> None:
    home = _home(tmp_path)
    page = build_page(home, "skills")
    check_page(page, "skills")
    assert [s["title"] for s in page["sections"]] == ["Registry", "Sources"]
    assert page["sections"][0]["rows"] == []
    sources = [item["t"] for item in page["sections"][1]["items"]]
    # The two registries the page can see: the home's own and the project's.
    assert sources == [str(home / "config" / "skills"), str(home.parent / ".agents" / "skills")]


def test_tier_and_description_come_from_the_sources(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / "config" / "skills", "operator-one", "the operator's skill")
    _skill(home.parent / ".agents" / "skills", "project-one", "the project's skill")

    page = build_page(home, "skills")
    check_page(page, "skills")
    rows = page["sections"][0]["rows"]
    by_name = {row["cells"][0]["t"]: row for row in rows}
    assert set(by_name) == {"operator-one", "project-one"}
    assert by_name["operator-one"]["cells"][1]["t"] == "operator"
    assert by_name["project-one"]["cells"][1]["t"] == "project"
    assert by_name["project-one"]["cells"][2]["t"] == "the project's skill"
    # A row opens the body on this same page, keyed by name.
    assert by_name["operator-one"]["do"] == {"page": "skills", "args": {"name": "operator-one"}}


def test_first_registry_wins_a_shared_name(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / "config" / "skills", "shared", "operator version")
    _skill(home.parent / ".agents" / "skills", "shared", "project version")
    page = build_page(home, "skills")
    rows = page["sections"][0]["rows"]
    assert len(rows) == 1
    assert rows[0]["cells"][1]["t"] == "operator"
    assert rows[0]["cells"][2]["t"] == "operator version"


def test_page_pins_the_registry_to_its_own_home(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    _skill(home.parent / ".agents" / "skills", "mine", "here")
    elsewhere = tmp_path / "elsewhere"
    _skill(elsewhere / "config" / "skills", "theirs", "another project")
    monkeypatch.setenv("MINI_ORK_HOME", str(elsewhere))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(elsewhere))

    page = build_page(home, "skills")
    names = [row["cells"][0]["t"] for row in page["sections"][0]["rows"]]
    assert names == ["mine"]


def test_missing_frontmatter_is_reported_not_crashed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / "config" / "skills", "no-description", None)
    (home / "config" / "skills" / "not-a-skill").mkdir()

    page = build_page(home, "skills")
    check_page(page, "skills")
    row = page["sections"][0]["rows"][0]
    assert row["cells"][0]["t"] == "no-description"
    assert row["cells"][2]["t"] == "—"
    assert "1 directory without a SKILL.md" in page["errors"]["Registry"]


def test_name_arg_renders_the_body(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / "config" / "skills", "reader", "read me")

    page = build_page(home, "skills", args={"name": "reader"})
    check_page(page, "skills")
    assert [s["title"] for s in page["sections"]] == [
        "Registry", "Sources", "reader · SKILL.md",
    ]
    body = page["sections"][2]
    assert body["type"] == "markdown" and "# Body" in body["text"]
    assert body["path"].endswith("reader/SKILL.md")


def test_unknown_name_says_so_instead_of_blanking_the_page(tmp_path: Path) -> None:
    home = _home(tmp_path)
    page = build_page(home, "skills", args={"name": "ghost"})
    check_page(page, "skills")
    read = [s for s in page["sections"] if s["title"] == "Read"]
    assert read and read[0]["items"][0]["t"].startswith("No skill named")


def test_mined_procedures_offer_promotion_into_the_project_registry(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "traceotter").mkdir(parents=True)
    (home / "traceotter" / "skills.json").write_text(json.dumps([
        {"skillId": "mined-1", "title": "Retry a flaky gate",
         "confidence": 0.82, "status": "candidate"},
        {"skillId": "", "title": "no id — not promotable"},
    ]), encoding="utf-8")

    page = build_page(home, "skills")
    check_page(page, "skills")
    mined = [s for s in page["sections"] if s["title"] == "Mined, not installed"]
    assert len(mined) == 1
    rows = mined[0]["rows"]
    assert len(rows) == 1
    assert [c["t"] for c in rows[0]["cells"]] == [
        "Retry a flaky gate", "mined-1", "0.82", "candidate",
    ]
    do = rows[0]["acts"][0]["do"]
    assert do["cli"] == ["skills", "promote", "mined-1", "--to", "root"]
    # ``skills`` has no ``--home`` flag: the IDE must not append one.
    assert do["home"] is False
    assert do["confirm"]


def test_unknown_tab_falls_back(tmp_path: Path) -> None:
    home = _home(tmp_path)
    page = skills.build(home, "nope", {})
    assert page["tab"] == "registry" and page["sections"]


def test_env_is_not_leaked_into_the_registry_lookup(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "gone"))
    env = skills._env(home)
    assert env["MINI_ORK_HOME"] == str(home)
    assert env["MINI_ORK_ROOT"] == str(home.parent)
    assert "MINI_ORK_RUN_DIR" not in env
    assert env.get("PATH") == os.environ.get("PATH")
