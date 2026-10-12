"""``/why`` — the failure-evidence aggregator.

The gap these pin: for the most common death there is — a run whose dispatch
chain died at its first node — every source ``aggregate`` read was *legitimately*
empty, because the run never reached the point of writing ``execute.log`` or a
verifier result. So ``/why`` answered "no diagnostic data found" for a run killed
by a hard quota wall, while the whole story sat in ``llm_calls``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.stores import migrate as mig
from mini_ork.web import db as webdb
from mini_ork.web import why

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    (h / "runs" / "run-why-1").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _lane(home: Path, run_id: str, lane: str, category: str, message: str) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO llm_calls (ts, provider, model_id, tier, feature_name, actor, run_id, "
        "status, cost_usd, duration_ms, error_category, error_message, retryable) "
        "VALUES (datetime('now'),'openai',?,1,'mini-ork:planner','scheduler',?,"
        "'failed',0,0,?,?,0)",
        (lane, run_id, category, message))
    con.commit()
    con.close()


def _aggregate(home: Path) -> dict:
    return why.aggregate(home, webdb.db_for(home), "run-why-1")


def test_a_dead_dispatch_chain_is_named_lane_by_lane(home: Path) -> None:
    """One row per dead lane, in walk order, with the terminal lane's own words."""
    _lane(home, "run-why-1", "glm", "unknown",
          "dispatch result failed shape check (looks_like_json)")
    _lane(home, "run-why-1", "minimax", "unknown",
          "dispatch result failed shape check (looks_like_json)")
    _lane(home, "run-why-1", "codex", "quota",
          '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}')

    diag = _aggregate(home)

    assert [f["lane"] for f in diag["lane_failures"]] == ["glm", "minimax", "codex"]
    # The terminal lane's message is unwrapped from the provider envelope.
    assert diag["lane_failures"][-1]["message"] == "You've hit your usage limit."
    assert diag["summary"] == (
        "dispatch chain died: glm(shape), minimax(shape), codex(quota)"
        " — You've hit your usage limit.")


def test_a_shape_rejection_is_named_not_called_unknown(home: Path) -> None:
    """``error_category`` has no value for a mini-ork-level rejection — the
    CHECK constraint pins it to the PROVIDER vocabulary — so a shape rejection
    lands in the ledger as ``unknown``. Deriving the label at display time is
    what keeps the failure that actually happens most from reading as "unknown".
    """
    _lane(home, "run-why-1", "glm", "unknown",
          "dispatch result failed shape check (looks_like_json)")

    diag = _aggregate(home)

    assert diag["lane_failures"][0]["category"] == "unknown"
    assert "glm(shape)" in diag["summary"]


def test_lane_failures_outrank_the_all_passed_catch_all(home: Path) -> None:
    """A run can write a passing verifier result and still have died at the
    planner on a *previous* attempt; "all verifiers passed" would read as if
    nothing had gone wrong."""
    (home / "runs" / "run-why-1" / "verifier-result-lint.json").write_text(
        json.dumps({"pass": True}))
    _lane(home, "run-why-1", "codex", "quota", "usage limit")

    assert _aggregate(home)["summary"].startswith("dispatch chain died: codex(quota)")


def test_a_failed_verifier_still_outranks_the_lane_ledger(home: Path) -> None:
    """Orchestrator-level failure is the more specific answer when it exists."""
    (home / "runs" / "run-why-1" / "verifier-result-lint.json").write_text(
        json.dumps({"pass": False}))
    _lane(home, "run-why-1", "codex", "quota", "usage limit")

    assert _aggregate(home)["summary"] == "verifier failure: lint"


def test_nothing_recorded_anywhere_is_still_honest(home: Path) -> None:
    diag = _aggregate(home)

    assert diag["lane_failures"] == []
    assert diag["summary"] == "no specific failure signal found"
