"""``mini-ork board`` — the JSON the IDE panels read."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, run_id: str, status: str) -> None:
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.5, now, now, "code_fix", "", "latest"))
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
        "confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
        (f"gr-{run_id}", "verifier.code_fix", "checks unclear", "record the checks", "{}", 0.4, now, "code_fix"))
    con.commit()
    con.close()


def test_board_has_every_section(home: Path) -> None:
    _seed_run(home, "run-1791000000-aaaaaa", "published")
    b = board_cmd.board(home)
    assert b["version"] == 1 and b["project"] == "proj"
    assert [r["id"] for r in b["runs"]] == ["run-1791000000-aaaaaa"]
    assert b["runs"][0]["state"] == "done"
    assert b["learnings"][0]["kind"] == "gradient"
    assert b["learnings"][0]["suggestion"] == "record the checks"
    for key in ("automations", "recipes", "workspaces"):
        assert isinstance(b[key], list)
    assert b["errors"] == {}
    json.dumps(b, default=str)  # serialisable


def test_a_broken_section_does_not_blank_the_board(home: Path, monkeypatch) -> None:
    _seed_run(home, "run-1791000000-bbbbbb", "failed")

    def boom(_home):
        raise RuntimeError("db locked")

    monkeypatch.setattr(board_cmd, "_learnings", boom)
    b = board_cmd.board(home)
    assert b["learnings"] == [] and "db locked" in b["errors"]["learnings"]
    assert len(b["runs"]) == 1


def test_cli_usage_and_actions(home: Path, capsys) -> None:
    assert board_cmd.main(["merge", "--home", str(home)], "") == 2
    assert board_cmd.main(["merge", "run-x", "--home", str(home)], "") == 1
    assert "no open workspace" in json.loads(capsys.readouterr().out)["error"]
    assert board_cmd.main(["--home", str(home), "--json"], "") == 0
    assert "runs" in json.loads(capsys.readouterr().out)


def test_kill_unknown_run_reports_ok_false(home: Path, capsys) -> None:
    assert board_cmd.main(["kill", "run-nope", "--home", str(home)], "") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"] == "task_run not found"


def test_resume_unknown_run_reports_ok_false(home: Path, capsys) -> None:
    assert board_cmd.main(["resume", "run-nope", "--home", str(home)], "") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"] == "run_dir not found"


def test_gate_usage_error_without_an_id(home: Path) -> None:
    assert board_cmd.main(["gate", "approve", "--home", str(home)], "") == 2


def test_gate_approve_then_double_approve(home: Path, capsys) -> None:
    con = sqlite3.connect(home / "state.db")
    cur = con.execute("INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json, status, enqueued_at) "
                      "VALUES (?,?,?,?,?,?)",
                      ("human_sign_off", "release", "staging", "{}", "pending", int(time.time())))
    con.commit()
    con.close()
    inbox_id = cur.lastrowid

    assert board_cmd.main(["gate", "approve", str(inbox_id), "--home", str(home)], "") == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True and first["status"] == "approved"
    # second call: row is no longer pending → ok: false, exit 1
    assert board_cmd.main(["gate", "approve", str(inbox_id), "--home", str(home)], "") == 1
    second = json.loads(capsys.readouterr().out)
    assert second == {"ok": False, "error": "not pending"}


def test_gate_reject_records_review_note(home: Path, capsys) -> None:
    con = sqlite3.connect(home / "state.db")
    cur = con.execute("INSERT INTO mo_inbox_gates (gate_id, feature, phase, context_json, status, enqueued_at) "
                      "VALUES (?,?,?,?,?,?)",
                      ("human_sign_off", "release", "staging", "{}", "pending", int(time.time())))
    con.commit()
    inbox_id = cur.lastrowid
    con.close()

    assert board_cmd.main(["gate", "reject", str(inbox_id), "--note", "no good",
                           "--home", str(home)], "") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["status"] == "rejected"

    con = sqlite3.connect(home / "state.db")
    row = con.execute("SELECT status, review_note FROM mo_inbox_gates WHERE inbox_id = ?",
                      (inbox_id,)).fetchone()
    con.close()
    assert row[0] == "rejected" and row[1] == "no good"


def test_gate_missing_state_db_does_not_bootstrap_one(home: Path, capsys) -> None:
    fresh = home.parent / "no-state"
    fresh.mkdir()
    assert board_cmd.main(["gate", "approve", "1", "--home", str(fresh)], "") == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "no state.db"}
    assert not (fresh / "state.db").exists()


def test_card_files_map_onto_the_project_when_the_worktree_is_gone(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "guide.md").write_text("x\n")
    gone = str(tmp_path / "worktrees" / "proj" / "task-abc123" / "docs" / "guide.md")
    fields = board_cmd._card_fields(
        {"files": [{"path": gone, "added": 3, "removed": 1},
                   {"path": str(tmp_path / "nowhere" / "x.py"), "added": 1, "removed": 0}],
         "steps": [{"node_id": "editor", "node_type": "implementer", "lane": "worker",
                    "duration": 44, "state": "done"}],
         "cost_by_stage": {"worker": 0.37, "learning": 0.0}, "cost_total": 0.37,
         "verdict": {"verdict": "pass"}},
        project)
    assert fields["files"][0] == {"path": "docs/guide.md", "abs": str(project / "docs" / "guide.md"),
                                  "added": 3, "removed": 1}
    assert fields["files"][1]["abs"] is None
    assert fields["steps"] == [{"name": "editor", "type": "implementer", "lane": "worker",
                                "seconds": 44, "state": "done"}]
    assert fields["cost_by_stage"] == {"worker": 0.37}
    assert fields["verdict"] == "pass"


def test_artifacts_are_grouped_for_the_run_tab(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-x"
    (run_dir / "evidence").mkdir(parents=True)
    for rel in ("kickoff.md", "plan.json", "verdict.json", "framework-edit.diff",
                "agent-editor.live.jsonl", "execute.log", "evidence/test-1.log", "notes.txt"):
        (run_dir / rel).write_text("x")
    groups = [(a["group"], a["path"]) for a in board_cmd._artifacts(run_dir)]
    assert groups == [
        ("Kickoff & plan", "kickoff.md"), ("Kickoff & plan", "plan.json"),
        ("Results", "verdict.json"), ("Diffs", "framework-edit.diff"),
        ("Agent output", "agent-editor.live.jsonl"), ("Logs", "execute.log"),
        ("Evidence", "evidence/test-1.log"), ("Other", "notes.txt"),
    ]
    assert board_cmd._artifacts(tmp_path / "missing") == []


# ── kickoff ide-board-perf-r2 — Part B tests ──────────────────────────────


def test_shell_payload_skips_full_board_sections(home: Path, monkeypatch) -> None:
    """``_board_shell_payload`` builds only ``_SHELL_KEYS`` and never touches
    ``_recipes``/``_learnings``/``_automations``/``_scheduler``/``_workspaces``.

    A raise on those helpers would otherwise abort the shell poll, so they
    must not even be called. The shape contract is exact equality, not subset.
    """
    _seed_run(home, "run-1791000000-aaaaaa", "published")

    def _boom(*_args, **_kwargs):
        raise AssertionError("shell must not reach full-board sections")

    monkeypatch.setattr(board_cmd, "_recipes", _boom)
    monkeypatch.setattr(board_cmd, "_learnings", _boom)
    monkeypatch.setattr(board_cmd, "_automations", _boom)
    monkeypatch.setattr(board_cmd, "_scheduler", _boom)
    monkeypatch.setattr(board_cmd, "_workspaces", _boom)

    payload = board_cmd._board_shell_payload(home)
    assert set(payload) == {
        "version", "project", "home", "generated_at", "header", "runs", "counts", "errors"
    }
    assert payload["version"] == 1
    assert payload["project"] == home.absolute().parent.name
    assert payload["home"] == str(home.absolute())
    assert isinstance(payload["generated_at"], int) and payload["generated_at"] > 0
    assert isinstance(payload["runs"], list)
    assert isinstance(payload["counts"], dict)
    assert isinstance(payload["errors"], dict)


def test_shell_payload_uses_build_parser_when_invoked_via_main(
    home: Path, monkeypatch, capsys,
) -> None:
    """``mini-ork board --json --shell`` runs ``_board_shell_payload``, not
    ``board()`` — verified end-to-end via ``main()`` and the real parser."""
    _seed_run(home, "run-1791000000-aaaaaa", "published")

    def _boom(*_args, **_kwargs):
        raise AssertionError("shell path must not reach full-board sections")

    monkeypatch.setattr(board_cmd, "_recipes", _boom)
    monkeypatch.setattr(board_cmd, "_learnings", _boom)

    rc = board_cmd.main(["--json", "--shell", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "version", "project", "home", "generated_at", "header", "runs", "counts", "errors"
    }


def test_worktree_count_uses_common_dir_when_git_worktree_list_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_worktree_count`` returns 1 + entries of ``<common>/worktrees/`` even
    when ``git worktree list`` would fail. Two linked worktree dirs on disk +
    the main checkout itself → 3."""
    from mini_ork.ide_pages import header

    project = tmp_path / "proj"
    project.mkdir()
    common = tmp_path / "common.git"
    (common / "worktrees" / "wt-a").mkdir(parents=True)
    (common / "worktrees" / "wt-b").mkdir(parents=True)

    def _fake_common_dir(_project):
        return common

    def _fake_git(_project, *args, **_kwargs):
        # header.py also calls ``git rev-parse --abbrev-ref HEAD`` as a
        # fallback for the branch line; we don't care here, but make sure it
        # returns empty so the test does not depend on a real git binary.
        return ""

    monkeypatch.setattr(header, "_git_common_dir", _fake_common_dir)
    monkeypatch.setattr(header, "_git", _fake_git)

    assert header._worktree_count(project) == 3


