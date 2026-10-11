"""``.mini-ork/config/features.yaml`` — the persisted feature/setting store.

The store is the one place a *choice* is remembered between runs, so the
properties that matter are: it never takes down the CLI when it is broken, it
writes atomically at a world-readable mode (it is policy, not secret), it
refuses to become a second secret store, and it materializes each feature's own
*off* value rather than one generic token.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from mini_ork.features import store
from mini_ork.features.registry import get_feature


@pytest.fixture(autouse=True)
def _own_store(tmp_path: Path, monkeypatch) -> Path:
    """Point the store at a temp file so no test can touch a real project."""
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_FEATURES", raising=False)
    return tmp_path / "config" / "features.yaml"


# ── path & load ─────────────────────────────────────────────────────────────


def test_store_path_prefers_explicit_env_then_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "h"))
    assert store.store_path() == tmp_path / "h" / "config" / "features.yaml"
    # an explicit file overrides the home entirely
    monkeypatch.setenv("MINI_ORK_FEATURES", str(tmp_path / "explicit.yaml"))
    assert store.store_path() == tmp_path / "explicit.yaml"


def test_a_missing_file_loads_empty(tmp_path: Path) -> None:
    assert store.load(tmp_path / "nope.yaml") == {"features": {}, "env": {}}


@pytest.mark.parametrize("text", [
    "just a scalar\n",
    "- a\n- list\n",                        # top level must be a mapping
    "features: [not, a, mapping]\n",        # feature section must be a mapping
    "env: 3\n",
    "{unclosed: [\n",                       # not valid YAML at all
])
def test_a_malformed_file_loads_empty_and_never_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(text, encoding="utf-8")
    assert store.load(path) == {"features": {}, "env": {}}


def test_an_oversized_file_is_refused_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "huge.yaml"
    path.write_text("features:\n" + "  a: '1'\n" * 60_000, encoding="utf-8")
    assert path.stat().st_size > store._MAX_BYTES
    assert store.load(path) == {"features": {}, "env": {}}


def test_a_null_value_is_dropped_not_stringified(tmp_path: Path) -> None:
    path = tmp_path / "nulls.yaml"
    path.write_text("features:\n  review_panel:\nenv:\n  MO_DAILY_BUDGET_USD:\n", encoding="utf-8")
    assert store.load(path) == {"features": {}, "env": {}}


# ── save ────────────────────────────────────────────────────────────────────


def test_save_round_trips_and_leaves_no_temp_behind(tmp_path: Path) -> None:
    path = tmp_path / "config" / "features.yaml"
    store.save({"features": {"review_panel": "codex"}, "env": {"MO_DAILY_BUDGET_USD": "20"}}, path)
    assert store.load(path) == {"features": {"review_panel": "codex"},
                               "env": {"MO_DAILY_BUDGET_USD": "20"}}
    assert list(path.parent.glob(".*tmp*")) == []
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["features"]["review_panel"] == "codex"


def test_the_store_is_world_readable_policy_not_secret(tmp_path: Path) -> None:
    path = tmp_path / "config" / "features.yaml"
    store.save({"features": {}, "env": {}}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_save_refuses_an_unknown_feature_id(tmp_path: Path) -> None:
    with pytest.raises(store.StoreError, match="unknown feature"):
        store.save({"features": {"no_such_feature": "1"}}, tmp_path / "f.yaml")


def test_save_refuses_a_credential_shaped_env_name(tmp_path: Path) -> None:
    with pytest.raises(store.StoreError, match="credential"):
        store.save({"env": {"ACME_API_KEY": "x"}}, tmp_path / "f.yaml")


def test_save_refuses_a_newline_in_a_value(tmp_path: Path) -> None:
    with pytest.raises(store.StoreError, match="newline"):
        store.save({"env": {"MO_DAILY_BUDGET_USD": "20\nMO_AUTO_MERGE=1"}}, tmp_path / "f.yaml")


# ── feature writes ──────────────────────────────────────────────────────────


def test_set_feature_persists_and_materializes_the_off_value(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    store.set_feature("plan_qa_gate", "off", path=path)
    assert store.load(path)["features"] == {"plan_qa_gate": "0"}
    assert store.effective_env(path) == {"MINI_ORK_EXECUTE_GATE": "0"}


def test_off_is_not_one_value(tmp_path: Path) -> None:
    """Each feature's off-knobs decide; three built-ins disagree with "0"."""

    def off_env(fid: str) -> dict[str, str]:
        path = tmp_path / f"{fid}.yaml"
        store.set_feature(fid, "off", path=path)
        return store.effective_env(path)

    assert off_env("plan_qa_gate") == {"MINI_ORK_EXECUTE_GATE": "0"}
    # split() as a lane list: "0" would be a lane *named* 0
    assert off_env("review_panel") == {"MO_REVIEW_PANEL": ""}
    # the engine's own fallback there is the neutral "probe" scorer
    assert off_env("code_arm_scorer") == {
        "MO_APPLY_SCORER": "probe", "MO_APPLY_CODE_PATCH": "", "MO_APPLY_PROBE_MAX_TASKS": "0"}


