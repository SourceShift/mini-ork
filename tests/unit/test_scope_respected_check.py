"""static-check's ``scope-respected`` gate (RB-12).

A kickoff's "Files in scope (touch ONLY these)" used to feed prompt preferences
only: nothing compared it with ``framework-edit.diff``, so a run could publish
edits to a file the kickoff ruled out. These tests pin the new check against
the four shapes the kickoff names:

- an ONLY-scope kickoff and a diff outside the allow list → the check FAILS and
  gates the overall verdict;
- a plain "Files in scope" hint and a diff outside it → recorded as
  non-gating, overall verdict still passes;
- a deny-listed file in the diff → FAIL;
- a glob allow entry matches, and a kickoff with no scope → pass with the
  ``no scope declared`` note.

Hermetic: every fixture is a throwaway git repo plus a throwaway run dir under
``tmp_path``; the verifier runs as a subprocess with ``MINI_ORK_*`` / ``MO_*``
stripped so it can never adopt the live run's identity or reach its DB.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"

ONLY_KICKOFF = """\
# ONLY-scope fixture

## Files in scope (touch ONLY these)

- `src/alpha.py`

Do not modify any other file.
"""

HINT_KICKOFF = """\
# Hint-scope fixture

## Files in scope

- `src/alpha.py`

## Goal

Widen the fix when the change asks for it.
"""

DENY_KICKOFF = """\
# Deny-scope fixture

## Files in scope

- `src/alpha.py`

## Out of scope

- `src/forbidden.py`
"""

GLOB_KICKOFF = """\
# Glob-scope fixture

## Files in scope (touch ONLY these)

- `tests/unit/test_x*.py`
"""

NO_SCOPE_KICKOFF = """\
# Plain fixture

## Goal

Improve the handler.

## Mechanism

Adjust the handler so the load path is shorter.
"""

_NON_GATING_RE = re.compile(r"non[- _]?gating|gating\b.{0,12}\bfalse", re.IGNORECASE)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=spec",
         "-c", "user.email=spec@test.example", "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True).stdout


def _run(tmp_path: Path, kickoff: str, files: dict[str, tuple[str, str]]):
    """Build a repo whose ``git diff`` is the run's diff; run the verifier.

    ``files`` maps a repo-relative path to ``(base, patched)``. The base tree is
    committed, the patched tree is diffed into ``framework-edit.diff``, and the
    checkout is restored so the diff applies forward — the real run's shape.
    """
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo.mkdir()
    run_dir.mkdir()
    _git(repo, "init", "-q")
    for rel, (base, _patched) in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(base)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for rel, (_base, patched) in files.items():
        (repo / rel).write_text(patched)
    diff = _git(repo, "diff")
    _git(repo, "checkout", "--", ".")

    kickoff_path = tmp_path / "kickoff.md"
    kickoff_path.write_text(kickoff)
    (run_dir / "framework-edit.diff").write_text(diff)
    (run_dir / "kickoff.md").write_text(kickoff)
    (run_dir / "run_profile.json").write_text(
        json.dumps({"kickoff_path": str(kickoff_path)}))

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MINI_ORK_", "MO_"))
           and k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MO_TARGET_CWD=str(repo))
    proc = subprocess.run([sys.executable, str(VERIFIER)], cwd=str(run_dir),
                          env=env, capture_output=True, text=True, timeout=240)

    verdict = None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('{"verifier"'):
            verdict = json.loads(line)
            break
    assert verdict is not None, f"verifier printed no verdict:\n{proc.stdout}\n{proc.stderr}"

    outputs = [proc.stdout, proc.stderr]
    for name in ("verifier-static-check.log", "verifier-static-check.checks.tsv",
                 "verdict.json"):
        path = run_dir / name
        if path.is_file():
            outputs.append(path.read_text())
    return verdict, "\n".join(outputs)


def _assert_ran(verdict):
    assert "scope-respected" in verdict.get("checks_run", []), (
        f"scope-respected did not run; checks_run={verdict.get('checks_run')}")


def test_only_scope_unlisted_file_fails_and_gates(tmp_path):
    verdict, _ = _run(tmp_path, ONLY_KICKOFF, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
        "src/sneaky.py": ("S = 1\n", "S = 2\n"),
    })
    _assert_ran(verdict)
    assert "scope-respected" in verdict["failed_checks"]
    assert verdict["pass"] is False


def test_only_scope_listed_file_passes(tmp_path):
    verdict, _ = _run(tmp_path, ONLY_KICKOFF, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
    })
    _assert_ran(verdict)
    assert verdict["failed_checks"] == []
    assert verdict["pass"] is True


def test_hint_scope_unlisted_file_is_non_gating(tmp_path):
    verdict, outputs = _run(tmp_path, HINT_KICKOFF, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
        "src/extra.py": ("E = 1\n", "E = 2\n"),
    })
    _assert_ran(verdict)
    assert "scope-respected" not in verdict["failed_checks"]
    assert verdict["pass"] is True
    assert "src/extra.py" in outputs
    assert _NON_GATING_RE.search(outputs)


def test_deny_listed_file_fails(tmp_path):
    verdict, _ = _run(tmp_path, DENY_KICKOFF, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
        "src/forbidden.py": ("F = 1\n", "F = 2\n"),
    })
    _assert_ran(verdict)
    assert "scope-respected" in verdict["failed_checks"]
    assert verdict["pass"] is False


def test_glob_allow_entry_matches(tmp_path):
    verdict, _ = _run(tmp_path, GLOB_KICKOFF, {
        "tests/unit/test_x1.py": ("VALUE = 1\n", "VALUE = 2\n"),
    })
    _assert_ran(verdict)
    assert verdict["failed_checks"] == []
    assert verdict["pass"] is True


def test_no_scope_declared_passes_with_note(tmp_path):
    verdict, outputs = _run(tmp_path, NO_SCOPE_KICKOFF, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
    })
    _assert_ran(verdict)
    assert verdict["failed_checks"] == []
    assert verdict["pass"] is True
    assert re.search(r"no scope declared", outputs, re.IGNORECASE)


def test_dotslash_allow_entry_is_normalised(tmp_path):
    kickoff = """\
# Dot-scope fixture

## Files in scope (touch ONLY these)

- ./src/alpha.py
"""
    verdict, _ = _run(tmp_path, kickoff, {
        "src/alpha.py": ("X = 1\n", "X = 2\n"),
    })
    _assert_ran(verdict)
    assert verdict["failed_checks"] == []
    assert verdict["pass"] is True
