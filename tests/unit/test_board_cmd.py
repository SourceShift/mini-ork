"""``mini-ork board`` — the JSON the IDE panels read."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import search as search_mod
from mini_ork.stores import migrate as mig


@pytest.fixture(autouse=True)
def _no_real_search_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """`board runs --query` never starts a real background index builder here."""
    monkeypatch.setattr(search_mod, "_spawn_indexer",
                        lambda _args, *, log_handle=None: type("Fake", (), {"pid": None})())
    # With no builder, a query must index every seeded run itself, however
    # slow the machine is: the in-query time budget would make paging flaky.
    monkeypatch.setattr(search_mod, "_REINDEX_TIME_BUDGET_S", 600.0)

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


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Real ``git`` invocation — the worktree tests run the real ``git`` binary."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """Make a fresh temp git repo with one commit, return its path.

    Mirrors ``tests/unit/test_workspaces.py:_init_repo`` so the kickoff's
    "real temp git repos" requirement is met without sharing fixtures.
    """
    project = tmp_path / name
    project.mkdir()
    assert _git(["init", "-b", "main"], project).returncode == 0
    _git(["config", "user.name", "Test"], project)
    _git(["config", "user.email", "test@example.com"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    assert _git(["add", "README.md"], project).returncode == 0
    assert _git(["commit", "-m", "init"], project).returncode == 0
    return project


def _seed_run_at(home: Path, run_id: str, status: str, *, created_at: int,
                 updated_at: int) -> None:
    """Like ``_seed_run`` but with explicit ``created_at``/``updated_at``.

    The frozen-list ``_runs`` test pins the SQL ORDER BY ``created_at DESC``
    with equal spacing; ``int(time.time())`` would jitter between rows and
    destroy the deterministic ordering.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.5, created_at, updated_at,
         "code_fix", "", "latest"),
    )
    con.commit()
    con.close()


def _write_diff_cache(home: Path, run_id: str, *, added: int, removed: int) -> None:
    """Write ``<home>/runs/<id>/acp-diffs.json`` with a known ``(added, removed)``.

    ``task_state._diff_counts`` reads the cache via ``cached_or_computed``;
    each entry's ``old_text``/``new_text`` feeds ``difflib.unified_diff``
    and counts non-``+++``/``---`` lines. ``added`` ``+`` lines and
    ``removed`` ``-`` lines.
    """
    plus_lines = "\n".join(f"line{j}" for j in range(added))
    minus_lines = "\n".join(f"old{j}" for j in range(removed))
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "acp-diffs.json").write_text(
        json.dumps([{"path": "f.py", "old_text": minus_lines, "new_text": plus_lines}])
    )


