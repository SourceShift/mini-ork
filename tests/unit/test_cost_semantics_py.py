"""F2: per-provider token semantics + rates for llm_calls cost telemetry.

Covers the three defects the audit measured on the live DB:
  1. anthropic breakdown zeroed by an unconditional in-cache subtraction
     ($0.0017 uncached across 2,856 rows);
  2. openai/codex breakdown priced at Anthropic list rates (~$1,160 phantom
     vs $115.73 real);
  3. codex cached tokens lost at the 2-field sidecar boundary
     (cached_input_tokens always 0 for sidecar lanes).
"""
from __future__ import annotations

import sqlite3

import pytest

from mini_ork.dispatch.llm_dispatch import write_llm_calls_row
from mini_ork.dispatch.models import TokenUsage
from mini_ork.dispatch.providers import _read_codex_sidecars
from mini_ork.dispatch.telemetry import cache_aware_cost, family_of, rates_for

SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT, actor TEXT,
  status TEXT, duration_ms INTEGER, cost_usd REAL, error_message TEXT,
  input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
  metadata_json TEXT,
  cached_input_tokens INTEGER DEFAULT 0,
  cache_creation_input_tokens INTEGER DEFAULT 0,
  cost_input_uncached_usd REAL DEFAULT 0,
  cost_input_cached_usd REAL DEFAULT 0,
  cost_cache_write_usd REAL DEFAULT 0,
  iter INTEGER, run_id TEXT, traceparent TEXT, session_id TEXT
)
"""


def _db(tmp_path):
    p = tmp_path / "calls.db"
    with sqlite3.connect(p) as con:
        con.execute(SCHEMA)
    return str(p)


def _breakdown(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT provider, cost_input_uncached_usd, cost_input_cached_usd, "
        "cost_cache_write_usd FROM llm_calls ORDER BY id"
    ).fetchall()
    con.close()
    return rows


# ── family resolution ───────────────────────────────────────────────────────

@pytest.mark.parametrize("alias,fam", [
    ("anthropic", "anthropic"), ("opus", "anthropic"), ("sonnet", "anthropic"),
    ("openai", "openai"), ("codex", "openai"),
    ("gateway", "gateway"), ("minimax", "gateway"), ("glm", "gateway"),
    ("kimi", "gateway"), ("deepseek", "gateway"),
    ("google", "google"), ("gemini", "google"),
    ("openrouter/foo", "unknown"), ("", "unknown"),
])
def test_family_of_collapses_aliases(alias, fam):
    assert family_of(alias) == fam


def test_rates_for_known_vs_unknown_families():
    assert rates_for("opus") == (15.0, 1.5, 18.75)
    assert rates_for("codex") == (1.25, 0.125, 0.0)
    assert rates_for("minimax") is None      # gateway: never fabricate
    assert rates_for("openrouter/x") is None


# ── write_llm_calls_row breakdown is family-aware ──────────────────────────

def test_writer_anthropic_uncached_is_input_as_is(tmp_path):
    db = _db(tmp_path)
    # anthropic envelope: input EXCLUDES cache; 44k fresh + 261k read is a
    # real live shape the old subtraction collapsed to zero.
    write_llm_calls_row(db, "anthropic", "opus", "default", "mini-ork:t", "a",
                        "success", 100, 0.67, "", 44219, 500, "{}", 260864, 0)
    (prov, unc, cac, cwr), = _breakdown(db)
    assert prov == "anthropic"
    assert unc == pytest.approx(44219 * 15.0 / 1e6)
    assert cac == pytest.approx(260864 * 1.5 / 1e6)
    assert cwr == 0.0


def test_writer_openai_subtracts_cache_at_gpt5_rates(tmp_path):
    db = _db(tmp_path)
    # codex stream: input INCLUDES cached (241M total / 95.8M cached live shape)
    write_llm_calls_row(db, "openai", "codex", "default", "mini-ork:t", "a",
                        "success", 100, 0.5, "", 1000, 500, "{}", 800, 0)
    (prov, unc, cac, cwr), = _breakdown(db)
    assert prov == "openai"
    assert unc == pytest.approx(200 * 1.25 / 1e6)
    assert cac == pytest.approx(800 * 0.125 / 1e6)
    assert cwr == 0.0


def test_writer_gateway_breakdown_is_zero_not_fabricated(tmp_path):
    db = _db(tmp_path)
    write_llm_calls_row(db, "gateway", "minimax", "default", "mini-ork:t", "a",
                        "success", 100, 0.30, "", 100000, 5000, "{}", 50000, 0)
    (prov, unc, cac, cwr), = _breakdown(db)
    assert prov == "gateway"
    assert (unc, cac, cwr) == (0.0, 0.0, 0.0)


def test_cache_aware_cost_explicit_rates_still_override():
    u = TokenUsage(input_tokens=1000, cached_input_tokens=100)
    unc, cac, _ = cache_aware_cost(u, provider="gateway",
                                   rate_uncached_in=0.3, rate_cached_in=0.03)
    assert unc == pytest.approx(1000 * 0.3 / 1e6)
    assert cac == pytest.approx(100 * 0.03 / 1e6)


# ── sidecar boundary carries cached/creation ────────────────────────────────

def test_sidecar_roundtrip_carries_cached_and_creation(tmp_path):
    usage_f = tmp_path / "u.tokens"
    cost_f = tmp_path / "c.cost"
    usage_f.write_text("3000\t1200\t300\t141\n")
    cost_f.write_text("0.004300\n")
    usage, cost = _read_codex_sidecars(str(usage_f), str(cost_f))
    assert usage.input_tokens == 3000
    assert usage.output_tokens == 1200
    assert usage.cached_input_tokens == 300       # was always 0 pre-F2
    assert usage.cache_creation_tokens == 141
    assert cost == pytest.approx(0.0043)


def test_sidecar_legacy_2_field_still_parses(tmp_path):
    usage_f = tmp_path / "u.tokens"
    usage_f.write_text("1500\t250\n")
    usage, cost = _read_codex_sidecars(str(usage_f), str(tmp_path / "missing"))
    assert usage.input_tokens == 1500
    assert usage.output_tokens == 250
    assert usage.cached_input_tokens == 0
    assert cost == 0.0


# ── backfill of historical rows ─────────────────────────────────────────────

def _backfill_db(tmp_path):
    """Seed rows carrying the PRE-F2 breakdown damage: an anthropic row with
    zeroed uncached cost, an openai row at phantom Anthropic rates, and a
    gateway row with fabricated numbers."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
        "status, duration_ms, cost_usd, input_tokens, output_tokens, "
        "total_tokens, metadata_json, cached_input_tokens, "
        "cache_creation_input_tokens, cost_input_uncached_usd, "
        "cost_input_cached_usd, cost_cache_write_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # in=44219 cached=260864 -> old uncached 0; correct = 44219*15/1e6
            ("anthropic", "opus", "default", "f", "a", "success", 1, 0.67,
             44219, 500, 44719, "{}", 260864, 0, 0.0, 0.391296, 0.0),
            # codex: in includes cached -> old 241M*15/1e6 phantom
            ("openai", "codex", "default", "f", "a", "success", 1, 0.5,
             1000, 100, 1100, "{}", 800, 0, 0.003, 0.0012, 0.0),
            # gateway: fabricated anthropic-rate numbers -> zero
            ("gateway", "minimax", "default", "f", "a", "success", 1, 0.3,
             100000, 5000, 105000, "{}", 50000, 0, 0.75, 0.075, 0.0),
        ],
    )
    con.commit()
    con.close()
    return db


