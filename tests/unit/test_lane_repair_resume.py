"""`mini-ork recover --lane <alias>=<lane>` + `board retry --lane` + ACP
`/recover --lane/--force` (kickoff lane-repair-resume).

Each test stands up an isolated tmp home so the ambient ``MINI_ORK_*`` family
cannot leak into the planner. The executor is monkeypatched via
``rp.cli_main(..., execute_fn=...)`` so no real dispatch ever runs; board/ACP
spawns are intercepted via their module-level ``_spawn`` seams.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner as rp  # noqa: E402
from mini_ork.cli import board_cmd  # noqa: E402
from mini_ork.acp import commands as cmds  # noqa: E402
from test_recover_lease_wiring import SCHEMA_SQL  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Clear the ambient MINI_ORK_* family so the planner cannot leak into a
    parent run dir (mirrors ``test_recover_verify.py:51-66``)."""
    for key in (
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_RECIPE",
        "MINI_ORK_WORKFLOW",
        "MINI_ORK_TASK_CLASS",
        "MINI_ORK_RUN_ID",
        "MINI_ORK_LEASE_TOKEN",
        "MINI_ORK_RECOVERY_REQUEST",
        "MINI_ORK_AGENTS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))


def _write_workflow(path: Path, *, nodes: list[dict], edges: list[dict]) -> None:
    import yaml

    body = {
        "version": "0.1.0",
        "task_class": "framework_edit",
        "nodes": nodes,
        "edges": edges,
    }
    path.write_text(yaml.safe_dump(body, sort_keys=False))


def _setup_db(tmp_path: Path) -> tuple[str, str]:
    """Fresh sqlite state.db (full schema) + a run dir, no seeded checkpoints —
    so the closure is every node and the dispatch path is reached."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "framework-edit",
                    "roots": {"exec_cwd": None, "target": None}})
    )
    return str(db), str(run_dir)


_NODES = [
    {"name": "planner", "type": "planner", "model_lane": "decomposer"},
    {"name": "lens", "type": "researcher", "model_lane": "codex_lens"},
    {"name": "verifier", "type": "verifier", "model_lane": "verifier"},
]
_EDGES = [
    {"from": "planner", "to": "lens", "edge_type": "depends_on"},
    {"from": "lens", "to": "verifier", "edge_type": "verifies"},
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. ``mini-ork recover --lane <alias>=<lane>``
# ─────────────────────────────────────────────────────────────────────────────


def test_recover_lane_writes_overlay_merges_env_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-lane-1"
    # Pre-existing overlay naming two aliases; only codex_lens is overridden.
    overlay = tmp_path / "agents.overlay.yaml"
    overlay.write_text(
        "lanes:\n  codex_lens: opus\n  minimax_lens: glm\n", encoding="utf-8"
    )
    monkeypatch.setenv("MINI_ORK_AGENTS", str(overlay))

    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    calls: list = []
    captured: dict[str, str | None] = {}

    def _exec(a):
        calls.append(list(a))
        captured["MINI_ORK_AGENTS"] = os.environ.get("MINI_ORK_AGENTS")
        return 0

    rc = rp.cli_main(
        [run_id, "--lane", "codex_lens=deepseek", "--workflow", str(wf), "--db", db],
        execute_fn=_exec,
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    assert len(calls) == 1, calls

    import yaml

    written = yaml.safe_load((Path(run_dir) / "config" / "agents.recover.yaml").read_text())
    assert written["lanes"]["codex_lens"] == "deepseek"
    assert written["lanes"]["minimax_lens"] == "glm"  # other alias kept

    # The execute call saw MINI_ORK_AGENTS pointed at the recover overlay.
    assert captured["MINI_ORK_AGENTS"] == str(Path(run_dir) / "config" / "agents.recover.yaml")

    log = (Path(run_dir) / "recover-lanes.log").read_text(encoding="utf-8")
    assert "codex_lens: opus -> deepseek" in log


def test_recover_lane_base_follows_agents_local_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """``$MINI_ORK_AGENTS`` unset but ``<home>/config/agents.local.yaml`` present:
    the recover overlay must be based on that LOCAL file (the execute merges it
    via ``agents_config.personal_path``). Reading only the env var dropped the
    local aliases and silently rerouted every node to the team template (BLOCKER)."""
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    cfg = tmp_path / "config"  # MINI_ORK_HOME == tmp_path (isolate_env)
    cfg.mkdir()
    (cfg / "agents.local.yaml").write_text(
        "lanes:\n  decomposer: glm\n  minimax_lens: glm\n", encoding="utf-8"
    )

    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)
    rc = rp.cli_main(
        ["run-lane-local", "--lane", "codex_lens=deepseek",
         "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: 0,
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)

    import yaml

    written = yaml.safe_load(
        (Path(run_dir) / "config" / "agents.recover.yaml").read_text()
    )
    assert written["lanes"]["codex_lens"] == "deepseek"   # the pin wins
    assert written["lanes"]["decomposer"] == "glm"        # local alias kept
    assert written["lanes"]["minimax_lens"] == "glm"


def test_recover_lane_second_call_keeps_prior_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A second ``recover --lane`` from a fresh shell (no env override carried
    over) must not drop the first call's pin — the prior ``agents.recover.yaml``
    is folded back in (reviewer MINOR)."""
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    rc1 = rp.cli_main(
        ["run-lane-twice", "--lane", "codex_lens=deepseek",
         "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: 0,
    )
    assert rc1 == 0, capsys.readouterr()

    # Fresh shell: the first call's ``MINI_ORK_AGENTS`` override does not exist.
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    rc2 = rp.cli_main(
        ["run-lane-twice", "--lane", "decomposer=deepseek",
         "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: 0,
    )
    out_err = capsys.readouterr()
    assert rc2 == 0, (rc2, out_err)

    import yaml

    written = yaml.safe_load(
        (Path(run_dir) / "config" / "agents.recover.yaml").read_text()
    )
    assert written["lanes"]["codex_lens"] == "deepseek"   # first pin survives
    assert written["lanes"]["decomposer"] == "deepseek"   # second pin lands


def test_recover_lane_unknown_alias_exits_2_with_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)
    rc = rp.main(["run-x", "--lane", "nope=deepseek", "--workflow", str(wf), "--db", db])
    out_err = capsys.readouterr()
    assert rc == 2, (rc, out_err)
    assert "unknown alias" in out_err.err
    assert "codex_lens" in out_err.err  # valid aliases listed


def test_recover_lane_unknown_lane_exits_2_with_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)
    rc = rp.main(["run-x", "--lane", "codex_lens=not-a-lane", "--workflow", str(wf), "--db", db])
    out_err = capsys.readouterr()
    assert rc == 2, (rc, out_err)
    assert "unknown lane" in out_err.err
    assert "deepseek" in out_err.err  # valid lanes listed


def test_recover_failing_execute_calls_retry_notify_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-notify-1"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    calls: list = []
    from mini_ork.recovery import retry_notify

    monkeypatch.setattr(retry_notify, "notify",
                        lambda home, rid: calls.append((str(home), rid)) or None)

    rc = rp.cli_main(
        [run_id, "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: 1,
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert len(calls) == 1, calls
    assert calls[0][1] == run_id


def test_recover_lane_status_shows_override_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)
    rc = rp.main(["run-x", "--status", "--lane", "codex_lens=deepseek",
                  "--workflow", str(wf), "--db", db])
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    assert "codex_lens:" in out_err.out
    assert "-> deepseek" in out_err.out
    # ``--status`` is read-only: no overlay / log written.
    assert not (Path(run_dir) / "config" / "agents.recover.yaml").exists()
    assert not (Path(run_dir) / "recover-lanes.log").exists()


def test_recover_lane_hint_with_lane_dispatches_without_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A ``kind='lane'`` needs_change is the one change that ``--lane``
    satisfies: the hint's own command (``recover <run> --lane …``) must not be
    refused by the gate — ``--lane`` IS the change, no ``--ack-change`` needed."""
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-lane-hint"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    hint = {
        "retryable": True,
        "strategy": "resume",
        "needs_change": {
            "kind": "lane",
            "alias": "codex_lens",
            "lane": "deepseek",
            "summary": "lane unavailable (429)",
            "detail": "codex_lens lane is dead; reroute it.",
        },
        "command": f"mini-ork recover {run_id} --lane codex_lens=deepseek",
    }
    monkeypatch.setattr(rp, "_load_retry_hint", lambda *a, **kw: hint)

    calls: list = []

    def _exec(a):
        calls.append(list(a))
        return 0

    rc = rp.cli_main(
        [run_id, "--lane", "codex_lens=deepseek", "--workflow", str(wf), "--db", db],
        execute_fn=_exec,
    )
    out_err = capsys.readouterr()
    assert rc == 0, (rc, out_err)
    assert len(calls) == 1, calls

    import yaml

    written = yaml.safe_load(
        (Path(run_dir) / "config" / "agents.recover.yaml").read_text()
    )
    assert written["lanes"]["codex_lens"] == "deepseek"


def test_recover_lane_hint_without_lane_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A lane hint with no ``--lane`` still refuses: the flag is the change,
    so its absence means the change has not been made."""
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-lane-hint-nopin"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    hint = {
        "retryable": True,
        "needs_change": {
            "kind": "lane",
            "alias": "codex_lens",
            "summary": "lane unavailable (429)",
        },
        "command": f"mini-ork recover {run_id} --lane codex_lens=deepseek",
    }
    monkeypatch.setattr(rp, "_load_retry_hint", lambda *a, **kw: hint)

    calls: list = []
    rc = rp.cli_main(
        [run_id, "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: calls.append(list(a)) or 0,
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert calls == [], calls
    assert "Needs a change before retrying" in out_err.err


def test_recover_lane_hint_alias_mismatch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The hint names the alias it suggests rerouting; a ``--lane`` pin for a
    *different* alias does not count as that change."""
    db, run_dir = _setup_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    run_id = "run-lane-hint-mismatch"
    wf = tmp_path / "wf.yaml"
    _write_workflow(wf, nodes=_NODES, edges=_EDGES)

    hint = {
        "retryable": True,
        "needs_change": {
            "kind": "lane",
            "alias": "verifier",
            "summary": "lane unavailable (429)",
        },
        "command": f"mini-ork recover {run_id} --lane verifier=deepseek",
    }
    monkeypatch.setattr(rp, "_load_retry_hint", lambda *a, **kw: hint)

    calls: list = []
    rc = rp.cli_main(
        [run_id, "--lane", "codex_lens=deepseek", "--workflow", str(wf), "--db", db],
        execute_fn=lambda a: calls.append(list(a)) or 0,
    )
    out_err = capsys.readouterr()
    assert rc == 1, (rc, out_err)
    assert calls == [], calls
    assert "Needs a change before retrying" in out_err.err


# ─────────────────────────────────────────────────────────────────────────────
# 2. ``board retry --lane <alias>=<lane>``
# ─────────────────────────────────────────────────────────────────────────────


def _patch_board(monkeypatch: pytest.MonkeyPatch, hint: dict) -> dict:
    captured: dict[str, object] = {}

    class _Fake:
        pid = 4242

    def _spawn(argv, *, cwd, env, stdout_path):
        captured["argv"] = argv
        return _Fake()

    monkeypatch.setattr(board_cmd, "_retry_spawn", _spawn)
    monkeypatch.setattr("mini_ork.recovery.retry_hint.load_or_compute",
                        lambda home, run_id, write=True: hint)
    return captured


def test_board_retry_lane_spawns_with_lane_no_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hint = {"retryable": True, "needs_change": None,
            "command": "mini-ork recover run-1"}
    captured = _patch_board(monkeypatch, hint)
    payload = board_cmd._act_retry(Path(tmp_path), "run-1", lanes=["codex_lens=opus"])
    assert payload["ok"] is True
    argv = captured["argv"]
    assert argv[2:4] == ["recover", "run-1"]
    assert "--lane" in argv
    assert "codex_lens=opus" in argv
    assert "--ack-change" not in argv
    assert "--force" not in argv


def test_board_retry_lane_hint_no_flag_spawns_hint_command_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hint = {"retryable": True,
            "needs_change": {"kind": "lane", "summary": "lane unavailable"},
            "command": "mini-ork recover run-1 --lane codex_lens=deepseek"}
    captured = _patch_board(monkeypatch, hint)
    payload = board_cmd._act_retry(Path(tmp_path), "run-1")
    assert payload["ok"] is True
    argv = captured["argv"]
    assert argv.count("--lane") == 1
    assert "codex_lens=deepseek" in argv


def test_board_retry_lane_replaces_same_alias_hint_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hint = {"retryable": True,
            "needs_change": {"kind": "lane", "summary": "lane unavailable"},
            "command": "mini-ork recover run-1 --lane codex_lens=deepseek"}
    captured = _patch_board(monkeypatch, hint)
    payload = board_cmd._act_retry(Path(tmp_path), "run-1", lanes=["codex_lens=opus"])
    assert payload["ok"] is True
    argv = captured["argv"]
    assert argv.count("--lane") == 1
    assert "codex_lens=opus" in argv
    assert "codex_lens=deepseek" not in argv


def test_board_retry_lane_refused_on_resume_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The resume-cost hint runs ``mini-ork resume``, which ignores ``--lane``:
    a lane switch there is refused (never silently dropped) and nothing spawns
    (reviewer MINOR)."""
    hint = {"retryable": True, "strategy": "resume-cost", "needs_change": None,
            "command": "mini-ork resume run-1"}
    captured = _patch_board(monkeypatch, hint)
    payload = board_cmd._act_retry(Path(tmp_path), "run-1", lanes=["codex_lens=opus"])
    assert payload["ok"] is False, payload
    assert "recover" in payload["error"]
    assert captured == {}, captured  # never spawned


def test_board_retry_parser_accepts_repeatable_lane() -> None:
    args = board_cmd.build_parser().parse_args(
        ["retry", "run-1", "--lane", "a=1", "--lane", "b=2"]
    )
    assert args.lane == ["a=1", "b=2"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. ACP ``/recover --lane <alias>=<lane> --force``
# ─────────────────────────────────────────────────────────────────────────────


def test_acp_recover_lane_force_builds_argv_and_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from mini_ork.acp.agent import MiniOrkAcpAgent

    home = tmp_path / ".mini-ork"
    home.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    agent = MiniOrkAcpAgent(home=str(home))

    captured: dict[str, object] = {}

    class FakePopen:
        pid = 7

        def __init__(self, *args, **kwargs):
            captured["args"] = args

    monkeypatch.setattr(cmds, "_spawn", lambda *a, **kw: FakePopen(*a, **kw))
    out = asyncio.run(cmds.handle_recover(
        agent, "run-lane-1", "--lane codex_lens=deepseek --force"
    ))
    argv = captured["args"][0]
    assert "recover" in argv
    assert "run-lane-1" in argv
    assert "--lane" in argv
    assert "codex_lens=deepseek" in argv
    assert "--force" in argv
    assert "Resuming" in out
    assert "codex_lens → deepseek" in out


def test_acp_recover_malformed_lane_usage_error_no_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``/recover --lane codex_lens deepseek`` (space, no ``=``) must reply with
    a usage error and NOT spawn — previously the token was dropped before argv
    was built, so recover silently ran the dead lane (reviewer MINOR)."""
    from mini_ork.acp.agent import MiniOrkAcpAgent

    home = tmp_path / ".mini-ork"
    home.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    agent = MiniOrkAcpAgent(home=str(home))

    spawned: list = []
    monkeypatch.setattr(cmds, "_spawn", lambda *a, **kw: spawned.append(a) or None)

    out = asyncio.run(cmds.handle_recover(
        agent, "run-lane-1", "--lane codex_lens deepseek"
    ))
    assert spawned == [], spawned
    assert "not spawning" in out
    assert "=" in out  # the usage error names the required <alias>=<lane> shape
