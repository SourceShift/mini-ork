"""Regression tests ported from the blind spec tests for the t0f-installer kickoff (install.sh).

Authored from the kickoff alone, before any implementation exists.
Repo files are resolved from the working directory, which the runner sets to
the target repo root.

The behavioral tests drive install.sh exactly the way the kickoff's remote
install branch runs: the script is copied to a bare directory (so the
"checkout mode" exec at the top does not trigger), every interpreter and tool
it needs is a recording stub on a PATH that contains nothing else, and the
install target is a stub checkout. No network, no host interpreters.
"""

import re
import shlex
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _fake_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _make_fake_python(bin_dir: Path, name: str, *, version_ok: bool, record: Path) -> None:
    """A stub interpreter that records its argv and answers the version probe."""
    quoted = shlex.quote(str(record))
    if version_ok:
        body = f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {quoted}\nexit 0\n"
    else:
        body = (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {quoted}\n"
            'if [ "$1" = "-c" ]; then\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n"
        )
    _fake_executable(bin_dir / name, body)


def _make_bin_dir(tmp_path: Path) -> Path:
    """A PATH directory holding only the stubs install.sh needs (and no bash)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_executable(bin_dir / "git", "#!/bin/sh\nexit 0\n")
    _fake_executable(
        bin_dir / "dirname",
        '#!/bin/sh\ncase "$1" in\n  */*) echo "${1%/*}" ;;\n  *) echo "." ;;\nesac\n',
    )
    return bin_dir


def _make_target(tmp_path: Path, *, system_deps_record=None) -> Path:
    """A stub mini-ork checkout: an existing target with scripts/full_install.py."""
    target = tmp_path / "target"
    (target / "scripts").mkdir(parents=True)
    (target / "scripts" / "full_install.py").write_text("# stub checkout marker\n")
    if system_deps_record is not None:
        quoted = shlex.quote(str(system_deps_record))
        _fake_executable(
            target / "scripts" / "install-system-deps.sh",
            f"#!/bin/sh\nprintf '%s\\n' \"$0\" >> {quoted}\nexit 0\n",
        )
    return target


def _run_remote_install(
    tmp_path: Path, bin_dir: Path, target: Path, *, install_system_deps: str
) -> subprocess.CompletedProcess:
    script = tmp_path / "install.sh"
    shutil.copyfile(REPO / "install.sh", script)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "MINI_ORK_INSTALL_DIR": str(target),
        "MINI_ORK_REF": "main",
        "INSTALL_SYSTEM_DEPS": install_system_deps,
        "MINI_ORK_NO_ONBOARD": "1",
    }
    return subprocess.run(
        ["/bin/sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_install_sh_runs_system_deps_under_bash():
    r"""Kickoff: "it contains `bash "$TARGET/scripts/install-system-deps.sh"`; - it contains no line matching the regex `^\s*sh "\$TARGET/scripts/install-system-deps.sh"`."
    """
    text = (REPO / "install.sh").read_text()
    assert 'bash "$TARGET/scripts/install-system-deps.sh"' in text
    assert re.search(r'^\s*sh "\$TARGET/scripts/install-system-deps\.sh"', text, re.MULTILINE) is None


def test_install_sh_probe_tries_python313_first():
    """Kickoff: "Read `install.sh` as text: it contains `for candidate in python3.13 python3.12 python3.11 python3; do`."
    """
    text = (REPO / "install.sh").read_text()
    assert "for candidate in python3.13 python3.12 python3.11 python3; do" in text


def test_install_sh_keeps_posix_syntax():
    """Kickoff: "The script must still pass the existing POSIX syntax check (it is executed by `sh`; only the system-deps child needs bash)."
    """
    proc = subprocess.run(
        ["/bin/sh", "-n", str(REPO / "install.sh")], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_remote_probe_picks_python313_over_python3(tmp_path):
    """Kickoff: "With fake interpreters on PATH (`python3.13` version-ok and recording its argv, `python3` version-ok), the remote install branch invokes `python3.13`, not `python3`."
    """
    bin_dir = _make_bin_dir(tmp_path)
    records = {
        name: tmp_path / f"record-{name}"
        for name in ("python3.13", "python3.12", "python3.11", "python3")
    }
    _make_fake_python(bin_dir, "python3.13", version_ok=True, record=records["python3.13"])
    _make_fake_python(bin_dir, "python3.12", version_ok=False, record=records["python3.12"])
    _make_fake_python(bin_dir, "python3.11", version_ok=False, record=records["python3.11"])
    _make_fake_python(bin_dir, "python3", version_ok=True, record=records["python3"])
    target = _make_target(tmp_path)

    proc = _run_remote_install(tmp_path, bin_dir, target, install_system_deps="0")

    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert records["python3.13"].exists(), (
        f"python3.13 was never invoked\nstderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    )
    picked = records["python3.13"].read_text().splitlines()
    assert any(line.endswith("scripts/full_install.py") for line in picked), picked
    if records["python3"].exists():
        fallback = records["python3"].read_text().splitlines()
        assert not any(line.endswith("scripts/full_install.py") for line in fallback), fallback


def test_system_deps_step_runs_the_script_under_bash(tmp_path):
    r"""Kickoff: "when `command -v bash` succeeds: `bash "$TARGET/scripts/install-system-deps.sh"`;"
    """
    bin_dir = _make_bin_dir(tmp_path)
    bash_record = tmp_path / "record-bash"
    quoted = shlex.quote(str(bash_record))
    _fake_executable(
        bin_dir / "bash", f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {quoted}\nexit 0\n"
    )
    _make_fake_python(
        bin_dir, "python3.13", version_ok=True, record=tmp_path / "record-python3.13"
    )
    target = _make_target(tmp_path, system_deps_record=tmp_path / "record-system-deps")

    proc = _run_remote_install(tmp_path, bin_dir, target, install_system_deps="1")

    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert bash_record.exists(), (
        f"bash was never invoked\nstderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    )
    assert str(target / "scripts" / "install-system-deps.sh") in bash_record.read_text().splitlines()


def test_system_deps_step_fails_closed_without_bash(tmp_path):
    r"""Kickoff: "otherwise: print `mini-ork: scripts/install-system-deps.sh needs bash; install bash or re-run with INSTALL_SYSTEM_DEPS=0` to stderr and `exit 1`."
    """
    bin_dir = _make_bin_dir(tmp_path)  # deliberately no bash anywhere on PATH
    _make_fake_python(
        bin_dir, "python3.13", version_ok=True, record=tmp_path / "record-python3.13"
    )
    system_deps_record = tmp_path / "record-system-deps"
    target = _make_target(tmp_path, system_deps_record=system_deps_record)

    proc = _run_remote_install(tmp_path, bin_dir, target, install_system_deps="1")

    assert proc.returncode == 1, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert (
        "mini-ork: scripts/install-system-deps.sh needs bash; "
        "install bash or re-run with INSTALL_SYSTEM_DEPS=0" in proc.stderr
    ), proc.stderr
    assert not system_deps_record.exists(), "the system-deps script ran without bash on PATH"


def test_existing_install_sh_tests_shadow_a_real_python313():
    """Kickoff: "every existing test or fixture that calls `_make_fake_python(bin_dir, "python3.12", version_ok=False)` must also call `_make_fake_python(bin_dir, "python3.13", version_ok=False)`."
    """
    text = (REPO / "tests" / "unit" / "test_install_sh.py").read_text()
    refuse_312 = text.count('_make_fake_python(bin_dir, "python3.12", version_ok=False)')
    refuse_313 = text.count('_make_fake_python(bin_dir, "python3.13", version_ok=False)')
    assert refuse_313 >= refuse_312, (
        f"{refuse_312} call(s) fake a refusing python3.12 but only {refuse_313} "
        "shadow python3.13; a real host python3.13 would satisfy the first probe"
    )
