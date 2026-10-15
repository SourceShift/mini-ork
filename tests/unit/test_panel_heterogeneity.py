"""mini_ork.verify.panel_heterogeneity: the family-level panel gate (R7a).

A panel is only as independent as its models, and the two ways it silently
wasn't — the operator lane-alias overlay, and a fallback tail re-pointing a
judge at another lane's model — are both invisible to a comparison of raw lane
strings. These tests pin the family mapping, the lane→model→family resolution
and every problem form, on hermetic temp DBs and a pinned provider registry.
"""
from __future__ import annotations

import sqlite3

import pytest

from mini_ork.verify import panel_heterogeneity

# The three llm_calls columns the check reads: the pinned alias, the lane that
# actually served it, and whether that call succeeded.
_LLM_CALLS_DDL = """
CREATE TABLE llm_calls (
    provider TEXT,
    model_id TEXT,
    actor TEXT,
    status TEXT CHECK (status IN ('success', 'failed')),
    run_id TEXT,
    cost_usd REAL
)
"""

# Only ``opus55`` is pinned here; every other lane name falls through to itself
# under either registry reading.
_REGISTRY = """\
providers:
  opus55:
    kind: anthropic-native
    extra_env:
      ANTHROPIC_MODEL: claude-opus-5-5
  judge_a:
    kind: anthropic-native
    model: GLM-5.3
  judge_b:
    kind: anthropic-native
    extra_env:
      ANTHROPIC_MODEL: glm-5.3
"""


@pytest.fixture(autouse=True)
def _pinned_registry(tmp_path, monkeypatch):
    """No operator overlay, no run-local registry: only this file's."""
    path = tmp_path / "providers.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(path))
    monkeypatch.delenv("MO_PANEL_EXPECT_MODELS", raising=False)


def _db(tmp_path, rows, *, with_table=True, name="state.db") -> str:
    """A hermetic llm_calls DB. ``rows`` are (run_id, actor, model_id, status)."""
    path = tmp_path / name
    con = sqlite3.connect(str(path))
    try:
        if with_table:
            con.execute(_LLM_CALLS_DDL)
            con.executemany(
                "INSERT INTO llm_calls (run_id, actor, model_id, status) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        else:
            con.execute("CREATE TABLE something_else (x TEXT)")
        con.commit()
    finally:
        con.close()
    return str(path)


# ── model_family ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model, family",
    [
        ("GLM-5.3", "zai"),
        ("MiniMax-M3", "minimax"),
        ("deepseek-v4-flash", "deepseek"),
        ("claude-opus-5-5", "anthropic"),
        ("opus", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("Claude-Opus-5", "anthropic"),
        ("kimi-k2.7-code", "moonshot"),
        ("moonshot-v1", "moonshot"),
        ("gpt-5.2", "openai"),
        ("codex-mini", "openai"),
        ("o3-mini", "openai"),
        ("gemini-3-pro", "google"),
        ("gemma-3", "google"),
        ("laguna-1", "poolside"),
        ("poolside-sec", "poolside"),
        ("qwen4-max", "alibaba"),
    ],
)
def test_model_family_maps_declared_tokens(model, family):
    assert panel_heterogeneity.model_family(model) == family


def test_model_family_unknown_keeps_its_own_family():
    # An unknown lane must stay distinct from every known family, not collapse
    # into one of them.
    assert panel_heterogeneity.model_family("ZZZ-Unknown-99") == "zzz-unknown-99"


# ── lane_model ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "lane, expected",
    [
        ("judge_a", "GLM-5.3"),          # the registry entry's model pin
        ("judge_b", "glm-5.3"),          # extra_env.ANTHROPIC_MODEL
        ("opus55", "claude-opus-5-5"),   # extra_env.ANTHROPIC_MODEL
        ("opus", "opus"),                # absent from the registry → lane name
    ],
)
def test_lane_model_resolution_chain(lane, expected):
    assert panel_heterogeneity.lane_model(lane) == expected


