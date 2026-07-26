"""Standalone contract tests for the Python-owned public CLI.

The pre-retirement run captured Bash parity before the dispatcher body was
removed. These tests preserve that verified surface as explicit golden values;
they never read or execute a legacy Bash implementation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import main as cli

BIN = REPO / "bin" / "mini-ork"


def _launcher(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = {key: value for key, value in os.environ.items() if key not in {
        "MINI_ORK_ENGINE_ROOT", "MINI_ORK_PROJECT_HOME", "MINI_ORK_TARGET_REPO",
        "MINI_ORK_ROOT", "MINI_ORK_HOME", "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID",
        "GLM_API_KEY", "KIMI_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
    }}
    return subprocess.run(
        [str(BIN), *args],
        capture_output=True,
        text=True,
        env={**clean_env, **(env or {})},
        check=False,
    )


def test_launcher_is_executable_python_only_and_symlink_safe(tmp_path):
    source = BIN.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env python3\n")
    assert os.access(BIN, os.X_OK)
    assert "runtime-select" not in source
    assert "MINI_ORK_RUNTIME" not in source
    assert "mini_ork.cli.main import main" in source

    link = tmp_path / "mini-ork"
    link.symlink_to(BIN)
    run = subprocess.run([str(link), "version"], capture_output=True, text=True, check=False)
    assert run.returncode == 0
    assert run.stdout == "mini-ork 0.6.0 (universal task loop runtime)\n"

    project = tmp_path / "project"
    home = project / ".mini-ork"
    home.mkdir(parents=True)
    (home / "engine").write_text(os.path.relpath(REPO, home) + "\n", encoding="utf-8")
    pointer_run = subprocess.run(
        [str(link), "doctor"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key not in {
            "MINI_ORK_ENGINE_ROOT", "MINI_ORK_PROJECT_HOME", "MINI_ORK_TARGET_REPO",
            "MINI_ORK_ROOT", "MINI_ORK_HOME",
        }},
    )
    assert pointer_run.returncode == 0
    assert f"MINI_ORK_HOME={home.resolve()}" in pointer_run.stdout


def test_version_help_and_unknown_golden_contract():
    version = _launcher("version")
    assert (version.returncode, version.stdout, version.stderr) == (
        0,
        "mini-ork 0.6.0 (universal task loop runtime)\n",
        "",
    )

    help_run = _launcher("help")
    assert help_run.returncode == 0
    assert help_run.stdout == cli._HELP
    assert help_run.stderr == ""

    unknown = _launcher("bogus")
    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert unknown.stderr == "Unknown subcommand: bogus. Try: mini-ork help\n"


def test_doctor_golden_sections():
    raw_home = "/tmp/mini-ork-doctor-home"
    run = _launcher("doctor", env={"MINI_ORK_HOME": raw_home})
    assert run.returncode == 0
    assert run.stdout.startswith("=== mini-ork doctor ===\n")
    assert "\nLib presence:\n" in run.stdout
    assert "\nProvider preflight:\n" in run.stdout
    assert f"  [OK]      MINI_ORK_HOME={Path(raw_home).resolve()}\n" in run.stdout
    assert "  [WARN]    glm ($GLM_API_KEY unset; run: mini-ork providers configure glm)\n" in run.stdout


def test_deadline_validation_golden_contract(capsys):
    assert cli.main(["run", "--deadline", "abc", "k.md"], root=str(REPO)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "--deadline: seconds must be a positive integer (got 'abc')\n"

    assert cli.main(["run", "--deadline"], root=str(REPO)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "--deadline requires <seconds>\n"


def test_closed_commands_route_to_native_modules_and_execute_stays_live(monkeypatch):
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    execute_calls: list[tuple[list[str], str]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("env")))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    for command in ("classify", "plan", "verify", "reflect"):
        assert cli.main([command, "arg"], root=str(REPO)) == 0
        assert calls[-1][0] == [
            sys.executable,
            "-m",
            f"mini_ork.cli.{command}",
            "arg",
        ]

    from mini_ork.cli import execute as mini_ork_execute

    def _fake_execute(argv, *, root=None, dispatch_fn=None):
        del dispatch_fn
        execute_calls.append((list(argv), root))
        return 0

    monkeypatch.setattr(mini_ork_execute, "main", _fake_execute)
    assert cli.main(["execute", "arg"], root=str(REPO)) == 0
    assert execute_calls == [(["arg"], str(REPO))]


def test_apply_remains_a_public_sibling_command(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["apply", "--help"], root=str(REPO)) == 0
    # apply is a closed fork: it dispatches to the native Python module
    # (mini_ork/cli/apply.py), not the retired bin/mini-ork-apply trampoline.
    assert calls == [[sys.executable, "-m", "mini_ork.cli.apply", "--help"]]


def _recipes(tmp_path: Path, mapping: dict[str, str | None]) -> Path:
    root = tmp_path / "root"
    (root / "recipes").mkdir(parents=True)
    for dirname, task_class in mapping.items():
        recipe = root / "recipes" / dirname
        recipe.mkdir()
        if task_class is not None:
            (recipe / "task_class.yaml").write_text(f"name: {task_class}\n", encoding="utf-8")
    return root


def test_resolve_recipe_golden_values(tmp_path):
    root = _recipes(
        tmp_path,
        {
            "code-fix": "code_fix",
            "db-migration": "db_migration",
            "empty-dir": None,
            "ui-audit": "ui_audit",
        },
    )
    assert cli.resolve_recipe(str(root), "code_fix") == "code-fix"
    assert cli.resolve_recipe(str(root), "db_migration") == "db-migration"
    assert cli.resolve_recipe(str(root), "ui_audit") == "ui-audit"
    assert cli.resolve_recipe(str(root), "does_not_exist") == ""
    assert cli.resolve_recipe(str(root), "empty_dir") == "empty-dir"


def test_gen_profile_golden_contract(tmp_path, monkeypatch):
    root = _recipes(tmp_path, {"code-fix": "code_fix"})
    (root / "recipes" / "code-fix" / "artifact_contract.yaml").write_text(
        "outputs:\n  - dist/widget.js\n",
        encoding="utf-8",
    )
    agents = root / "agents.yaml"
    agents.write_text("lanes:\n  implementer: codex\n", encoding="utf-8")
    kickoff = tmp_path / "k.md"
    kickoff.write_text(
        """# Ship the widget

## Success
- widget renders
- tests pass

## In scope
- src/widget.py

## Verification commands
- `pytest tests/widget`
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    data = cli.gen_profile(
        kickoff,
        root,
        "code-fix",
        "code_fix",
        profile,
        agents,
    )

    persisted = json.loads(profile.read_text(encoding="utf-8"))
    assert persisted == data
    assert data["schema_version"] == "1.0"
    assert data["recipe"] == "code-fix"
    assert data["task_class"] == "code_fix"
    assert data["user_goal"] == "Ship the widget"
    assert data["success_criteria"] == ["widget renders", "tests pass"]
    assert data["scope_allow"] == ["src/widget.py"]
    assert data["verification_command"] == ["pytest tests/widget"]
    assert data["artifact_destination"] == ["dist/widget.js"]
    assert data["profile_status"] == "ready"
    assert data["human_questions"] == []
