from pathlib import Path

from mini_ork.cli.execute_handlers import resolve_prompt_file


def _write(path: Path, content: str = "prompt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_override_wins_over_same_basename_recipe_prompt(tmp_path, monkeypatch):
    recipe_prompt = _write(
        tmp_path / "recipes" / "demo" / "prompts" / "implementer.md"
    )
    override_prompt = _write(tmp_path / "overrides" / recipe_prompt.name, "override")
    monkeypatch.setenv("MINI_ORK_PROMPT_OVERRIDE_DIR", str(override_prompt.parent))

    resolved = resolve_prompt_file(
        str(tmp_path), "demo", "prompts/implementer.md", "implementer"
    )

    assert resolved == str(override_prompt)


def test_unset_override_env_keeps_recipe_prompt_ref_resolution(
    tmp_path, monkeypatch
):
    recipe_prompt = _write(tmp_path / "recipes" / "demo" / "custom" / "research.md")
    monkeypatch.delenv("MINI_ORK_PROMPT_OVERRIDE_DIR", raising=False)

    resolved = resolve_prompt_file(
        str(tmp_path), "demo", "custom/research.md", "researcher"
    )

    assert resolved == str(recipe_prompt)


def test_missing_override_basename_falls_back_to_recipe_prompt(
    tmp_path, monkeypatch
):
    recipe_prompt = _write(
        tmp_path / "recipes" / "demo" / "prompts" / "reviewer.md"
    )
    override_dir = tmp_path / "overrides"
    override_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_PROMPT_OVERRIDE_DIR", str(override_dir))

    resolved = resolve_prompt_file(
        str(tmp_path), "demo", "prompts/reviewer.md", "reviewer"
    )

    assert resolved == str(recipe_prompt)


def test_empty_override_env_behaves_as_unset(tmp_path, monkeypatch):
    recipe_prompt = _write(
        tmp_path / "recipes" / "demo" / "prompts" / "researcher.md"
    )
    monkeypatch.setenv("MINI_ORK_PROMPT_OVERRIDE_DIR", "")

    resolved = resolve_prompt_file(
        str(tmp_path), "demo", "prompts/researcher.md", "researcher"
    )

    assert resolved == str(recipe_prompt)


def test_empty_prompt_ref_uses_root_node_type_fallback(tmp_path, monkeypatch):
    fallback_prompt = _write(tmp_path / "prompts" / "implementer.md")
    monkeypatch.setenv("MINI_ORK_PROMPT_OVERRIDE_DIR", str(tmp_path / "overrides"))

    resolved = resolve_prompt_file(str(tmp_path), "", "", "implementer")

    assert resolved == str(fallback_prompt)
