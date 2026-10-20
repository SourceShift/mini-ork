"""RB-03: a recover's execute must not inherit ``MINI_ORK_PLAN_PATH``.

``mini-ork run`` exports ``MINI_ORK_PLAN_PATH`` for its own execute. A recover
spawned by auto-repair inherits the whole environment (``_spawn_recover`` builds
``dict(os.environ)``), and ``recovery.planner.cli_main`` also hands the recover
plan to execute positionally. Since ``execute._parse_execute_argv`` seeds
``plan_path`` from the env, the inherited value turns the positional into an
"Unexpected argument" and the child exits rc=2 before dispatching a node (RB-03).

The fix is two-sided: ``cli_main`` pops the key in its existing
``MINI_ORK_RECOVERY_*`` pop loop, and the auto-repair child env drops it while
leaving every other inherited key intact.

Hermetic: tmp dirs, stubbed ``planner.main`` / ``execute_fn`` / ``_spawn``, no
live DB, no network. The process env is snapshotted and scrubbed of every
``MINI_ORK_*`` / ``MO_*`` key so the run's own identity cannot steer the code
under test (RB-04).
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.cli.execute import _parse_execute_argv
from mini_ork.recovery import auto_repair, planner

ABSENT = "<absent>"
_RECOVERY_KEYS = (
    "MINI_ORK_RECOVERY_RUN_ID",
    "MINI_ORK_RECOVERY_SKU",
    "MINI_ORK_RECOVERY_CLOSURE",
    "MINI_ORK_RECOVERY_STRATEGY",
    "MINI_ORK_RECOVERY_FROM",
)


@pytest.fixture
def isolated_env():
    """Scrub inherited run identity for the test, restore it afterwards."""
    saved = dict(os.environ)
    for key in [k for k in os.environ if k.startswith(("MINI_ORK_", "MO_"))]:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _make_run_dir(tmp_path: Path) -> Path:
    """A tmp run dir holding the ``plan.json`` the recover dispatches."""
    run_dir = tmp_path / "runs" / "spec-run"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text('{"nodes": []}\n', encoding="utf-8")
    return run_dir


def _stub_main(monkeypatch, run_dir: Path) -> dict:
    """Fill the handoff like the real ``planner.main`` dispatch path does, and
    record the env it observed — i.e. after ``cli_main``'s pop loop."""
    at_entry: dict = {}

    def fake_main(argv, *, handoff=None):
        at_entry["plan_path"] = os.environ.get("MINI_ORK_PLAN_PATH", ABSENT)
        at_entry["recovery"] = {k: os.environ.get(k, ABSENT) for k in _RECOVERY_KEYS}
        if handoff is not None:
            handoff.update({
                "run_id": "spec-run",
                "run_dir": str(run_dir),
                "workflow": "workflow.yaml",
                "db_path": str(run_dir.parent / "state.db"),
                "recipe": "framework-edit",
                "request_id": "",
                "lease_token": "",
            })
        return 0

    monkeypatch.setattr(planner, "main", fake_main)
    return at_entry


def _execute_stub() -> tuple[dict, object]:
    """Capture the argv, the plan-path env at call time, and execute's parse
    verdict on that argv in exactly that environment."""
    seen: dict = {}

    def execute_stub(argv):
        seen["argv"] = list(argv)
        seen["plan_path_at_call"] = os.environ.get("MINI_ORK_PLAN_PATH", ABSENT)
        seen["recovery_at_call"] = {k: os.environ.get(k, ABSENT)
                                    for k in _RECOVERY_KEYS}
        seen["parse"] = _parse_execute_argv(list(argv))
        return 0

    return seen, execute_stub


def test_cli_main_drops_plan_path_and_passes_positional_plan(
        isolated_env, tmp_path, monkeypatch):
    """The positional plan is the only authority: env plan is gone at dispatch
    and the stub receives exactly ``[<plan.json>, "--recovery"]``."""
    run_dir = _make_run_dir(tmp_path)
    _stub_main(monkeypatch, run_dir)
    seen, execute_stub = _execute_stub()
    os.environ["MINI_ORK_PLAN_PATH"] = str(tmp_path / "parent-plan.json")

    rc = planner.cli_main(["spec-run"], execute_fn=execute_stub)

    assert rc == 0
    assert seen.get("argv") == [str(run_dir / "plan.json"), "--recovery"]
    assert seen.get("plan_path_at_call") == ABSENT