def test_worktree_count_resolves_relative_common_dir(tmp_path: Path, monkeypatch) -> None:
    """The fix-2 regression: ``git rev-parse --git-common-dir`` historically
    printed a relative ``.git`` for the main checkout, which made the absolute
    check drop the count to zero. After the patch, the helper returns the
    project root, and the count is ``1 + len(worktrees/)`` again."""
    from mini_ork.ide_pages import header

    project = tmp_path / "proj"
    project.mkdir()
    common_rel = Path(".git")  # the relative shape before --path-format=absolute
    real_common = tmp_path / ".git"
    (real_common / "worktrees" / "wt-1").mkdir(parents=True)

    def _fake_common_dir(_project):
        return real_common  # already resolved against project by the patched path

    monkeypatch.setattr(header, "_git_common_dir", _fake_common_dir)
    monkeypatch.setattr(header, "_git", lambda *_a, **_k: "")
    del common_rel  # silence linters; kept only to anchor the regression intent

    assert header._worktree_count(project) == 2  # main + wt-1


def test_runs_output_is_stable_against_frozen_list(home: Path) -> None:
    """``_runs`` output is identical before/after — pin the exact row shape.

    The run was seeded with no kickoff on disk; ``history._title_from_kickoff``
    therefore falls back to ``f"{recipe} run"``. Pin the shape so a future
    change to that fallback shows up as a single-line update here.
    """
    _seed_run(home, "run-1791000000-aaaaaa", "published")
    runs, counts = board_cmd._runs(home)
    assert counts == {"working": 0, "needs_you": 0, "done": 1, "failed": 0}
    assert runs == [
        {
            "id": "run-1791000000-aaaaaa",
            "title": "code-fix run",
            "recipe": "code-fix",
            "state": "done",
            "mark": "✓",
            "step": "",
            "started_at": runs[0]["started_at"],
            "ended_at": runs[0]["ended_at"],
            "cost_usd": 0.5,
            "added": 0,
            "removed": 0,
            "has_workspace": False,
        },
    ]


