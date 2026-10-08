"""Stale pytest basetemps are pruned; live ones and the current-session link are not."""
from __future__ import annotations

import os
from pathlib import Path

from scratch_prune import prune_stale_basetemps

NOW = 1_800_000_000.0
HOUR = 3600.0


def _basetemp(root: Path, name: str, age_s: float) -> Path:
    path = root / name
    (path / "test_x0").mkdir(parents=True)
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


def test_prunes_only_stale_basetemps(tmp_path):
    stale = _basetemp(tmp_path, "pytest-10", 5 * HOUR)
    live = _basetemp(tmp_path, "pytest-11", 0.5 * HOUR)
    (tmp_path / "pytest-current").symlink_to(live)

    removed = prune_stale_basetemps(tmp_path, max_age_s=3 * HOUR, now=NOW)

    assert removed == [stale]
    assert not stale.exists()
    assert live.exists() and (tmp_path / "pytest-current").is_symlink()


def test_never_removes_the_kept_basetemp_even_when_old(tmp_path):
    kept = _basetemp(tmp_path, "pytest-12", 10 * HOUR)

    assert prune_stale_basetemps(tmp_path, max_age_s=HOUR, keep=kept, now=NOW) == []
    assert kept.exists()


def test_missing_root_is_a_no_op(tmp_path):
    assert prune_stale_basetemps(tmp_path / "absent", max_age_s=HOUR, now=NOW) == []


def test_ignores_non_basetemp_entries(tmp_path):
    other = tmp_path / "notes"
    other.mkdir()
    os.utime(other, (NOW - 10 * HOUR, NOW - 10 * HOUR))

    assert prune_stale_basetemps(tmp_path, max_age_s=HOUR, now=NOW) == []
    assert other.exists()
