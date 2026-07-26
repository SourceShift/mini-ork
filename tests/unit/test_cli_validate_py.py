"""Unit + native-integration tests for ``mini_ork.cli.validate``.

The unit tests drive the ported checks against tmp fixtures (kickoff files,
recipe manifests, agents.yaml, providers.yaml) and assert the exact
stderr/stdout/exit-code contract of the retired ``bin/mini-ork-validate``.
The integration tests prove ``mini-ork validate`` is dispatched natively
(registered in ``_NATIVE_SUBS``, absent from ``_EXEC_SUBS``) and that
``python3 -m mini_ork.cli.validate --help`` exits 0 without any
``bin/mini-ork-*`` entrypoint.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.cli import main as cli
from mini_ork.cli import validate

REPO = Path(__file__).resolve().parents[2]


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated (root, home) wired through the env contract."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    (root / "recipes").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    home.mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text("lanes: {}\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MO_MAX_KICKOFF_BYTES", raising=False)
    return root, home


def _recipe(root: Path, name: str, *, workflow: str = "nodes: []\n",
            contract: str = "outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n") -> Path:
    d = root / "recipes" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task_class.yaml").write_text("name: demo\n", encoding="utf-8")
    (d / "workflow.yaml").write_text(workflow, encoding="utf-8")
    (d / "artifact_contract.yaml").write_text(contract, encoding="utf-8")
    return d


# ── usage / arg parsing ───────────────────────────────────────────────────────

def test_help_to_stdout(capsys):
    assert validate.main(["--help"]) == 0
    assert capsys.readouterr().out == validate.HELP_TEXT
    assert validate.main(["-h"]) == 0
    assert capsys.readouterr().out == validate.HELP_TEXT


def test_usage_errors(capsys):
    assert validate.main(["--nope"]) == 2
    assert capsys.readouterr().err == f"Unknown flag: --nope\n{validate.HELP_TEXT}"
    assert validate.main(["a.md", "b.md"]) == 2
    assert capsys.readouterr().err == f"Unexpected argument: b.md\n{validate.HELP_TEXT}"
    # bash aborts under set -u; the port reports usage (documented divergence).
    assert validate.main(["--recipe"]) == 2
    assert capsys.readouterr().err == validate.HELP_TEXT


# ── kickoff checks ────────────────────────────────────────────────────────────

def test_kickoff_without_goal_heading_warns(env, tmp_path, capsys):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Ship it\n\nsome prose\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert (f"[warning] kickoff {kickoff} has no recognizable Goal/Done-When heading\n"
            f"          Fix: add a '## Goal' and '## Done When' section to {kickoff}\n"
            ) in captured.err
    assert captured.err.endswith("validate: 0 error(s), 1 warning(s)\n")


@pytest.mark.parametrize("heading", ["## Goal", "# Done When", "### Done-When",
                                     "## Definition of Done", "## acceptance criteria"])
def test_kickoff_recognized_headings(env, tmp_path, capsys, heading):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text(f"{heading}\n- thing\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"
    assert captured.err == ""


def test_kickoff_size_cap(env, tmp_path, capsys, monkeypatch):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("## Goal\n" + "x" * 64 + "\n", encoding="utf-8")
    monkeypatch.setenv("MO_MAX_KICKOFF_BYTES", "10")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    size = kickoff.stat().st_size
    assert (f"[error]   kickoff {kickoff} is {size} bytes (cap: 10)\n"
            "          Fix: split into single-deliverable kickoffs; "
            "move detail to kickoffs/references/\n") in captured.err
    assert captured.err.endswith("validate: 1 error(s), 0 warning(s)\n")


def test_missing_kickoff_file_is_silently_skipped(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    # bash: all kickoff checks are guarded by [ -f "$KICKOFF" ].
    assert validate.main(["no/such/kickoff.md", "--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


# ── recipe manifest checks ────────────────────────────────────────────────────

def test_recipe_not_found(env, capsys):
    _root, _home = env
    assert validate.main(["--recipe", "ghost"]) == 1
    captured = capsys.readouterr()
    assert ("[error]   recipe not found: ghost\n"
            "          Fix: check recipes/ or run 'mini-ork classify $KICKOFF'\n"
            ) in captured.err
    assert captured.err.endswith("validate: 1 error(s), 0 warning(s)\n")


def test_missing_manifests(env, capsys):
    root, _home = env
    d = root / "recipes" / "demo"
    d.mkdir()
    (d / "task_class.yaml").write_text("name: demo\n", encoding="utf-8")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    for req in ("workflow.yaml", "artifact_contract.yaml"):
        assert (f"[error]   recipe demo missing {req}\n"
                f"          Fix: create {d}/{req} from the recipe template\n"
                ) in captured.err
    assert captured.err.endswith("validate: 2 error(s), 0 warning(s)\n")


def test_invalid_workflow_yaml(env, capsys):
    root, _home = env
    d = _recipe(root, "demo", workflow="not: [valid\n")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert ("[error]   recipe demo workflow.yaml is not valid YAML\n"
            f"          Fix: fix YAML syntax in {d}/workflow.yaml\n") in captured.err


def test_output_collision_warns_with_other_recipe_count(env, capsys):
    root, _home = env
    d = _recipe(root, "demo", contract="outputs:\n  - dist/app.js\n")
    _recipe(root, "other", contract="outputs:\n  - dist/app.js\n")
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] recipe demo output path 'dist/app.js' is also targeted by "
            "1 other recipe(s)\n"
            f"          Fix: use a recipe-specific output path in {d}/artifact_contract.yaml\n"
            ) in captured.err


def test_run_dir_outputs_skip_collision_check(env, capsys):
    root, _home = env
    _recipe(root, "demo", contract="outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n")
    _recipe(root, "other", contract="outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n")
    assert validate.main(["--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


# ── agents.yaml / provider secrets ────────────────────────────────────────────

def test_missing_agents_yaml_warns(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    (root / "config" / "agents.yaml").unlink()
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] no agents.yaml found in MINI_ORK_HOME/config or "
            "ENGINE_ROOT/config\n"
            "          Fix: run 'mini-ork init' to seed default config\n") in captured.err


def test_home_agents_yaml_takes_precedence_and_invalid_yaml_errors(env, capsys):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "agents.yaml").write_text("not: [valid\n", encoding="utf-8")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert (f"[error]   agents.yaml is not valid YAML: {cfg}/agents.yaml\n"
            "          Fix: fix YAML syntax\n") in captured.err


def test_provider_secret_missing_warns(env, capsys, monkeypatch):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    api_key_env: GLM_TEST_MISSING_KEY_XYZ\n",
        encoding="utf-8")
    monkeypatch.delenv("GLM_TEST_MISSING_KEY_XYZ", raising=False)
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] provider secret missing: glm -> GLM_TEST_MISSING_KEY_XYZ\n"
            f"          Fix: set it in {home}/config/secrets.local.sh\n") in captured.err


def test_provider_secret_present_is_clean(env, capsys, monkeypatch):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    api_key_env: GLM_TEST_PRESENT_KEY\n", encoding="utf-8")
    monkeypatch.setenv("GLM_TEST_PRESENT_KEY", "x")
    assert validate.main(["--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


# ── strict mode / summary ─────────────────────────────────────────────────────

def test_strict_turns_warnings_into_exit_1(env, tmp_path, capsys):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# no headings\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo", "--strict"]) == 1
    captured = capsys.readouterr()
    assert captured.err.endswith("validate: 0 error(s), 1 warning(s) (strict mode)\n")


def test_clean_validate_ok(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"
    assert captured.err == ""


# ── recipe inference via classify ─────────────────────────────────────────────

def test_infer_recipe_maps_task_class_underscores_to_dashes(tmp_path):
    """bash: classify | grep ^task_class= | cut -d= -f2 | tr '_' '-'."""
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Fix the bug in the parser\n", encoding="utf-8")
    recipe = validate._infer_recipe(str(kickoff), str(REPO))
    assert recipe  # classify must produce some task class in dry-run
    assert "_" not in recipe
    assert validate._infer_recipe(str(tmp_path / "missing.md"), str(REPO)) == ""


# ── native integration ────────────────────────────────────────────────────────

def test_validate_is_registered_natively():
    assert "validate" in cli._NATIVE_SUBS
    assert "validate" not in cli._EXEC_SUBS
    assert "validate" in cli.SUBCOMMAND_REGISTRY


def test_validate_dispatches_via_python_m(monkeypatch):
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: (calls.append(argv), _Done())[1])
    assert cli.main(["validate", "k.md"], root=str(REPO)) == 0
    assert calls == [[sys.executable, "-m", "mini_ork.cli.validate", "k.md"]]


def test_module_help_exits_zero_without_bin_entrypoint(tmp_path):
    """``python3 -m mini_ork.cli.validate --help`` runs the module directly —
    no bin/mini-ork-validate involved (cwd is an empty tmp dir)."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MINI_ORK_", "MO_"))}
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else "")
    run = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.validate", "--help"],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False)
    assert run.returncode == 0
    assert run.stdout == validate.HELP_TEXT
    assert run.stderr == ""
