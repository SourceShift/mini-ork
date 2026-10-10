"""Unit tests for the replay anti-gaming changes (kickoff antigaming-b).

Three behaviours, pinned hermetically:

1. Weak fail-to-pass: an overlap whose base failure is a collection/load
   error (pytest ``ERROR``) is ``weak`` — ``weak_overlap`` lists it and
   ``strong_overlap`` is empty. The verifier then requires an ADEQUATE
   suite-adequacy verdict to keep the pass.
2. Strong fail-to-pass: an assertion failure on the base is strong overlap,
   and the verifier pass is byte-identical to today.
3. Flake re-run: when ``MO_REPLAY_FLAKE_RERUN`` is on (default) and the first
   overlap is non-empty, both sides re-run and only the stable intersection
   survives; ids that drop out land in ``replay["flaky"]``.
4. Surviving mutants feed the revise loop: a weak replay whose adequacy audit
   is INADEQUATE with survivors fails (exit 1) while revise rounds remain, and
   abstains with ``adequacy_unverified`` once rounds are exhausted.

The weak/strong/flake classifier is exercised against a fake ``pytest``
executable (no real pytest, no network). The adequacy-routing branches drive
the verifier in-process with a stubbed ``audit_suite``; the strong pass path
drives the verifier as a subprocess against a throwaway git repo.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from mini_ork.certify import replay_check

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "recipes" / "code-fix" / "verifiers" / "test.py"

# `-p no:cacheprovider` keeps `.pytest_cache/` out of the candidate tree; no
# `-q` so pytest prints the per-test lines replay_check parses.
TEST_CMD = f"{sys.executable} -m pytest -p no:cacheprovider"

# Fake pytest prints one per-test line per invocation. `base`/`cand` are the
# directory names replay_check runs in; the base side fails per `mode`.
_FAKE_PYTEST = """#!/usr/bin/env python3
import os
import sys

cwd = os.getcwd()
is_base = os.path.basename(cwd.rstrip("/")) == "base"
mode = {mode!r}

if not is_base:
    print("test_mod.py::test_target PASSED")
    sys.exit(0)

if mode == "weak":
    print("test_mod.py::test_target ERROR")
    sys.exit(1)
if mode == "rerun-dies":
    marker = os.path.join(cwd, ".rerun_dies")
    if os.path.exists(marker):
        sys.exit(2)  # second run crashes with no parsable output
    with open(marker, "w") as fh:
        fh.write("1")
if mode == "flake":
    marker = os.path.join(cwd, ".flake_ran")
    if os.path.exists(marker):
        print("test_mod.py::test_target PASSED")
        sys.exit(0)
    with open(marker, "w") as fh:
        fh.write("1")
print("test_mod.py::test_target FAILED")
sys.exit(1)
"""


def _install_fake_pytest(tmp_path: Path, mode: str) -> Path:
    """Write an executable `pytest` shim into a fresh bin dir and return it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pytest").write_text(_FAKE_PYTEST.format(mode=mode))
    (bindir / "pytest").chmod(0o755)
    return bindir


