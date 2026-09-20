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
import tomllib
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import main as cli

BIN = REPO / "bin" / "mini-ork"
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _version_output() -> str:
    return f"mini-ork {VERSION} (universal task loop runtime)\n"


def _launcher(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = {key: value for key, value in os.environ.items() if key not in {
        "MINI_ORK_ENGINE_ROOT", "MINI_ORK_PROJECT_HOME", "MINI_ORK_TARGET_REPO",
        "MINI_ORK_ROOT", "MINI_ORK_HOME", "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID",
        "GLM_API_KEY", "KIMI_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
    }}
    return subprocess.run(
        # Pin the interpreter running the suite: the launcher's shebang
        # resolves `python3` from PATH, which on dev machines may be a bare
        # pyenv python without mini-ork's deps (ModuleNotFoundError: yaml).
        # These tests assert CLI logic and golden output, not shebang luck.
        [sys.executable, str(BIN), *args],
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
    # sys.executable prefix (see _launcher): __file__ still resolves through
    # the symlink, so the symlink-safety property stays under test.
    run = subprocess.run([sys.executable, str(link), "version"], capture_output=True, text=True, check=False)
    assert run.returncode == 0
    assert run.stdout == _version_output()

    project = tmp_path / "project"
    home = project / ".mini-ork"
    home.mkdir(parents=True)
    (home / "engine").write_text(os.path.relpath(REPO, home) + "\n", encoding="utf-8")
    pointer_run = subprocess.run(
        [sys.executable, str(link), "doctor"],
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
        _version_output(),
        "",
    )

    help_run = _launcher("help")
    assert help_run.returncode == 0
    assert help_run.stdout == cli._HELP
    assert help_run.stderr == ""
    assert "Provider credentials:\n" in help_run.stdout
    assert "providers status <lane>" in help_run.stdout
    assert "providers configure <lane>" in help_run.stdout
    assert "providers configure --workflow <path>" in help_run.stdout
    assert "keys never use CLI flags" in help_run.stdout

    unknown = _launcher("bogus")
    assert unknown.returncode == 2
    assert unknown.stdout == ""
    assert unknown.stderr == "Unknown subcommand: bogus. Try: mini-ork help\n"


def test_doctor_golden_sections():
    raw_home = "/tmp/mini-ork-doctor-home"
    run = _launcher("doctor", env={"MINI_ORK_HOME": raw_home})
    assert run.returncode == 0
    assert run.stdout.startswith("=== mini-ork doctor ===\n")
    assert "\nLib presence:\n" not in run.stdout
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


def test_fresh_run_pins_run_dir_over_leaked_ambient(monkeypatch, tmp_path):
    """A stale MINI_ORK_RUN_DIR inherited from a long-lived parent (e.g. a
    book-generation worker whose env leaked it) must not hijack a fresh run:
    classify/plan write to home/runs/<id>, so execute's node artifacts must land
    there too — else the caller reads runs/<id>/verified-artifact.json from a dir
    nothing ever wrote (the live W9_scaffold_sections ENOENT)."""
    from mini_ork.context import context_env

    stale = tmp_path / "leaked-run-dir"
    stale.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(stale))  # leaked from the parent
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-pin-regression")
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)

    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\n", encoding="utf-8")

    real_run = cli.subprocess.run

    def fake_run(argv, **kwargs):
        if "mini_ork.cli.classify" in list(argv):
            return SimpleNamespace(
                returncode=0, stdout="task_class=code_fix\n", stderr=""
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    # gen_profile runs immediately after the run-dir pin and still inside the
    # run_context_scope: capture what the run resolves there, then stop the
    # lifecycle before it needs a provider.
    seen: dict[str, str] = {}

    class _Stop(Exception):
        pass

    def fake_profile(*_args, **_kwargs):
        seen["run_dir"] = context_env("MINI_ORK_RUN_DIR", "")
        raise _Stop()

    monkeypatch.setattr(cli, "gen_profile", fake_profile)

    try:
        cli.main(["run", "code-fix", str(kickoff)], root=str(REPO))
    except _Stop:
        pass

    expected = str(home / "runs" / "run-pin-regression")
    assert seen.get("run_dir") == expected  # pinned to the canonical run dir
    assert seen["run_dir"] != str(stale)  # leaked ambient value overridden


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


def _overlay_home(tmp_path: Path, recipe: str, files: dict[str, str] | None = None) -> Path:
    """A MINI_ORK_HOME whose recipes/<recipe>/ exists — the researcher symlinks a
    private overlay recipe there while MINI_ORK_ROOT still points at a checkout
    that lacks it (the live verified-artifact env-shadow that failed W9)."""
    home = tmp_path / "home"
    rdir = home / "recipes" / recipe
    rdir.mkdir(parents=True)
    for name, body in (files or {}).items():
        (rdir / name).write_text(body, encoding="utf-8")
    return home


def test_resolve_recipe_base_prefers_home_overlay(tmp_path, monkeypatch):
    # root has code-fix but NOT verified-artifact; the home overlay supplies it.
    root = _recipes(tmp_path, {"code-fix": "code_fix"})
    home = _overlay_home(tmp_path, "verified-artifact")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    # Pin cwd to a dir with no recipes/ so the cwd probe stays neutral (else the
    # real repo checkout pytest runs from would leak its own recipes/).
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_recipe_base(str(root), "verified-artifact") == (
        str(home), "verified-artifact")
    # a recipe present in root still resolves from root (home lacks it).
    assert cli._resolve_recipe_base(str(root), "code-fix") == (str(root), "code-fix")


def test_resolve_recipe_base_root_fallback_and_swap(tmp_path, monkeypatch):
    # Dev checkout: MINI_ORK_HOME has no recipes/ → root wins (historical byte-
    # for-byte behavior), and the '_'→'-' spelling still resolves.
    root = _recipes(tmp_path, {"db-migration": "db_migration"})
    devhome = tmp_path / "home"
    devhome.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(devhome))
    monkeypatch.chdir(tmp_path)  # neutral cwd (no recipes/) for the cwd probe
    assert cli._resolve_recipe_base(str(root), "db_migration") == (
        str(root), "db-migration")
    # unset home behaves the same as a home without recipes/.
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    assert cli._resolve_recipe_base(str(root), "db-migration") == (
        str(root), "db-migration")


