"""RBF-26: framework-edit verifiers run kickoff commands hermetically.

Every test builds a throwaway git repo plus a run dir under ``tmp_path`` and
drives the real verifier scripts as subprocesses, exactly the way the executor
does: env in, JSON envelope out. Nothing here reads live mini-ork state.

Covered (see the kickoff's mechanism list):

1. ``test.py`` builds its command env with the ABSOLUTE patched copy on
   ``PYTHONPATH`` (generic commands and web-smoke), prepending the inherited
   value rather than a cwd-relative ``.``.
2. ``kickoff-contract.py`` pins ``PATH`` to ``dirname(sys.executable)`` so a
   bare ``python3`` in a measure resolves the verifier's interpreter.
3. A command failing on both the patched and the base copy is recorded as a
   non-gating ``base-fails`` abstention when both fail with the SAME exit
   code; a different base exit code, or a patched-only failure, stays a
   gating regression.
4. Two overlapping glob tokens reach pytest as concrete files, each once.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER_TEST = REPO / "recipes" / "framework-edit" / "verifiers" / "test.py"
VERIFIER_KICKOFF = REPO / "recipes" / "framework-edit" / "verifiers" / "kickoff-contract.py"

MARKER_DIFF = (
    "diff --git a/marker.txt b/marker.txt\n"
    "--- a/marker.txt\n"
    "+++ b/marker.txt\n"
    "@@ -1 +1 @@\n"
    "-BASE\n"
    "+PATCHED\n"
)


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True).stdout.strip()


def _target_repo(tmp: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A throwaway repo whose single commit is the BASE tree."""
    repo = tmp / "target"
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "spec@hermetic.local")
    _git(repo, "config", "user.name", "spec")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _run_dir(tmp: Path, repo: Path, base_ref: str, commands: list[str]) -> Path:
    """A run dir shaped the way ``test.py`` expects, with a kickoff section."""
    run = tmp / "run"
    run.mkdir()
    (run / "framework-edit.diff").write_text(MARKER_DIFF)
    (run / "pre-implementer-ref").write_text(base_ref + "\n")
    kickoff = run / "kickoff.md"
    body = ["# hermetic fixture kickoff", "", "## Verification commands", ""]
    body.extend("- `%s`" % command for command in commands)
    kickoff.write_text("\n".join(body) + "\n")
    (run / "run_profile.json").write_text(json.dumps({"kickoff_path": str(kickoff)}))
    return run


def _measure_run_dir(tmp: Path, commands: list[str]) -> Path:
    run = tmp / "measure-run"
    run.mkdir()
    kickoff = run / "kickoff.md"
    kickoff.write_text(
        "# hermetic fixture kickoff\n\n## Self-application measurement\n\n"
        "```measure\n" + "\n".join(commands) + "\n```\n")
    (run / "run_profile.json").write_text(json.dumps({"kickoff_path": str(kickoff)}))
    return run


def _child_env() -> dict[str, str]:
    """The parent env minus live mini-ork state and provider credentials.

    tests/conftest.py does not pop every MINI_ORK_*/MO_* name, so a nested run
    could adopt this run's identity (RB-04) and write its live DB (RB-27).
    """
    env = {}
    for key, value in os.environ.items():
        if key.startswith(("MINI_ORK_", "MO_", "PYTEST_", "ANTHROPIC_")):
            continue
        if key.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_SECRET", "_SECRET_KEY")):
            continue
        if key in ("OPENAI_API_BASE", "OPENAI_BASE_URL"):
            continue
        env[key] = value
    return env


def _run_verifier(script: Path, repo: Path, run: Path, **overrides: str) -> subprocess.CompletedProcess:
    env = _child_env()
    env["MINI_ORK_RUN_DIR"] = str(run)
    env["MO_TARGET_CWD"] = str(repo)
    env.update(overrides)
    return subprocess.run([sys.executable, str(script)], env=env, cwd=str(repo),
                          capture_output=True, text=True, timeout=240)


