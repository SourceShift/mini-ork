"""Local instance configs are ignored, and their committed examples are not.

Two classes of file must never reach the repository: secrets, and one machine's
settings. Both have a committed `*.example.yaml` sibling, and both are copied
into place by the operator — so the ignore rules are what make the difference
between "local by design" and "one `git add -A` away from being published".

`config/rsi-cycle.yaml` was in neither state: not committed, not ignored, so it
sat in every `git status` and in reach of a broad `git add`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path],
        capture_output=True,
    )
    return result.returncode == 0


def test_local_instance_configs_are_ignored() -> None:
    for path in ("config/rsi-cycle.yaml", ".mini-ork/config/secrets.local.sh"):
        assert _ignored(path), f"{path} must never be committed"


def test_committed_examples_stay_trackable() -> None:
    """An ignore rule wide enough to hide the example defeats its purpose."""
    for path in ("config/rsi-cycle.example.yaml", "config/providers.yaml"):
        assert not _ignored(path), f"{path} ships with the repo"