def test_execute_argv_parses_rc0_without_inherited_plan(
        isolated_env, tmp_path, monkeypatch):
    """The repro in RB-03: with the env plan gone, the positional parses."""
    run_dir = _make_run_dir(tmp_path)
    _stub_main(monkeypatch, run_dir)
    seen, execute_stub = _execute_stub()
    os.environ["MINI_ORK_PLAN_PATH"] = str(tmp_path / "parent-plan.json")

    planner.cli_main(["spec-run"], execute_fn=execute_stub)

    args, rc = seen["parse"]
    assert args is not None
    assert rc == 0


def test_cli_main_pops_plan_path_with_the_recovery_keys(
        isolated_env, tmp_path, monkeypatch):
    """The pop happens in the existing loop, i.e. before control reaches
    ``main`` — not somewhere on the dispatch path afterwards."""
    run_dir = _make_run_dir(tmp_path)
    at_entry = _stub_main(monkeypatch, run_dir)
    _seen, execute_stub = _execute_stub()
    os.environ["MINI_ORK_PLAN_PATH"] = str(tmp_path / "parent-plan.json")

    planner.cli_main(["spec-run"], execute_fn=execute_stub)

    assert at_entry["plan_path"] == ABSENT


def test_cli_main_still_pops_recovery_env_keys(
        isolated_env, tmp_path, monkeypatch):
    """The pre-existing ``MINI_ORK_RECOVERY_*`` pop is untouched."""
    run_dir = _make_run_dir(tmp_path)
    at_entry = _stub_main(monkeypatch, run_dir)
    seen, execute_stub = _execute_stub()
    os.environ["MINI_ORK_PLAN_PATH"] = str(tmp_path / "parent-plan.json")
    for key in _RECOVERY_KEYS:
        os.environ[key] = "stale-" + key

    planner.cli_main(["spec-run"], execute_fn=execute_stub)

    assert set(at_entry["recovery"].values()) == {ABSENT}
    assert set(seen["recovery_at_call"].values()) == {ABSENT}


def test_parser_positional_plan_beats_inherited_env_plan(
        isolated_env, tmp_path):
    """Defence in depth with the recover env pop (RB-03): even when a stale
    ``MINI_ORK_PLAN_PATH`` is inherited, an explicit positional plan wins
    instead of being rejected as an "Unexpected argument" (RB-03 robustness
    half, delivered with RB-43)."""
    stale = str(tmp_path / "x" / "stale-plan.json")
    explicit = str(tmp_path / "y" / "plan.json")
    os.environ["MINI_ORK_PLAN_PATH"] = stale

    args, rc = _parse_execute_argv([explicit, "--recovery"])

    assert args is not None
    assert rc == 0
    assert args.plan_path == explicit


def test_spawn_recover_env_drops_only_plan_path(
        isolated_env, tmp_path, monkeypatch):
    """The spawned child env never carries the parent's plan path, and every
    other inherited variable survives (exactly one key is dropped)."""
    home = tmp_path / "home"
    home.mkdir()
    run_dir = tmp_path / "runs" / "spec-run"
    run_dir.mkdir(parents=True)
    os.environ["MINI_ORK_PLAN_PATH"] = str(tmp_path / "parent-plan.json")
    os.environ["SPEC_INHERITED_MARKER"] = "keep-me"
    os.environ["MINI_ORK_SPEC_INHERITED_MARKER"] = "keep-me-too"
    captured: dict = {}

    def fake_spawn(argv, *, cwd, env, stdout_path):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        return SimpleNamespace(pid=424242)

    monkeypatch.setattr(auto_repair, "_spawn", fake_spawn)

    result = auto_repair._spawn_recover(
        home, "spec-run", run_dir,
        {"from_node": "node-1", "lanes": {}, "ack_change": False}, 1)

    assert result.get("spawned") is True
    assert captured, "the stubbed spawn was never called"
    assert "MINI_ORK_PLAN_PATH" not in captured["env"]
    assert captured["env"]["SPEC_INHERITED_MARKER"] == "keep-me"
    assert captured["env"]["MINI_ORK_SPEC_INHERITED_MARKER"] == "keep-me-too"
    assert captured["env"]["PATH"]
