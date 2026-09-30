"""Per-user lane-config overlay (``mini_ork.dispatch.agents_config``).

Verifies the kickoff's nine cases:

  1. no overlay → ``effective_path()`` == template path; nothing written;
  2. overlay overrides one lane and keeps every other template lane;
  3. ``$MINI_ORK_AGENTS`` pointing elsewhere wins over ``agents.local.yaml``;
  4. null in overlay removes a key; lists are replaced, not concatenated;
  5. malformed overlay raises ``ValueError`` naming the file;
  6. ``config_resolve.snapshot_run_config(run_dir)`` freezes the MERGED content;
  7. ``decision_service.default_lane('implementer')`` returns the overlay's value;
  8. template bytes are unchanged after every test;
  9. ``git check-ignore`` reports ``.mini-ork/config/agents.local.yaml`` ignored
     in the worktree (skipped when git is unavailable).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_FOUR_VARS = (
    "MINI_ORK_RUN_DIR",
    "MINI_ORK_HOME",
    "MINI_ORK_ROOT",
    "MINI_ORK_AGENTS",
)
_NONEXISTENT = "/nonexistent/__mo_overlay_fixture__"


def _with_env(overrides: dict | None = None, drop: tuple[str, ...] = ()) -> dict:
    """Save the four vars, drop any extra, apply overrides. Pair with
    ``_restore_env`` so pytest's collection order cannot leak env between
    fixtures."""
    saved = {k: os.environ.pop(k, None) for k in _FOUR_VARS}
    for k in drop:
        os.environ.pop(k, None)
    if overrides:
        for k, v in overrides.items():
            os.environ[k] = v
    return saved


def _restore_env(saved: dict) -> None:
    for k in _FOUR_VARS:
        os.environ.pop(k, None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def _seed_template(home: Path, lanes: dict[str, str]) -> None:
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    body = "lanes:\n" + "".join(f"  {k}: {v}\n" for k, v in lanes.items())
    (cfg / "agents.yaml").write_text(body, encoding="utf-8")


def _seed_overlay(home: Path, body: str) -> Path:
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "agents.local.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ── Case 1: no overlay → template path, nothing written ────────────────────


def test_no_overlay_returns_template_path_unchanged(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {"planner": "glm", "implementer": "minimax"})
    saved = _with_env({"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT})
    try:
        from mini_ork.dispatch import agents_config
        p = agents_config.effective_path()
        assert p == str(home / "config" / "agents.yaml")
        assert not (home / "config" / ".agents.effective.yaml").exists()
    finally:
        _restore_env(saved)


# ── Case 2: overlay overrides one lane, keeps every other ──────────────────


def test_overlay_merges_recursively_over_template(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {
        "planner": "glm", "implementer": "minimax", "verifier": "deepseek",
        "reviewer": "minimax",
    })
    _seed_overlay(home, "lanes:\n  implementer: deepseek\n")
    saved = _with_env({"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT})
    try:
        from mini_ork.dispatch import agents_config
        p = agents_config.effective_path()
        assert p == str(home / "config" / ".agents.effective.yaml")
        assert (home / "config" / ".agents.effective.yaml").is_file()

        # Read lanes back via the existing _load_lanes helper, which is what
        # decision_service uses internally — pins the wire format end-to-end.
        sys_path = str(REPO)
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)
        from mini_ork.steering import decision_service as ds
        lanes = ds._load_lanes(p)
        assert lanes == {
            "planner": "glm",
            "implementer": "deepseek",
            "verifier": "deepseek",
            "reviewer": "minimax",
        }
    finally:
        _restore_env(saved)


# ── Case 3: $MINI_ORK_AGENTS pointing elsewhere wins over agents.local.yaml ─


def test_env_override_beats_local_overlay(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {"implementer": "minimax"})
    _seed_overlay(home, "lanes:\n  implementer: kimi\n")
    vendor = tmp_path / "vendor" / "agents.yaml"
    vendor.parent.mkdir(parents=True, exist_ok=True)
    vendor.write_text("lanes:\n  implementer: deepseek\n", encoding="utf-8")

    saved = _with_env({
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": _NONEXISTENT,
        "MINI_ORK_AGENTS": str(vendor),
    })
    try:
        from mini_ork.dispatch import agents_config
        p = agents_config.effective_path()
        assert p == str(home / "config" / ".agents.effective.yaml")
        sys_path = str(REPO)
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)
        from mini_ork.steering import decision_service as ds
        assert ds._load_lanes(p)["implementer"] == "deepseek"
    finally:
        _restore_env(saved)


# ── Case 4: null removes; lists replace not concatenate ────────────────────


def test_overlay_null_removes_key_and_list_replaces(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {
        "planner": "glm", "implementer": "minimax",
        "tags": ["alpha", "beta"],
    })
    _seed_overlay(home, (
        "lanes:\n"
        "  planner: null\n"
        "  implementer: deepseek\n"
        "tags:\n"
        "  - gamma\n"
    ))
    saved = _with_env({"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT})
    try:
        from mini_ork.dispatch import agents_config
        p = agents_config.effective_path()
        import yaml as _yaml
        with open(p, encoding="utf-8") as f:
            merged = _yaml.safe_load(f)
        # null → key removed
        assert "planner" not in (merged.get("lanes") or {})
        assert merged["lanes"]["implementer"] == "deepseek"
        # list replaced, not concatenated
        assert merged["tags"] == ["gamma"]
    finally:
        _restore_env(saved)


# ── Case 5: malformed overlay raises ValueError naming the file ────────────


def test_malformed_overlay_raises_value_error(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {"implementer": "minimax"})
    bad = _seed_overlay(home, "lanes: [unterminated\n  : :\n")
    saved = _with_env({"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT})
    try:
        from mini_ork.dispatch import agents_config
        with pytest.raises(ValueError) as ei:
            agents_config.effective_path()
        assert str(bad) in str(ei.value)
    finally:
        _restore_env(saved)


# ── Case 6: snapshot_run_config freezes the MERGED content ────────────────


def test_snapshot_freezes_merged_content(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {"planner": "glm", "implementer": "minimax"})
    _seed_overlay(home, "lanes:\n  implementer: deepseek\n")
    run = tmp_path / "run"
    saved = _with_env({
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": _NONEXISTENT,
        "MINI_ORK_RUN_DIR": str(run),
    })
    try:
        from mini_ork.dispatch.config_resolve import snapshot_run_config
        snapshot_run_config(str(run))
        dest = run / "config" / "agents.yaml"
        assert dest.is_file()
        body = dest.read_text(encoding="utf-8")
        # Merged content: template's planner preserved, overlay's implementer wins.
        assert "planner: glm" in body
        assert "implementer: deepseek" in body
        # Template file itself untouched
        tpl = (home / "config" / "agents.yaml").read_text(encoding="utf-8")
        assert "planner: glm" in tpl
        assert "implementer: minimax" in tpl
    finally:
        _restore_env(saved)


# ── Case 7: decision_service.default_lane reads the overlay ────────────────


def test_decision_service_default_lane_reads_overlay(tmp_path):
    home = tmp_path / "home"
    _seed_template(home, {"implementer": "minimax", "verifier": "deepseek"})
    _seed_overlay(home, "lanes:\n  implementer: deepseek\n")
    saved = _with_env({
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_ROOT": _NONEXISTENT,
        "MINI_ORK_RUN_DIR": _NONEXISTENT,
    })
    try:
        sys_path = str(REPO)
        if sys_path not in __import__("sys").path:
            __import__("sys").path.insert(0, sys_path)
        from mini_ork.steering import decision_service as ds
        assert ds.default_lane("implementer") == "deepseek"
        assert ds.default_lane("verifier") == "deepseek"  # template untouched
    finally:
        _restore_env(saved)


# ── Case 8: template file bytes unchanged after every test ─────────────────


@pytest.mark.parametrize("case", [
    "no_overlay",
    "merge",
    "null_remove",
    "snapshot",
])
def test_template_bytes_unchanged(tmp_path, case):
    home = tmp_path / "home"
    body = "lanes:\n  planner: glm\n  implementer: minimax\n  verifier: deepseek\n"
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "agents.yaml").write_text(body, encoding="utf-8")
    if case == "no_overlay":
        env = {"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT}
    elif case == "merge":
        (home / "config" / "agents.local.yaml").write_text(
            "lanes:\n  implementer: deepseek\n", encoding="utf-8")
        env = {"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT}
    elif case == "null_remove":
        (home / "config" / "agents.local.yaml").write_text(
            "lanes:\n  planner: null\n", encoding="utf-8")
        env = {"MINI_ORK_HOME": str(home), "MINI_ORK_ROOT": _NONEXISTENT}
    else:  # snapshot
        (home / "config" / "agents.local.yaml").write_text(
            "lanes:\n  implementer: deepseek\n", encoding="utf-8")
        env = {
            "MINI_ORK_HOME": str(home),
            "MINI_ORK_ROOT": _NONEXISTENT,
            "MINI_ORK_RUN_DIR": str(tmp_path / "run"),
        }

    saved = _with_env(env)
    try:
        from mini_ork.dispatch import agents_config
        from mini_ork.dispatch.config_resolve import snapshot_run_config
        agents_config.effective_path()
        if case == "snapshot":
            snapshot_run_config(str(tmp_path / "run"))
        assert (home / "config" / "agents.yaml").read_text(encoding="utf-8") == body
    finally:
        _restore_env(saved)


# ── Case 9: git check-ignore confirms the overlay path is ignored ──────────


def test_git_check_ignore_on_overlay_path():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    overlay = REPO / ".mini-ork" / "config" / "agents.local.yaml"
    proc = subprocess.run(
        ["git", "check-ignore", "-v", str(overlay)],
        cwd=str(REPO), capture_output=True, text=True,
    )
    # rc=0 means "ignored"; rc=1 means "not ignored" (test fails).
    if proc.returncode == 128:
        pytest.skip(f"git not a repo here: {proc.stderr.strip()}")
    assert proc.returncode == 0, (
        f"git check-ignore did not confirm ignore for {overlay}: "
        f"rc={proc.returncode} stderr={proc.stderr!r}"
    )


# ── merge() unit checks (non-IO, non-env) ───────────────────────────────────


def test_merge_does_not_mutate_inputs():
    from mini_ork.dispatch.agents_config import merge
    base = {"a": 1, "b": {"c": 2, "d": [1, 2, 3]}}
    over = {"b": {"d": [9]}, "e": None}
    base_copy = {"a": 1, "b": {"c": 2, "d": [1, 2, 3]}}
    over_copy = {"b": {"d": [9]}, "e": None}
    merged = merge(base, over)
    assert merged == {"a": 1, "b": {"c": 2, "d": [9]}}
    assert base == base_copy
    assert over == over_copy


def test_merge_list_replaces_and_null_removes():
    from mini_ork.dispatch.agents_config import merge
    out = merge(
        {"a": [1, 2], "b": {"c": 3}, "d": "keep"},
        {"a": [9], "b": None, "d": "changed"},
    )
    assert out == {"a": [9], "d": "changed"}


# ── review repairs ─────────────────────────────────────────────────────────


def test_a_missing_MINI_ORK_AGENTS_file_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    """A typo in the path used to fall back to the team template silently."""
    from mini_ork.dispatch import agents_config
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text("lanes:\n  implementer: codex\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_AGENTS", str(tmp_path / "typo.yaml"))

    import pytest
    with pytest.raises(ValueError, match="typo.yaml"):
        agents_config.effective_path()


def test_a_broken_template_is_named_as_the_template(tmp_path, monkeypatch):
    from mini_ork.dispatch import agents_config
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text("lanes: [unclosed\n")
    (home / "config" / "agents.local.yaml").write_text("lanes:\n  implementer: kimi\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    import pytest
    with pytest.raises(ValueError, match="agents.yaml template"):
        agents_config.effective_path()


def test_coalition_gate_reads_the_overlay_from_home_not_the_engine_root(tmp_path, monkeypatch):
    """native_gates passed the ENGINE root in as home, so the user's overlay
    under $MINI_ORK_HOME was never seen by the coalition gate."""
    from mini_ork.dispatch import agents_config
    home, root = tmp_path / "home", tmp_path / "engine"
    (home / "config").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text("lanes:\n  implementer: codex\n")
    (home / "config" / "agents.local.yaml").write_text("lanes:\n  implementer: kimi\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    import yaml
    path = agents_config.effective_path(root=str(root))
    assert yaml.safe_load(open(path))["lanes"]["implementer"] == "kimi"
    src = (REPO / "mini_ork" / "gates" / "native_gates.py").read_text()
    assert "overlay_or(" in src and "effective_path(home=home)" not in src


def test_snapshot_reports_a_broken_overlay_instead_of_hiding_it(tmp_path, monkeypatch, capsys):
    from mini_ork.dispatch import config_resolve
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text("lanes:\n  implementer: codex\n")
    (home / "config" / "agents.local.yaml").write_text("lanes: [broken\n")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    assert config_resolve.snapshot_run_config(str(tmp_path / "run")) is True
    assert "agents.local.yaml" in capsys.readouterr().err