def _envelope(proc: subprocess.CompletedProcess) -> dict:
    for line in reversed(proc.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError("no JSON envelope; rc=%s\nstdout tail: %r\nstderr tail: %r"
                         % (proc.returncode, proc.stdout[-1500:], proc.stderr[-1500:]))


def test_generic_command_env_pythonpath_is_the_absolute_patched_copy(tmp_path: Path) -> None:
    out = tmp_path / "cmd-env.txt"
    inherited = "hermetic-inherited-7c4a"
    repo, ref = _target_repo(tmp_path, {"marker.txt": "BASE\n"})
    run = _run_dir(tmp_path, repo, ref, [
        "{ pwd; env; } > " + shlex.quote(str(out)),
    ])

    proc = _run_verifier(VERIFIER_TEST, repo, run, PYTHONPATH=inherited)
    data = _envelope(proc)

    assert data["pass"] is True, data
    lines = out.read_text().splitlines()
    cmd_cwd = lines[0]
    envmap = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
    components = envmap["PYTHONPATH"].split(os.pathsep)

    assert os.path.isabs(components[0]), components
    assert "." not in components, components
    assert os.path.realpath(components[0]) == os.path.realpath(cmd_cwd), components
    assert inherited in components[1:], components


def test_web_smoke_env_pythonpath_is_the_absolute_patched_copy(tmp_path: Path) -> None:
    dump = tmp_path / "web-smoke-env.txt"
    smoke = (
        "import os\n"
        "with open(%r, 'w') as fh:\n"
        "    fh.write(os.getcwd() + '\\n' + os.environ.get('PYTHONPATH', '') + '\\n')\n"
        "\n"
        "def test_web_smoke_hermetic():\n"
        "    assert True\n" % str(dump))
    repo, ref = _target_repo(tmp_path, {
        "marker.txt": "BASE\n",
        "tests/test_web_smoke.py": smoke,
    })
    run = _run_dir(tmp_path, repo, ref, [])

    data = _envelope(_run_verifier(VERIFIER_TEST, repo, run))

    assert data["pass"] is True, data
    assert all(c["pass"] for c in data["checks"] if "web-smoke" in c["name"]), data["checks"]
    lines = dump.read_text().splitlines()
    components = lines[1].split(os.pathsep)
    assert os.path.isabs(components[0]), components
    assert "." not in components, components
    assert os.path.realpath(components[0]) == os.path.realpath(lines[0]), components


def test_kickoff_contract_measure_env_pins_the_interpreter(tmp_path: Path) -> None:
    fakebin = tmp_path / "fake-bin"
    fakebin.mkdir()
    stub = fakebin / "python3"
    stub.write_text("#!/bin/sh\necho stub-interpreter\n")
    stub.chmod(0o755)

    path_out = tmp_path / "measure-path.txt"
    which_out = tmp_path / "measure-which.txt"
    run = _measure_run_dir(tmp_path, [
        'printf %s "$PATH" > ' + shlex.quote(str(path_out)),
        "command -v python3 > " + shlex.quote(str(which_out)),
    ])
    cwd = tmp_path / "measure-cwd"
    cwd.mkdir()

    proc = _run_verifier(
        VERIFIER_KICKOFF, cwd, run,
        PATH=os.pathsep.join([str(fakebin), "/bin", "/usr/bin"]))
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-1500:], proc.stderr[-1500:])
    payload = json.loads((run / "verifier_kickoff-contract.json").read_text())
    assert payload["pass"] is True, payload

    parts = path_out.read_text().split(os.pathsep)
    assert parts[0] == os.path.dirname(sys.executable), parts
    assert str(fakebin) in parts[1:], parts
    resolved = which_out.read_text().strip()
    assert os.path.realpath(os.path.dirname(resolved)) == os.path.realpath(
        os.path.dirname(sys.executable)), resolved


def _base_markers(dump: Path) -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in dump.read_text().splitlines() if line.strip()]
    return ([line for line in lines if line not in ("BASE", "PATCHED")],
            [line for line in lines if line in ("BASE", "PATCHED")])


