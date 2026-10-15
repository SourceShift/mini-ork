"""The code-fix test verifier must run the run's declared verification command.

``detect_test_cmd()`` used to ignore ``run_profile.json`` and auto-detect a bare
runner (``pytest`` / ``python3 -m pytest``) in the run's cwd. On a code-fix run
against a large repo that collected the whole suite (run-1791930074-82118:
9113 tests), blew the 120 s deadline, and killed the run — while the patch was
correct and had already self-verified against its one-file command.

These tests pin the new tier: the profile's ``verification_command`` is read
from ``${MINI_ORK_RUN_DIR}/run_profile.json`` (falling back to
``${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}/run_profile.json``), ranked between
an explicit ``MINI_ORK_TEST_CMD`` and the auto-detect chain, and it fails OPEN:
a missing/unreadable/malformed/empty profile falls through to auto-detect
exactly as before.

Two harnesses, mirroring ``tests/unit/test_codefix_red_base.py``:

* the throwaway-repo subprocess harness (``_make_repo`` / ``_run_verifier``)
  proves end-to-end wiring — the profile command actually runs — by sentinel
  side effect, not by reading a log line alone;
* the in-process loader (``_load_verifier``) drives ``detect_test_cmd()``
  directly so the fail-open matrix and the join rules are asserted without
  standing up a suite.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

# The verifier imports ``mini_ork.*`` by full subpath at module import; make the
# checkout importable when these tests run from a bare pytest (same seam
# test_profile_verification_commands.py uses).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _sentinel_cmd(name: str) -> str:
    """A shell command that writes ``name`` into cwd and exits 0.

    Used as the profile's ``verification_command``: the auto-detected runner
    (pytest over the repo) never writes this file, so its presence is proof the
    profile command — not a fallback — was the one executed.
    """
    return (
        'python3 -c "import pathlib;'
        f"pathlib.Path('{name}').write_text('1')\""
    )


PROFILE_CMD = _sentinel_cmd("profile_ran.txt")
EXPLICIT_CMD = _sentinel_cmd("explicit_ran.txt")


# ── throwaway-repo harness (end-to-end, side-effect proven) ─────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path, files: dict[str, str]) -> Path:
    """Stand up a throwaway git repo with the given files (initial commit)."""
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_verifier(repo: Path, tmp_path: Path, *, run_id: str,
                  profile_text: str | None = None,
                  extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Invoke the verifier on ``repo`` with ``run_profile.json`` planted.

    ``MINI_ORK_TEST_CMD`` is explicitly dropped (the red_base harness sets it
    unconditionally) so the profile tier is exercised rather than shadowed;
    pass it back via ``extra_env`` for the precedence tests.
    """
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    if profile_text is not None:
        (run_dir / "run_profile.json").write_text(profile_text, encoding="utf-8")
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MO_CODEFIX_REPLAY"] = "0"
    env["MO_SUITE_ADEQUACY"] = "0"
    env.pop("MINI_ORK_TEST_CMD", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120,
    )


def _repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path, {
        "mod.py": "def add(a, b):\n    return a + b\n",
        "test_ok.py": (
            "from mod import add\n\n\n"
            "def test_ok():\n"
            "    assert add(1, 2) == 3\n"
        ),
    })


# ── 1. the profile command is used (and actually runs) ──────────────────────


def test_profile_command_is_used(tmp_path):
    """The profile's command runs even though a bare pytest would succeed.

    Proved by the sentinel side effect, not by a log line: the auto-detected
    runner would run ``test_ok.py`` and write nothing, so ``profile_ran.txt``
    existing can only mean the profile command was the executed one.
    """
    repo = _repo(tmp_path)

    proc = _run_verifier(
        repo, tmp_path, run_id="cfprofile-used",
        profile_text=json.dumps({"verification_command": [PROFILE_CMD],
                                 "kickoff_path": ""}),
    )

    assert (repo / "profile_ran.txt").is_file(), proc.stderr
    assert proc.returncode == 0, proc.stderr


# ── 2. an explicit MINI_ORK_TEST_CMD still wins over the profile ────────────


def test_explicit_test_cmd_wins_over_profile(tmp_path):
    repo = _repo(tmp_path)

    proc = _run_verifier(
        repo, tmp_path, run_id="cfprofile-explicit",
        profile_text=json.dumps({"verification_command": [PROFILE_CMD],
                                 "kickoff_path": ""}),
        extra_env={"MINI_ORK_TEST_CMD": EXPLICIT_CMD},
    )

    assert (repo / "explicit_ran.txt").is_file(), proc.stderr
    assert not (repo / "profile_ran.txt").exists(), proc.stderr


# ── 3. the resolved command is echoed into the evidence stream ──────────────


def test_resolved_command_is_echoed(tmp_path):
    repo = _repo(tmp_path)

    proc = _run_verifier(
        repo, tmp_path, run_id="cfprofile-echo",
        profile_text=json.dumps({"verification_command": [PROFILE_CMD],
                                 "kickoff_path": ""}),
    )

    cmd_lines = [ln for ln in proc.stderr.splitlines()
                 if ln.startswith("[test] cmd:")]
    assert cmd_lines, proc.stderr
    assert cmd_lines[0] == f"[test] cmd: {PROFILE_CMD}", proc.stderr


