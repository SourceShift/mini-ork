"""Skill registry: names → directories, and a per-node grant that cleans up.

The two properties worth defending are that a grant never overwrites something
a human put in the checkout, and that releasing removes only what the grant
created — a node's own output in that directory is not scaffolding to delete.
"""
from __future__ import annotations

from pathlib import Path

from mini_ork.dispatch.skill_registry import (
    SKILL_DIR_RELATIVE,
    granted_skills,
    load_skills,
    materialize_skills,
    skill_source_dirs,
    target_skills_dir,
)


def _make_skill(directory: Path, name: str) -> Path:
    skill = directory / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    return skill


# ── registry ────────────────────────────────────────────────────────────────


def test_source_dirs_follow_run_then_home_then_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "root"))

    dirs = skill_source_dirs()
    assert dirs == [
        tmp_path / "run" / "config" / "skills",
        tmp_path / "home" / "config" / "skills",
        tmp_path / "root" / ".agents" / "skills",
    ]


def test_load_skills_takes_the_first_definition_of_a_name(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "root"
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))

    _make_skill(home / "config" / "skills", "arxiv-search")
    _make_skill(root / ".agents" / "skills", "arxiv-search")
    _make_skill(root / ".agents" / "skills", "release-notes")

    skills = load_skills()
    assert sorted(skills) == ["arxiv-search", "release-notes"]
    assert skills["arxiv-search"] == home / "config" / "skills" / "arxiv-search"


def test_load_skills_ignores_files_and_missing_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skills_dir = home / "config" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "README.md").write_text("not a skill", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    assert load_skills() == {}


# ── materialize / release ───────────────────────────────────────────────────


def test_materialize_links_into_the_node_checkout(tmp_path):
    source = _make_skill(tmp_path / "registry", "arxiv-search")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    handle = materialize_skills(["arxiv-search"], checkout, registry={"arxiv-search": source})

    link = checkout / SKILL_DIR_RELATIVE / "arxiv-search"
    assert link.is_symlink()
    assert link.resolve() == source.resolve()
    assert (link / "SKILL.md").is_file()
    assert handle.created == [link]


def test_release_removes_only_what_it_created(tmp_path):
    source = _make_skill(tmp_path / "registry", "arxiv-search")
    checkout = tmp_path / "checkout"
    destination = target_skills_dir(checkout)
    human = _make_skill(destination, "hand-written")  # already there, not ours

    handle = materialize_skills(["arxiv-search"], checkout, registry={"arxiv-search": source})
    handle.release()

    assert not (destination / "arxiv-search").exists()
    assert human.is_dir()
    assert (human / "SKILL.md").is_file()


def test_release_is_idempotent(tmp_path):
    source = _make_skill(tmp_path / "registry", "s")
    handle = materialize_skills(["s"], tmp_path, registry={"s": source})
    handle.release()
    handle.release()
    assert not (tmp_path / SKILL_DIR_RELATIVE / "s").exists()


def test_release_leaves_a_directory_the_node_created(tmp_path):
    # The node replaced our link with real output; that is a result, not litter.
    source = _make_skill(tmp_path / "registry", "s")
    handle = materialize_skills(["s"], tmp_path, registry={"s": source})
    link = tmp_path / SKILL_DIR_RELATIVE / "s"
    link.unlink()
    link.mkdir()
    (link / "output.txt").write_text("made by the node", encoding="utf-8")

    handle.release()

    assert (link / "output.txt").is_file()


def test_an_existing_skill_is_never_overwritten(tmp_path, capsys):
    source = _make_skill(tmp_path / "registry", "arxiv-search")
    destination = target_skills_dir(tmp_path)
    human = _make_skill(destination, "arxiv-search")

    handle = materialize_skills(["arxiv-search"], tmp_path, registry={"arxiv-search": source})

    assert handle.created == []
    assert handle.skipped == ["arxiv-search"]
    assert not human.is_symlink()
    assert (human / "SKILL.md").is_file()
    assert "already exists" in capsys.readouterr().err


def test_an_unknown_skill_is_reported_not_fatal(tmp_path, capsys):
    handle = materialize_skills(["ghost"], tmp_path, registry={})
    assert handle.missing == ["ghost"]
    assert handle.created == []
    assert "no skill by that name" in capsys.readouterr().err


def test_an_empty_grant_creates_nothing(tmp_path):
    handle = materialize_skills([], tmp_path, registry={})
    assert handle.created == []
    assert not target_skills_dir(tmp_path).exists()


def test_materialize_can_be_used_as_a_context_manager(tmp_path):
    source = _make_skill(tmp_path / "registry", "s")
    with materialize_skills(["s"], tmp_path, registry={"s": source}):
        assert (tmp_path / SKILL_DIR_RELATIVE / "s").is_symlink()
    assert not (tmp_path / SKILL_DIR_RELATIVE / "s").exists()


def test_registry_defaults_to_the_configured_sources(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _make_skill(home / "config" / "skills", "from-registry")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)

    handle = materialize_skills(["from-registry"], tmp_path / "checkout")
    assert (tmp_path / "checkout" / SKILL_DIR_RELATIVE / "from-registry").is_symlink()
    handle.release()


# ── reading a compiled workflow ─────────────────────────────────────────────


class _Node:
    def __init__(self, skills):
        self.skills = skills


class _Workflow:
    def __init__(self, nodes):
        self.nodes = nodes


def test_granted_skills_reads_the_declaration():
    workflow = _Workflow({"reviewer": _Node(("arxiv-search", "style"))})
    assert granted_skills(workflow, "reviewer") == ("arxiv-search", "style")


def test_granted_skills_is_empty_for_an_undeclared_node():
    assert granted_skills(_Workflow({}), "missing") == ()
    assert granted_skills(None, "anything") == ()


def test_granted_skills_accepts_a_comma_separated_string():
    workflow = _Workflow({"n": _Node("a, b ,c")})
    assert granted_skills(workflow, "n") == ("a", "b", "c")


def test_granted_skills_handles_none():
    assert granted_skills(_Workflow({"n": _Node(None)}), "n") == ()
