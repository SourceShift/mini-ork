"""The publisher ships only what was verified, and records APPROVE (RB-02/RB-14/RB-28).

Seven clauses from kickoff ``rbf-publish-truth.md``:

- a recipe ``verdict.json`` of ``pass: false`` outvotes ``fail_count == 0`` in
  the run-level verdict writer (``_emit_run_verdict``);
- the publisher refuses before any commit when the recipe verdict says
  ``pass: false``, or when a gating ``verifier_<stem>.json`` envelope does
  (even with an approving reviewer), writing ``publish-abstain.json`` and
  ending ``failed``;
- a non-gating (``gating: false``) failure does not block;
- a reviewer-less workflow treats every passing verifier envelope as approval;
  a failing one ends ``failed`` with a note and commits nothing;
- the delegated-to-children path still finalizes as before;
- a real publish writes ``status='published'`` AND ``verdict='APPROVE'``.

Hermetic: tmp git repos, tmp sqlite DBs, tmp recipe roots. No network, no live
home, no provider credentials.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RUN_ID = "run-publish-truth-1"

_IMPL = "  - name: implementer\n    type: implementer\n    model_lane: implementer\n"
_VERIFIER = "  - name: docs_verifier\n    type: verifier\n    model_lane: verifier\n    verifier_ref: verifiers/docs.py\n"
_REVIEWER = "  - name: reviewer\n    type: reviewer\n    model_lane: reviewer\n"
_PUBLISHER = "  - name: publisher\n    type: publisher\n    model_lane: publisher\n"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Strip ambient run/git identity; pin the orthogonal gates off."""
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_", "GIT_")):
            monkeypatch.delenv(key, raising=False)
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    monkeypatch.setenv("MO_KICKOFF_GUARD", "off")
    monkeypatch.setenv("MO_PROBE_VALIDITY", "0")
    monkeypatch.setenv("MO_EVIDENCE_LEDGER", "0")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_json(path, payload):
    Path(path).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "notes.txt").write_text("v1 baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "baseline")
    (path / "notes.txt").write_text("v2 implementer edit\n", encoding="utf-8")
    return path


def _make_db(path: Path, delegated: bool = False) -> Path:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE task_runs (
            id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,
            workflow_version TEXT, kickoff_path TEXT, plan_path TEXT,
            plan_hash TEXT, artifact_path TEXT, artifact_hash TEXT,
            status TEXT, verdict TEXT, cost_usd REAL, duration_ms INTEGER,
            trace_id TEXT, created_at INTEGER, updated_at INTEGER,
            ended_at INTEGER, parent_epic_id TEXT, notes TEXT
        );
        CREATE TABLE run_spawns (
            spawn_id TEXT PRIMARY KEY, parent_run_id TEXT, child_run_id TEXT,
            root_run_id TEXT, depth INTEGER
        );
        """)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, status, created_at, updated_at)"
        " VALUES (?, 'docs', 'docsrecipe', 'executing', ?, ?)", (RUN_ID, now, now))
    if delegated:
        con.execute(
            "INSERT INTO run_spawns VALUES ('s1', ?, 'child-1', ?, 1)", (RUN_ID, RUN_ID))
    con.commit()
    con.close()
    return path


def _make_recipe(root: Path, name: str, *, with_reviewer: bool) -> Path:
    rdir = root / "recipes" / name
    rdir.mkdir(parents=True)
    nodes = _IMPL + _VERIFIER + (_REVIEWER if with_reviewer else "") + _PUBLISHER
    (rdir / "workflow.yaml").write_text(
        f'task_class: docs\ndescription: fixture\nnodes:\n{nodes}', encoding="utf-8")
    (rdir / "artifact_contract.yaml").write_text(
        "task_class: docs\nsource_artifact: synthesis.md\noutputs: []\n", encoding="utf-8")
    return rdir


def _make_run_dir(path: Path, repo: Path, recipe: str, *, with_reviewer: bool) -> Path:
    path.mkdir(parents=True)
    _write_json(path / "run_profile.json", {
        "run_id": RUN_ID, "recipe": recipe,
        "roots": {"target": str(repo), "run_dir": str(path),
                  "home": str(path.parent), "engine": str(path.parent)},
    })
    decomposition = [{"id": "implementer", "node_type": "implementer"},
                     {"id": "docs_verifier", "node_type": "verifier"}]
    if with_reviewer:
        decomposition.append({"id": "reviewer", "node_type": "reviewer"})
    decomposition.append({"id": "publisher", "node_type": "publisher"})
    _write_json(path / "plan.json", {"objective": "t", "decomposition": decomposition})
    return path


@pytest.fixture
def scenario(tmp_path):
    def _build(*, with_reviewer=False, delegated=False):
        base = tmp_path / f"case{len(list(tmp_path.iterdir()))}"
        base.mkdir()
        root = base / "root"
        root.mkdir()
        recipe = "codedrecipe" if with_reviewer else "docsrecipe"
        _make_recipe(root, recipe, with_reviewer=with_reviewer)
        repo = _make_repo(base / "repo")
        db = _make_db(base / "state.db", delegated=delegated)
        run_dir = _make_run_dir(base / "run", repo, recipe, with_reviewer=with_reviewer)
        return SimpleNamespace(root=root, repo=repo, db=db, run_dir=run_dir, recipe=recipe)
    return _build


def _verifier(run_dir, stem, payload, warning=False):
    lines = (["[warn] verifier stderr noise"] if warning else []) + [json.dumps(payload)]
    (Path(run_dir) / f"verifier_{stem}.json").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT status, verdict, notes FROM task_runs WHERE id = ?", (RUN_ID,)).fetchone()
    finally:
        con.close()


def _abstain(run_dir):
    p = Path(run_dir) / "publish-abstain.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _publish(s):
    from mini_ork.cli.publisher import publisher_node
    return publisher_node(str(s.root), str(s.run_dir), str(s.db), RUN_ID, s.recipe, "docs")


# ── run-level verdict ───────────────────────────────────────────────────────

def test_recipe_verdict_false_outvotes_zero_fail_count(tmp_path):
    from mini_ork.cli.execute import _emit_run_verdict

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(run_dir / "verdict.json", {"pass": False, "files_changed": 2})

    _emit_run_verdict(str(run_dir), 0, 3)

    text = (run_dir / "run-verdict.json").read_text(encoding="utf-8")
    assert json.loads(text)["verdict"] == "fail"
    assert "recipe verdict.json pass=false" in text
    # The recipe's own evidence stays intact.
    assert json.loads((run_dir / "verdict.json").read_text())["pass"] is False


def test_recipe_verdict_pass_true_stays_pass(tmp_path):
    from mini_ork.cli.execute import _emit_run_verdict

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(run_dir / "verdict.json", {"pass": True, "files_changed": 2})

    _emit_run_verdict(str(run_dir), 0, 3)

    text = (run_dir / "run-verdict.json").read_text(encoding="utf-8")
    assert json.loads(text)["verdict"] == "pass"
    assert "recipe verdict.json pass=false" not in text


# ── publisher precondition ──────────────────────────────────────────────────

def test_recipe_verdict_false_blocks_approving_reviewer(scenario, capsys):
    s = scenario(with_reviewer=True)
    _write_json(s.run_dir / "review-verdict.json", {"verdict": "APPROVE", "pass": True})
    _write_json(s.run_dir / "verdict.json", {"pass": False, "files_changed": 1})
    _verifier(s.run_dir, "docs", {"pass": True, "gating": True})
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})
    head_before = _head(s.repo)

    _publish(s)

    assert "[skip-publish] recipe verdict.json pass=false" in capsys.readouterr().out
    assert _head(s.repo) == head_before
    assert _abstain(s.run_dir) is not None
    status, verdict, _notes = _row(s.db)
    assert status == "failed"
    assert verdict is None


def test_failing_gating_verifier_blocks_approving_reviewer(scenario, capsys):
    s = scenario(with_reviewer=True)
    _write_json(s.run_dir / "review-verdict.json", {"verdict": "APPROVE", "pass": True})
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 1})
    _verifier(s.run_dir, "test", {"pass": False, "gating": True}, warning=True)
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})
    head_before = _head(s.repo)

    _publish(s)

    assert "[skip-publish] verifier test pass=false" in capsys.readouterr().out
    assert _head(s.repo) == head_before
    assert _abstain(s.run_dir) is not None
    assert _row(s.db)[0] == "failed"


def test_non_gating_verifier_failure_does_not_block(scenario):
    s = scenario(with_reviewer=True)
    _write_json(s.run_dir / "review-verdict.json", {"verdict": "APPROVE", "pass": True})
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 1})
    _verifier(s.run_dir, "shape", {"pass": False, "gating": False})
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})
    head_before = _head(s.repo)

    rc, finish = _publish(s)

    assert (rc, finish) == (0, "done")
    assert _head(s.repo) != head_before
    assert _abstain(s.run_dir) is None
    assert _row(s.db)[0] == "published"


# ── reviewer-less recipes ───────────────────────────────────────────────────

def test_reviewer_less_all_verifiers_pass_commits_and_publishes(scenario):
    s = scenario(with_reviewer=False)
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 1})
    _verifier(s.run_dir, "docs", {"pass": True, "gating": True})
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})
    head_before = _head(s.repo)

    rc, finish = _publish(s)

    assert (rc, finish) == (0, "done")
    assert _head(s.repo) != head_before
    assert "v2 implementer edit" in _git(s.repo, "show", "HEAD:notes.txt").stdout
    assert _row(s.db)[0] == "published"


def test_reviewer_less_failing_verifier_fails_with_note(scenario):
    s = scenario(with_reviewer=False)
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 1})
    # gating omitted: an envelope that does not declare gating:false is gating.
    _verifier(s.run_dir, "docs", {"pass": False})
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})
    head_before = _head(s.repo)

    _publish(s)

    status, _verdict, notes = _row(s.db)
    assert status == "failed"
    assert notes
    assert _head(s.repo) == head_before
    assert _abstain(s.run_dir) is not None


def test_delegated_to_children_still_finalizes(scenario):
    s = scenario(with_reviewer=False, delegated=True)
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 0})
    _verifier(s.run_dir, "docs", {"pass": True, "gating": True})
    head_before = _head(s.repo)

    rc, finish = _publish(s)

    assert (rc, finish) == (0, "done")
    status, _verdict, notes = _row(s.db)
    assert status == "published"
    assert notes
    assert _head(s.repo) == head_before


# ── RB-28: a real publish records APPROVE ───────────────────────────────────

def test_successful_publish_records_approve(scenario):
    s = scenario(with_reviewer=False)
    _write_json(s.run_dir / "verdict.json", {"pass": True, "files_changed": 1})
    _verifier(s.run_dir, "docs", {"pass": True, "gating": True})
    _write_json(s.run_dir / "implementer-summary.json", {"files_changed": ["notes.txt"]})

    rc, _finish = _publish(s)

    assert rc == 0
    status, verdict, _notes = _row(s.db)
    assert status == "published"
    assert verdict == "APPROVE"

    from mini_ork.orchestration.active_state_index import render_active_state_block
    assert RUN_ID in render_active_state_block(db_path=str(s.db))
