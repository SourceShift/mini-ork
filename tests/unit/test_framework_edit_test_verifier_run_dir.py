"""framework-edit test verifier must keep MINI_ORK_RUN_DIR for kickoff commands.

Run ``sde-3b2-ledger-20261015-142338`` failed with every test green (84 passed).
The framework-edit test verifier's ``epic-verification-commands-pass`` check ran
a kickoff command that writes ``--junitxml="${MINI_ORK_RUN_DIR}/ledger-junit.xml"``
and crashed with ``OSError: [Errno 30] Read-only file system: '/ledger-junit.xml'``:
``_epic_verification_commands_pass`` runs the kickoff's acceptance commands under
``scrubbed_test_env()``, which drops ``MINI_ORK_RUN_DIR`` — so the variable
expanded to the empty string and the artifact path became ``/ledger-junit.xml``.

Kickoffs now declare run-local artifacts under ``${MINI_ORK_RUN_DIR}`` (the
kickoff-contract verifier, ``a7caba16``), so rejecting the reference is not an
option. The run dir is a run-local path, not a credential: the verifier restores
that ONE variable after scrubbing and leaves GLM_API_KEY et al. dropped.

These tests load the verifier module with ``importlib`` against a temp run dir
and a temp target repo — the throwaway copy is built by the verifier itself, the
way the sibling framework-edit verifier tests drive it — so they need no network
and no real provider. The module-level checks run on import (that is the
verifier's actual behavior), so ``_epic_verification_commands_pass`` executes for
real against the ``verification_command`` the temp ``run_profile.json`` declares.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "test.py"
CHECK = "epic-verification-commands-pass"

# The kickoff's probe command, verbatim: it can only write where the child's
# MINI_ORK_RUN_DIR points, so a bump on this row is exactly the scrub leak.
PROBE_CMD = (
    'python3 -c "import os,sys; '
    "open(os.environ['MINI_ORK_RUN_DIR'] + '/probe.txt','w').write('ok')\""
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _target_repo(tmp: Path) -> Path:
    """A minimal git repo for the verifier to archive and patch."""
    root = tmp / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("VALUE = 1\n")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _diff_for(root: Path) -> str:
    (root / "mod.py").write_text("VALUE = 2\n")
    diff = _git(root, "diff")
    _git(root, "checkout", "-q", "--", "mod.py")
    return diff


def _prepare(tmp: Path, command: str) -> tuple[Path, Path]:
    """A temp run dir declaring ``command`` plus the target repo it verifies."""
    target = _target_repo(tmp)
    run_dir = tmp / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(_diff_for(target))
    (run_dir / "run_profile.json").write_text(
        json.dumps({"verification_command": [command]}))
    return run_dir, target


def _load_verifier(monkeypatch, run_dir: Path, target: Path, name: str):
    """Import the verifier against ``run_dir``/``target``; the module-level
    checks — including ``_epic_verification_commands_pass`` — run during load.

    ``MINI_ORK_*``/``MO_*`` are cleared first so an inherited ``MO_TARGET_CWD``
    from the surrounding test session cannot redirect the copy at the real repo.
    """
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_ROOT", str(target))
    monkeypatch.setenv("MO_TARGET_CWD", str(target))

    saved_path = list(sys.path)
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    module = importlib.util.module_from_spec(spec)
    try:
        with contextlib.suppress(SystemExit):  # the verifier ends with sys.exit(0)
            spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    return module


def _rows(run_dir: Path) -> dict[str, str]:
    out = {}
    for line in (run_dir / "verifier-test.checks.tsv").read_text().splitlines():
        cid, _, val = line.split("\t")[:3]
        out[cid] = val
    return out


def test_kickoff_command_can_write_into_the_run_dir(tmp_path: Path, monkeypatch) -> None:
    """The probe command resolves ${MINI_ORK_RUN_DIR} and writes into it."""
    run_dir, target = _prepare(tmp_path, PROBE_CMD)
    _load_verifier(monkeypatch, run_dir, target, "fe_verifier_rundir_probe")

    assert (run_dir / "probe.txt").read_text() == "ok", \
        "MINI_ORK_RUN_DIR was not in the kickoff command's env"
    assert _rows(run_dir)[CHECK] == "true", _rows(run_dir)


def test_credentials_stay_scrubbed_for_kickoff_commands(tmp_path: Path,
                                                        monkeypatch) -> None:
    """A provider credential in the parent env must not reach the child."""
    monkeypatch.setenv("GLM_API_KEY", "x")
    dump = (
        'python3 -c "import os,json; '
        "json.dump(sorted(k for k in os.environ if k.endswith('_API_KEY')), "
        "open(os.environ['MINI_ORK_RUN_DIR'] + '/env-keys.json','w'))\""
    )
    run_dir, target = _prepare(tmp_path, dump)
    _load_verifier(monkeypatch, run_dir, target, "fe_verifier_rundir_scrub")

    # The dump itself only lands if MINI_ORK_RUN_DIR survived — so a present
    # file with no API-key names proves both halves at once.
    assert json.loads((run_dir / "env-keys.json").read_text()) == [], \
        "a provider credential leaked into the kickoff command's env"


def test_scrubbed_env_is_where_run_dir_is_lost() -> None:
    """Teeth: the scrub is what removes MINI_ORK_RUN_DIR, so a verifier that
    does not restore it cannot hand the kickoff its own run dir."""
    sys.path.insert(0, str(REPO))
    try:
        from mini_ork.verify.test_env import scrubbed_test_env
    finally:
        sys.path.pop(0)
    scrubbed = scrubbed_test_env({"MINI_ORK_RUN_DIR": "/run", "GLM_API_KEY": "x",
                                 "PATH": "/bin"})
    assert "MINI_ORK_RUN_DIR" not in scrubbed
    assert "GLM_API_KEY" not in scrubbed
    assert scrubbed["PATH"] == "/bin"