# ── in-process loader (direct detect_test_cmd() calls) ──────────────────────

_LOAD_COUNTER = itertools.count()


def _load_verifier(monkeypatch, tmp_path: Path, *, run_dir: Path | None = None,
                   home: Path | None = None, run_id: str = "cfprofile-run",
                   drop_run_dir: bool = False):
    """Import the verifier script fresh under a controlled profile-path env.

    A fresh module per call keeps ``MINI_ORK_HOME`` / ``MINI_ORK_RUN_ID`` — read
    into module globals at import — honest for the HOME-fallback path.
    ``MINI_ORK_TEST_CMD`` is dropped so ``detect_test_cmd()`` reaches the
    profile tier.
    """
    monkeypatch.setenv("MINI_ORK_HOME",
                       str(home if home is not None else tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    monkeypatch.delenv("MINI_ORK_TEST_CMD", raising=False)
    if drop_run_dir:
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    else:
        monkeypatch.setenv(
            "MINI_ORK_RUN_DIR",
            str(run_dir if run_dir is not None else tmp_path / "run"),
        )
    name = f"codefix_verifier_uut_{next(_LOAD_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, VERIFIER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plant(run_dir: Path, text: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(text, encoding="utf-8")
    return run_dir


# ── 4. the fail-open matrix: every bad profile falls through, never raises ──


@pytest.mark.parametrize("label,profile_text", [
    ("missing_file", None),
    ("malformed_json", "{ this is not json"),
    ("no_verification_command_key", json.dumps({"kickoff_path": ""})),
    ("empty_list", json.dumps({"verification_command": []})),
    ("whitespace_only_string", json.dumps({"verification_command": "   "})),
    ("not_a_list_or_string", json.dumps({"verification_command": {"a": 1}})),
    ("null", json.dumps({"verification_command": None})),
])
def test_bad_profile_falls_through_to_autodetect(monkeypatch, tmp_path, label,
                                                 profile_text):
    """Each malformed/absent profile must equal the no-profile auto-detect result.

    The control call (profile path pointing at a directory with no
    ``run_profile.json``) is the pre-change behaviour; a bad profile must not
    change ``detect_test_cmd()`` at all.
    """
    control = _load_verifier(
        monkeypatch, tmp_path, run_dir=tmp_path / "absent-run",
    ).detect_test_cmd()

    run_dir = tmp_path / "run"
    if profile_text is not None:
        _plant(run_dir, profile_text)
    else:
        run_dir.mkdir(parents=True, exist_ok=True)

    actual = _load_verifier(
        monkeypatch, tmp_path, run_dir=run_dir,
    ).detect_test_cmd()

    assert actual == control, (label, actual, control)
    assert "profile_ran.txt" not in actual, (label, actual)


# ── 5. the HOME fallback is used when MINI_ORK_RUN_DIR is unset ─────────────


def test_home_fallback_used_when_run_dir_unset(monkeypatch, tmp_path):
    home = tmp_path / "home"
    run_id = "cfprofile-fallback"
    _plant(home / "runs" / run_id,
           json.dumps({"verification_command": [PROFILE_CMD]}))

    mod = _load_verifier(monkeypatch, tmp_path, home=home, run_id=run_id,
                         drop_run_dir=True)

    assert mod._profile_verification_command() == PROFILE_CMD
    assert mod.detect_test_cmd() == PROFILE_CMD


# ── 6. list join: stripped, deduped, order preserved, `` && ``-joined ───────


def test_list_commands_are_normalized_and_joined(monkeypatch, tmp_path):
    run_dir = _plant(tmp_path / "run", json.dumps({
        "verification_command": [
            "python3 -m pytest a.py",
            "`ruff check .`",
            "python3 -m pytest a.py",   # duplicate — dropped, order kept
            "   ",                       # blank — dropped
        ],
    }))

    mod = _load_verifier(monkeypatch, tmp_path, run_dir=run_dir)

    expected = "python3 -m pytest a.py && ruff check ."
    assert mod._profile_verification_command() == expected
    assert mod.detect_test_cmd() == expected


# ── 7. a bare string is accepted (not just a list) ──────────────────────────


def test_string_command_is_accepted(monkeypatch, tmp_path):
    run_dir = _plant(tmp_path / "run",
                     json.dumps({"verification_command": "`pytest -q test_tally.py`"}))

    mod = _load_verifier(monkeypatch, tmp_path, run_dir=run_dir)

    assert mod._profile_verification_command() == "pytest -q test_tally.py"
    assert mod.detect_test_cmd() == "pytest -q test_tally.py"


# ── 8. source-level pins: the tier is wired ahead of the auto-detect chain ──


def test_verifier_source_ranks_profile_between_override_and_autodetect():
    src = VERIFIER.read_text(encoding="utf-8")
    assert "def _profile_verification_command(" in src
    assert 'data.get("verification_command")' in src
    # The profile tier is inserted after the override and before the npm chain.
    override_at = src.index('if os.environ.get("MINI_ORK_TEST_CMD"):')
    profile_at = src.index("profile_cmd = _profile_verification_command()")
    npm_at = src.index('if os.path.isfile("package.json"):')
    assert override_at < profile_at < npm_at
    # The resolved command is echoed for observability.
    assert 'sys.stderr.write(f"[test] cmd: {CMD}\\n")' in src