def test_import_acp_fleet_does_not_pull_in_agent_module():
    """``mini_ork.acp.fleet`` is import-lazy: pulling it in a fresh interpreter
    must NOT import ``mini_ork.acp.agent`` (which depends on the LLM client
    surface and would otherwise load on every board poll).
    """
    import subprocess
    import sys

    code = (
        "import mini_ork.acp.fleet as f\n"
        "import sys\n"
        "assert 'mini_ork.acp.agent' not in sys.modules, "
        f"{sorted(m for m in sys.modules if m.startswith('mini_ork.acp'))!r}\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr


def test_effective_lanes_honours_run_dir_agents_yaml(tmp_path: Path, monkeypatch) -> None:
    """``_effective_lanes`` honours ``$MINI_ORK_RUN_DIR/config/agents.yaml``
    before the home's policy — the run-snapshot wins. Inside pytest the env
    may carry a real ``MINI_ORK_RUN_DIR``; clear it for a deterministic read.
    """
    from mini_ork.dispatch import llm_dispatch

    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    run_dir = tmp_path / "run"
    (run_dir / "config").mkdir(parents=True)
    (run_dir / "config" / "agents.yaml").write_text(
        "lanes:\n  opus_lens: opus\n  glm_lens: glm\n"
    )

    # Home policy disagrees — must NOT win.
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n  opus_lens: glm\n"
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    lanes = llm_dispatch._effective_lanes(str(home), str(home))
    assert lanes is not None
    assert lanes["opus_lens"] == "opus"  # run-snapshot wins over home's glm
    assert lanes["glm_lens"] == "glm"


def test_diff_counts_cached_reads_list_shape(home: Path) -> None:
    """Regression guard for fix 3: the diff cache is a bare JSON list, and
    ``_diff_counts_cached`` must parse it (the old ``raw.get("diffs")`` shape
    always returned ``None`` and fell through to ``_diff_counts``).
    """
    from mini_ork.acp import fleet as acp_fleet

    run_id = "r-cache"
    _seed_run(home, run_id, "published")
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # ``CACHE_NAME = "acp-diffs.json"`` — the same name ``diffs._write_cache``
    # uses (``json.dump(diffs, handle)``).
    cache_path = run_dir / acp_fleet.CACHE_NAME
    cache_path.write_text(
        json.dumps([{"path": "f.py", "old_text": "a\n", "new_text": "a\nb\nc\n"}])
    )

    counts = acp_fleet._diff_counts_cached(run_dir)
    assert counts == (2, 0)


def test_events_by_run_filters_to_lifecycle_events(home: Path) -> None:
    """Fix 4: the batched lifecycle query must mirror the per-run read at
    ``fetch_node_lifecycle_events`` and drop ``run_events`` whose type is not
    in ``('node_start','node_end')``.
    """
    import sqlite3 as _sqlite3

    from mini_ork.acp import fleet as acp_fleet

    con = _sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        ("r-evt", "code-fix", "published", 0.0, now, now, "code_fix", "", "latest"),
    )
    # Lifecycle events: must survive the filter.
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("e1", "r-evt", "node_start", json.dumps({"node_id": "p"}), now - 5),
    )
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("e2", "r-evt", "node_end", json.dumps({"node_id": "p", "finish_reason": "done"}), now),
    )
    # Non-lifecycle noise: must NOT survive.
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("e3", "r-evt", "llm_call", json.dumps({"foo": 1}), now - 3),
    )
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("e4", "r-evt", "user_prompt", json.dumps({"foo": 2}), now - 2),
    )
    con.commit()
    con.close()

    events_by_run = acp_fleet._events_by_run(home, ["r-evt"])
    events = events_by_run["r-evt"]
    types = sorted(e["event_type"] for e in events)
    assert types == ["node_end", "node_start"]
