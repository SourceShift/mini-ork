"""The lane fuse is keyed per placement — RB-24.

A lane that failed a credential check on a remote node (``daytona``) used to
open the fuse for every dispatch of that lane, including host dispatches where
the lane had never run. Runs were refused at plan stage (rc 43) under a
placement where nothing had failed.

Hermetic: every test builds its own sqlite ledger under ``tmp_path`` and scrubs
the run-identity/placement environment (RB-04, RB-27) so nothing can touch the
live DB or adopt this run's identity.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.dispatch import llm_dispatch as L

# The post-migration llm_calls shape the fuse reads and the row writer inserts,
# with the real CHECK constraints so a bad row fails loudly.
SCHEMA = """
CREATE TABLE llm_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  tier            TEXT NOT NULL,
  feature_name    TEXT NOT NULL,
  actor           TEXT,
  run_id          INTEGER,
  iter            INTEGER,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ('success','failed')),
  error_message   TEXT,
  traceparent     TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  session_id      TEXT,
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  error_category  TEXT NULL CHECK (
    error_category IS NULL OR error_category IN (
      'auth','quota','capacity','request','safety',
      'network','stream','provider','config','unknown')),
  retryable       INTEGER NULL CHECK (retryable IS NULL OR retryable IN (0, 1))
);
"""

_SCRUB_ENV = (
    "MO_PLACEMENT", "MO_NODE_ENV",
    "MO_FUSE_TERMINAL", "MO_FUSE_TRANSIENT", "MO_FUSE_WINDOW_HOURS",
    "MO_FUSE_ENABLED", "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR",
    "MINI_ORK_DB", "MINI_ORK_HOME", "MO_LANE_ACTOR", "MO_TARGET_CWD",
)

_LANE = "glm"
_PRESPAWN_GUARD = (
    "remote placement: lane 'glm' is anthropic-native and its api key is "
    "missing on the node: the local Claude login cannot travel."
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    for var in _SCRUB_ENV:
        monkeypatch.delenv(var, raising=False)
    path = str(tmp_path / "state.db")
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return path


def _host(monkeypatch):
    monkeypatch.delenv("MO_PLACEMENT", raising=False)
    monkeypatch.delenv("MO_NODE_ENV", raising=False)


def _daytona(monkeypatch):
    monkeypatch.setenv("MO_PLACEMENT", "remote")
    monkeypatch.setenv("MO_NODE_ENV", "daytona")


def _write_row(db, *, feature_name="mini-ork:planner", error_category="auth",
               metadata_json="{}", actor="planner"):
    """One failed row through the real writer (which stamps the placement)."""
    L.write_llm_calls_row(
        db, "anthropic", _LANE, "default", feature_name, actor, "failed",
        1200, "0", "Please run /login",
        metadata_json=metadata_json, error_category=error_category, retryable=0)


def _insert_legacy_row(db, *, metadata_json="{}"):
    """A pre-RB-24 row: written directly, so it carries no placement key."""
    con = sqlite3.connect(db, timeout=5.0)
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor,"
        " status, error_message, metadata_json, error_category, retryable)"
        " VALUES ('anthropic', ?, 'default', 'mini-ork:planner', 'planner',"
        " 'failed', 'Please run /login', ?, 'auth', 0)",
        (_LANE, metadata_json))
    con.commit()
    con.close()


def _metadata(db):
    con = sqlite3.connect(db, timeout=5.0)
    row = con.execute(
        "SELECT metadata_json FROM llm_calls WHERE model_id = ?"
        " ORDER BY id DESC LIMIT 1", (_LANE,)).fetchone()
    con.close()
    assert row is not None
    return json.loads(row[0] or "{}")


# ── mechanism 1: the row records its placement ──────────────────────────────

def test_row_records_node_env_name_as_placement(db, monkeypatch):
    _daytona(monkeypatch)
    _write_row(db, metadata_json='{"dispatch_attempt": 1}')
    md = _metadata(db)
    assert md["placement"] == "daytona"
    # The placement is merged into the caller's metadata, not written instead
    # of it.
    assert md["dispatch_attempt"] == 1


def test_row_records_host_placement_when_placement_env_missing(db, monkeypatch):
    _host(monkeypatch)
    _write_row(db)
    assert _metadata(db)["placement"] == "host"


# ── mechanism 2: the fuse counts same-placement strikes only ────────────────

def test_daytona_auth_strike_does_not_open_host_fuse(db, monkeypatch):
    _daytona(monkeypatch)
    _write_row(db)
    assert _metadata(db)["placement"] == "daytona"

    _host(monkeypatch)
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is False


def test_daytona_auth_strike_opens_daytona_fuse(db, monkeypatch):
    _daytona(monkeypatch)
    _write_row(db)
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is True


def test_host_strike_opens_host_fuse(db, monkeypatch):
    """Regression: scoping the fuse per placement must not blind it at home."""
    _host(monkeypatch)
    _write_row(db)
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is True


def test_legacy_row_without_placement_counts_as_host(db, monkeypatch):
    _insert_legacy_row(db)  # metadata_json="{}" — no placement key
    _host(monkeypatch)
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is True

    _daytona(monkeypatch)
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is False


def test_chain_fuse_is_scoped_to_the_dispatch_placement(db, monkeypatch):
    _daytona(monkeypatch)
    _write_row(db)

    _host(monkeypatch)
    _chain, refused = L._apply_lane_fuse(db, _LANE)
    assert refused == ""

    _daytona(monkeypatch)
    _chain, refused = L._apply_lane_fuse(db, _LANE)
    assert refused == "auth"


# ── mechanism 3: pre-spawn guard refusals are never strikes ─────────────────

def test_prespawn_guard_refusal_never_counts_as_strike(db, monkeypatch, tmp_path):
    """Drive a real dispatch through a stub that returns the guard message.

    The guard fires before anything is sent, so the row it leaves must carry
    the ``:refused`` actor suffix and stay out of the fuse streak for its own
    placement. Asserting on a hand-seeded suffixed row would prove only the SQL
    predicate, not the writer.
    """
    _daytona(monkeypatch)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "1000")
    out = str(tmp_path / "out.txt")

    def guard_stub(model, prompt, out_file, timeout_s, max_turns):
        with open(out_file + ".err.log", "w", encoding="utf-8") as fh:
            fh.write(_PRESPAWN_GUARD + "\n")
        return 2

    rc = L.llm_dispatch(
        ["--node-type", "planner", "--model", _LANE, "--prompt-text", "hi",
         "--out", out],
        root=str(tmp_path / "root"), dispatch_fn=guard_stub)
    assert rc == 2

    con = sqlite3.connect(db, timeout=5.0)
    actor, metadata_json = con.execute(
        "SELECT actor, metadata_json FROM llm_calls WHERE model_id = ?"
        " ORDER BY id DESC LIMIT 1", (_LANE,)).fetchone()
    con.close()
    assert actor.endswith(":refused")
    assert json.loads(metadata_json)["placement"] == "daytona"
    assert L.check_lane_fuse(db, _LANE, "config", strikes=1) is False


def test_refused_feature_row_never_counts_as_strike(db, monkeypatch):
    """A guard refusal written with the historic feature-name suffix is
    likewise outside the streak (the existing predicate survives the new
    placement filter)."""
    _daytona(monkeypatch)
    _write_row(db, feature_name="mini-ork:planner:refused",
               metadata_json='{"refused": "auth"}')
    assert L.check_lane_fuse(db, _LANE, "auth", strikes=1) is False
