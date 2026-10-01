"""Subprocess contract tests for install.sh in remote and checkout modes.

install.sh detects its mode by sentinel files in its directory: if both
``bin/mini-ork`` and ``scripts/full_install.py`` exist there, it execs
``python3 bin/mini-ork install "$@"`` (the legacy checkout mode). Otherwise
it enters remote mode: resolves a Python 3.11+ interpreter, ensures ``git``
is on PATH, clones/updates ``$MINI_ORK_INSTALL_DIR``, optionally invokes
``scripts/install-system-deps.sh``, then execs ``scripts/full_install.py``.

Tests cover both branches by shimming ``git``/``python3.X`` into a fake
``PATH`` (prepended ahead of the system path) and copying ``install.sh``
alone into a scratch directory to force the remote-mode detection.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "install.sh"

GIT_SHIM = (
    "#!/bin/sh\n"
    "FETCH_RC=${MO_GIT_FETCH_RC:-0}\n"
    "MERGE_RC=${MO_GIT_MERGE_RC:-0}\n"
    "GIT_LOG=${MO_GIT_LOG:-}\n"
    "if [ -n \"$GIT_LOG\" ]; then\n"
    "  printf 'git %s\\n' \"$*\" >> \"$GIT_LOG\"\n"
    "fi\n"
    # Identify the subcommand by skipping flags / -C <dir>.
    "subcmd=\"\"\n"
    "skip=0\n"
    "for a in \"$@\"; do\n"
    "  if [ \"$skip\" = \"1\" ]; then skip=0; continue; fi\n"
    "  case \"$a\" in\n"
    "    -C) skip=1 ;;\n"
    "    -*) ;;\n"
    "    *) subcmd=\"$a\"; break ;;\n"
    "  esac\n"
    "done\n"
    "case \"$subcmd\" in\n"
    "  clone)\n"
    # The destination is the last non-flag, non-url positional.
    "    dest=\"\"\n"
    "    for a in \"$@\"; do\n"
    "      case \"$a\" in\n"
    "        -*) ;;\n"
    "        http*) ;;\n"
    "        git@*) ;;\n"
    "        ssh:*) ;;\n"
    "        *) dest=\"$a\" ;;\n"
    "      esac\n"
    "    done\n"
    "    mkdir -p \"$dest/scripts\"\n"
    "    printf '#!/bin/sh\\nexit 0\\n' > \"$dest/scripts/full_install.py\"\n"
    "    printf '#!/bin/sh\\ntouch \"%s/.deps-marker\"\\n' \"$dest\" "
    "> \"$dest/scripts/install-system-deps.sh\"\n"
    "    chmod +x \"$dest/scripts/full_install.py\" \"$dest/scripts/install-system-deps.sh\"\n"
    "    ;;\n"
    "  fetch) exit \"$FETCH_RC\" ;;\n"
    "  merge) exit \"$MERGE_RC\" ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)


def _system_path() -> str:
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.pathsep.join(parts) or "/usr/bin:/bin"


def _write(path: Path, body: str, mode: int = 0o755) -> None:
    path.write_text(body)
    path.chmod(mode)


def _make_fake_git(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    git_sh = bin_dir / "git"
    _write(git_sh, GIT_SHIM)
    return git_sh


def _make_fake_python(bin_dir: Path, name: str, *, version_ok: bool,
                      record_argv: Path | None = None) -> Path:
    """python shim. ``version_ok=True`` passes -c checks; ``False`` always exits 1."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / name
    if not version_ok:
        _write(py, "#!/bin/sh\nexit 1\n")
        return py
    if record_argv is None:
        _write(py, (
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  -c) exit 0 ;;\n"
            "  *) exec \"$@\" ;;\n"
            "esac\n"
        ))
        return py
    _write(py, (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  -c) exit 0 ;;\n"
        "  *)\n"
        "    cmd=\"\"\n"
        "    for a in \"$@\"; do cmd=\"$cmd $a\"; done\n"
        f"    printf '%s\\n' \"$cmd\" >> '{record_argv}'\n"
        "    exec \"$@\"\n"
        "    ;;\n"
        "esac\n"
    ))
    return py