def test_failure_shared_with_the_base_copy_abstains_without_gating(tmp_path: Path) -> None:
    dump = tmp_path / "runs.txt"
    cmd = "{ pwd; cat marker.txt; } >> " + shlex.quote(str(dump)) + "; exit 9"
    repo, ref = _target_repo(tmp_path, {"marker.txt": "BASE\n"})
    run = _run_dir(tmp_path, repo, ref, [cmd])

    data = _envelope(_run_verifier(VERIFIER_TEST, repo, run))

    _, markers = _base_markers(dump)
    assert markers.count("PATCHED") == 1, markers
    assert markers.count("BASE") >= 1, (
        "the failing command was never replayed on the base copy: %s" % markers)

    abstentions = [c for c in data["checks"] if c.get("failure_kind") == "base-fails"]
    assert abstentions, data["checks"]
    for check in abstentions:
        assert check["gating"] is False, check
        assert "marker.txt" in json.dumps(check), check
    assert data["pass"] is True, data
    assert data["failed_checks"] == [], data["failed_checks"]


def test_failure_only_on_the_patched_copy_is_a_regression(tmp_path: Path) -> None:
    dump = tmp_path / "runs.txt"
    cmd = ("{ pwd; cat marker.txt; } >> " + shlex.quote(str(dump))
           + "; grep -q BASE marker.txt")
    repo, ref = _target_repo(tmp_path, {"marker.txt": "BASE\n"})
    run = _run_dir(tmp_path, repo, ref, [cmd])

    data = _envelope(_run_verifier(VERIFIER_TEST, repo, run))

    _, markers = _base_markers(dump)
    assert markers.count("PATCHED") == 1, markers
    assert markers.count("BASE") >= 1, markers
    assert not [c for c in data["checks"] if c.get("failure_kind") == "base-fails"], data["checks"]
    assert data["pass"] is False, data
    assert data["failed_checks"], data


def test_overlapping_globs_reach_pytest_as_each_file_once(tmp_path: Path) -> None:
    argv_dump = tmp_path / "argv.txt"
    conftest = (
        "import sys\n"
        "with open(%r, 'a') as fh:\n"
        "    fh.write('\\n'.join(sys.argv[1:]) + '\\n===EOF===\\n')\n" % str(argv_dump))
    repo, ref = _target_repo(tmp_path, {
        "marker.txt": "BASE\n",
        "tests/conftest.py": conftest,
        "tests/test_ide_pages_run_learnings_a.py": "def test_a():\n    assert True\n",
        "tests/test_ide_pages_run_b.py": "def test_b():\n    assert True\n",
    })
    run = _run_dir(tmp_path, repo, ref, [
        "pytest tests/test_ide_pages_run_learnings*.py tests/test_ide_pages_run*.py -q",
    ])

    data = _envelope(_run_verifier(VERIFIER_TEST, repo, run))

    assert data["pass"] is True, data
    records = [r for r in argv_dump.read_text().split("===EOF===") if r.strip()]
    assert records, "the command never reached pytest collection"
    args = [token for token in records[0].split("\n") if token.strip()]
    basenames = [os.path.basename(token) for token in args]
    for name in ("test_ide_pages_run_learnings_a.py", "test_ide_pages_run_b.py"):
        assert basenames.count(name) == 1, (name, args)
    assert not [token for token in args if set("*?[") & set(token)], args


def test_failure_with_a_different_exit_code_on_the_base_stays_gating(tmp_path: Path) -> None:
    """A base failure excuses the patched one only when it is the SAME failure.

    Patched rc=1 (marker is PATCHED) vs base rc=4 (marker is BASE) is the
    measured incident shape: the base copy cannot run the same check at all,
    so the failure must stay gating and no ``base-fails`` row may appear.
    """
    dump = tmp_path / "runs.txt"
    cmd = ("{ pwd; cat marker.txt; } >> " + shlex.quote(str(dump))
           + "; if grep -q BASE marker.txt; then exit 4; else exit 1; fi")
    repo, ref = _target_repo(tmp_path, {"marker.txt": "BASE\n"})
    run = _run_dir(tmp_path, repo, ref, [cmd])

    data = _envelope(_run_verifier(VERIFIER_TEST, repo, run))

    _, markers = _base_markers(dump)
    assert markers.count("PATCHED") == 1, markers
    assert markers.count("BASE") >= 1, markers
    assert not [c for c in data["checks"] if c.get("failure_kind") == "base-fails"], data["checks"]
    assert data["pass"] is False, data
    assert data["failed_checks"], data
    assert "[base-differs]" in (run / "verifier-test.log").read_text()