def test_backfill_script_corrects_historical_breakdown(tmp_path):
    import subprocess
    import sys as _sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    db = _backfill_db(tmp_path)

    def run(*extra):
        return subprocess.run(
            [_sys.executable, str(repo / "scripts/llm_calls_cost_backfill.py"),
             db, *extra], capture_output=True, text=True, check=True)

    out = run().stdout
    assert "DRY RUN" in out
    assert _breakdown(db)[0][1] == 0.0  # dry-run changed nothing

    run("--apply")
    anth, oai, gtw = _breakdown(db)
    assert anth[1] == pytest.approx(44219 * 15.0 / 1e6)   # was 0.0
    assert oai[1] == pytest.approx(200 * 1.25 / 1e6)      # was phantom 0.003
    assert oai[2] == pytest.approx(800 * 0.125 / 1e6)
    assert gtw[1:] == (0.0, 0.0, 0.0)                     # fabricated -> zero
    # tokens/cost_usd are never touched
    con = sqlite3.connect(db)
    rows = con.execute("SELECT provider, cost_usd, input_tokens FROM llm_calls "
                       "ORDER BY id").fetchall()
    con.close()
    assert rows == [("anthropic", 0.67, 44219),
                    ("openai", 0.5, 1000),
                    ("gateway", 0.3, 100000)]
