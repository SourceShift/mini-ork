"""``gen_profile`` stores the kickoff's explicit ``## Verification commands``
section verbatim, and the framework-edit verifier runs every command it finds
without truncation.

Three concrete defects were observed in the wild (run-1791853722-board-k3):

1. ``command_hints()`` regex-scanned the WHOLE kickoff for prose mentions of
   the test runner and produced ``pytest -q …`` in slot 1.
2. ``commands[:3]`` then pushed the kickoff's real ``ruff check …`` past the
   cap.
3. ``_epic_verification_commands`` in
   ``recipes/framework-edit/verifiers/test.py`` added its own silent ``[:4]``
   cap on top.

These tests drive the integration surface the way ``test_verifier_test_env.py``
does: real ``gen_profile`` + the framework-edit verifier's
``_epic_verification_commands`` loaded with a stubbed ``subprocess.run``.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
FW_VERIFIER_DIR = REPO_ROOT / "recipes" / "framework-edit" / "verifiers"

sys.path.insert(0, str(REPO_ROOT))
from mini_ork.cli import main as cli


# ── 1. gen_profile stores the explicit section verbatim ──────────────────────


def _kickoff(tmp_path: Path, text: str) -> Path:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(text, encoding="utf-8")
    return kickoff


def _agents(tmp_path: Path) -> Path:
    agents = tmp_path / "agents.yaml"
    agents.write_text("lanes:\n  implementer: codex\n", encoding="utf-8")
    return agents


def _drive_gen_profile(tmp_path: Path, kickoff_text: str) -> dict:
    """Drive the real ``gen_profile`` against a tmp workspace."""
    return cli.gen_profile(
        _kickoff(tmp_path, kickoff_text),
        tmp_path,
        "code-fix",
        "code_fix",
        tmp_path / "run_profile.json",
        _agents(tmp_path),
    )


def test_explicit_section_exact_order_no_prose_leak(tmp_path):
    """Prose that names the test runner must not become a ``verification_command``
    entry. The three explicit bullets reach the profile in order, backticks
    stripped, with no cap. One of them is ``ruff check x.py`` — exactly the
    kind of command the regex scan never knew about."""
    data = _drive_gen_profile(
        tmp_path,
        """# Ship it

## Goal

Remove the unused import of the test runner so the suite below runs cleanly.

## Verification commands
- `pytest tests/unit/test_widget.py`
- `python3.11 -m pytest -q tests/unit/test_widget.py`
- `ruff check x.py`
""",
    )

    assert data["verification_command"] == [
        "pytest tests/unit/test_widget.py",
        "python3.11 -m pytest -q tests/unit/test_widget.py",
        "ruff check x.py",
    ]


def test_five_commands_no_cap(tmp_path):
    """An explicit section with five commands keeps all five in the profile."""
    data = _drive_gen_profile(
        tmp_path,
        """# Do the thing

## Verification commands
- `make c1`
- `make c2`
- `make c3`
- `make c4`
- `make c5`
""",
    )

    assert data["verification_command"] == [
        "make c1", "make c2", "make c3", "make c4", "make c5"]


def test_prose_fallback_unchanged_when_no_section(tmp_path):
    """A kickoff with no explicit section still uses ``command_hints()`` with
    its cap of three — that path is untouched. The kickoff's prose mentions
    ``pytest`` and ``ruff``, so the regex scan surfaces both, capped at 3."""
    data = _drive_gen_profile(
        tmp_path,
        """# No header at all

Run pytest tests/unit/test_widget.py and ruff check mini_ork/cli/main.py when done.
""",
    )

    cmds = data["verification_command"]
    assert len(cmds) <= 3, f"prose fallback must stay capped at three, got {cmds}"
    assert cmds, "prose fallback must surface something when runners are named"
    # Both runners appear at least once across the captured commands.
    joined = " ".join(cmds)
    assert "pytest" in joined
    assert "ruff" in joined


def test_explicit_section_dedups_repeated_bullets(tmp_path):
    """Duplicates in the explicit section collapse to a single entry — but the
    first occurrence wins. Surrounding backticks are stripped on each."""
    data = _drive_gen_profile(
        tmp_path,
        """# Repeat

## Verification commands
- `pytest tests/unit/test_widget.py`
- `pytest tests/unit/test_widget.py`
- pytest tests/unit/test_widget.py
""",
    )

    assert data["verification_command"] == ["pytest tests/unit/test_widget.py"]


# ── 2. The verifier drops its own silent cap ────────────────────────────────
#
# ``tests/unit/test_verifier_test_env.py`` already loads the framework-edit
# verifier with a stubbed ``subprocess.run`` and a tmp worktree; this harness
# is the canonical pattern, reused here to drive ``_epic_verification_commands``
# against a profile that carries five commands.


def _load_fwedit_verifier(monkeypatch, tmp_path):
    """Import the framework-edit verifier with the same stubbing shape
    ``test_verifier_test_env.py`` uses, so we can drive
    ``_epic_verification_commands`` without spinning up a real git worktree."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    work_parent = run_dir / "verifier-test-work"
    worktree = work_parent / "repo"
    worktree.mkdir(parents=True)

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")

    sys.path.insert(0, str(FW_VERIFIER_DIR))

    captured: list[dict] = []

    def fake_run(argv, **_kwargs):
        captured.append({"argv": argv})
        if isinstance(argv, list) and "rev-parse" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=str(worktree.resolve()).encode() + b"\n",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    spec = importlib.util.spec_from_file_location(
        "_fwedit_test_verifier", FW_VERIFIER_DIR / "test.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def _profile(mod, tmp_path, payload: dict) -> None:
    (tmp_path / "run" / "run_profile.json").write_text(json.dumps(payload))


def test_verifier_returns_every_command_when_profile_carries_five(monkeypatch, tmp_path):
    """The verifier's own silent ``[:4]`` cap is removed, so a profile with
    five commands surfaces all five — driven the way
    ``tests/unit/test_verifier_test_env.py`` loads the verifier."""
    mod = _load_fwedit_verifier(monkeypatch, tmp_path)
    cmds = [f"cmd-{i}" for i in range(5)]
    _profile(mod, tmp_path, {"verification_command": cmds})

    assert mod._epic_verification_commands() == cmds


def test_verifier_keeps_dedupe_for_small_lists(monkeypatch, tmp_path):
    """Removing the cap must not change behaviour for profiles with four or
    fewer commands — dedup and ordering stay identical."""
    mod = _load_fwedit_verifier(monkeypatch, tmp_path)
    _profile(mod, tmp_path, {
        "verification_command": ["a", "b", "a", "c", "b"]})

    assert mod._epic_verification_commands() == ["a", "b", "c"]


def test_verifier_drops_surrounding_backticks(monkeypatch, tmp_path):
    """The verifier's backtick-strip + dedup path stays a single-shot for the
    non-truncated case."""
    mod = _load_fwedit_verifier(monkeypatch, tmp_path)
    _profile(mod, tmp_path, {
        "verification_command": ["`make test`", "`make test`", "make lint"]})

    assert mod._epic_verification_commands() == ["make test", "make lint"]


# ── helpers ────────────────────────────────────────────────────────────────
#
# Each gen_profile test takes pytest's ``tmp_path`` fixture directly. The four
# gen_profile tests live at the top of the file so the explicit-section story is
# easy to read end-to-end.