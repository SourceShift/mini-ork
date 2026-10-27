"""Unit tests: the region slice as a learned-routing pick source.

RSI Tier-1 #10. ``learning_governed_lane`` now passes ``segment=`` into
``decision_service.decide`` so an unpinned implementer under
``learning_governed`` consults the region slice before the domain and global
slices. Hermetic: temp SQLite DB via ``db/init.sh``, temp dirs, no network.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import mini_ork
import mini_ork.cli.execute as execute
import mini_ork.cli.execute_handlers as execute_handlers
from mini_ork import lane_router, trace_store
from mini_ork.context import run_context_scope
from mini_ork.dispatch import routing

REPO = Path(mini_ork.__file__).resolve().parents[1]

MINI = [
    "`mini_ork/dispatch/routing.py`",
    "`mini_ork/cli/execute_handlers.py`",
    "`tests/unit/test_x.py` (new)",
    "Do NOT modify `docs/a.md` or any other file.",
]


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    env = dict(os.environ)
    env.update({
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
        "MO_STORE_DB": db,
    })
    subprocess.run(["bash", str(REPO / "db" / "init.sh")], cwd=str(REPO), env=env,
                   check=True, capture_output=True, timeout=300)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_STORE_DB", db)
    monkeypatch.setenv("EPSILON", "0")
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "1")
    monkeypatch.setenv("MO_LEARNING_HALFLIFE_DAYS", "0")
    for var in ("MO_ROUTE_REGION", "MO_ROUTING_POLICY", "MINI_ORK_OBJECTIVE_DOMAIN",
                "MO_OBJECTIVE_DOMAIN", "MO_EQUIROUTER",
                # Not in the kickoff's list, but a dispatched test process has
                # MINI_ORK_RUN_DIR pointed at this run: trace_write would then
                # stamp the live .last-llm-lane onto every seeded row, erasing
                # the laneA/laneB slices this file is about.
                "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MO_NODE_ID", "MO_TARGET_CWD"):
        monkeypatch.delenv(var, raising=False)
    return db


def _seed(db, lane, rv, region, n):
    for _ in range(n):
        tid = trace_store.trace_write({
            "task_class": "code-fix",
            "status": "success",
            "agent_version_id": lane,
            "objective_domain": "code-delivery",
            "verifier_output": {"node_type": "implementer"},
            "reward_value": rv,
            "reward_anchor": 0.5,
            "reward_direction": "higher_is_better",
        }, db=db)
        if region is not None:
            con = sqlite3.connect(db)
            con.execute("UPDATE execution_traces SET code_region=? WHERE trace_id=?",
                        (region, tid))
            con.commit()
            con.close()


def _one_region(db):
    _seed(db, "laneB", 1.0, "mini_ork", 3)
    _seed(db, "laneA", 0.0, "mini_ork", 3)
    _seed(db, "laneA", 1.0, None, 6)
    _seed(db, "laneB", 0.0, None, 6)
    lane_router.recompute_advantages(db=db)


def _profile(tmp_path: Path, entries, name: str = "run") -> str:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_profile.json").write_text(
        json.dumps({"scope_allow": list(entries)}), encoding="utf-8")
    return str(d)


@pytest.fixture(autouse=True)
def _no_ambient_route_region(monkeypatch):
    monkeypatch.delenv("MO_ROUTE_REGION", raising=False)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("mini_ork/cli/execute.py", "mini_ork"),
        ("./docs/a.md", "docs"),
        ("README.md", "(root)"),
        ("", ""),
        ("../x/y.py", ""),
        ("/abs/p.py", ""),
        ("recipes/*/workflow.yaml", "recipes"),
        ("a\\b\\c.py", "a"),
    ],
)
def test_region_of_path(path, expected):
    assert routing.region_of_path(path) == expected


def test_scope_region_mini_profile(tmp_path):
    assert routing.scope_region(_profile(tmp_path, MINI)) == "mini_ork"


def test_scope_region_tie_goes_to_first(tmp_path):
    d = _profile(tmp_path, ["`docs/a.md`", "`tests/b.py`"], name="tie")
    assert routing.scope_region(d) == "docs"


def test_scope_region_empty_scope(tmp_path):
    assert routing.scope_region(_profile(tmp_path, [], name="empty")) == ""


def test_scope_region_missing_dir(tmp_path):
    assert routing.scope_region(str(tmp_path / "no-such-run")) == ""


def test_scope_region_none():
    assert routing.scope_region(None) == ""
    assert routing.scope_region("") == ""


def test_route_segment_implementer_mini(tmp_path):
    d = _profile(tmp_path, MINI)
    assert routing.route_segment("implementer", d) == "mini_ork"


def test_route_segment_non_implementer_is_default(tmp_path):
    d = _profile(tmp_path, MINI)
    assert routing.route_segment("reviewer", d) == "default"


def test_route_segment_implementer_without_run_dir():
    assert routing.route_segment("implementer", None) == "default"


def test_route_segment_empty_scope_dir(tmp_path):
    d = _profile(tmp_path, [], name="empty")
    assert routing.route_segment("implementer", d) == "default"


def test_route_segment_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ROUTE_REGION", "0")
    d = _profile(tmp_path, MINI)
    assert routing.route_segment("implementer", d) == "default"


def test_region_slice_decides_laneb(env_db, tmp_path):
    _one_region(env_db)
    d = _profile(tmp_path, MINI)
    assert routing.learning_governed_lane(
        "implementer", "implementer", task_class="code-fix", run_dir=d) == "laneB"


def test_without_run_dir_domain_lane_wins(env_db):
    _one_region(env_db)
    assert routing.learning_governed_lane(
        "implementer", "implementer", task_class="code-fix") == "laneA"


def test_route_region_disabled_restores_domain_lane(env_db, tmp_path, monkeypatch):
    _one_region(env_db)
    monkeypatch.setenv("MO_ROUTE_REGION", "0")
    d = _profile(tmp_path, MINI)
    assert routing.learning_governed_lane(
        "implementer", "implementer", task_class="code-fix", run_dir=d) == "laneA"


def test_unknown_region_falls_through_to_domain(env_db, tmp_path):
    _one_region(env_db)
    d = _profile(tmp_path, ["`tests/a.py`"], name="tests-scope")
    assert routing.learning_governed_lane(
        "implementer", "implementer", task_class="code-fix", run_dir=d) == "laneA"


def test_policy_route_lane_threads_run_dir_learned(env_db, tmp_path):
    _one_region(env_db)
    d = _profile(tmp_path, MINI)
    lane = routing.policy_route_lane(
        "implementer", "implementer", task_class="code-fix", run_dir=d)
    assert lane == "laneB"
    assert routing.last_route_provenance()["route_source"] == "learned"


def test_policy_route_lane_pinned_unchanged(env_db, tmp_path):
    _one_region(env_db)
    d = _profile(tmp_path, MINI)
    lane = routing.policy_route_lane(
        "implementer", "codex_lens", task_class="code-fix", run_dir=d)
    assert lane == "codex_lens"
    assert routing.last_route_provenance()["route_source"] == "pinned"


def test_routing_context_run_dir_defaults_none():
    assert routing.RoutingContext("implementer", "implementer").run_dir is None


def test_dispatch_node_passes_its_run_dir(tmp_path, monkeypatch):
    captured: list[str] = []

    def capture(ctx):
        captured.append(ctx.run_dir)
        return ctx.current_lane

    root = tmp_path / "root"
    root.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    monkeypatch.setattr(execute, "_capture_pre_impl_baseline", lambda *a, **k: None)
    monkeypatch.setattr(execute, "_intervention_decision",
                        lambda *a, **k: ("proceed", {}))
    monkeypatch.setitem(execute_handlers.NODE_HANDLER_REGISTRY,
                        "rsi1probe", lambda ctx: (0, "done"))
    for var in ("MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_HOME",
                "MO_TARGET_CWD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(routing.POLICY_REGISTRY, "rsi-t1-capture", capture)
    monkeypatch.setenv("MO_ROUTING_POLICY", "rsi-t1-capture")

    with run_context_scope({}):
        execute_handlers.dispatch_node(
            ["rsi1probe-n1", "rsi1probe", "probe", "", "", "", "", ""],
            root=str(root), run_dir=str(run_dir), plan_path="",
            task_class="framework-edit", db=str(tmp_path / "missing.db"),
            run_id="run-rsi1", dispatch_fn=lambda *a: (0, "ok"))

    assert captured == [str(run_dir)]
