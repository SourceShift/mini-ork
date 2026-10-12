"""The lane fuse, the fallback chain, and the stale-epic reclaim.

The defect these pin: a lane that failed 100% of its calls stayed the terminal
rung of every coding chain for a day, and nothing in the framework noticed —
because the fuse was off by default, keyed on the ROLE (a name a fallback tail
lane never appears under), and gated on ``retryable=1``, the one flag a quota
wall is deliberately written without.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.dispatch import llm_dispatch as L  # noqa: E402
from mini_ork.dispatch.routing import dispatch_chain  # noqa: E402

SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  provider TEXT, model_id TEXT, tier INTEGER, feature_name TEXT,
  status TEXT, error_category TEXT, error_message TEXT, retryable INTEGER
);
"""


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return str(path)


def _fail(db: str, lane: str, category: str, *, n: int = 1, feature: str = "mini-ork:planner",
          retryable: int = 0, ago_hours: int = 0) -> None:
    # ISO-8601 with the 'T' and 'Z', exactly as the column's DEFAULT writes it.
    con = sqlite3.connect(db)
    for _ in range(n):
        con.execute(
            "INSERT INTO llm_calls (ts, provider, model_id, tier, feature_name, status, "
            "error_category, error_message, retryable) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), 'openai', ?, 1, ?, "
            "'failed', ?, 'boom', ?)",
            (f"-{ago_hours} hours", lane, feature, category, retryable))
    con.commit()
    con.close()


# ── the fuse is keyed on the LANE ────────────────────────────────────────────

def test_fuse_trips_on_a_lane_even_when_no_role_ever_named_it(db: str) -> None:
    """The tail lane's rows carry the LANE, never the node role.

    Keyed on ``feature_name LIKE '%:planner'`` the fuse could not see a fallback
    lane at all: that row's feature is ``mini-ork:planner`` regardless of which
    lane ran, and a tail lane is only ever reached when the lead already failed.
    """
    _fail(db, "codex", "quota", n=1, feature="mini-ork:implementer")

    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is True
    assert L.check_lane_fuse(db, "minimax", "quota", strikes=1) is False


def test_fuse_sees_a_terminal_category_that_is_not_retryable(db: str) -> None:
    """``quota`` is written ``retryable=0`` on purpose — retrying cannot fix it.

    A fuse that demanded ``retryable=1`` therefore could never open on the
    category that actually kills runs.
    """
    _fail(db, "codex", "quota", n=1, retryable=0)

    assert L.error_retryable("quota") == "0"
    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is True


def test_fuse_keeps_three_strikes_for_a_transient_category(db: str) -> None:
    _fail(db, "glm", "network", n=2)

    assert L.check_lane_fuse(db, "glm", "network", strikes=3) is False
    _fail(db, "glm", "network", n=1)
    assert L.check_lane_fuse(db, "glm", "network", strikes=3) is True


def test_a_success_breaks_the_streak(db: str) -> None:
    _fail(db, "glm", "network", n=2)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO llm_calls (provider, model_id, feature_name, status) "
                "VALUES ('gateway', 'glm', 'mini-ork:planner', 'success')")
    con.commit()
    con.close()
    _fail(db, "glm", "network", n=2)

    assert L.check_lane_fuse(db, "glm", "network", strikes=3) is False


def test_the_window_lets_a_lane_recover(db: str) -> None:
    """A fused lane records nothing while fused, so without a window its own
    last failure would hold the fuse open forever."""
    _fail(db, "codex", "quota", n=1, ago_hours=48)

    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is False


