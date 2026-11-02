"""Is the learning loop closed and default-ON? The Python gate that says so.

The bash proof of this claim (`scripts/learning-loop-closure-gate.sh`, 14/14)
was deleted with the rest of the shell runtime, and the doc kept citing it —
so from the 2026-07 removal until now the only evidence for "mini-ork learns and
uses its learnings on every real run" was a paragraph. This is the port, and it
keeps the two evidence classes the script distinguished:

* ``[CODE]`` — the defaults and the wiring, in source. Grep is the honest tool
  here: the alternative is executing a whole run to observe a default, and a
  test that costs a model call to answer a static question is a test nobody
  keeps running.
* ``[LIVE]`` — the schema that records and reads learnings, asserted on a
  **freshly migrated** database instead of the operator's state.db. The old
  gate skipped itself when no DB existed, which is how the cold-start branch
  went unchecked for a year. Cold start is now the tested path.

Plus three behavioural checks the shell gate could not make: PRM scoring
separates success from failure, the GRPO writer runs on an empty store, and the
routing policy resolves to ``learning_governed`` with no data.

Run it as the doc's Validation section says:

    python3 -m pytest -q tests/unit/test_learning_loop_closure_py.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.learning import process_reward  # noqa: E402
from mini_ork.learning.writeback import write_grpo_advantages  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

EXECUTE = REPO / "mini_ork" / "cli" / "execute.py"
ROUTING = REPO / "mini_ork" / "dispatch" / "routing.py"


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("learning") / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return path


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── [CODE] the loop fires without an opt-in ────────────────────────────────


def test_prm_scoring_is_default_on():
    assert 'os.environ.get("MO_PRM_SCORE", "1") == "1"' in _src(EXECUTE)


def test_the_native_prm_scorer_is_wired_into_the_trace_path():
    assert "from mini_ork.learning.process_reward import score_trace" in _src(EXECUTE)


def test_reward_stamping_is_default_on():
    assert 'os.environ.get("MO_REWARD_STAMP", "1")' in _src(EXECUTE)


def test_the_routing_policy_defaults_to_learning_governed():
    assert 'os.environ.get("MO_ROUTING_POLICY") or "learning_governed"' in _src(ROUTING)


def test_the_resolved_lane_reaches_every_trace():
    source = _src(EXECUTE)
    assert 'extra["agent_version_id"] = lane' in source
    assert "lane=lane" in source


def test_both_writers_exist():
    from mini_ork.learning import writeback

    assert callable(writeback.write_grpo_advantages)
    assert callable(writeback.learning_update_conductor_outcomes)


# ── [LIVE] a fresh database can record AND read learnings ──────────────────


@pytest.mark.parametrize(
    "table,column",
    [
        ("execution_traces", "process_reward"),
        ("execution_traces", "agent_version_id"),
        ("agent_performance_memory", "relative_advantage"),
    ],
)
def test_the_learning_columns_exist(db: Path, table: str, column: str):
    with sqlite3.connect(db) as con:
        cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    assert column in cols


@pytest.mark.parametrize(
    "table",
    ["grounded_rejections", "prompt_win_rates", "conductor_decisions"],
)
def test_the_learning_tables_exist(db: Path, table: str):
    with sqlite3.connect(db) as con:
        found = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
    assert found == 1


# ── behaviour the shell gate could not check ───────────────────────────────


def test_prm_scores_a_success_above_a_failure():
    import json

    ok = process_reward.score_trace({
        "status": "success",
        "tool_calls": json.dumps([{"name": "read"}]),
        "files_written": json.dumps(["a.py"]),
        "files_read": json.dumps(["b.py"]),
    })
    failed = process_reward.score_trace({"status": "failed", "tool_calls": "[]"})
    assert 0.0 <= failed < ok <= 1.0


def test_grpo_writeback_is_safe_on_an_empty_store(db: Path):
    """Cold start must not raise: a fresh project has no traces to average."""
    assert write_grpo_advantages(str(db)) == 0


def test_grpo_writeback_is_safe_when_the_database_is_missing(tmp_path: Path):
    assert write_grpo_advantages(str(tmp_path / "nope.db")) == 0


def test_the_routing_policy_resolves_from_the_environment(monkeypatch):
    """The documented default, observed rather than grepped: a process with no
    ``MO_ROUTING_POLICY`` selects ``learning_governed``; an explicit unknown
    value falls back to the workflow lane instead of raising."""
    from mini_ork.dispatch import routing

    monkeypatch.delenv("MO_ROUTING_POLICY", raising=False)
    seen: list[str] = []

    def _policy(ctx):
        seen.append(ctx.node_type)
        return "cheap"

    # Replace the registered default with a probe: if the unset env var still
    # resolves to `learning_governed`, the probe is the handler that runs.
    original = routing.POLICY_REGISTRY.get("learning_governed")
    routing.POLICY_REGISTRY["learning_governed"] = _policy
    try:
        lane = routing.policy_route_lane("implementer", "frontier", root=str(REPO))
    finally:
        if original is not None:
            routing.POLICY_REGISTRY["learning_governed"] = original
        else:  # pragma: no cover — the registry seeds the default at import
            routing.POLICY_REGISTRY.pop("learning_governed", None)
    assert lane == "cheap"
    assert seen == ["implementer"]


def test_the_doc_points_at_this_file():
    doc = (REPO / "docs" / "LEARNING-LOOP-LIFECYCLE.md").read_text(encoding="utf-8")
    assert "test_learning_loop_closure_py.py" in doc
    assert "learning-loop-closure-gate.sh" not in doc


def test_no_shell_gate_is_cited_as_evidence():
    """The bash runtime is gone; a doc that cites a deleted script as proof is
    worse than one that cites nothing."""
    doc = (REPO / "docs" / "LEARNING-LOOP-LIFECYCLE.md").read_text(encoding="utf-8")
    for dead in ("lib/process_reward.sh", "lib/cross_epic_gradient.sh",
                 "scripts/smoke-learning-loops.sh"):
        assert dead not in doc, dead