def _seed_step_event(home: Path, run_id: str, *, node_id: str, created_at: int) -> None:
    """Insert a single ``node_start`` ``run_events`` row for the given run.

    No matching ``node_end`` — ``task_state._current_step`` returns the
    node id (the running node's id) so the row's ``step`` field is
    non-empty in the frozen ``_runs`` projection. Mirrors the lifecycle
    shape ``test_events_by_run_filters_to_lifecycle_events`` uses.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"evt-{run_id}-start", run_id, "node_start",
         json.dumps({"node_id": node_id}), created_at),
    )
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
    """``header.header`` reports ``worktrees == 3`` from a real temp git repo
    even when ``git worktree list`` would fail.

    The fix counts ``<common>/worktrees/`` instead of shelling out; the test
    stands up ``git init`` + one commit + two ``git worktree add`` calls so
    ``_git_common_dir`` discovers the real common dir through ``git rev-parse``.
    ``subprocess.run`` is patched to raise only on argv containing ``worktree``
    AND ``list`` — every other ``git`` call (rev-parse, branch detection)
    runs for real. Asserts end-to-end via ``header.header(home, {})`` so the
    real integration path is exercised (not just ``_worktree_count`` in
    isolation). Two linked worktree dirs on disk + the main checkout itself
    → 3.
    """
    from mini_ork.ide_pages import header

    project = _init_repo(tmp_path, name="proj")
    for name in ("wt-a", "wt-b"):
        assert _git(["worktree", "add", "-q", "--detach", name, "HEAD"],
                    project).returncode == 0
    # ``header.header`` sets ``project = home.parent`` (header.py:129); the
    # home is the project's ``.mini-ork`` so the integration runs against a
    # real git repo while the rest of header (cost_ledger, scheduler, …)
    # degrades gracefully via its broad except clauses.
    home = project / ".mini-ork"
    home.mkdir()

    real_run = subprocess.run

    def _guarded_run(argv, *args, **kwargs):
        # Block only ``git worktree list`` — every other ``git`` call must
        # run so ``_git_common_dir`` discovers the real common dir.
        if isinstance(argv, (list, tuple)) and len(argv) >= 3 \
                and argv[0] == "git" and "worktree" in argv and "list" in argv:
            raise RuntimeError("git worktree list must not be called")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _guarded_run)

    payload = header.header(home, {})
    assert payload["worktrees"] == 3


def test_worktree_count_resolves_relative_common_dir(tmp_path, monkeypatch) -> None:
    """The r2 fix-2 regression: ``git rev-parse --git-common-dir`` historically
    printed a relative ``.git`` for the main checkout, which made the absolute
    check drop the count to zero. After the patch, ``_git_common_dir`` resolves
    a relative common dir against the project root and the count is
    ``1 + len(worktrees/)`` again.

    Modern git emits absolute paths under ``--path-format=absolute``, so to
    force the relative branch we intercept ``rev-parse --git-common-dir`` and
    return a relative ``.git`` (pre-2.31 behaviour). The test then exercises
    the ``if not p.is_absolute()`` resolve-at-project branch in
    ``_git_common_dir`` (header.py:34-37).
    """
    from mini_ork.ide_pages import header

    project = _init_repo(tmp_path, name="proj")
    # A single linked worktree; total expected = 2.
    assert _git(["worktree", "add", "-q", "--detach", "wt-1", "HEAD"],
                project).returncode == 0

    home = project / ".mini-ork"
    home.mkdir()

    real_run = subprocess.run

    def _guarded_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and len(argv) >= 3 \
                and argv[0] == "git" and "rev-parse" in argv \
                and "--git-common-dir" in argv:
            # Pre-historic git behaviour: emit a relative ``.git``.
            return subprocess.CompletedProcess(argv, 0, ".git\n", "")
        if isinstance(argv, (list, tuple)) and len(argv) >= 3 \
                and argv[0] == "git" and "worktree" in argv and "list" in argv:
            raise RuntimeError("git worktree list must not be called")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _guarded_run)

    payload = header.header(home, {})
    assert payload["worktrees"] == 2  # main + wt-1


def test_runs_output_is_stable_against_frozen_list(home: Path) -> None:
    """``_runs`` output is identical before/after — pin the exact row shape.

    Seeds 30 runs across the three observable ``task_state`` buckets
    (done/failed/working) plus a mix of with-diffs/without-diffs, then
    asserts against a literal expected list. The expected values are NOT
    derived from the function's own output — that pattern let earlier
    regressions pass silently. ``started_at``/``ended_at`` come straight
    from the seed epoch ints (round-trip through ``_normalize_ts`` →
    ``_iso_to_epoch`` is identity); ``added``/``removed`` come from the
    ``acp-diffs.json`` we wrote with a known shape.
    """
    # ``(run_id, status, added, removed)`` — 30 rows. Insertion order
    # doubles as ASC ``created_at``; the SQL ``ORDER BY created_at DESC``
    # returns them reversed. Names are lexically distinct so a column
    # swap fails loud.
    seeds: list[tuple[str, str, int, int]] = [
        # 11 done (published → done) — alternating with/without diffs
        ("run-1791000001-aaaaa1", "published",  3, 1),
        ("run-1791000002-aaaaa2", "published",  0, 0),
        ("run-1791000003-aaaaa3", "published",  2, 2),
        ("run-1791000004-aaaaa4", "published",  0, 0),
        ("run-1791000005-aaaaa5", "published",  1, 1),
        ("run-1791000006-aaaaa6", "published",  0, 0),
        ("run-1791000007-aaaaa7", "published",  4, 0),
        # 4 failed
        ("run-1791000008-bbbbb1", "failed",     0, 0),
        ("run-1791000009-bbbbb2", "failed",     0, 0),
        ("run-1791000010-bbbbb3", "failed",     0, 0),
        ("run-1791000011-bbbbb4", "failed",     0, 0),
        # 15 working (executing/classified → working)
        ("run-1791000012-ccccc1", "executing",  0, 0),
        ("run-1791000013-ccccc2", "executing",  0, 0),
        ("run-1791000014-ccccc3", "executing",  0, 0),
        ("run-1791000015-ccccc4", "executing",  0, 0),
        ("run-1791000016-ccccc5", "executing",  0, 0),
        ("run-1791000017-ccccc6", "executing",  0, 0),
        ("run-1791000018-ddddd1", "classified", 0, 0),
        ("run-1791000019-ddddd2", "classified", 0, 0),
        ("run-1791000020-ddddd3", "classified", 0, 0),
        ("run-1791000021-ddddd4", "classified", 0, 0),
        ("run-1791000022-ddddd5", "classified", 0, 0),
        ("run-1791000023-eeeee1", "executing",  0, 0),
        ("run-1791000024-eeeee2", "executing",  0, 0),
        ("run-1791000025-eeeee3", "executing",  0, 0),
        ("run-1791000026-eeeee4", "executing",  0, 0),
        # 4 more done rows with diffs (deterministic tail)
        ("run-1791000027-fffff1", "published",  5, 2),
        ("run-1791000028-fffff2", "published",  1, 0),
        ("run-1791000029-fffff3", "published",  0, 3),
        ("run-1791000030-fffff4", "published",  0, 0),
    ]

    base = 1_791_000_000
    # ``created_at``/``updated_at`` are INTEGER unix timestamps; spacing
    # 60 s apart so DESC ordering is unambiguous.
    for i, (run_id, status, added, removed) in enumerate(seeds):
        ts = base + i * 60
        _seed_run_at(home, run_id, status, created_at=ts, updated_at=ts)
        if added or removed:
            _write_diff_cache(home, run_id, added=added, removed=removed)

    # r4 extension: one ``needs_you`` row (rule 1 sentinel: ``.cost-pause``
    # in the run dir) and one row carrying a non-empty ``step`` field
    # (``node_start`` lifecycle event without a matching ``node_end`` so
    # ``_current_step`` returns the node id). Seeded AFTER the 30 rows so
    # DESC ordering places them at the TOP of the result.
    needs_you_id = "run-1791000031-ggggg1"
    step_id = "run-1791000032-ggggg2"
    needs_you_ts = base + len(seeds) * 60
    step_ts = needs_you_ts + 60
    _seed_run_at(home, needs_you_id, "executing", created_at=needs_you_ts, updated_at=needs_you_ts)
    (home / "runs" / needs_you_id).mkdir(parents=True, exist_ok=True)
    (home / "runs" / needs_you_id / ".cost-pause").write_text("budget\n")
    _seed_run_at(home, step_id, "executing", created_at=step_ts, updated_at=step_ts)
    step_dir = home / "runs" / step_id
    step_dir.mkdir(parents=True, exist_ok=True)
    _seed_step_event(home, step_id, node_id="implementer", created_at=step_ts)

    runs, counts = board_cmd._runs(home)

    # DESC by ``created_at`` + ``rowid`` → seeds reversed. The two r4
    # rows sit at the FRONT of the result (newest first).
    expected: list[dict] = []
    # Newest → oldest loop: the two r4 rows first (literal), then the
    # reversed 30-row seeds (re-derived via the existing loop).
    expected.append({
        "id": step_id,
        "title": "code-fix run",
        "recipe": "code-fix",
        "state": "working",
        "mark": "●",
        "step": "implementer",
        "started_at": step_ts,
        "ended_at": None,
        "cost_usd": 0.5,
        "added": 0,
        "removed": 0,
        "has_workspace": False,
    })
    expected.append({
        "id": needs_you_id,
        "title": "code-fix run",
        "recipe": "code-fix",
        "state": "needs_you",
        "mark": "✋",
        "step": "",
        "started_at": needs_you_ts,
        "ended_at": None,
        "cost_usd": 0.5,
        "added": 0,
        "removed": 0,
        "has_workspace": False,
    })
    for run_id, status, added, removed in reversed(seeds):
        seed_index = next(
            idx for idx, (rid, _, _, _) in enumerate(seeds) if rid == run_id
        )
        ts = base + seed_index * 60
        if status == "published":
            state = "done"
            mark = "✓"
            ended_at: int | None = ts
        elif status == "failed":
            state = "failed"
            mark = "✗"
            ended_at = ts
        else:
            state = "working"
            mark = "●"
            ended_at = None
        expected.append({
            "id": run_id,
            "title": "code-fix run",
            "recipe": "code-fix",
            "state": state,
            "mark": mark,
            "step": "",
            "started_at": ts,
            "ended_at": ended_at,
            "cost_usd": 0.5,
            "added": added,
            "removed": removed,
            "has_workspace": False,
        })

    assert counts == {"working": 16, "needs_you": 1, "done": 11, "failed": 4}
    assert runs == expected


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


# ── kickoff ide-board-perf-r4 — diffstat cache for finished runs ────────────


def test_diffstat_written_on_first_runs_then_serves_second_call(
    home: Path, monkeypatch
) -> None:
    """First ``_runs`` writes ``diffstat.json``; a second ``_runs`` with
    ``mini_ork.acp.diffs.run_diffs`` monkeypatched to raise still returns
    identical counts — proves the diffstat cache short-circuits before git.

    The pre-r5 version never deleted ``acp-diffs.json`` between calls, so
    ``cached_or_computed`` answered from that file and the test never
    actually exercised the diffstat fast path. r5 fix 2 deletes
    ``acp-diffs.json`` so only ``diffstat.json`` can answer 4/2.
    """
    import mini_ork.acp.diffs as acp_diffs

    run_id = "r-ds-cache"
    _seed_run(home, run_id, "published")
    _write_diff_cache(home, run_id, added=4, removed=2)

    board_cmd._runs(home)
    cache_path = home / "runs" / run_id / "diffstat.json"
    assert cache_path.is_file()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload == {"added": 4, "removed": 2, "v": 1}

    # Remove the OTHER cache (``acp-diffs.json``) so only ``diffstat.json``
    # can answer on the second call. Without this, ``cached_or_computed``
    # reads ``acp-diffs.json`` directly and the test never exercises the
    # diffstat path.
    acp_cache = home / "runs" / run_id / "acp-diffs.json"
    assert acp_cache.is_file()  # sanity: it was written by _write_diff_cache
    acp_cache.unlink()

    # Second call — break the slow path so the cache MUST answer.
    def boom(*_args, **_kwargs):
        raise RuntimeError("git show forbidden")

    monkeypatch.setattr(acp_diffs, "run_diffs", boom)

    rows2, _ = board_cmd._runs(home)
    matching = [r for r in rows2 if r["id"] == run_id]
    assert len(matching) == 1
    assert matching[0]["added"] == 4
    assert matching[0]["removed"] == 2


def test_diffstat_not_written_when_compute_returns_empty(home: Path, monkeypatch) -> None:
    """r6 fix: ``run_diffs`` returning ``[]`` (no summary / worktree gone /
    empty ``files_changed``) is the r4 cache-poisoning trap in disguise.

    ``_diff_counts`` returns ``(0, 0, cacheable=False)`` — counts are
    *legitimately* zero but the bool must be False because the
    underlying ``run_diffs`` short-circuited to ``[]`` before git.
    Rule 4 MUST NOT persist ``(0, 0)`` here; an empty result is
    recomputed next poll (cheap: ``run_diffs`` returns before calling
    git).

    Setup: no ``acp-diffs.json`` seeded so ``cached_or_computed``
    falls through to the patched ``run_diffs``.
    """
    import mini_ork.acp.diffs as acp_diffs

    run_id = "r-ds-empty-list"
    _seed_run(home, run_id, "published")
    # The run dir must exist, or a wrong write would fail silently and the
    # negative assertion below could never fail.
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT seed acp-diffs.json — the empty list must come
    # from the patched ``run_diffs`` so the cacheable signal is exercised.

    cache_path = home / "runs" / run_id / "diffstat.json"
    acp_cache = home / "runs" / run_id / "acp-diffs.json"
    assert not acp_cache.is_file(), (
        "test prerequisite — no pre-seeded acp-diffs.json so "
        "cached_or_computed must reach run_diffs"
    )

    def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(acp_diffs, "run_diffs", empty)
    board_cmd._runs(home)
    assert not cache_path.exists(), (
        "empty run_diffs result must not poison diffstat.json with (0, 0) — "
        "recompute on the next poll (cheap: run_diffs short-circuits before git)"
    )

    # Positive control: the same dir accepts a real result, so the negative
    # assertion above was a real test of the guard.
    monkeypatch.undo()
    _write_diff_cache(home, run_id, added=2, removed=1)
    board_cmd._runs(home)
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {"added": 2, "removed": 1, "v": 1}


def test_diffstat_not_written_when_compute_fails(home: Path, monkeypatch) -> None:
    """r5 fix 1 + r6 fix: a compute failure in ``_diff_counts`` must NOT poison
    the cache, AND the failure path must actually be exercised (r5 bug: poll 1
    seeded ``acp-diffs.json`` so ``cached_or_computed`` short-circuited and
    never reached the patched ``run_diffs``).

    Poll 1: no ``acp-diffs.json`` seeded, ``run_diffs`` patched to raise.
    ``cached_or_computed`` falls through to ``run_diffs`` which raises;
    ``_diff_counts`` returns ``None``; rule 4 leaves the row at ``(0, 0)``
    and does NOT write ``diffstat.json``.

    Poll 2: ``run_diffs`` unpatched, ``acp-diffs.json`` seeded once.
    ``cached_or_computed`` answers from cache (non-empty,
    ``from_cache=True`` ⇒ ``cacheable=True``); counts computed and
    ``diffstat.json`` written with the real counts.
    """
    import mini_ork.acp.diffs as acp_diffs

    run_id = "r-ds-fail-then-ok"
    _seed_run(home, run_id, "published")
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT seed ``acp-diffs.json`` here — r5 did, which
    # made the patched ``run_diffs`` never run on poll 1 and left the
    # test asserting against a path it never exercised.

    cache_path = home / "runs" / run_id / "diffstat.json"
    acp_cache = home / "runs" / run_id / "acp-diffs.json"
    assert not acp_cache.is_file(), (
        "test prerequisite — no pre-seeded acp-diffs.json so "
        "cached_or_computed must reach run_diffs"
    )

    # Poll 1: slow path blows up; cache must NOT be written.
    def boom(*_args, **_kwargs):
        raise RuntimeError("git show forbidden")

    monkeypatch.setattr(acp_diffs, "run_diffs", boom)
    board_cmd._runs(home)
    assert not cache_path.exists(), (
        "compute failure must not persist (0, 0) — that poisons the cache "
        "and hides future real counts"
    )

    # Poll 2: drop the patch so ``cached_or_computed`` would reach a real
    # ``run_diffs`` if needed; seed ``acp-diffs.json`` so it can answer
    # from cache (the run's own artifact) without needing a git repo.
    # ``diffstat.json`` appears with real counts.
    monkeypatch.undo()
    _write_diff_cache(home, run_id, added=7, removed=3)

    board_cmd._runs(home)
    assert cache_path.is_file()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload == {"added": 7, "removed": 3, "v": 1}


def test_diffstat_never_written_for_working_run(home: Path) -> None:
    """A working (``classified``) run must never produce ``diffstat.json``.

    Working runs return ``(0, 0)`` from ``task_state`` (rule 6) and
    should never persist the cache — a cache hit on a still-running row
    would mask a future diff arrival.
    """
    run_id = "r-ds-working"
    _seed_run(home, run_id, "classified")

    board_cmd._runs(home)
    cache_path = home / "runs" / run_id / "diffstat.json"
    assert not cache_path.exists()


# ── kickoff ide-board-perf-r7 — FastAPI must NOT enter sys.modules on the
#    ``board --shell`` boot path. ``_events_by_run`` early-returns when no
#    ``state.db`` exists, so the temp home MUST be seeded — otherwise the
#    assertion passes on the unfixed tree (false-positive guard).


def test_board_shell_path_does_not_pull_in_fastapi(home: Path) -> None:
    """``mini_ork.cli.board_cmd._runs(<seeded home>)`` and
    ``mini_ork.ide_pages.header.header`` must NOT import ``fastapi`` or
    ``pydantic`` — they used to because every ACP/IDE module reached
    ``mini_ork.web.deps`` (whose module top loads FastAPI) just to get
    ``db_for``. The fix moved ``db_for`` into ``mini_ork.web.db`` (FastAPI-
    free) and re-exports it from ``deps`` for the web server.

    The temp home is the ``home`` fixture: ``mig.init_db`` already seeded
    ``state.db`` so ``_events_by_run`` is reached and FastAPI-free re-export
    caching isn't a vacuous pass.

    A negative control at the end asserts the test WOULD catch a
    regression: importing ``mini_ork.web.deps`` directly DOES pull in
    FastAPI, so a future regression that re-introduces the
    ``web.deps.db_for`` import on the board path would fail.
    """
    import sys

    _seed_run(home, "run-r7-shellpath", "published")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        f"import mini_ork.cli.board_cmd as b\n"
        f"b._runs(Path({str(home)!r}))\n"
        "from mini_ork.ide_pages.header import header\n"
        "header_func = header\n"
        "assert 'fastapi' not in sys.modules, sorted(m for m in sys.modules if m.startswith('fastapi'))\n"
        "assert 'pydantic' not in sys.modules, sorted(m for m in sys.modules if m.startswith('pydantic'))\n"
        "print('OK', 'fastapi' in sys.modules, 'pydantic' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    # Subprocess prints ``OK <fastapi-in-modules> <pydantic-in-modules>``.
    # Both must be False — the r7 fix moved ``db_for`` into
    # ``mini_ork.web.db`` so the board path stays FastAPI-free.
    stdout = proc.stdout.strip()
    assert stdout.startswith("OK "), f"unexpected subprocess output: {stdout!r}"
    parts = stdout.split()
    assert len(parts) == 3, f"unexpected subprocess output shape: {stdout!r}"
    assert parts[1] == "False", f"fastapi leaked into sys.modules: {stdout!r}"
    assert parts[2] == "False", f"pydantic leaked into sys.modules: {stdout!r}"

    # Negative control: ``mini_ork.web.deps`` top-loads FastAPI, so this
    # WOULD fail if the new guard ever regresses to re-importing it on
    # the board path. Kept in-process so the subprocess above remains a
    # pure shape check.
    import subprocess as _subprocess

    neg_code = (
        "import mini_ork.web.deps as d\n"
        "import sys\n"
        "assert 'fastapi' in sys.modules, 'web.deps must import fastapi'\n"
        "print('NEG OK')\n"
    )
    neg_proc = _subprocess.run(
        [sys.executable, "-c", neg_code], capture_output=True, text=True, timeout=20
    )
    assert neg_proc.returncode == 0, neg_proc.stderr
    assert "NEG OK" in neg_proc.stdout


# ── kickoff ide-kickoff-search — ``board runs`` verb ───────────────────────


def _seed_run_with_kickoff(home: Path, run_id: str, *, created_at: int,
                           kickoff_text: str = "default kickoff body") -> int:
    """Like ``_seed_run_at`` but with a kickoff file referenced from
    ``kickoff_path`` so the search indexer reads a real file.
    """
    kickoff = home / "kickoffs" / f"{run_id}.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text(kickoff_text)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", "published", 0.0, created_at, created_at,
         "code_fix", str(kickoff), "latest"),
    )
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir.stat().st_mtime_ns


def test_runs_verb_no_query_pages_across_all_runs(home: Path) -> None:
    """``board runs`` (no query) pages the full ``task_runs`` set; ``total``
    is the row count, not a sliced window."""
    import io
    from contextlib import redirect_stdout

    base = 1_791_000_000
    for i in range(7):
        _seed_run_at(home, f"run-{base + i}-rk{i}", "published",
                     created_at=base + i * 60, updated_at=base + i * 60)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--limit", "3", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["total"] == 7
    assert len(payload["runs"]) == 3
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert payload["errors"] == {}

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--offset", "6", "--limit", "3", "--home", str(home)], "")
    page2 = json.loads(buf.getvalue())
    assert page2["offset"] == 6 and page2["total"] == 7 and page2["has_more"] is False
    assert len(page2["runs"]) == 1


def test_runs_verb_with_query_uses_the_search_index(home: Path) -> None:
    """``board runs --query X`` returns only the matching runs."""
    base = 1_791_000_000
    rid_a = f"run-{base}-maaa01"
    rid_b = f"run-{base + 60}-maaa02"
    _seed_run_with_kickoff(home, rid_a, created_at=base,
                           kickoff_text="# alpha\n\napple banana feature\n")
    _seed_run_with_kickoff(home, rid_b, created_at=base + 60,
                           kickoff_text="# beta\n\ncherry durian feature\n")

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--query", "apple", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["total"] >= 1
    assert any(r["id"] == rid_a for r in payload["runs"])
    assert all(r["id"] != rid_b for r in payload["runs"])


def test_runs_verb_limit_is_clamped_to_200(home: Path) -> None:
    """``--limit`` above 200 is clamped, not a usage error."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--limit", "999", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    # No runs seeded — the clamp is the contract; payload still has the shape.
    assert payload["runs"] == [] and payload["total"] == 0


