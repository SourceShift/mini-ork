"""Focused contracts for the Make-backed full MiniOrk installation path."""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "bin" / "mini-ork"
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _installer_module():
    path = REPO / "scripts" / "full_install.py"
    spec = importlib.util.spec_from_file_location("mini_ork_full_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_package_declares_core_and_full_runtime_dependencies():
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    assert "PyYAML>=6.0" in project["dependencies"]
    assert project["optional-dependencies"]["yaml"] == []
    assert project["optional-dependencies"]["full"] == [
        "fastapi>=0.110",
        "uvicorn[standard]>=0.29",
        "verifiers>=0.2.0,<0.3",
        "gepa>=0.1.4,<0.2",
    ]


def test_full_install_command_sequence_uses_venv_and_full_extra(tmp_path):
    installer = _installer_module()
    root = tmp_path / "engine"
    venv = root / ".venv"
    commands = installer.installation_commands(root, venv, ("--no-path",))

    assert commands[0] == (sys.executable, "-m", "venv", str(venv))
    assert commands[2][-2:] == ("--editable", ".[full]")
    assert commands[3][-2:] == ("install", "--no-path")
    assert commands[4][-1] == "doctor"
    assert installer.venv_python(venv, windows=False) == venv / "bin" / "python"
    assert installer.venv_python(venv, windows=True) == venv / "Scripts" / "python.exe"
    assert installer.SYSTEM_TOOLS == ("bash", "sqlite3", "jq", "yq", "git", "curl")


def test_full_install_dry_run_does_not_create_venv(tmp_path, monkeypatch, capsys):
    installer = _installer_module()
    monkeypatch.setattr(installer, "missing_system_tools", lambda: ())
    root = tmp_path / "engine"
    root.mkdir()
    venv = root / ".venv"

    installer.install(root, venv, install_args=("--no-path",), dry_run=True)

    assert venv.exists() is False
    assert ".[full]" in capsys.readouterr().out


def test_runtime_pointer_selects_a_custom_venv(tmp_path):
    installer = _installer_module()
    root = tmp_path / "engine"
    venv = tmp_path / "custom-venv"
    root.mkdir()

    pointer = installer.write_runtime_pointer(root, venv, dry_run=False)

    assert pointer == root / ".mini-ork" / "runtime-python"
    assert pointer.read_text(encoding="utf-8") == str(installer.venv_python(venv).resolve()) + "\n"


def test_full_installer_exposes_git_for_windows_bash_to_its_process(tmp_path):
    installer = _installer_module()
    program_files = tmp_path / "Program Files"
    bash = program_files / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    environment = {"ProgramFiles": str(program_files), "PATH": "C:/Windows/System32"}

    configured = installer.configure_windows_git_bash(platform_name="nt", environment=environment)

    assert configured == bash
    assert environment["PATH"].split(os.pathsep)[0] == str(bash.parent)


def test_make_install_wires_system_bootstrap_and_full_installer():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "install-system-deps:" in makefile
    assert "scripts/full_install.py --venv \"$(VENV)\" $(INSTALL_ARGS)" in makefile
    assert "INSTALL_SYSTEM_DEPS ?= 1" in makefile


def test_launcher_recognizes_git_for_windows_bash_and_modern_python_candidates():
    launcher = BIN.read_text(encoding="utf-8")
    system_installer = (REPO / "scripts" / "install-system-deps.sh").read_text(encoding="utf-8")
    bootstrap = (REPO / "scripts" / "full_install.py").read_text(encoding="utf-8")

    assert 'root / "Git" / "bin" / "bash.exe"' in launcher
    assert "python3.14" in system_installer
    assert "python3.14" in bootstrap


def test_launcher_reexecs_the_configured_venv_for_normal_commands(tmp_path):
    if os.name == "nt":
        return
    venv = tmp_path / "venv"
    python = venv / "bin" / "python"
    marker = tmp_path / "reexec.txt"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nprintf '%s' \"$0\" > {marker}\nexec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    environment = {
        **os.environ,
        "MINI_ORK_VENV": str(venv),
        "MINI_ORK_VENV_ACTIVE": "",
        "MINI_ORK_USE_VENV": "1",
    }

    run = subprocess.run([str(BIN), "version"], capture_output=True, text=True, env=environment, check=False)

    assert run.returncode == 0, run.stderr
    assert run.stdout == f"mini-ork {VERSION} (universal task loop runtime)\n"
    assert marker.read_text(encoding="utf-8") == str(python)


