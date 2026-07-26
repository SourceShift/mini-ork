"""Unit + native-integration tests for ``mini_ork.cli.garden``.

The unit tests drive the ported logic against tmp fixtures (recipes tree,
.mini-ork home, target repo) and assert the exact stderr/stdout/exit-code
contract of the retired ``bin/mini-ork-garden``. The integration tests prove
``mini-ork garden`` is dispatched natively (registered in ``_NATIVE_SUBS``,
absent from ``_EXEC_SUBS``) and that ``python3 -m mini_ork.cli.garden
--help`` exits 0 without any ``bin/mini-ork-*`` entrypoint.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork.cli import garden
from mini_ork.cli import main as cli

REPO = Path(__file__).resolve().parents[2]


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated (root, home, target_repo) wired through the env contract."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    target = tmp_path / "target"
    for sub in (root / "recipes", root / "lib", root / "bin", home, target):
        sub.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_TARGET_REPO", str(target))
    return root, home, target


def _contract(recipe_dir: Path, outputs: list[str]) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "artifact_contract.yaml").write_text(
        "outputs:\n" + "".join(f"  - {o}\n" for o in outputs), encoding="utf-8")


# ── usage / arg parsing ───────────────────────────────────────────────────────

def test_help_to_stdout(capsys):
    assert garden.main(["--help"]) == 0
    assert capsys.readouterr().out == garden.HELP_TEXT
    assert garden.main(["-h"]) == 0
    assert capsys.readouterr().out == garden.HELP_TEXT


def test_usage_errors(capsys):
    assert garden.main(["--nope"]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"Unknown flag: --nope\n{garden.HELP_TEXT}"
    assert garden.main(["positional"]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"Unexpected argument: positional\n{garden.HELP_TEXT}"


# ── checks ────────────────────────────────────────────────────────────────────

def test_clean_tree(env, capsys):
    assert garden.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == "garden: clean\n"
    assert captured.err == ""


def test_output_collision_is_error(env, capsys):
    root, _home, _target = env
    _contract(root / "recipes" / "r-one", ["dist/app.js"])
    _contract(root / "recipes" / "r-two", ["dist/app.js"])
    assert garden.main([]) == 1
    captured = capsys.readouterr()
    assert ("[error]   output collision: 'dist/app.js' used by r-one,r-two\n"
            "          Fix: use recipe-specific output paths in artifact_contract.yaml\n"
            ) in captured.err
    assert captured.err.endswith("garden: 1 error(s), 0 warning(s), 0 info\n")


def test_run_dir_internal_outputs_are_shared_not_collisions(env, capsys):
    root, _home, _target = env
    _contract(root / "recipes" / "r-one", ["${MINI_ORK_RUN_DIR}/out.md"])
    _contract(root / "recipes" / "r-two", ["${MINI_ORK_RUN_DIR}/out.md"])
    assert garden.main([]) == 0
    assert capsys.readouterr().out == "garden: clean\n"


def test_dict_form_outputs_collide(env, capsys):
    root, _home, _target = env
    for name in ("r-one", "r-two"):
        d = root / "recipes" / name
        d.mkdir(parents=True)
        (d / "artifact_contract.yaml").write_text(
            "outputs:\n  - path: dist/app.js\n", encoding="utf-8")
    assert garden.main([]) == 1
    assert "output collision: 'dist/app.js' used by r-one,r-two" in capsys.readouterr().err


def test_oversize_prompt_and_workflow_warnings_and_strict(env, capsys):
    root, _home, _target = env
    recipe = root / "recipes" / "r"
    (recipe / "prompts").mkdir(parents=True)
    (recipe / "prompts" / "big.md").write_bytes(b"x" * (33 * 1024 + 1))
    (recipe / "workflow.yaml").write_bytes(b"y" * (17 * 1024 + 1))

    assert garden.main([]) == 0
    captured = capsys.readouterr()
    assert f"[warning] recipe prompt exceeds 32 KiB: {recipe}/prompts/big.md (33 KiB)\n" in captured.err
    assert "          Fix: split detail into recipes/<name>/prompts/references/\n" in captured.err
    assert f"[warning] recipe workflow exceeds 16 KiB: {recipe}/workflow.yaml (17 KiB)\n" in captured.err
    assert "          Fix: decompose into smaller nodes or move detail to references/\n" in captured.err
    assert captured.err.endswith("garden: 0 error(s), 2 warning(s), 0 info\n")

    assert garden.main(["--strict"]) == 1
    captured = capsys.readouterr()
    assert captured.err.endswith("garden: 0 error(s), 2 warning(s), 0 info (strict mode)\n")


def test_prompt_exactly_at_cap_is_clean(env, capsys):
    root, _home, _target = env
    recipe = root / "recipes" / "r"
    recipe.mkdir(parents=True)
    (recipe / "ok.md").write_bytes(b"x" * (32 * 1024))  # 32 KiB is NOT > 32
    assert garden.main([]) == 0
    assert capsys.readouterr().out == "garden: clean\n"


def test_stale_run_dir_info(env, capsys):
    _root, home, _target = env
    runs = home / "runs"
    runs.mkdir()
    stale = runs / "run-old"
    stale.mkdir()
    old = time.time() - 40 * 86400
    os.utime(stale, (old, old))
    fresh = runs / "run-new"
    fresh.mkdir()

    assert garden.main([]) == 0
    captured = capsys.readouterr()
    assert f"[info]    run directory older than 30 days: {stale}\n" in captured.err
    assert f"          Fix: archive or remove with 'rm -rf {stale}'\n" in captured.err
    assert "run-new" not in captured.err
    assert captured.err.endswith("garden: 0 error(s), 0 warning(s), 1 info\n")


def test_stale_run_boundary_30_days_is_not_flagged(env, capsys):
    _root, home, _target = env
    runs = home / "runs"
    runs.mkdir()
    borderline = runs / "run-30d"
    borderline.mkdir()
    exactly_30 = time.time() - 30 * 86400  # find -mtime +30 needs > 30 days
    os.utime(borderline, (exactly_30, exactly_30))
    assert garden.main([]) == 0
    assert capsys.readouterr().out == "garden: clean\n"


def test_env_var_docs_drift(env, capsys):
    root, _home, _target = env
    (root / "lib" / "x.sh").write_text("export MO_SPECIAL_THING=1\n", encoding="utf-8")
    assert garden.main([]) == 0
    captured = capsys.readouterr()
    env_doc = root / "docs" / "operator" / "env-vars.md"
    assert f"[warning] env-var documentation missing: {env_doc}\n" in captured.err
    assert f"          Fix: create {env_doc} documenting env vars\n" in captured.err
    assert captured.err.endswith("garden: 0 error(s), 1 warning(s), 0 info\n")


def test_env_var_exclusions_and_doc_present_are_clean(env, capsys):
    root, _home, _target = env
    (root / "lib" / "x.sh").write_text(
        "echo $MINI_ORK_ROOT $MINI_ORK_HOME $MINI_ORK_DB $MINI_ORK_RUN_ID\n"
        "echo $MINI_ORK_RECIPE $MINI_ORK_WORKFLOW $MINI_ORK_TASK_CLASS\n"
        "echo $MINI_ORK_PROFILE_PATH $MINI_ORK_TARGET_REPO\n"
        "echo $MINI_ORK_ENGINE_ROOT $MINI_ORK_PROJECT_HOME\n",
        encoding="utf-8")
    # Even a non-exempt var is fine when the doc file exists.
    (root / "bin" / "y").write_text("MO_EXTRA=1\n", encoding="utf-8")
    doc = root / "docs" / "operator" / "env-vars.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# env vars\n", encoding="utf-8")
    assert garden.main([]) == 0
    assert capsys.readouterr().out == "garden: clean\n"


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_orphan_stash_prints_but_does_not_count(env, capsys, tmp_path):
    """bash quirk parity: the stash loop runs in a pipeline subshell, so the
    WARNINGS increment is lost — the line prints, but the summary and
    --strict ignore it."""
    _root, _home, target = env

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(target), *args],
                       check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (target / "f.txt").write_text("v1\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    (target / "f.txt").write_text("v2\n", encoding="utf-8")
    # git stash save/push prepend "On <branch>:" to the message, which the
    # bash regex ('^stash@\{N\}: wip-pre-implementer') does NOT match — the
    # verbatim-message entries the regex targets are created via stash store.
    sha = subprocess.run(["git", "-C", str(target), "stash", "create"],
                         check=True, capture_output=True, text=True).stdout.strip()
    git("stash", "store", "-m", "wip-pre-implementer node implementer", sha)

    assert garden.main([]) == 0
    captured = capsys.readouterr()
    assert ("[warning] orphaned implementer stash in target repo: "
            "stash@{0}: wip-pre-implementer node implementer\n") in captured.err
    assert ("          Fix: review and drop with 'git stash drop <stash>'\n") in captured.err
    # Quirk: warnings counter unchanged → summary reports clean.
    assert captured.out == "garden: clean\n"

    assert garden.main(["--strict"]) == 0
    assert capsys.readouterr().out == "garden: clean\n"


# ── native integration ────────────────────────────────────────────────────────

def test_garden_is_registered_natively():
    assert "garden" in cli._NATIVE_SUBS
    assert "garden" not in cli._EXEC_SUBS
    assert "garden" in cli.SUBCOMMAND_REGISTRY


def test_garden_dispatches_via_python_m(monkeypatch):
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: (calls.append(argv), _Done())[1])
    assert cli.main(["garden", "--strict"], root=str(REPO)) == 0
    assert calls == [[sys.executable, "-m", "mini_ork.cli.garden", "--strict"]]


def test_module_help_exits_zero_without_bin_entrypoint(tmp_path):
    """``python3 -m mini_ork.cli.garden --help`` runs the module directly —
    no bin/mini-ork-garden involved (cwd is an empty tmp dir)."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MINI_ORK_", "MO_"))}
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else "")
    run = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.garden", "--help"],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False)
    assert run.returncode == 0
    assert run.stdout == garden.HELP_TEXT
    assert run.stderr == ""