def test_the_window_is_measured_in_HOURS_not_days(
        db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The column stores ISO-8601 (``...T06:00:00.000Z``); SQLite's
    ``datetime('now')`` yields a space-separated string, and ``'T'`` (0x54)
    sorts above ``' '`` (0x20). A raw string comparison therefore drops the time
    half of the window and keeps every row from TODAY — so a lane that failed
    this morning still reads as fused tonight."""
    monkeypatch.setenv("MO_FUSE_WINDOW_HOURS", "1")
    _fail(db, "codex", "quota", n=1, ago_hours=2)

    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is False

    _fail(db, "codex", "quota", n=1)  # now, inside the window
    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is True


def test_a_refusal_row_never_keeps_a_fuse_open(db: str) -> None:
    """The fuse's own refusal row names the fused category; counting it would
    make the fuse self-sustaining."""
    _fail(db, "codex", "quota", n=1, feature="mini-ork:planner:refused")

    assert L.check_lane_fuse(db, "codex", "quota", strikes=1) is False


# ── the chain narrows around a fused lane ────────────────────────────────────

def test_chain_drops_the_fused_lane_and_keeps_the_rest(db: str) -> None:
    _fail(db, "codex", "quota", n=1)

    assert L._apply_lane_fuse(db, "glm,minimax,codex") == ("glm,minimax", "")


def test_a_chain_of_only_fused_lanes_is_refused(db: str) -> None:
    _fail(db, "codex", "quota", n=1)

    chain, refused = L._apply_lane_fuse(db, "codex")

    assert chain == "codex" and refused == "quota"


def test_a_fused_lead_does_not_doom_its_chain(db: str) -> None:
    _fail(db, "codex", "quota", n=1)

    assert L._apply_lane_fuse(db, "codex,glm") == ("glm", "")


def test_an_unfused_chain_is_returned_untouched(db: str) -> None:
    assert L._apply_lane_fuse(db, "glm,minimax,kimi,deepseek_flash") == (
        "glm,minimax,kimi,deepseek_flash", "")


def test_transient_categories_stay_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Halting on a transient category is the false positive the default avoids."""
    monkeypatch.delenv("MO_FUSE_TERMINAL", raising=False)
    monkeypatch.delenv("MO_FUSE_TRANSIENT", raising=False)

    assert dict(L.fuse_categories()) == {"quota": 1, "auth": 1, "config": 1}

    monkeypatch.setenv("MO_FUSE_TRANSIENT", "1")
    assert dict(L.fuse_categories())["network"] == 3


# ── the default coding tail is same-kind and registry-clean ──────────────────

def test_default_coding_tail_prefers_same_kind_gateway_lanes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default ended at ``codex`` — a native lane of a different
    kind whose own quota wall then removed the last rung."""
    monkeypatch.delenv("MO_FALLBACK_CODING", raising=False)
    chain = dispatch_chain("implementer", "minimax", str(REPO))

    assert chain.split(",")[-1] != "codex"
    assert "kimi" in chain and "deepseek_flash" in chain


def test_an_env_opt_in_still_reaches_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MO_FALLBACK_CODING", "minimax,kimi,codex")

    assert dispatch_chain("implementer", "minimax", str(REPO)).endswith("codex")


# ── the dispatch-kwarg seam ──────────────────────────────────────────────────

def test_a_custom_dispatcher_that_declares_the_hooks_receives_them() -> None:
    """Gating the observer on "is this the default dispatcher" starved every
    caller-supplied dispatcher of per-lane ledger rows."""

    def with_hooks(model, prompt, out, timeout, turns, *, on_attempt=None, accept=None):
        return 0

    def without_hooks(model, prompt, out, timeout, turns):
        return 0

    assert L._accepts_kwarg(with_hooks, "on_attempt") is True
    assert L._accepts_kwarg(with_hooks, "accept") is True
    assert L._accepts_kwarg(without_hooks, "on_attempt") is False
    assert L._accepts_kwarg(len, "on_attempt") is False


def test_error_message_head_unwraps_a_truncated_envelope() -> None:
    """``error_message`` is a tail slice, so the envelope arrives cut at the front."""
    raw = (':19 AM."} {"type":"turn.failed","error":'
           '{"message":"You\'ve hit your usage limit. Upgrade to Plus."}}')

    assert L.error_message_head(raw) == "You've hit your usage limit. Upgrade to Plus."


def test_error_message_head_passes_plain_text_through() -> None:
    assert L.error_message_head("  connection   reset \n") == "connection reset"