def test_resolve_recipe_base_not_found_and_home_equals_root(tmp_path, monkeypatch):
    root = _recipes(tmp_path, {"code-fix": "code_fix"})
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.chdir(tmp_path)  # neutral cwd (no recipes/) for the cwd probe
    assert cli._resolve_recipe_base(str(root), "nope") == ("", "nope")
    # MINI_ORK_HOME == root must not double-count; root still wins cleanly.
    monkeypatch.setenv("MINI_ORK_HOME", str(root))
    assert cli._resolve_recipe_base(str(root), "code-fix") == (str(root), "code-fix")


def test_resolve_recipe_base_cwd_signal_when_home_clobbered(tmp_path, monkeypatch):
    # The live env-shadow: the launcher's _configure_paths prefers an inherited
    # MINI_ORK_PROJECT_HOME and overwrites MINI_ORK_HOME with it, so home points
    # back at a checkout that LACKS the overlay recipe. The invocation cwd (the
    # overlay home the consumer spawned into) is the un-clobberable signal that
    # still resolves verified-artifact. Without this, W9 fails "no recipe".
    root = _recipes(tmp_path, {"code-fix": "code_fix"})  # no verified-artifact
    clobbered = tmp_path / "clobbered-home"              # home minus the overlay
    (clobbered / "recipes").mkdir(parents=True)
    overlay = _overlay_home(tmp_path, "verified-artifact")
    monkeypatch.setenv("MINI_ORK_HOME", str(clobbered))
    monkeypatch.chdir(overlay)
    base, name = cli._resolve_recipe_base(str(root), "verified-artifact")
    assert name == "verified-artifact"
    assert os.path.realpath(base) == os.path.realpath(overlay)


def test_gen_profile_reads_assets_from_recipe_base(tmp_path, monkeypatch):
    # gen_profile must read task_class.yaml + artifact_contract.yaml from the
    # overlay base, not root — else the verified-artifact output filename is lost.
    root = tmp_path / "root"
    (root / "recipes").mkdir(parents=True)  # deliberately NO verified-artifact
    home = _overlay_home(tmp_path, "verified-artifact", {
        "task_class.yaml": "name: verified_artifact\n",
        "artifact_contract.yaml": "outputs:\n  - out/verified.json\n",
    })
    agents = root / "agents.yaml"
    agents.write_text("lanes:\n  implementer: codex\n", encoding="utf-8")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Verify\n\n## Success\n- ok\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    data = cli.gen_profile(
        kickoff, str(root), "verified-artifact", "verified_artifact",
        tmp_path / "profile.json", agents, recipe_base=str(home),
    )
    assert data["artifact_destination"] == ["out/verified.json"]