def test_lane_model_ambient_native_lane_is_claude_prefixed(tmp_path, monkeypatch):
    registry = tmp_path / "providers.yaml"
    registry.write_text(
        "providers:\n  sonnet:\n    kind: anthropic-native\n", encoding="utf-8"
    )
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(registry))
    # No model pin and no extra_env: the ambient-auth lanes ARE claude/<lane>.
    assert panel_heterogeneity.lane_model("sonnet") == "claude-sonnet"


# ── check_panel: heterogeneous vs collapsed panels ──────────────────────────

def test_two_lanes_of_one_family_fail(tmp_path):
    run = "run-same-family"
    db = _db(tmp_path, [
        (run, "opus_lens", "glm", "success"),
        (run, "minimax_lens", "minimax", "success"),
        (run, "laguna_lens", "minimax", "success"),
        # Another run's rows are not this run's panel.
        ("another-run", "kimi_lens", "glm", "success"),
    ])
    result = panel_heterogeneity.check_panel(
        run, ["opus_lens", "minimax_lens", "laguna_lens"], db=db
    )
    assert result["ok"] is False
    assert "same_family:minimax_lens,laguna_lens:minimax" in result["problems"]
    assert result["served"]["laguna_lens"] == ["minimax"]
    assert result["families"]["opus_lens"] == ["zai"]
    assert result["skipped"] is None


def test_two_anthropic_pins_fail(tmp_path):
    # `opus` and `opus55` are two pins of ONE family — the case the old
    # lane-string comparison read as two different models.
    run = "run-anthropic-double"
    db = _db(tmp_path, [
        (run, "opus_lens", "opus55", "success"),
        (run, "kimi_lens", "opus", "success"),
    ])
    result = panel_heterogeneity.check_panel(run, ["opus_lens", "kimi_lens"], db=db)
    assert result["ok"] is False
    assert "same_family:opus_lens,kimi_lens:anthropic" in result["problems"]


def test_families_come_from_the_registry_not_the_lane_name(tmp_path):
    # Two differently NAMED lanes pinned to the same gateway model are one
    # family: the resolution has to go through the registry, not the name.
    run = "run-registry-families"
    db = _db(tmp_path, [
        (run, "a_lens", "judge_a", "success"),
        (run, "b_lens", "judge_b", "success"),
    ])
    result = panel_heterogeneity.check_panel(run, ["a_lens", "b_lens"], db=db)
    assert result["ok"] is False
    assert "same_family:a_lens,b_lens:zai" in result["problems"]


def test_heterogeneous_panel_passes(tmp_path):
    run = "run-heterogeneous"
    db = _db(tmp_path, [
        (run, "opus_lens", "opus55", "success"),
        (run, "glm_lens", "glm", "success"),
        (run, "deepseek_lens", "deepseek_flash", "success"),
        (run, "minimax_lens", "minimax", "success"),
        # An actor outside the panel is not graded.
        (run, "synthesizer", "glm", "success"),
    ])
    result = panel_heterogeneity.check_panel(
        run, ["opus_lens", "glm_lens", "deepseek_lens", "minimax_lens"], db=db
    )
    assert result["ok"] is True
    assert result["problems"] == []
    assert set(result["served"]) == {
        "opus_lens", "glm_lens", "deepseek_lens", "minimax_lens"
    }


def test_fallback_tail_is_a_failure(tmp_path):
    run = "run-fallback"
    db = _db(tmp_path, [
        (run, "laguna_lens", "laguna_free", "failed"),
        (run, "laguna_lens", "minimax", "success"),
    ])
    result = panel_heterogeneity.check_panel(run, ["laguna_lens"], db=db)
    assert result["ok"] is False
    assert "fallback:laguna_lens:served=minimax" in result["problems"]
    # Only the lane that actually served it counts.
    assert result["served"]["laguna_lens"] == ["minimax"]


def test_same_lane_retry_is_not_a_fallback(tmp_path):
    run = "run-retry"
    db = _db(tmp_path, [
        (run, "laguna_lens", "laguna_free", "failed"),
        (run, "laguna_lens", "laguna_free", "success"),
    ])
    result = panel_heterogeneity.check_panel(run, ["laguna_lens"], db=db)
    assert result["ok"] is True
    assert result["problems"] == []