def test_set_feature_normalizes_and_refuses_bad_values(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    store.set_feature("oracle_gates", "yes", path=path)  # any on-word means on
    assert store.load(path)["features"]["oracle_gates"] == "1"
    store.set_feature("assay_relations", " 5 ", path=path)
    assert store.load(path)["features"]["assay_relations"] == "5"
    with pytest.raises(store.StoreError, match="whole number"):
        store.set_feature("assay_relations", "many", path=path)
    with pytest.raises(store.StoreError, match=">= 0"):
        store.set_feature("assay_relations", "-1", path=path)
    with pytest.raises(store.StoreError, match="unknown feature"):
        store.set_feature("not_a_feature", "1", path=path)


def test_a_premium_feature_needs_the_opt_in_and_records_it(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    with pytest.raises(store.StoreError, match="premium"):
        store.set_feature("review_panel", "codex kimi glm", path=path)
    assert store.load(path) == {"features": {}, "env": {}}  # nothing written

    store.set_feature("review_panel", "codex kimi glm", accept_premium=True, path=path)
    data = store.load(path)
    assert data["features"]["review_panel"] == "codex kimi glm"
    assert data["env"][store.ACCEPT_PREMIUM_ENV] == "1"
    assert store.enabled_premium(data) == ["review_panel"]


def test_unset_feature_drops_the_opt_in_only_when_no_premium_remains(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    store.set_feature("review_panel", "codex kimi glm", accept_premium=True, path=path)
    store.set_feature("recursion", "4", accept_premium=True, path=path)
    assert store.unset_feature("review_panel", path=path) is True
    assert store.load(path)["env"][store.ACCEPT_PREMIUM_ENV] == "1"  # recursion still premium
    assert store.unset_feature("recursion", path=path) is True
    assert store.ACCEPT_PREMIUM_ENV not in store.load(path)["env"]
    assert store.unset_feature("recursion", path=path) is False  # idempotent


def test_unset_feature_leaves_a_hand_set_env_name_alone(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    store.set_env("MO_DAILY_BUDGET_USD", "20", path=path)
    store.set_feature("oracle_gates", "off", path=path)
    store.unset_feature("oracle_gates", path=path)
    data = store.load(path)
    assert data["env"] == {"MO_DAILY_BUDGET_USD": "20"}
    assert store.effective_env(path) == {"MO_DAILY_BUDGET_USD": "20"}


# ── raw settings ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["ACME_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD", "MY_SECRET"])
def test_set_env_refuses_credential_shaped_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(store.StoreError, match="credential"):
        store.set_env(name, "hunter2", path=tmp_path / "f.yaml")


def test_set_env_refuses_a_name_a_feature_owns(tmp_path: Path) -> None:
    """Otherwise a hand-set knob could fight the toggle that owns it."""
    with pytest.raises(store.StoreError, match="controlled by the 'review_panel' feature"):
        store.set_env("MO_REVIEW_PANEL", "sonnet", path=tmp_path / "f.yaml")
    with pytest.raises(store.StoreError, match="controlled by the 'assay_relations' feature"):
        store.set_env("MO_ASSAY_RELATIONS_K", "9", path=tmp_path / "f.yaml")
    with pytest.raises(store.StoreError, match="not a valid env name"):
        store.set_env("has-a-dash", "1", path=tmp_path / "f.yaml")


def test_a_hand_written_owned_name_cannot_beat_the_feature(tmp_path: Path) -> None:
    """The file may be edited by hand; ``features:`` still wins over ``env:``."""
    path = tmp_path / "f.yaml"
    path.write_text(yaml.safe_dump({"features": {"review_panel": "off"},
                                    "env": {"MO_REVIEW_PANEL": "sonnet"}}), encoding="utf-8")
    # review_panel off wins: the panel is empty, not "sonnet"
    assert store.effective_env(path)["MO_REVIEW_PANEL"] == ""


def test_unset_env_reports_whether_it_removed_anything(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    store.set_env("MO_DAILY_BUDGET_USD", "20", path=path)
    assert store.unset_env("MO_DAILY_BUDGET_USD", path=path) is True
    assert store.unset_env("MO_DAILY_BUDGET_USD", path=path) is False
    assert store.effective_env(path) == {}


def test_effective_env_applies_a_persisted_premium_as_the_opt_in(tmp_path: Path) -> None:
    """A stored premium feature *is* the opt-in, so it must not be stripped."""
    path = tmp_path / "f.yaml"
    store.set_feature("review_panel", "codex kimi glm", accept_premium=True, path=path)
    env = store.effective_env(path)
    assert env["MO_REVIEW_PANEL"] == "codex kimi glm"
    assert env[store.ACCEPT_PREMIUM_ENV] == "1"


# ── provenance ──────────────────────────────────────────────────────────────


def test_current_attributes_a_value_to_the_file_that_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    feat = get_feature("oracle_gates")

    # nothing set anywhere -> the engine's own default, which is what runs
    at_default = store.current(feat, environ={}, data=store.load(path), secrets={})
    assert (at_default["value"], at_default["source"], at_default["off"]) == ("1", "default", False)

    # A knob that reached the file by hand: ``set_env`` refuses feature-owned
    # names, so this is the only way MO_ORACLE_GATES_AUTO lands in ``env:``. The
    # page must still attribute it to the file rather than the engine default.
    path.write_text(yaml.safe_dump({"env": {"MO_ORACLE_GATES_AUTO": "0"}}), encoding="utf-8")
    from_store = store.current(feat, environ={}, data=store.load(path), secrets={})
    assert (from_store["value"], from_store["source"]) == ("off", "project")

    # a features: entry outranks nothing here, but the real environment outranks both
    by_env = store.current(feat, environ={"MO_ORACLE_GATES_AUTO": "1"}, data=store.load(path))
    assert (by_env["value"], by_env["source"]) == ("1", "env")

    # and the credential store is the last resort before the default
    by_secret = store.current(feat, environ={}, data=store.load(path), secrets={"MO_ORACLE_GATES_AUTO": "1"})
    assert by_secret["source"] == "project"  # a project value still wins over a secret


def test_current_reports_the_engine_default_not_the_wizard_suggestion() -> None:
    """``code_arm_scorer`` is suggested at 1 probe but runs off unless asked."""
    cur = store.current(get_feature("code_arm_scorer"), environ={}, data={}, secrets={})
    assert (cur["value"], cur["off"]) == ("off", True)
    assert cur["multiplier"] == 1.0  # nothing is being paid
    assert cur["wizard_default"] == "1" and cur["engine_default"] == "0"


def test_current_never_reports_a_multiplier_for_an_off_feature() -> None:
    """"off" is a word, not a count — pricing it on the raw string would lie."""
    cur = store.current(get_feature("review_panel"), environ={}, data={"features": {"review_panel": "off"}})
    assert cur["off"] is True and cur["multiplier"] == 1.0


def test_a_secret_store_value_is_attributed_to_the_secret_store() -> None:
    feat = get_feature("prm_score")
    cur = store.current(feat, environ={}, data={}, secrets={"MO_PRM_SCORE": "1"})
    assert (cur["value"], cur["source"]) == ("1", "secrets")


def test_importing_the_package_alone_does_not_pull_the_store() -> None:
    """``features/__init__`` stays stdlib-only; the store needs PyYAML.

    ``hasattr(pkg, "store")`` cannot test this — importing a submodule binds it
    on the parent no matter what ``__init__`` does. The property that matters is
    that importing the *package* leaves both the store and PyYAML unimported, so
    the hot path (``catalogue_json``) never pays for a YAML parser. Only a fresh
    interpreter can show it, since this suite has long since imported the store.
    """
    import subprocess
    import sys

    probe = (
        "import sys, mini_ork.features as pkg;"
        "assert 'store' not in pkg.__all__, pkg.__all__;"
        "bad = [m for m in ('mini_ork.features.store', 'yaml') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "", out.stdout


def test_store_path_expands_a_home_relative_to_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert store.store_path(None) == Path(".mini-ork") / "config" / "features.yaml"


def test_save_does_not_re_mode_the_parent_dir(tmp_path: Path) -> None:
    """The credential file's fail-closed check owns ``config/``'s mode."""
    parent = tmp_path / "config"
    parent.mkdir(mode=0o700)
    before = stat.S_IMODE(parent.stat().st_mode)
    store.save({"features": {}, "env": {}}, parent / "features.yaml")
    assert stat.S_IMODE(parent.stat().st_mode) == before


def test_effective_env_never_writes_to_environ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "keep")
    path = tmp_path / "f.yaml"
    store.set_env("MO_DAILY_BUDGET_USD", "20", path=path)
    store.effective_env(path)
    assert os.environ["MO_ORACLE_GATES_AUTO"] == "keep"
    assert "MO_DAILY_BUDGET_USD" not in os.environ
