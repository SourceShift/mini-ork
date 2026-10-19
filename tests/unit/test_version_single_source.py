"""The package version has exactly one source.

Four hand-maintained copies had drifted: ``--help`` said ``v0.1`` while
``mini-ork version`` said ``0.9.0``, and nothing noticed. These tests pin the
contract — ``pyproject.toml`` reads ``mini_ork/version.py``, and every display
surface reads the same value from the same place.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from mini_ork.cli import main as cli_main
from mini_ork.version import __version__

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_reads_the_version_from_the_module():
    data = _pyproject()
    assert "version" not in data["project"], "static version reintroduced"
    assert data["project"]["dynamic"] == ["version"]
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "mini_ork.version.__version__"


def test_help_header_matches_the_package_version():
    first = cli_main._HELP.splitlines()[0]
    assert f"(v{__version__})" in first


def test_version_subcommand_prints_the_package_version(capsys):
    assert cli_main._version_handler([], None) == 0
    assert f"mini-ork {__version__} " in capsys.readouterr().out


def test_no_hand_maintained_version_copies_remain():
    targets = [ROOT / "mini_ork/cli/main.py", ROOT / "mini_ork/acp/agent.py"]
    literal = re.compile(rf"""["']{re.escape(__version__)}["']""")
    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert not literal.search(text), f"{path} hardcodes the version"
        assert "__version__" in text, f"{path} does not read mini_ork.version"