def test_gen_profile_target_repo_prefers_mo_target_cwd(tmp_path, monkeypatch):
    """A spawned child runs with cwd = an EMPTY scratch workspace, so
    ``Path.cwd()`` is not the repo under repair. ``MO_TARGET_CWD`` is the
    documented lever for where dispatched agents write, and the profile is what
    the implementer reads to find its target — the two must agree.

    Live receipt (book d0df3cdb ch2): every goal-loop child read
    ``target_repo=<empty runs/.../children/<id>/worktree>``, reported "no source
    visibility", and no-op'd, so the outer loop saw a 0-byte diff it could never
    move."""
    root = tmp_path / "root"
    root.mkdir()
    agents = root / "agents.yaml"
    agents.write_text("lanes:\n  implementer: codex\n", encoding="utf-8")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Fix\n\n## Scope\n- server/\n", encoding="utf-8")
    scratch = tmp_path / "scratch"  # the empty spawn workspace (== cwd)
    scratch.mkdir()
    real = tmp_path / "real-target"  # the repo actually under repair
    real.mkdir()
    monkeypatch.chdir(scratch)

    # Unset -> the cwd fallback (unchanged host behaviour).
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    data = cli.gen_profile(
        kickoff, str(root), "code-fix", "code_fix", tmp_path / "p1.json", agents,
    )
    assert data["target_repo"] == str(scratch.resolve())

    # Set -> the explicit lever wins over the scratch cwd.
    monkeypatch.setenv("MO_TARGET_CWD", str(real))
    data = cli.gen_profile(
        kickoff, str(root), "code-fix", "code_fix", tmp_path / "p2.json", agents,
    )
    assert data["target_repo"] == str(real)

    # A stale lever (dir gone) must not poison the profile - fall back to cwd.
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / "gone"))
    data = cli.gen_profile(
        kickoff, str(root), "code-fix", "code_fix", tmp_path / "p3.json", agents,
    )
    assert data["target_repo"] == str(scratch.resolve())


def test_recipe_root_helpers_honor_recipe_root_env(monkeypatch):
    from mini_ork.cli import execute as ex
    from mini_ork.cli import execute_handlers as eh
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", "/overlay/home")
    assert ex._recipe_root("/root") == "/overlay/home"
    assert eh._recipe_root("/root") == "/overlay/home"
    monkeypatch.delenv("MINI_ORK_RECIPE_ROOT", raising=False)
    assert ex._recipe_root("/root") == "/root"
    assert eh._recipe_root("/root") == "/root"


def test_run_threads_recipe_root_from_home_overlay(tmp_path, monkeypatch):
    """W9 regression: `run verified-artifact` resolves the recipe from the
    MINI_ORK_HOME overlay even though root=MINI_ORK_ROOT lacks it, and threads
    the winning base via MINI_ORK_RECIPE_ROOT + MINI_ORK_WORKFLOW so prompts and
    verifiers follow the recipe to where it actually lives."""
    root = tmp_path / "root"
    (root / "recipes").mkdir(parents=True)  # no verified-artifact under root
    home = _overlay_home(tmp_path, "verified-artifact", {
        "workflow.yaml": "nodes: []\n",
        "task_class.yaml": "name: verified_artifact\n",
    })
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RECIPE_ROOT", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-recipe-base")
    monkeypatch.chdir(tmp_path)  # neutral cwd; the home overlay must win regardless

    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Verify\n", encoding="utf-8")

    real_run = cli.subprocess.run

    def fake_run(argv, **kwargs):
        if "mini_ork.cli.classify" in list(argv):
            return SimpleNamespace(
                returncode=0, stdout="task_class=verified_artifact\n", stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    seen: dict[str, str] = {}

    class _Stop(Exception):
        pass

    def fake_profile(*_args, **_kwargs):
        seen["recipe_root"] = os.environ.get("MINI_ORK_RECIPE_ROOT", "")
        seen["workflow"] = os.environ.get("MINI_ORK_WORKFLOW", "")
        raise _Stop()

    monkeypatch.setattr(cli, "gen_profile", fake_profile)

    try:
        cli.main(["run", "verified-artifact", str(kickoff)], root=str(root))
    except _Stop:
        pass

    assert seen.get("recipe_root") == str(home)
    assert seen.get("workflow") == str(
        home / "recipes" / "verified-artifact" / "workflow.yaml")


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