def test_launcher_reexecs_the_persisted_runtime_pointer(tmp_path):
    if os.name == "nt":
        return
    engine = tmp_path / "engine"
    launcher = engine / "bin" / "mini-ork"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(BIN, launcher)
    (engine / "mini_ork").symlink_to(REPO / "mini_ork", target_is_directory=True)
    venv = tmp_path / "runtime"
    python = venv / "bin" / "python"
    marker = tmp_path / "pointer-reexec.txt"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nprintf '%s' \"$0\" > {marker}\nexec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    pointer = engine / ".mini-ork" / "runtime-python"
    pointer.parent.mkdir()
    pointer.write_text(str(python) + "\n", encoding="utf-8")
    environment = {
        **os.environ,
        "MINI_ORK_ROOT": str(engine),
        "MINI_ORK_ENGINE_ROOT": str(engine),
        "MINI_ORK_VENV": "",
        "MINI_ORK_VENV_ACTIVE": "",
        "MINI_ORK_USE_VENV": "1",
    }

    run = subprocess.run([str(launcher), "version"], capture_output=True, text=True, env=environment, check=False)

    assert run.returncode == 0, run.stderr
    assert run.stdout == f"mini-ork {VERSION} (universal task loop runtime)\n"
    assert marker.read_text(encoding="utf-8") == str(python)


def _launcher_functions(*names: str) -> dict:
    """Exec only the named top-level defs/assigns from the launcher (its module
    body re-execs and imports the engine, so it cannot be imported directly)."""
    import ast
    tree = ast.parse(BIN.read_text(encoding="utf-8"))
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets))]
    ns: dict = {"os": os, "sys": sys, "Path": Path}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(BIN), "exec"), ns)
    return ns


def test_launcher_refuses_an_interpreter_older_than_requires_python(monkeypatch, capsys):
    """On 3.9 the engine half-worked (verifiers crashed on PEP 604 syntax,
    PYTHONSAFEPATH was ignored) and every symptom read as a normal failed run."""
    ns = _launcher_functions("MIN_PYTHON", "_require_supported_python")
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    try:
        ns["_require_supported_python"](Path("/x"))
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("an unsupported interpreter must exit")
    assert "Python 3.11+ is required" in capsys.readouterr().err


def test_launcher_min_python_matches_pyproject():
    ns = _launcher_functions("MIN_PYTHON")
    spec = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    assert spec == ">=" + ".".join(map(str, ns["MIN_PYTHON"]))


def test_launcher_in_a_worktree_without_a_venv_uses_the_main_checkouts(tmp_path):
    """A linked worktree has no gitignored .venv; the launcher used to fall
    through to the first python3 on PATH instead of the main checkout's venv."""
    if os.name == "nt":
        return
    main = tmp_path / "main"
    (main / "bin").mkdir(parents=True)
    shutil.copy2(BIN, main / "bin" / "mini-ork")
    (main / "mini_ork").symlink_to(REPO / "mini_ork", target_is_directory=True)
    (main / "pyproject.toml").write_text((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    git = ["git", "-C", str(main)]
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(git + args, check=True, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(git + ["worktree", "add", "-q", "--detach", str(wt)], check=True, capture_output=True)
    python = main / ".venv" / "bin" / "python"
    marker = tmp_path / "shared-venv.txt"
    python.parent.mkdir(parents=True)
    python.write_text(f"#!/bin/sh\nprintf '%s' \"$0\" > {marker}\nexec {sys.executable} \"$@\"\n",
                      encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    environment = {**os.environ, "MINI_ORK_ROOT": str(wt), "MINI_ORK_ENGINE_ROOT": str(wt),
                   "MINI_ORK_VENV": "", "MINI_ORK_VENV_ACTIVE": "", "MINI_ORK_USE_VENV": "1"}

    run = subprocess.run([str(wt / "bin" / "mini-ork"), "version"], capture_output=True,
                         text=True, env=environment, check=False)

    assert run.returncode == 0, run.stderr
    assert marker.read_text(encoding="utf-8") == str(python)