def _patch_path(monkeypatch, bindir: Path) -> None:
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def _make_trees(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    return base, cand


# ── 1. weak / strong / flake classification (fake runner, no real pytest) ──


def test_weak_import_error_overlap_is_weak(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path)
    _patch_path(monkeypatch, _install_fake_pytest(tmp_path, mode="weak"))

    result = replay_check("pytest", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["weak"] is True, result
    assert result["replay"]["weak_overlap"] == ["test_mod.py::test_target"], result["replay"]
    assert result["replay"]["strong_overlap"] == [], result["replay"]
    assert result["replay"]["flaky"] == [], result["replay"]


def test_strong_assertion_overlap_is_strong(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path)
    _patch_path(monkeypatch, _install_fake_pytest(tmp_path, mode="strong"))

    result = replay_check("pytest", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["weak"] is False, result
    assert result["replay"]["strong_overlap"] == ["test_mod.py::test_target"], result["replay"]
    assert result["replay"]["weak_overlap"] == [], result["replay"]


def test_flaky_base_failure_empties_overlap(tmp_path, monkeypatch):
    base, cand = _make_trees(tmp_path)
    _patch_path(monkeypatch, _install_fake_pytest(tmp_path, mode="flake"))

    result = replay_check("pytest", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is False, result
    assert result["replay"]["overlap"] == [], result["replay"]
    assert result["replay"]["flaky"] == ["test_mod.py::test_target"], result["replay"]
    assert "tests-do-not-exercise-change" in result["reason"], result
    assert "test_mod.py::test_target" in result["reason"], result


def test_flake_rerun_disabled_keeps_single_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_REPLAY_FLAKE_RERUN", "0")
    base, cand = _make_trees(tmp_path)
    _patch_path(monkeypatch, _install_fake_pytest(tmp_path, mode="flake"))

    result = replay_check("pytest", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["passed"] is True, result
    assert result["replay"]["overlap"] == ["test_mod.py::test_target"], result["replay"]
    assert result["replay"]["flaky"] == [], result["replay"]


# ── 2. strong overlap: the verifier pass is as before (real temp git repo) ──


def _git(cwd: Path, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(parent: Path, *, mod_src: str, test_src: str) -> Path:
    repo = parent / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "mod.py").write_text(mod_src)
    (repo / "test_mod.py").write_text(test_src)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_verifier(repo: Path, tmp_path: Path, *, run_id: str) -> dict:
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(mini_home)
    env["MINI_ORK_RUN_ID"] = run_id
    env["MO_CODEFIX_REPLAY"] = "1"
    env["MO_SUITE_ADEQUACY"] = "0"
    env["MINI_ORK_TEST_CMD"] = TEST_CMD
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120,
    )
    last = proc.stdout.strip().splitlines()
    assert last, (
        f"verifier produced no JSON: rc={proc.returncode} "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return json.loads(last[-1])


def test_verifier_strong_overlap_passes_as_before(tmp_path):
    repo = _make_repo(
        tmp_path,
        mod_src="def add(a, b):\n    return a - b\n",
        test_src="from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b\n")

    out = _run_verifier(repo, tmp_path, run_id="antigaming-strong")

    assert out["pass"] is True, out
    assert out["error_summary"] == "post-patch suite green; replay: tests exercise the change", out
    assert any("test_add" in tid for tid in out["replay"]["strong_overlap"]), out["replay"]
    assert out["replay"]["weak_overlap"] == [], out["replay"]
    assert out["replay"]["flaky"] == [], out["replay"]


# ── 3. adequacy routing for a weak replay (in-process, stubbed audit) ────────

_WEAK_REPLAY = {
    "passed": True,
    "reason": "tests exercise the change (delta-gate overlap)",
    "unverified": False,
    "weak": True,
    "replay": {
        "candidate_passed": ["test_mod.py::test_target"],
        "base_failed": ["test_mod.py::test_target"],
        "overlap": ["test_mod.py::test_target"],
        "weak_overlap": ["test_mod.py::test_target"],
        "strong_overlap": [],
        "flaky": [],
    },
}

_ADEQUATE = {"verdict": "ADEQUATE", "reason": "score 1.0 >= 0.6", "score": 1.0, "survivors": []}
_NOT_APPLICABLE = {"verdict": "NOT_APPLICABLE", "reason": "no-sources", "score": None, "survivors": []}
_INADEQUATE_SURVIVORS = {
    "verdict": "INADEQUATE",
    "reason": "score 0.0 < 0.6",
    "score": 0.0,
    "survivors": [
        {"id": "M01", "file": "mod.py", "line": 2, "operator": "arith_swap",
         "original": "a - b", "mutated": "a + b"},
    ],
}


def _load_verifier(monkeypatch, tmp_path, run_id, *, revise_rounds=None):
    """Load the code-fix verifier as a module (its `__main__` guard does not
    fire) with a controlled env: a green test command, no tamper guard, and an
    explicit (or default) MO_REVISE_ROUNDS."""
    mini_home = tmp_path / "mo-home"
    mini_home.mkdir(exist_ok=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(mini_home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    monkeypatch.setenv("MINI_ORK_TEST_CMD", f"{sys.executable} -c 'pass'")
    monkeypatch.setenv("MO_CODEFIX_REPLAY", "1")
    monkeypatch.setenv("MO_SUITE_ADEQUACY", "1")
    monkeypatch.setenv("MO_ALLOW_TEST_CHANGES", "1")
    if revise_rounds is None:
        monkeypatch.delenv("MO_REVISE_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("MO_REVISE_ROUNDS", str(revise_rounds))

    spec = importlib.util.spec_from_file_location("_codefix_test_verifier", VERIFIER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_revise_current(mod, data: dict) -> None:
    rev_dir = Path(mod.LOG_DIR) / "revise"
    rev_dir.mkdir(parents=True, exist_ok=True)
    (rev_dir / "current.json").write_text(json.dumps(data))


def _run_main(mod, monkeypatch, audit_verdict, capsys, *, replay=None):
    from mini_ork.gates import suite_adequacy as sa

    monkeypatch.setattr(mod, "run_suite", lambda log, env=None: 0)
    # Isolate the weak-f2p/adequacy logic from the replay-applicability gate
    # (decided off the test command BEFORE any replay runs). These unit tests pin
    # the post-replay decision, so the gate is stubbed "applicable" — exactly as
    # ``_run_replay_check`` is already stubbed. The gate itself is covered by
    # tests/unit/test_verify_levels.py.
    monkeypatch.setattr(mod, "_replay_applies", lambda *a, **k: True)
    monkeypatch.setattr(
        mod, "_run_replay_check",
        lambda: dict(replay if replay is not None else _WEAK_REPLAY),
    )
    monkeypatch.setattr(
        sa, "audit_suite",
        lambda repo_dir, test_cmd, source_files, report_path=None: audit_verdict,
    )
    rc = mod.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "verifier printed no JSON"
    return rc, json.loads(out[-1])


def test_weak_replay_requires_adequate_not_applicable_abstains(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "weak-na")

    rc, out = _run_main(mod, monkeypatch, _NOT_APPLICABLE, capsys)

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["weak_f2p_unverified"] is True, out
    assert "adequacy_unverified" not in out, out


def test_weak_replay_adequate_passes(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "weak-adequate")

    rc, out = _run_main(mod, monkeypatch, _ADEQUATE, capsys)

    assert rc == 0, out
    assert out["pass"] is True, out
    assert "suite adequacy ADEQUATE" in out["error_summary"], out
    assert "weak_f2p_unverified" not in out, out


def test_weak_surviving_mutants_rounds_remain_fails(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "weak-survivors-remain", revise_rounds=2)

    rc, out = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    assert rc == 1, out
    assert out["pass"] is False, out
    assert "mod.py:2" in out["error_summary"], out
    assert "strengthen the assertions so these mutants fail" in out["error_summary"], out


def test_weak_surviving_mutants_rounds_exhausted_abstains(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "weak-survivors-exhausted", revise_rounds=2)
    _write_revise_current(mod, {"round": 2, "max_rounds": 2})

    rc, out = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["adequacy_unverified"] is True, out
    assert "weak_f2p_unverified" not in out, out


def test_weak_surviving_mutants_revise_disabled_abstains(tmp_path, monkeypatch, capsys):
    mod = _load_verifier(monkeypatch, tmp_path, "weak-survivors-disabled", revise_rounds=0)

    rc, out = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    assert rc == 0, out
    assert out["pass"] is False, out
    assert out["adequacy_unverified"] is True, out
    assert "weak_f2p_unverified" not in out, out


def test_unusable_rerun_abstains_instead_of_flaky(tmp_path, monkeypatch):
    """An infra hiccup on the flake re-run must not be called flakiness and
    fail the patch: it abstains like an unrunnable first run."""
    base, cand = _make_trees(tmp_path)
    _patch_path(monkeypatch, _install_fake_pytest(tmp_path, mode="rerun-dies"))

    result = replay_check("pytest", base_cwd=str(base), candidate_cwd=str(cand))

    assert result["unverified"] is True, result
    assert result["passed"] is False, result
    assert "cannot confirm the overlap" in result["reason"], result
    assert result["replay"] is None, result


def test_rounds_remaining_respects_mo_revise_rounds_cap(tmp_path, monkeypatch, capsys):
    """MO_REVISE_ROUNDS below the edge max: round 1 of an edge max of 2 is the
    runtime's LAST granted round when the cap is 1, so the verifier abstains."""
    mod = _load_verifier(monkeypatch, tmp_path, "weak-survivors-capped", revise_rounds=1)
    _write_revise_current(mod, {"round": 1, "max_rounds": 2})

    rc, out = _run_main(mod, monkeypatch, _INADEQUATE_SURVIVORS, capsys)

    assert rc == 0, out
    assert out["adequacy_unverified"] is True, out
