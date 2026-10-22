"""Unit tests for the SDE-6b ``split`` graph patch and supervisor R7.

Hermetic: a temp home whose ``state.db`` is built by applying the repo's real
migrations, no network, no provider credentials, and an injected ``spawn`` so
an accidental revive would be visible instead of launching a process.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from mini_ork.recovery import graph_patch, supervisor
from mini_ork.scheduler import ensure_retry_schema
from mini_ork.stores.migrate import migrate_apply

RUN_ID = "run-sde6b-unit"
PARENT = RUN_ID
CHILD_IDS = [f"split-{RUN_ID}-001-1", f"split-{RUN_ID}-001-2"]
FROM_NODE = "implementer"
REASON = "remaining work too large for one run"
SNAPSHOT = "# Parent kickoff\n\nShip the whole thing, part by part.\n"
FALLBACK_TEXT = "# Fallback kickoff\n\nFrom the run profile.\n"
CHILDREN = [
    {"title": "Child one", "goal": "Deliver part one of the remaining work"},
    {"title": "Child two", "goal": "Deliver part two of the remaining work"},
]


# ── hermetic home ───────────────────────────────────────────────────────────


def _repo_root() -> Path:
    return Path(graph_patch.__file__).resolve().parents[2]


def _make_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    run_dir = home / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    db = home / "state.db"
    rc, _lines = migrate_apply(str(_repo_root() / "db" / "migrations"),
                               db=str(db))
    assert rc == 0, "the repo's real migrations must apply to the temp home"
    ensure_retry_schema(str(db))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home, run_dir, db


def _insert_task_run(db: Path, run_id: str, *, status: str = "failed",
                     recipe: str | None = "framework-edit") -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status,"
        " created_at, updated_at) VALUES (?, 'framework_edit', ?, ?, ?, 1, 1)",
        (run_id, recipe, f"/kickoffs/{run_id}.md", status))
    con.commit()
    con.close()


def _insert_epic(db: Path, epic_id: str, status: str) -> None:
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO epics (id, title, status) VALUES (?, ?, ?)",
                (epic_id, f"Epic {epic_id}", status))
    con.commit()
    con.close()


def _rows(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _write_snapshot(run_dir: Path, text: str = SNAPSHOT) -> None:
    (run_dir / "context-pack.v2.json").write_text(
        json.dumps({"kickoff_snapshot": text}), encoding="utf-8")


def _write_profile(run_dir: Path, kickoff_path) -> None:
    (run_dir / "run_profile.json").write_text(
        json.dumps({"kickoff_path": str(kickoff_path)}), encoding="utf-8")


def _split_patch(children, from_node: str = FROM_NODE,
                 reason: str = REASON) -> dict:
    return {"kind": "split", "from_node": from_node, "reason": reason,
            "children": children}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return None


def _waiting_run(home: Path, run_id: str, child_ids: list[str],
                 split_patch: str = "001") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "repair.json").write_text(json.dumps({
        "state": "waiting_on_children",
        "child_epic_ids": child_ids,
        "split_patch": split_patch,
        "fix_epic_id": f"legacy-{run_id}",
    }), encoding="utf-8")
    return run_dir


def _sweep(home: Path, db: Path, tmp_path: Path, *, dry_run: bool = False):
    return supervisor.sweep(
        str(home), str(db), engine_root=str(tmp_path), now=1700000000.0,
        dry_run=dry_run, spawn=lambda *args, **kwargs: None)


def _split_actions(actions: list[dict]) -> list[dict]:
    return [a for a in actions
            if str(a.get("action", "")).startswith("split")]


def _oversight_count(db: Path, run_id: str) -> int:
    return _rows(
        db,
        "SELECT COUNT(*) FROM mo_inbox_gates WHERE gate_id=? AND feature=?",
        (supervisor.SUPERVISOR_GATE, f"split child failed: {run_id}"))[0][0]


# ── schema ──────────────────────────────────────────────────────────────────


def test_split_kind_and_children_key_are_accepted(tmp_path, monkeypatch):
    assert "split" in graph_patch.KINDS
    _home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    assert graph_patch.propose(run_dir, _split_patch(CHILDREN)) == 1
    document = yaml.safe_load(
        (run_dir / "graph-patches" / "001.yaml").read_text(encoding="utf-8"))
    assert document["kind"] == "split"
    assert document["children"] == CHILDREN


@pytest.mark.parametrize("children", [
    [],
    [{"title": f"part {i}", "goal": f"goal {i}"} for i in range(5)],
    [{"title": "Has title only"}],
    [{"title": "T", "goal": "g", "owner": "x"}],
    [{"title": "", "goal": "g"}],
    [{"title": "T", "goal": "  "}],
    [{"title": 7, "goal": "g"}],
    "not-a-list",
    ["not-a-mapping"],
])
def test_split_children_must_be_one_to_four_title_goal_mappings(
        tmp_path, monkeypatch, children):
    _home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    with pytest.raises(graph_patch.GraphPatchError):
        graph_patch.propose(run_dir, _split_patch(children))
    assert not (run_dir / "graph-patches" / "001.yaml").exists()


def test_split_accepts_one_and_four_children(tmp_path, monkeypatch):
    _home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    one = [{"title": "Only child", "goal": "Do everything"}]
    four = [{"title": f"part {i}", "goal": f"goal {i}"} for i in range(4)]
    assert graph_patch.propose(run_dir, _split_patch(one)) == 1
    assert graph_patch.propose(run_dir, _split_patch(four)) == 2


def test_materialize_refuses_split(tmp_path, monkeypatch):
    home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    with pytest.raises(graph_patch.GraphPatchError,
                       match=r"split patches do not change the workflow;"
                             r" apply them"):
        graph_patch.materialize(home, RUN_ID, number)


# ── apply ───────────────────────────────────────────────────────────────────


def test_apply_split_creates_waiting_children(tmp_path, monkeypatch):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID)
    _write_snapshot(run_dir)
    (run_dir / "repair.json").write_text(
        json.dumps({"state": "gave_up", "fix_epic_id": "legacy-7"}),
        encoding="utf-8")

    spawn = _Recorder()
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                      spawn=spawn)
    assert spawn.calls == []

    rows = _rows(
        db,
        "SELECT id, title, status, kickoff_path, recipe, notes FROM epics"
        " WHERE id LIKE ? ORDER BY id", (f"split-{RUN_ID}-%",))
    assert [row[0] for row in rows] == CHILD_IDS
    for (epic_id, title, status, kickoff_path, recipe, notes), child \
            in zip(rows, CHILDREN):
        assert title == child["title"]
        assert status == "not started"
        assert recipe == "framework-edit"
        assert notes == f"split from {RUN_ID} graph patch 001"
        path = run_dir / "graph-patches" / "001-split" / f"{epic_id}.md"
        assert Path(kickoff_path).resolve() == path.resolve()
        text = path.read_text(encoding="utf-8")
        assert "Ship the whole thing, part by part." in text
        assert "## This child's scope" in text
        assert child["title"] in text
        assert child["goal"] in text
        assert (f"Parent run: {RUN_ID} (failing node {FROM_NODE},"
                " graph patch 001).") in text

    applied = json.loads(
        (run_dir / "graph-patches" / "001.applied.json").read_text(
            encoding="utf-8"))
    assert applied["child_epic_ids"] == CHILD_IDS
    assert applied["from_node"] == FROM_NODE
    assert applied["applied_at"]

    repair = json.loads(
        (run_dir / "repair.json").read_text(encoding="utf-8"))
    assert repair["state"] == "waiting_on_children"
    assert repair["child_epic_ids"] == CHILD_IDS
    assert repair["split_patch"] == "001"
    assert repair["fix_epic_id"] == "legacy-7"
    # R7, not apply, moves the parent out of `failed`.
    assert _rows(db, "SELECT status FROM task_runs WHERE id=?",
                 (RUN_ID,))[0][0] == "failed"


def test_apply_split_duplicate_epic_id_rolls_back(tmp_path, monkeypatch):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID)
    _write_snapshot(run_dir)
    (run_dir / "repair.json").write_text(
        json.dumps({"state": "gave_up", "note": "pre-existing"}),
        encoding="utf-8")
    _insert_epic(db, CHILD_IDS[0], "not started")

    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    with pytest.raises(graph_patch.GraphPatchError):
        graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                          spawn=_Recorder())

    assert _rows(db, "SELECT id FROM epics WHERE id LIKE ?",
                 (f"split-{RUN_ID}-%",)) == [(CHILD_IDS[0],)]
    split_dir = run_dir / "graph-patches" / "001-split"
    assert (list(split_dir.glob("*.md")) if split_dir.is_dir() else []) == []
    assert not (run_dir / "graph-patches" / "001.applied.json").is_file()
    repair = json.loads((run_dir / "repair.json").read_text(encoding="utf-8"))
    assert repair == {"state": "gave_up", "note": "pre-existing"}


def test_split_kickoff_prefers_snapshot_over_run_profile(tmp_path, monkeypatch):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID)
    _write_snapshot(run_dir)
    kickoff = tmp_path / "parent.md"
    kickoff.write_text(FALLBACK_TEXT, encoding="utf-8")
    _write_profile(run_dir, kickoff)
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                      spawn=_Recorder())
    text = (run_dir / "graph-patches" / "001-split"
            / f"{CHILD_IDS[0]}.md").read_text(encoding="utf-8")
    assert "Ship the whole thing, part by part." in text
    assert "From the run profile." not in text


@pytest.mark.parametrize("snapshot", [None, ""])
def test_split_kickoff_falls_back_to_run_profile(tmp_path, monkeypatch, snapshot):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID)
    if snapshot is not None:
        _write_snapshot(run_dir, snapshot)
    kickoff = tmp_path / "parent.md"
    kickoff.write_text(FALLBACK_TEXT, encoding="utf-8")
    _write_profile(run_dir, kickoff)
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                      spawn=_Recorder())
    text = (run_dir / "graph-patches" / "001-split"
            / f"{CHILD_IDS[0]}.md").read_text(encoding="utf-8")
    assert "From the run profile." in text


def test_split_without_kickoff_source_raises(tmp_path, monkeypatch):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID)
    (run_dir / "run_profile.json").write_text(
        json.dumps({"run_id": RUN_ID}), encoding="utf-8")
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    with pytest.raises(graph_patch.GraphPatchError):
        graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                          spawn=_Recorder())
    assert _rows(db, "SELECT id FROM epics WHERE id LIKE ?",
                 (f"split-{RUN_ID}-%",)) == []
    assert not (run_dir / "graph-patches" / "001.applied.json").is_file()


def test_split_refuses_live_parent(tmp_path, monkeypatch):
    home, run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, RUN_ID, status="executing")
    _write_snapshot(run_dir)
    spawn = _Recorder()
    number = graph_patch.propose(run_dir, _split_patch(CHILDREN))
    with pytest.raises(graph_patch.GraphPatchError, match="executing"):
        graph_patch.apply(home, RUN_ID, number, engine_root=str(tmp_path),
                          spawn=spawn)
    assert spawn.calls == []
    assert _rows(db, "SELECT id FROM epics WHERE id LIKE ?",
                 (f"split-{RUN_ID}-%",)) == []


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_propose_accepts_children_json(tmp_path, monkeypatch):
    from mini_ork.cli import graph_patch as cli_graph_patch

    _home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    rc = cli_graph_patch.main([
        "propose", RUN_ID, "--kind", "split", "--from-node", FROM_NODE,
        "--children-json", json.dumps(CHILDREN), "--reason", REASON, "--json",
    ])
    assert rc == 0
    document = yaml.safe_load(
        (run_dir / "graph-patches" / "001.yaml").read_text(encoding="utf-8"))
    assert document["children"] == CHILDREN


@pytest.mark.parametrize("bad", [
    "not-json-at-all",
    json.dumps({"title": "t", "goal": "g"}),
])
def test_cli_propose_rejects_non_list_children_json(tmp_path, monkeypatch, bad):
    from mini_ork.cli import graph_patch as cli_graph_patch

    _home, run_dir, _db = _make_home(tmp_path, monkeypatch)
    rc = cli_graph_patch.main([
        "propose", RUN_ID, "--kind", "split", "--from-node", FROM_NODE,
        "--children-json", bad, "--reason", REASON,
    ])
    assert rc == 2
    assert not (run_dir / "graph-patches" / "001.yaml").exists()


# ── R7 (supervisor) ─────────────────────────────────────────────────────────


def test_r7_all_children_done_supersedes_parent(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT)
    for epic_id in CHILD_IDS:
        _insert_epic(db, epic_id, "done")
    run_dir = _waiting_run(home, PARENT, CHILD_IDS)

    actions = _sweep(home, db, tmp_path)
    assert any(a.get("action") == "split_done" and a.get("run_id") == PARENT
               for a in actions), actions

    status, notes = _rows(
        db, "SELECT status, COALESCE(notes, '') FROM task_runs WHERE id=?",
        (PARENT,))[0]
    assert status == "superseded"
    assert "superseded: delivered by split children" in notes
    for epic_id in CHILD_IDS:
        assert epic_id in notes

    repair = json.loads((run_dir / "repair.json").read_text(encoding="utf-8"))
    assert repair["state"] == "split_done"
    assert repair["fix_epic_id"] == f"legacy-{PARENT}"
    outcome = json.loads(
        (run_dir / "graph-patches" / "001.outcome.json").read_text(
            encoding="utf-8"))
    assert outcome["passed"] is True


def test_r7_outcome_file_is_write_once(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT)
    for epic_id in CHILD_IDS:
        _insert_epic(db, epic_id, "done")
    run_dir = _waiting_run(home, PARENT, CHILD_IDS)
    outcome_path = run_dir / "graph-patches" / "001.outcome.json"
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    preexisting = {"passed": False, "note": "written by an operator"}
    outcome_path.write_text(json.dumps(preexisting), encoding="utf-8")

    _sweep(home, db, tmp_path)
    assert json.loads(outcome_path.read_text(encoding="utf-8")) == preexisting


def test_r7_supersede_is_compare_and_set(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT, status="published")
    for epic_id in CHILD_IDS:
        _insert_epic(db, epic_id, "done")
    _waiting_run(home, PARENT, CHILD_IDS)

    actions = _sweep(home, db, tmp_path)
    assert not [a for a in actions if a.get("action") == "split_done"]
    assert _rows(db, "SELECT status FROM task_runs WHERE id=?",
                 (PARENT,))[0][0] == "published"


def test_r7_failed_child_gives_up_and_notifies_once(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT)
    _insert_epic(db, CHILD_IDS[0], "done")
    _insert_epic(db, CHILD_IDS[1], "escalated")
    run_dir = _waiting_run(home, PARENT, CHILD_IDS)

    actions = _sweep(home, db, tmp_path)
    assert any(a.get("action") == "split_failed" and a.get("run_id") == PARENT
               for a in actions), actions

    repair = json.loads((run_dir / "repair.json").read_text(encoding="utf-8"))
    assert repair["state"] == "gave_up"
    assert CHILD_IDS[1] in str(repair.get("note", ""))
    outcome = json.loads(
        (run_dir / "graph-patches" / "001.outcome.json").read_text(
            encoding="utf-8"))
    assert outcome["passed"] is False
    assert _oversight_count(db, PARENT) == 1
    assert _rows(db, "SELECT status FROM task_runs WHERE id=?",
                 (PARENT,))[0][0] == "failed"

    # A second sweep adds neither a second action nor a second oversight row.
    second = _sweep(home, db, tmp_path)
    assert not _split_actions(second)
    assert _oversight_count(db, PARENT) == 1


def test_r7_children_in_progress_do_nothing(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT)
    _insert_epic(db, CHILD_IDS[0], "done")
    _insert_epic(db, CHILD_IDS[1], "in progress")
    run_dir = _waiting_run(home, PARENT, CHILD_IDS)
    before = json.loads((run_dir / "repair.json").read_text(encoding="utf-8"))

    actions = _sweep(home, db, tmp_path)
    assert _split_actions(actions) == []
    assert json.loads((run_dir / "repair.json").read_text(
        encoding="utf-8")) == before
    assert not (run_dir / "graph-patches" / "001.outcome.json").is_file()
    assert _oversight_count(db, PARENT) == 0


def test_r7_dry_run_reports_same_actions_without_writes(tmp_path, monkeypatch):
    home, _run_dir, db = _make_home(tmp_path, monkeypatch)
    _insert_task_run(db, PARENT)
    for epic_id in CHILD_IDS:
        _insert_epic(db, epic_id, "done")
    done_dir = _waiting_run(home, PARENT, CHILD_IDS)
    done_before = json.loads(
        (done_dir / "repair.json").read_text(encoding="utf-8"))

    other = "run-sde6b-other"
    _insert_task_run(db, other)
    other_ids = [f"split-{other}-001-1", f"split-{other}-001-2"]
    _insert_epic(db, other_ids[0], "done")
    _insert_epic(db, other_ids[1], "escalated")
    failed_dir = _waiting_run(home, other, other_ids)
    failed_before = json.loads(
        (failed_dir / "repair.json").read_text(encoding="utf-8"))

    actions = _sweep(home, db, tmp_path, dry_run=True)
    kinds = {(a.get("run_id"), a.get("action")) for a in actions}
    assert (PARENT, "split_done") in kinds
    assert (other, "split_failed") in kinds

    assert json.loads((done_dir / "repair.json").read_text(
        encoding="utf-8")) == done_before
    assert json.loads((failed_dir / "repair.json").read_text(
        encoding="utf-8")) == failed_before
    for run_id, run_dir in ((PARENT, done_dir), (other, failed_dir)):
        assert _rows(db, "SELECT status FROM task_runs WHERE id=?",
                     (run_id,))[0][0] == "failed"
        assert not (run_dir / "graph-patches" / "001.outcome.json").is_file()
        assert _oversight_count(db, run_id) == 0


# ── docs ────────────────────────────────────────────────────────────────────


def test_self_driving_docs_document_split_and_r7():
    text = (_repo_root() / "docs" / "operator" / "self-driving.md"
            ).read_text(encoding="utf-8")
    assert "R7" in text
    assert "split" in text.lower()
