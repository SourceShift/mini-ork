"""Contract tests for ``mini-ork prefs`` and the preference-injection path.

Mirror of ``tests/unit/test_learned_record.py`` fixture pattern: a fresh temp
DB built per test via ``db/init.sh`` so the ``user_preference_memory`` table
(migration 0009) exists. Direct module import for both surfaces so we
exercise the public API end-to-end (round trip + caps + injection ordering).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute, prefs_cmd  # noqa: E402
from mini_ork.memory import preferences  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


@pytest.fixture
def prefs_env(db, monkeypatch, tmp_path):
    """Wire ``MINI_ORK_DB`` + a clean ``config/`` dir to a temp MINI_ORK_HOME."""
    home = tmp_path / "home"
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home, cfg


# ─── API round-trip ─────────────────────────────────────────────────────


def test_set_list_rm_round_trip(prefs_env):
    """set → list sees the row → rm removes it → list no longer sees it."""
    row = preferences.set_pref("tone", "be terse")
    assert row["key"] == "tone" and row["value"] == "be terse"
    rows = preferences.list_prefs()
    assert any(r["key"] == "tone" and r["source"] == "db" for r in rows)

    assert preferences.remove_pref("tone") is True
    assert any(r["key"] == "tone" for r in preferences.list_prefs()) is False


def test_set_pref_upserts(prefs_env):
    """A second set with the same (key, scope, target) replaces the row."""
    preferences.set_pref("tone", "first")
    preferences.set_pref("tone", "second")
    rows = [r for r in preferences.list_prefs() if r["key"] == "tone"]
    assert len(rows) == 1 and rows[0]["value"] == "second"


def test_set_pref_rejects_bad_scope(prefs_env):
    """``scope="role"`` raises ValueError naming the allowed list."""
    with pytest.raises(ValueError) as exc:
        preferences.set_pref("tone", "x", scope="role")
    assert "global" in str(exc.value) and "task_class" in str(exc.value)


def test_set_pref_global_rejects_nonempty_target(prefs_env):
    """``scope=global`` with a non-empty target raises."""
    with pytest.raises(ValueError) as exc:
        preferences.set_pref("tone", "x", scope="global", target="code-fix")
    assert "global" in str(exc.value)


def test_remove_pref_unknown_returns_false(prefs_env):
    assert preferences.remove_pref("never-set") is False


# ─── prefs_for ───────────────────────────────────────────────────────────


def test_prefs_for_filters_to_task_class(prefs_env):
    """prefs_for returns global + the matching task_class only."""
    preferences.set_pref("g1", "global-rule")
    preferences.set_pref("cf1", "code-fix-rule", scope="task_class", target="code-fix")
    preferences.set_pref("rf1", "review-rule", scope="task_class", target="review")

    rows = preferences.prefs_for("code-fix")
    keys = [r["key"] for r in rows]
    assert "g1" in keys and "cf1" in keys
    assert "rf1" not in keys


def test_prefs_for_orders_global_first(prefs_env):
    """Globals appear before scoped entries regardless of set_at."""
    preferences.set_pref("sc1", "scoped", scope="task_class", target="code-fix")
    preferences.set_pref("g1", "global")
    rows = preferences.prefs_for("code-fix")
    assert rows[0]["scope"] == "global"


def test_prefs_for_respects_caps(prefs_env):
    """12-entry cap + 600-char value cap."""
    # 12 globals → all in the result.
    for i in range(11):
        preferences.set_pref(f"g{i}", "x")
    long_val = "y" * 1500
    preferences.set_pref("big", long_val)
    rows = preferences.prefs_for("code-fix")
    assert len(rows) == 12
    big = [r for r in rows if r["key"] == "big"][0]
    assert len(big["value"]) == 600
    # Now overflow → still 12 entries; oldest set_at rows fall out.
    for i in range(11, 30):
        preferences.set_pref(f"g{i}", "x")
    rows = preferences.prefs_for("code-fix")
    assert len(rows) == 12


# ─── legacy file entries ─────────────────────────────────────────────────


def test_list_prefs_includes_legacy_files(prefs_env):
    """Legacy user_preferences.json + constraints.json surface as file:… entries."""
    _, cfg = prefs_env
    (cfg / "user_preferences.json").write_text(
        json.dumps({"legacy_key": "legacy_value"})
    )
    (cfg / "constraints.json").write_text(
        json.dumps({"constraints": ["hard cap $2000/day"]})
    )
    rows = preferences.list_prefs()
    legacy = [r for r in rows if r["source"].startswith("file:")]
    keys = [r["key"] for r in legacy]
    assert "legacy_key" in keys and "constraint-0" in keys
    for r in legacy:
        assert r["scope"] == "global"


def test_prefs_for_includes_legacy(prefs_env):
    """Legacy file entries reach the prompt block via prefs_for."""
    _, cfg = prefs_env
    (cfg / "user_preferences.json").write_text(json.dumps({"lk": "lv"}))
    rows = preferences.prefs_for("code-fix")
    assert any(r["key"] == "lk" for r in rows)


# ─── render_block ────────────────────────────────────────────────────────


def test_render_block_empty():
    assert preferences.render_block([]) == ""


def test_render_block_global_omits_scope_tag():
    block = preferences.render_block(
        [{"key": "tone", "value": "be terse", "scope": "global", "target": ""}]
    )
    assert "be terse" in block
    assert "[scope:" not in block
    assert "Operator preferences" in block
    assert "/operator preferences" in block


def test_render_block_scoped_includes_tag():
    block = preferences.render_block(
        [{"key": "rule", "value": "scope it",
          "scope": "task_class", "target": "code-fix"}]
    )
    assert "scope it" in block
    assert "[scope: task_class=code-fix]" in block


# ─── _learned_block injection ───────────────────────────────────────────


def _reset_steering_attr():
    """Drop the cached ``operator_steering`` attribute on the ``mini_ork.steering``
    module so subsequent tests' ``monkeypatch.setitem(sys.modules, ...)`` takes
    effect — Python's ``from X import Y`` caches Y on X the first time it runs.
    """
    import mini_ork.steering as _pkg
    _pkg.__dict__.pop("operator_steering", None)


def test_learned_block_starts_with_prefs(db, monkeypatch, prefs_env):
    """A global pref reaches the rendered block and sits FIRST."""
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "1")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.delenv("MO_EMERGENT_INJECT", raising=False)
    preferences.set_pref("tone", "be terse")

    sources: list[dict] = []
    block = execute._learned_block(
        None, "code-fix", "implementer", lane="minimax",
        node_id="implementer", sources=sources,
    )
    assert block.startswith("--- Operator preferences")
    assert "be terse" in block
    assert sources[0]["kind"] == "preference"
    assert sources[0]["id"] == "pref:global::tone"
    assert sources[0]["text"] == "be terse"
    _reset_steering_attr()


def test_learned_block_disabled_by_mo_inject(db, monkeypatch, prefs_env):
    """``MO_INJECT_LEARNINGS=0`` produces an empty block, even with a pref."""
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "0")
    monkeypatch.setenv("MINI_ORK_DB", db)
    preferences.set_pref("tone", "be terse")
    sources: list[dict] = []
    block = execute._learned_block(
        None, "code-fix", "implementer", lane="minimax",
        node_id="implementer", sources=sources,
    )
    assert block == ""
    assert sources == []
    _reset_steering_attr()


def test_cli_set_global(capsys, prefs_env):
    rc = prefs_cmd.main(["set", "tone", "be terse", "--scope", "global"])
    assert rc == 0
    rows = preferences.list_prefs()
    assert any(r["key"] == "tone" and r["value"] == "be terse" for r in rows)


# ─── CLI ────────────────────────────────────────────────────────────────


def test_cli_list_json(capsys, prefs_env):
    preferences.set_pref("tone", "be terse")
    rc = prefs_cmd.main(["list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert any(r["key"] == "tone" for r in payload)


def test_cli_set_bad_scope_exits_2(capsys):
    rc = prefs_cmd.main(["set", "x", "y", "--scope", "role"])
    assert rc == 2
    assert "invalid scope" in capsys.readouterr().err


def test_cli_help(capsys):
    rc = prefs_cmd.main(["--help"])
    assert rc == 0
    assert "prefs list" in capsys.readouterr().out


def test_cli_rm_missing_positional(capsys):
    rc = prefs_cmd.main(["rm"])
    assert rc == 2