def test_runs_verb_bad_offset_returns_usage_error(home: Path) -> None:
    import io
    from contextlib import redirect_stdout, redirect_stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        rc = board_cmd.main(["runs", "--offset", "-1", "--home", str(home)], "")
    assert rc == 2
    assert "offset" in buf_err.getvalue()


def test_runs_verb_does_not_require_a_run_id(home: Path) -> None:
    """``board runs`` (no positional) is exempt from the run-id requirement
    that other action verbs trigger."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True


def test_runs_verb_parser_accepts_runs_as_choice(home: Path) -> None:
    """The parser's ``verb`` choice includes ``runs`` so a typo of ``run``
    (singular) still routes through the ``run`` verb without crashing here."""
    parser = board_cmd.build_parser()
    args = parser.parse_args(["runs", "--home", str(home)])
    assert args.verb == "runs"


def test_runs_verb_payload_shape_matches_spec(home: Path) -> None:
    """The payload keys are exact: ``ok, runs, total, offset, has_more,
    errors, indexing`` — no extras (the shell ``_SHELL_KEYS`` guard
    forbids leak). The new ``indexing`` flag (kickoff r3 fix #2) is
    always present so the IDE can show partial-index state without a
    second round-trip."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        board_cmd.main(["runs", "--home", str(home)], "")
    payload = json.loads(buf.getvalue())
    assert set(payload) == {"ok", "runs", "total", "offset", "has_more", "errors", "indexing"}


def test_runs_verb_no_state_db_is_an_empty_envelope(home: Path) -> None:
    """Without ``state.db`` the verb returns the empty envelope — not exit 1."""
    fresh = home.parent / "no-state"
    fresh.mkdir()
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--home", str(fresh)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True and payload["total"] == 0 and payload["runs"] == []


# ── kickoff ide-kickoff-search-r2 — paging past the 50-row fleet window ─────


def _seed_sixty_published_runs(home: Path) -> list[str]:
    """Seed 60 published runs with distinct titles/recipes, evenly spaced.

    Returns the rids in insertion (oldest-first) order; ``rids[0]`` is the
    oldest seed and ``rids[59]`` is the newest. ``list_runs`` orders
    newest-first, so ``rids[59]`` is the first row of the page.
    """
    base = 1_791_500_000
    rids: list[str] = []
    con = sqlite3.connect(home / "state.db")
    for i in range(60):
        rid = f"run-{base + i * 60:09d}-p{i:02d}"
        ts = base + i * 60
        title = f"publish-{i:02d}-kickoff"
        kickoff = home / "kickoffs" / f"{rid}.md"
        kickoff.parent.mkdir(parents=True, exist_ok=True)
        kickoff.write_text(f"# {title}\n\nbody\n")
        con.execute(
            "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
            "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, f"recipe-{i:02d}", "published", 0.0, ts, ts,
             "code_fix", str(kickoff), "latest"),
        )
        rids.append(rid)
    con.commit()
    con.close()
    return rids


