"""Native contract tests for deterministic provider-price lookup."""

from __future__ import annotations

import pytest
import yaml

from mini_ork.dispatch.pricing_strategy import lookup

# ── Fixtures ─────────────────────────────────────────────────────────────────
# Each entry yields the yaml body + the lookup triple. Written as
# standalone functions so the yaml content stays close to its assertions
# and the parametrize ids read like a spec.

def _yaml_anthropic() -> str:
    return (
        "pricing:\n"
        "  anthropic:\n"
        "    claude-sonnet-4-6:\n"
        "      input:       3.00\n"
        "      output:     15.00\n"
        "      cache_read:  0.30\n"
        "      cache_write: 3.75\n"
        "  openai:\n"
        "    gpt-5:\n"
        "      input:       2.50\n"
        "      output:     10.00\n"
    )


def _yaml_custom_decimal() -> str:
    return (
        "pricing:\n"
        "  custom:\n"
        "    special-model:\n"
        "      input:  10.50\n"
        "      output: 99.99\n"
    )


def _yaml_int_rates() -> str:
    return (
        "pricing:\n"
        "  anthropic:\n"
        "    claude-haiku-4-5:\n"
        "      input:  5\n"
        "      output: 25\n"
    )


def _yaml_missing_cache_column() -> str:
    """gpt-5 has no cache_read/cache_write — those keys must silently miss."""
    return (
        "pricing:\n"
        "  openai:\n"
        "    gpt-5:\n"
        "      input:  2.50\n"
        "      output: 10.00\n"
    )


def _yaml_minimal() -> str:
    return "pricing:\n  anthropic:\n    claude-sonnet-4-6:\n      input: 3.00\n"


FIXTURES = [
    # (id, yaml_body, provider, model, kind, expected)
    ("f01_hit_input",            _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "input",       "3.0"),
    ("f02_hit_cache_read",       _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "cache_read",  "0.3"),
    ("f03_hit_output",           _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "output",      "15.0"),
    ("f04_miss_provider",        _yaml_anthropic(),         "unknown",   "claude-sonnet-4-6", "input",       "0"),
    ("f05_miss_model",           _yaml_anthropic(),         "anthropic", "gpt-5",             "input",       "0"),
    ("f06_miss_cache_write",     _yaml_missing_cache_column(), "openai", "gpt-5",            "cache_write", "0"),
    ("f07_unknown_kind",         _yaml_anthropic(),         "anthropic", "claude-sonnet-4-6", "bogus_kind",  "0"),
    ("f08_custom_decimal",       _yaml_custom_decimal(),    "custom",    "special-model",     "input",       "10.5"),
    ("f09_int_rate",             _yaml_int_rates(),         "anthropic", "claude-haiku-4-5",  "input",       "5"),
    ("f10_minimal",              _yaml_minimal(),           "anthropic", "claude-sonnet-4-6", "input",       "3.0"),
]


@pytest.mark.parametrize(
    "yaml_body,provider,model,kind,expected",
    [(f[1], f[2], f[3], f[4], f[5]) for f in FIXTURES],
    ids=[f[0] for f in FIXTURES],
)
def test_pricing_lookup_handles_fixture(yaml_body, provider, model, kind, expected):
    assert lookup(yaml.safe_load(yaml_body), provider, model, kind) == expected


def test_known_price_lookup_returns_scalar_string():
    assert lookup(yaml.safe_load(_yaml_anthropic()), "anthropic", "claude-sonnet-4-6", "input") == "3.0"


def test_smoke_import_and_lookup_no_io():
    """Pure-path smoke: import the module, call lookup against an in-memory
    dict — no env, no file, no subprocess. Confirms the port works
    in-process and exercises the ALLOWED branch.
    """
    data = {
        "pricing": {
            "anthropic": {
                "claude-sonnet-4-6": {
                    "input": 3.00,
                    "cache_read": 0.30,
                }
            }
        }
    }
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "input") == "3.0"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "cache_read") == "0.3"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "cache_write") == "0"
    assert lookup(data, "anthropic", "claude-sonnet-4-6", "notakind") == "0"
    assert lookup(data, "unknown", "claude-sonnet-4-6", "input") == "0"
    assert lookup(data, "anthropic", "unknown", "input") == "0"
    assert lookup(None, "anthropic", "claude-sonnet-4-6", "input") == "0"
    assert lookup({"pricing": "not a dict"}, "anthropic", "claude-sonnet-4-6", "input") == "0"