def test_expect_allowlist_flags_a_remapped_lens(tmp_path):
    run = "run-expect"
    db = _db(tmp_path, [(run, "opus_lens", "glm", "success")])
    result = panel_heterogeneity.check_panel(
        run, ["opus_lens"], db=db, expect={"opus_lens": {"opus", "opus55"}}
    )
    assert result["ok"] is False
    assert "expect:opus_lens:served=glm" in result["problems"]


def test_expect_allowlist_passes_on_an_allowed_lane(tmp_path):
    run = "run-expect-ok"
    db = _db(tmp_path, [(run, "opus_lens", "opus55", "success")])
    result = panel_heterogeneity.check_panel(
        run, ["opus_lens"], db=db, expect={"opus_lens": {"opus", "opus55"}}
    )
    assert result["ok"] is True
    assert result["problems"] == []


def test_alias_with_no_calls_in_a_run_that_logged_is_a_failure(tmp_path):
    run = "run-no-calls"
    db = _db(tmp_path, [(run, "opus_lens", "glm", "success")])
    result = panel_heterogeneity.check_panel(run, ["opus_lens", "kimi_lens"], db=db)
    assert result["ok"] is False
    assert "no_calls:kimi_lens" in result["problems"]


def test_multiple_successful_lanes_are_served_sorted(tmp_path):
    run = "run-multi"
    db = _db(tmp_path, [
        (run, "mixed_lens", "minimax", "success"),
        (run, "mixed_lens", "glm", "success"),
    ])
    result = panel_heterogeneity.check_panel(run, ["mixed_lens"], db=db)
    assert result["ok"] is True
    assert result["served"]["mixed_lens"] == ["glm", "minimax"]
    assert result["families"]["mixed_lens"] == ["minimax", "zai"]


def test_result_shape(tmp_path):
    run = "run-shape"
    db = _db(tmp_path, [(run, "opus_lens", "opus55", "success")])
    result = panel_heterogeneity.check_panel(run, ["opus_lens"], db=db)
    assert set(result) == {"ok", "problems", "served", "families", "skipped"}
    assert isinstance(result["ok"], bool)
    assert all(isinstance(p, str) for p in result["problems"])
    assert result["skipped"] is None


# ── skip semantics: fail closed, never crash, never create ──────────────────

def test_missing_db_skips_and_is_not_created(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    result = panel_heterogeneity.check_panel("run-nodb", ["opus_lens"], db=str(missing))
    assert result["ok"] is True
    assert result["problems"] == []
    assert isinstance(result["skipped"], str) and result["skipped"]
    # Read-only open: a missing DB is an error, not an empty file.
    assert not missing.exists()


def test_missing_table_skips(tmp_path):
    db = _db(tmp_path, [], with_table=False)
    result = panel_heterogeneity.check_panel("run-notable", ["opus_lens"], db=db)
    assert result["ok"] is True
    assert isinstance(result["skipped"], str) and result["skipped"]


def test_run_with_no_rows_skips(tmp_path):
    db = _db(tmp_path, [("a-different-run", "opus_lens", "glm", "success")])
    result = panel_heterogeneity.check_panel("run-empty", ["opus_lens"], db=db)
    assert result["ok"] is True
    assert result["problems"] == []
    assert isinstance(result["skipped"], str) and result["skipped"]


def test_db_resolves_from_the_environment_and_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "nowhere.db"))
    result = panel_heterogeneity.check_panel("run-env-db", ["opus_lens"])
    assert result["ok"] is True
    assert isinstance(result["skipped"], str) and result["skipped"]


def test_the_check_never_writes_to_the_db(tmp_path):
    run = "run-readonly"
    db = _db(tmp_path, [
        (run, "opus_lens", "glm", "success"),
        (run, "minimax_lens", "minimax", "success"),
    ])
    with open(db, "rb") as fh:
        before = fh.read()
    panel_heterogeneity.check_panel(run, ["opus_lens", "minimax_lens"], db=db)
    with open(db, "rb") as fh:
        assert fh.read() == before