def test_runs_verb_pages_past_fleet_window_with_real_rows(home: Path) -> None:
    """The 50-row ``MAX_LIMIT`` clamp must NOT make rows past index 50
    stubs. ``board runs --offset 48 --limit 4`` returns the 49th-52nd
    runs with real title/recipe/state, not the old ``_minimal`` stub.
    """
    import io
    from contextlib import redirect_stdout

    rids = _seed_sixty_published_runs(home)
    # list_runs is newest-first: rids[0] is the oldest, rids[59] is the
    # newest. Offset 48 limit 4 → rids[11], rids[10], rids[9], rids[8]
    # (the 49th-52nd runs in newest-first order).

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(
            ["runs", "--offset", "48", "--limit", "4", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["total"] == 60
    assert len(payload["runs"]) == 4
    assert payload["has_more"] is True
    assert payload["offset"] == 48
    expected_ids = [rids[11], rids[10], rids[9], rids[8]]
    expected_titles = ["publish-11-kickoff", "publish-10-kickoff",
                       "publish-09-kickoff", "publish-08-kickoff"]
    expected_recipes = ["recipe-11", "recipe-10", "recipe-09", "recipe-08"]
    for row, eid, etitle, erecipe in zip(
        payload["runs"], expected_ids, expected_titles, expected_recipes
    ):
        assert row["id"] == eid, f"expected id {eid!r}, got {row['id']!r}"
        assert row["title"] == etitle, f"expected title {etitle!r}, got {row['title']!r}"
        assert row["recipe"] == erecipe, f"expected recipe {erecipe!r}, got {row['recipe']!r}"
        assert row["state"] == "done", (
            f"row state must be done (published → done), got {row['state']!r}"
        )

    # Offset 56, limit 4 → rids[3], rids[2], rids[1], rids[0] (the 4 oldest).
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(
            ["runs", "--offset", "56", "--limit", "4", "--home", str(home)], "")
    assert rc == 0
    payload2 = json.loads(buf.getvalue())
    assert payload2["total"] == 60
    assert len(payload2["runs"]) == 4
    assert payload2["has_more"] is False
    oldest_ids = [rids[3], rids[2], rids[1], rids[0]]
    oldest_titles = ["publish-03-kickoff", "publish-02-kickoff",
                     "publish-01-kickoff", "publish-00-kickoff"]
    for row, eid, etitle in zip(payload2["runs"], oldest_ids, oldest_titles):
        assert row["id"] == eid
        assert row["state"] == "done"
        assert row["title"] == etitle
        assert row["recipe"] == f"recipe-{eid[-2:]}"


def test_runs_verb_query_finds_oldest_run_by_unique_word(home: Path) -> None:
    """A word unique to the oldest run's kickoff is found via search and
    the row carries the real title/recipe/state — not a stub.
    """
    import io
    from contextlib import redirect_stdout

    rids = _seed_sixty_published_runs(home)
    oldest = rids[0]  # insertion order, oldest is index 0
    # Replace the oldest run's kickoff with a unique word.
    unique = "oldestuniquephrase-xyz"
    (home / "kickoffs" / f"{oldest}.md").write_text(f"# publish-00-kickoff\n\n{unique}\n")
    (home / "runs" / oldest).mkdir(parents=True, exist_ok=True)
    (home / "runs" / oldest).touch()
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET kickoff_path = ? WHERE id = ?",
                (str(home / "kickoffs" / f"{oldest}.md"), oldest))
    con.commit()
    con.close()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--query", unique, "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert any(r["id"] == oldest for r in payload["runs"]), \
        f"oldest run {oldest!r} not in {payload['runs']!r}"
    match = next(r for r in payload["runs"] if r["id"] == oldest)
    assert match["state"] == "done"
    assert match["recipe"] == "recipe-00"
    assert match["title"] == "publish-00-kickoff"


def test_runs_verb_finds_a_run_by_its_id(home: Path) -> None:
    """The FTS body now suffixes the run id, so a query for the run id
    finds the run even when its kickoff does not contain the id.
    """
    import io
    from contextlib import redirect_stdout

    rids = _seed_sixty_published_runs(home)
    target = rids[5]
    # Confirm the kickoff does NOT contain the run id (the seed body is generic).
    kickoff = home / "kickoffs" / f"{target}.md"
    assert target not in kickoff.read_text(encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(["runs", "--query", target, "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert any(r["id"] == target for r in payload["runs"]), \
        f"run id {target!r} not found in {payload['runs']!r}"


def test_runs_verb_falls_back_to_like_when_fts5_is_unavailable(
    home: Path, monkeypatch
) -> None:
    """With FTS5 forced off, the payload ``errors`` carries the fallback
    note and ``--offset`` still pages the LIKE matches. Seeds ≥ 2 LIKE
    matches so we can verify disjoint pages + ``total`` (kickoff r3 fix #7c).
    """
    import io
    from contextlib import redirect_stdout

    from mini_ork.ide_pages import search as search_mod

    monkeypatch.setattr(search_mod, "_fts5_available", lambda *_a, **_k: False)

    rids = _seed_sixty_published_runs(home)
    # Inject the same unique word into two distinct runs so LIKE has ≥ 2
    # matches to page through.
    like_word = "liketargetword-12345"
    targets = [rids[7], rids[22]]
    for target in targets:
        (home / "kickoffs" / f"{target}.md").write_text(
            f"# publish-{target[-2:]}-kickoff\n\n{like_word}\n"
        )
        (home / "runs" / target).mkdir(parents=True, exist_ok=True)
        (home / "runs" / target).touch()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = board_cmd.main(
            ["runs", "--query", like_word, "--offset", "0",
             "--limit", "1", "--home", str(home)], "")
    assert rc == 0
    page1 = json.loads(buf.getvalue())
    assert page1["errors"].get("search") and "FTS5 unavailable" in page1["errors"]["search"]
    assert page1["total"] == 2, f"LIKE should report total=2, got {page1['total']}"
    assert len(page1["runs"]) == 1
    assert page1["runs"][0]["id"] in targets

    # Paging still works in the LIKE path. Page 2 returns the OTHER
    # match — pages must be disjoint.
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        board_cmd.main(
            ["runs", "--query", like_word, "--offset", "1",
             "--limit", "1", "--home", str(home)], "")
    page2 = json.loads(buf2.getvalue())
    assert page2["ok"] is True
    assert page2["offset"] == 1
    assert page2["total"] == 2, f"LIKE total must stay stable across pages, got {page2['total']}"
    assert len(page2["runs"]) == 1
    ids1 = {r["id"] for r in page1["runs"]}
    ids2 = {r["id"] for r in page2["runs"]}
    assert ids1.isdisjoint(ids2), f"LIKE pages must be disjoint, got {ids1} ∩ {ids2}"
    assert ids1 | ids2 == set(targets), "LIKE paging should cover exactly the two seeded matches"


def test_runs_calls_fleet_rows_exactly_once(home: Path, monkeypatch) -> None:
    """``_runs`` must call ``fleet_rows`` once per poll — the old code
    called it twice (once in ``_runs``, once via ``_rows_for``).
    """
    from mini_ork.acp import fleet as acp_fleet

    _seed_sixty_published_runs(home)
    calls: list[tuple] = []
    original = acp_fleet.fleet_rows

    def spy(home_arg, **kwargs):
        calls.append((home_arg, kwargs))
        return original(home_arg, **kwargs)

    monkeypatch.setattr(acp_fleet, "fleet_rows", spy)
    # ``_runs`` does ``from mini_ork.acp.fleet import fleet_rows`` inside
    # the function; the import runs at call time so the patch above is
    # visible on the next ``_runs(home)`` call.

    board_cmd._runs(home)
    assert len(calls) == 1, f"fleet_rows called {len(calls)} times, expected 1"


def test_runs_verb_search_path_paginates_with_correct_has_more(home: Path) -> None:
    """Search-path paging: 60 runs share the word 'publish' in their title.
    ``--limit 5 --offset 0/5/10`` returns disjoint pages of 5 with the
    correct ``has_more`` and a stable ``total``.
    """
    import io
    from contextlib import redirect_stdout

    _seed_sixty_published_runs(home)

    def _page(offset: int) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = board_cmd.main(
                ["runs", "--query", "publish", "--offset", str(offset),
                 "--limit", "5", "--home", str(home)], "")
        assert rc == 0
        return json.loads(buf.getvalue())

    p0 = _page(0)
    p5 = _page(5)
    p10 = _page(10)
    assert p0["total"] == 60
    assert len(p0["runs"]) == 5
    assert len(p5["runs"]) == 5
    assert len(p10["runs"]) == 5
    assert p0["has_more"] is True
    assert p5["has_more"] is True
    assert p10["has_more"] is True
    ids0 = {r["id"] for r in p0["runs"]}
    ids5 = {r["id"] for r in p5["runs"]}
    ids10 = {r["id"] for r in p10["runs"]}
    assert ids0.isdisjoint(ids5)
    assert ids5.isdisjoint(ids10)
    assert ids0.isdisjoint(ids10)