def _make_fake_python3_exec(bin_dir: Path, record_argv: Path) -> Path:
    """python3 shim for checkout mode: records its argv then execs."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "python3"
    _write(py, (
        "#!/bin/sh\n"
        "cmd=\"\"\n"
        "for a in \"$@\"; do cmd=\"$cmd $a\"; done\n"
        f"printf '%s\\n' \"$cmd\" >> '{record_argv}'\n"
        "exec \"$@\"\n"
    ))
    return py


def _copy_install_sh(target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / "install.sh"
    shutil.copy(INSTALL_SH, dest)
    return dest


def _run(script: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        ["sh", str(script)],
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.fixture()
def remote(tmp_path: Path) -> dict:
    """Pre-fabricated fake-bin + scratch install dir for remote-mode tests."""
    bin_dir = tmp_path / "fake-bin"
    _make_fake_git(bin_dir)
    py_record = tmp_path / "py-argv.txt"
    git_log = tmp_path / "git.log"
    _make_fake_python(bin_dir, "python3.12", version_ok=False)
    _make_fake_python(bin_dir, "python3.11", version_ok=True, record_argv=py_record)
    install_dir = tmp_path / "install-dir"
    script = _copy_install_sh(install_dir)
    target = tmp_path / "install-target"
    return {
        "bin_dir": bin_dir,
        "script": script,
        "py_record": py_record,
        "git_log": git_log,
        "target": target,
        "env_base": {
            "PATH": f"{bin_dir}{os.pathsep}{_system_path()}",
            "MO_GIT_LOG": str(git_log),
            "HOME": str(tmp_path),
            "MINI_ORK_INSTALL_DIR": str(target),
        },
    }


def test_remote_fresh_install_uses_default_url_and_branch(remote: dict) -> None:
    env = {**remote["env_base"], "INSTALL_SYSTEM_DEPS": "1"}
    result = _run(remote["script"], env)
    assert result.returncode == 0, result.stderr
    assert (remote["target"] / ".deps-marker").exists()
    assert "mini-ork: installing into" in result.stderr
    py_argv_lines = remote["py_record"].read_text().splitlines()
    assert any("scripts/full_install.py" in line for line in py_argv_lines)
    git_log = remote["git_log"].read_text()
    assert "--branch main" in git_log


def test_remote_install_honors_mini_ork_ref(remote: dict) -> None:
    env = {**remote["env_base"], "INSTALL_SYSTEM_DEPS": "0", "MINI_ORK_REF": "v0.9.0"}
    result = _run(remote["script"], env)
    assert result.returncode == 0, result.stderr
    assert "--branch v0.9.0" in remote["git_log"].read_text()
    assert not (remote["target"] / ".deps-marker").exists()


def test_remote_rerun_on_existing_checkout_uses_fetch_merge(remote: dict) -> None:
    remote["target"].mkdir(parents=True, exist_ok=True)
    (remote["target"] / "scripts").mkdir()
    full_install = remote["target"] / "scripts" / "full_install.py"
    full_install.write_text("#!/bin/sh\nexit 0\n")
    full_install.chmod(0o755)

    env = {**remote["env_base"], "INSTALL_SYSTEM_DEPS": "0"}
    result = _run(remote["script"], env)
    assert result.returncode == 0, result.stderr
    log = remote["git_log"].read_text()
    assert "git clone" not in log
    assert "fetch" in log
    assert "merge --ff-only" in log


def test_remote_merge_ff_only_failure_falls_through(remote: dict) -> None:
    remote["target"].mkdir(parents=True, exist_ok=True)
    (remote["target"] / "scripts").mkdir()
    full_install = remote["target"] / "scripts" / "full_install.py"
    full_install.write_text("#!/bin/sh\nexit 0\n")
    full_install.chmod(0o755)

    env = {
        **remote["env_base"],
        "INSTALL_SYSTEM_DEPS": "0",
        "MO_GIT_MERGE_RC": "1",
    }
    result = _run(remote["script"], env)
    assert result.returncode == 0, result.stderr
    assert "could not fast-forward" in result.stderr
    py_argv_lines = remote["py_record"].read_text().splitlines()
    assert any("scripts/full_install.py" in line for line in py_argv_lines)


def test_remote_target_exists_with_unrelated_file_refuses(remote: dict) -> None:
    remote["target"].mkdir(parents=True, exist_ok=True)
    unrelated = remote["target"] / "README.md"
    unrelated.write_text("do not touch me\n")
    before = unrelated.read_text()

    env = {**remote["env_base"], "INSTALL_SYSTEM_DEPS": "0"}
    result = _run(remote["script"], env)
    assert result.returncode == 1, result.stderr
    assert "MINI_ORK_INSTALL_DIR" in result.stderr
    assert unrelated.read_text() == before
    assert not remote["git_log"].exists() or "clone" not in remote["git_log"].read_text()


def test_remote_install_system_deps_zero_skips_marker(remote: dict) -> None:
    env = {**remote["env_base"], "INSTALL_SYSTEM_DEPS": "0"}
    result = _run(remote["script"], env)
    assert result.returncode == 0, result.stderr
    assert not (remote["target"] / ".deps-marker").exists()


def test_remote_no_acceptable_python_exits_with_message(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    _make_fake_python(bin_dir, "python3.12", version_ok=False)
    _make_fake_python(bin_dir, "python3.11", version_ok=False)
    _make_fake_python(bin_dir, "python3", version_ok=False)

    install_dir = tmp_path / "install-dir"
    script = _copy_install_sh(install_dir)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{_system_path()}",
        "HOME": str(tmp_path),
    }
    result = _run(script, env)
    assert result.returncode == 1, result.stderr
    assert "needs Python 3.11 or newer" in result.stderr


def test_checkout_mode_executes_bin_mini_ork_install(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fake-bin"
    py_record = tmp_path / "py-argv.txt"
    _make_fake_python3_exec(bin_dir, py_record)

    install_dir = tmp_path / "checkout"
    install_dir.mkdir()
    _copy_install_sh(install_dir)
    (install_dir / "bin").mkdir()
    (install_dir / "scripts").mkdir()
    bin_mini_ork = install_dir / "bin" / "mini-ork"
    _write(bin_mini_ork, "#!/bin/sh\nexit 0\n")
    (install_dir / "scripts" / "full_install.py").write_text("#!/bin/sh\nexit 0\n")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{_system_path()}",
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        ["sh", str(install_dir / "install.sh"), "--foo"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    argv_lines = py_record.read_text().splitlines()
    assert any("/bin/mini-ork install --foo" in line for line in argv_lines)


@pytest.mark.parametrize("checker", ["sh", "dash"])
def test_install_sh_passes_posix_syntax_check(checker: str) -> None:
    if shutil.which(checker) is None:
        pytest.skip(f"{checker} not on PATH")
    result = subprocess.run(
        [checker, "-n", str(INSTALL_SH)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
