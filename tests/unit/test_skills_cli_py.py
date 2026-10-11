"""``mini-ork skills`` — the operator surface over the skill registry.

Covered: list (json + tiers), show, validate (hollow entries, missing
descriptions), and promote — the TraceOtter last mile, where a mined
procedure becomes a grantable directory.
"""

from __future__ import annotations

import json

from mini_ork.cli.skills import main


def _make_skill(skills_dir, name: str, description: str = "does a thing") -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "root"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    return home, root


def test_list_reports_tiers_and_descriptions(tmp_path, monkeypatch, capsys):
    home, root = _env(tmp_path, monkeypatch)
    _make_skill(home / "config" / "skills", "operator-skill")
    _make_skill(root / ".agents" / "skills", "project-skill")

    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    by_name = {e["name"]: e for e in payload}
    assert by_name["operator-skill"]["source"] == "operator"
    assert by_name["project-skill"]["source"] == "project"
    assert by_name["operator-skill"]["description"] == "does a thing"


def test_show_prints_the_skill_body(tmp_path, monkeypatch, capsys):
    home, _ = _env(tmp_path, monkeypatch)
    _make_skill(home / "config" / "skills", "shown")
    assert main(["show", "shown"]) == 0
    assert "# shown" in capsys.readouterr().out


def test_show_fails_for_an_unknown_skill(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    assert main(["show", "ghost"]) == 1
    assert "no skill named" in capsys.readouterr().err


def test_validate_fails_on_a_hollow_entry_and_a_bare_description(
    tmp_path, monkeypatch, capsys
):
    home, _ = _env(tmp_path, monkeypatch)
    skills_dir = home / "config" / "skills"
    (skills_dir / "hollow").mkdir(parents=True)
    bare = skills_dir / "bare"
    bare.mkdir(parents=True)
    (bare / "SKILL.md").write_text(
        "---\nname: bare\ndescription:\n---\nbody\n", encoding="utf-8"
    )

    assert main(["validate"]) == 1
    err = capsys.readouterr().err
    assert "hollow" in err
    assert "bare" in err


def test_validate_passes_a_clean_registry(tmp_path, monkeypatch, capsys):
    home, _ = _env(tmp_path, monkeypatch)
    _make_skill(home / "config" / "skills", "fine")
    assert main(["validate"]) == 0
    assert "registry ok" in capsys.readouterr().out


def _mined(tmp_path) -> None:
    (tmp_path / "home" / "traceotter").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "traceotter" / "skills.json").write_text(
        json.dumps(
            [
                {
                    "skillId": "skl-1",
                    "title": "Verify Before Publish",
                    "trigger": "run the verifier before the publisher",
                    "procedure": ["run verifier", "read the sidecar"],
                    "verification": ["sidecar pass=true"],
                    "sourceEpisodeIds": ["ep-1", "ep-2"],
                    "confidence": 0.8,
                    "status": "candidate",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_promote_writes_a_grantable_skill(tmp_path, monkeypatch, capsys):
    home, _ = _env(tmp_path, monkeypatch)
    _mined(tmp_path)

    assert main(["promote", "skl-1"]) == 0
    skill_dir = home / "config" / "skills" / "verify-before-publish"
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "name: verify-before-publish" in body
    assert "run the verifier before the publisher" in body
    assert "1. run verifier" in body
    assert "ep-1" in body

    # The promoted skill is immediately grantable.
    from mini_ork.dispatch.skill_registry import load_skills

    assert "verify-before-publish" in load_skills()


def test_promote_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    home, _ = _env(tmp_path, monkeypatch)
    _mined(tmp_path)
    assert main(["promote", "skl-1"]) == 0
    assert main(["promote", "skl-1"]) == 1
    assert "--force" in capsys.readouterr().err
    assert main(["promote", "skl-1", "--force"]) == 0


def test_promote_root_target_lands_in_agents_skills(tmp_path, monkeypatch, capsys):
    _, root = _env(tmp_path, monkeypatch)
    _mined(tmp_path)
    assert main(["promote", "verify-before-publish", "--to", "root"]) == 0
    assert (root / ".agents" / "skills" / "verify-before-publish" / "SKILL.md").is_file()


def test_promote_without_traceotter_output_fails_cleanly(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    assert main(["promote", "skl-1"]) == 1
    assert "no TraceOtter skills" in capsys.readouterr().err
