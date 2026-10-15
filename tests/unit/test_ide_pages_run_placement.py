"""A run that executed off this machine says so in its page header."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mini_ork.ide_pages import run as run_page


def _run(tmp_path: Path, profile: dict | None) -> SimpleNamespace:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    if profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile))
    return SimpleNamespace(run_dir=run_dir)


def test_remote_run_gets_a_placement_chip(tmp_path: Path) -> None:
    chip = run_page._placement_chip(_run(tmp_path, {"placement": "remote", "environment": "daytona"}))
    assert chip == {"t": "remote · daytona", "c": "blue"}


def test_local_run_has_no_placement_chip(tmp_path: Path) -> None:
    assert run_page._placement_chip(_run(tmp_path, {"placement": "local"})) is None
    assert run_page._placement_chip(_run(tmp_path / "x", None)) is None
