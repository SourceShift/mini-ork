"""Real list-price billing for anthropic-compat lanes.

The Claude CLI prices models it does not know at Anthropic rates, which
overstates anthropic-compat lane spend by 3-13x. These tests pin the
new ``claude_cost`` behaviour: ``modelUsage`` entries resolve through
``pricing.yaml`` (per model + per kind), Anthropic-prefixed models keep
the CLI's billed figure, and unknown / missing-table cases fall back to
``costUSD`` so the meter never silently zeroes a real spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.dispatch.models import TokenUsage
from mini_ork.dispatch.providers import claude_cost

# ── Pricing YAML fixture ────────────────────────────────────────────────────
# Mirrors the rows added to .mini-ork/config/pricing.yaml for this fix.
# The shipped file is the authoritative copy; this constant exists so a
# broken shipped file surfaces as a failing test rather than a 13× meter
# in production. (The shipped-row guard below asserts on the actual file.)

_PRICING_YAML = (
    "pricing:\n"
    "  zhipu:\n"
    "    GLM-5.3:\n"
    "      input:       1.40\n"
    "      output:      4.40\n"
    "      cache_read:  0.26\n"
    "  deepseek:\n"
    "    deepseek-v4-pro:\n"
    "      input:       1.32\n"
    "      output:      3.96\n"
    "      cache_read:  0.044\n"
    "    deepseek-flash:\n"
    "      input:       0.30\n"
    "      output:      1.20\n"
    "      cache_read:  0.006\n"
    "    deepseek-v4-flash:\n"
    "      input:       0.30\n"
    "      output:      1.20\n"
    "      cache_read:  0.006\n"
    "  minimax:\n"
    "    MiniMax-M3:\n"
    "      input:       0.30\n"
    "      output:      1.20\n"
    "      cache_read:  0.06\n"
    "  moonshot:\n"
    "    kimi-k2.7-code:\n"
    "      input:       0.95\n"
    "      output:      4.00\n"
    "      cache_read:  0.30\n"
)


# ── Envelope fixtures ───────────────────────────────────────────────────────
# Each block pins a single pricing case so the assertions read as a spec.

_GLM_USAGE = {
    "GLM-5.3": {
        "inputTokens": 40442,
        "outputTokens": 15103,
        "cacheReadInputTokens": 168960,
        "cacheCreationInputTokens": 0,
        # CLI-reported figure — 13.0x the real list price. The new code
        # must IGNORE this for non-Anthropic models.
        "costUSD": 0.664,
    },
}

_DEEPSEEK_SUFFIX_USAGE = {
    "deepseek-v4-pro[1m]": {
        "inputTokens": 1_000_000,
        "outputTokens": 100_000,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "costUSD": 5.0,
    },
}

_MIXED_USAGE = {
    "GLM-5.3": {
        "inputTokens": 10_000,
        "outputTokens": 1_000,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "costUSD": 0.0,
    },
    "claude-haiku-4-5": {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "costUSD": 0.01,
    },
}

_UNKNOWN_USAGE = {
    "foo-1": {
        "inputTokens": 1,
        "outputTokens": 2,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "costUSD": 0.5,
    },
}


def _envelope(model_usage: dict | None, total_cost_usd: float = 0.0) -> str:
    return json.dumps(
        {"result": "", "total_cost_usd": total_cost_usd, "modelUsage": model_usage}
    )


def _write_pricing(tmp_path, monkeypatch, yaml_text: str = _PRICING_YAML) -> Path:
    p = tmp_path / "pricing.yaml"
    p.write_text(yaml_text)
    monkeypatch.setenv("MO_PRICING_YAML", str(p))
    return p


# ── Per-case behaviour ──────────────────────────────────────────────────────


def test_claude_cost_glm5_uses_list_price(tmp_path, monkeypatch):
    """GLM-5.3 envelope → list price (40442*1.40 + 168960*0.26 + 15103*4.40)/1e6."""
    _write_pricing(tmp_path, monkeypatch)
    expected = (40442 * 1.40 + 168960 * 0.26 + 15103 * 4.40) / 1e6
    assert claude_cost(_envelope(_GLM_USAGE), TokenUsage()) == pytest.approx(expected)


def test_claude_cost_strips_model_suffix_brackets(tmp_path, monkeypatch):
    """``deepseek-v4-pro[1m]`` key resolves via the ``deepseek-v4-pro`` row."""
    _write_pricing(tmp_path, monkeypatch)
    expected = (1_000_000 * 1.32 + 100_000 * 3.96) / 1e6
    assert claude_cost(_envelope(_DEEPSEEK_SUFFIX_USAGE), TokenUsage()) == pytest.approx(expected)


def test_claude_cost_mixed_anthropic_and_listpriced(tmp_path, monkeypatch):
    """Two-model envelope: list-price for the non-Anthropic + costUSD for the Anthropic one."""
    _write_pricing(tmp_path, monkeypatch)
    glm_cost = (10_000 * 1.40 + 1_000 * 4.40) / 1e6
    expected = glm_cost + 0.01
    assert claude_cost(_envelope(_MIXED_USAGE), TokenUsage()) == pytest.approx(expected)


def test_claude_cost_unknown_model_falls_back_to_costusd(tmp_path, monkeypatch):
    """Unknown model name → sum of its costUSD (today's behaviour, never guess)."""
    _write_pricing(tmp_path, monkeypatch)
    assert claude_cost(_envelope(_UNKNOWN_USAGE), TokenUsage()) == pytest.approx(0.5)


def test_claude_cost_no_model_usage_returns_total_cost():
    """Envelope without ``modelUsage`` → the bundled ``total_cost_usd`` exactly."""
    env = json.dumps({"result": "", "total_cost_usd": 0.0123})
    assert claude_cost(env, TokenUsage()) == pytest.approx(0.0123)


def test_claude_cost_missing_pricing_file_emits_costusd_per_model(tmp_path, monkeypatch):
    """Missing table → treat as empty → sum-of-costUSD per model (never zero)."""
    monkeypatch.setenv("MO_PRICING_YAML", str(tmp_path / "no-such-file.yaml"))
    env = _envelope(_GLM_USAGE, total_cost_usd=0.0)
    # GLM-5.3 is unknown (empty table) → falls back to mu["costUSD"] == 0.664.
    assert claude_cost(env, TokenUsage()) == pytest.approx(0.664)


def test_claude_cost_stream_json_envelope(tmp_path, monkeypatch):
    """``stream-json`` stdout with a ``type=="result"`` event at the end."""
    _write_pricing(tmp_path, monkeypatch)
    stream = (
        '{"type":"system","session_id":"abc"}\n'
        '{"type":"assistant","message":{"content":"working"}}\n'
        '{"type":"result","result":"","total_cost_usd":0.0,"modelUsage":'
        + json.dumps(_GLM_USAGE)
        + "}\n"
    )
    expected = (40442 * 1.40 + 168960 * 0.26 + 15103 * 4.40) / 1e6
    assert claude_cost(stream, TokenUsage()) == pytest.approx(expected)


def test_shipped_pricing_yaml_has_non_anthropic_rows_with_sane_input():
    """The shipped ``.mini-ork/config/pricing.yaml`` carries the four provider
    additions and none of them is at Anthropic rates (guards against a
    copy-paste of the Anthropic block).

    No monkeypatch — the test reads the actual file the dispatch will load.
    """
    import yaml

    path = Path(__file__).resolve().parents[2] / ".mini-ork" / "config" / "pricing.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    required = {
        "minimax": "MiniMax-M3",
        "zhipu": "GLM-5.3",
        "deepseek": ["deepseek-v4-pro", "deepseek-flash"],
        "moonshot": "kimi-k2.7-code",
    }
    for prov, models in required.items():
        if isinstance(models, str):
            models = [models]
        for model in models:
            rate = data["pricing"][prov][model]["input"]
            assert rate < 2.0, (prov, model, rate)