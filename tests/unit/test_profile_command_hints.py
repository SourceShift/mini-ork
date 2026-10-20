"""RBF-09: prose is never executed as a verification command.

``gen_profile``'s ``verification_command`` is filled ONLY from an explicit
``## Verification commands`` section. Prose harvested by ``command_hints()`` —
the whole-kickoff regex scan — goes to ``verification_hints`` instead, a key no
verifier reads. A command carrying an unexpanded ``<…>`` placeholder is not
runnable as written, so it is dropped from the command slot and kept in hints;
``${MINI_ORK_RUN_DIR}`` is a shell variable the verifier expands and stays.

Hermetic: every kickoff lives in ``tmp_path``.
"""
from __future__ import annotations

import json

import pytest

from mini_ork.cli import main as cli


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in (
        "MO_TARGET_CWD",
        "MO_PLACEMENT",
        "MO_NODE_ENV",
        "MINI_ORK_RUN_ID",
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_DB",
        "MINI_ORK_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _gen(tmp_path, body: str) -> tuple[dict, dict]:
    """Drive the real ``gen_profile``; return (dict, on-disk JSON)."""
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(body, encoding="utf-8")
    profile = tmp_path / "run_profile.json"
    data = cli.gen_profile(
        kickoff_path=str(kickoff),
        root=str(tmp_path),
        recipe="code-fix",
        task_class="code_fix",
        profile_path=str(profile),
        agents_path=str(tmp_path / "agents.yaml"),
        recipe_base=str(tmp_path),
    )
    return data, json.loads(profile.read_text(encoding="utf-8"))


def test_prose_command_never_reaches_verification_command(tmp_path):
    data, on_disk = _gen(
        tmp_path,
        """# Fix the widget parser

Write pytest files into the run dir for this change.

## Goal

Make the widget parser stop eating the third bullet.
""",
    )
    assert data["verification_command"] == []
    assert on_disk["verification_command"] == []
    hints = data["verification_hints"]
    assert isinstance(hints, list) and hints
    assert any("pytest files into" in h for h in hints), hints


def test_explicit_section_fenced_command_is_kept_unchanged(tmp_path):
    cmd = "cd /tmp && python3 -m pytest -q tests/unit/test_widget.py"
    data, on_disk = _gen(
        tmp_path,
        f"""# Fix the widget parser

## Verification commands

```bash
{cmd}
```
""",
    )
    assert data["verification_command"] == [cmd]
    assert on_disk["verification_command"] == [cmd]


def test_declared_placeholder_command_is_dropped(tmp_path):
    data, on_disk = _gen(
        tmp_path,
        """# Fix the widget parser

## Verification commands

```bash
python -m pytest -q <run_dir>/spec-tests
```
""",
    )
    assert data["verification_command"] == []
    assert on_disk["verification_command"] == []
    assert any("<run_dir>" in h for h in data["verification_hints"])


def test_shell_variable_in_declared_command_is_kept(tmp_path):
    cmd = (
        "python -m pytest -q tests/unit "
        '--junitxml="${MINI_ORK_RUN_DIR}/rbf-command-hints-junit.xml"'
    )
    data, on_disk = _gen(
        tmp_path,
        f"""# Fix the widget parser

## Verification commands

```bash
{cmd}
```
""",
    )
    assert data["verification_command"] == [cmd]
    assert on_disk["verification_command"] == [cmd]


def test_placeholder_drop_is_per_command(tmp_path):
    """One placeholder command must not reject the clean command beside it."""
    data, _ = _gen(
        tmp_path,
        """# Fix the widget parser

## Verification commands

```bash
pytest -q <profile_path>/tests
make test
```
""",
    )
    assert data["verification_command"] == ["make test"]
    assert any("<profile_path>" in h for h in data["verification_hints"])


def test_verification_command_key_survives_for_readers(tmp_path):
    _, on_disk = _gen(
        tmp_path,
        """# Fix the widget parser

No runner is named here.
""",
    )
    assert "verification_command" in on_disk
    assert on_disk["verification_command"] == []
