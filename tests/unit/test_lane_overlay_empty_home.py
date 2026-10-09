"""The per-user lane overlay must reach the dispatch chain even when the
resolver passes an empty ``home``.

Regression for: ``resolve_lane_family(lane)`` and the routing policies call
``_effective_lanes(root, home)`` with ``home=""``. A falsy home used to skip the
overlay outright (``personal_path(home=home) if home else None``), so a run's
snapshot lanes silently outranked the operator's ``agents.local.yaml`` /
``$MINI_ORK_AGENTS`` — the exact "user's lane choice never reached dispatch"
failure the overlay was added to prevent.
"""

from __future__ import annotations

import os

from mini_ork.dispatch.llm_dispatch import _effective_lanes


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_overlay_env_wins_over_snapshot_with_empty_home(tmp_path, monkeypatch):
    snap = tmp_path / "run" / "config"
    _write(str(snap / "agents.yaml"), "lanes:\n  codex_lens: codex\n  implementer: codex\n")
    overlay = tmp_path / "overlay.local.yaml"
    _write(str(overlay), "lanes:\n  codex_lens: deepseek\n  implementer: deepseek\n")

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("MINI_ORK_AGENTS", str(overlay))

    lanes = _effective_lanes("", "") or {}
    assert lanes.get("codex_lens") == "deepseek", lanes
    assert lanes.get("implementer") == "deepseek", lanes


def test_home_local_overlay_wins_when_home_empty(tmp_path, monkeypatch):
    snap = tmp_path / "run" / "config"
    _write(str(snap / "agents.yaml"), "lanes:\n  codex_lens: codex\n")
    home = tmp_path / "home"
    _write(str(home / "config" / "agents.local.yaml"), "lanes:\n  codex_lens: glm\n")

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    lanes = _effective_lanes("", "") or {}
    assert lanes.get("codex_lens") == "glm", lanes
