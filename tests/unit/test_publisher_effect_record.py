"""The publisher's effect record: mark before the effect, close at node end.

``_handle_publisher`` writes a ``pending`` record immediately before
``publisher_node`` runs and refuses to run the effect at all when the record
cannot be written. ``_cp`` — the single node-end bookkeeping path — flips the
record to ``done`` on success and leaves it ``pending`` on every other
outcome. The publisher takes the ``append_node_attempt`` branch (it writes no
``node_checkpoints`` row), which is why the close sits before the
success/output-file split in ``_cp``.

Self-contained: a tmp DB with the 0050 node_attempts / node_checkpoints shape
and a tmp run dir; no repo state, no network, no dispatch.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

RUN_ID = "run-effect-1"
RECIPE = "test-recipe"
TASK_CLASS = "framework_edit"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL, recipe_version TEXT NOT NULL,
    config_hash TEXT NOT NULL, artifact_manifest_json TEXT NOT NULL,
    session_ref TEXT, failure_class TEXT, created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL, node_type TEXT,
    started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
    result TEXT NOT NULL, failure_class TEXT,
    checkpoint_used INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL, provider_session_id TEXT, initiator TEXT
);
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
                "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE", "MINI_ORK_TASK_CLASS",
                "MINI_ORK_LEASE_TOKEN", "MINI_ORK_LEASE_RUN_ID",
                "MO_NODE_FINISH_REASON", "MO_NODE_GIT_CKPT",
                "MINI_ORK_RECOVERY_CLOSURE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    con = sqlite3.connect(str(path))
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    return str(d)


def _mk_ctx(run_dir_eff, tmp_path, node_id="pub1", node_type="publisher"):
    from mini_ork.cli.execute_handlers import NodeDispatch
    return NodeDispatch(
        node_id=node_id, node_type=node_type, node_desc="publish",
        prompt_ref="", verifier_ref="", model_lane="claude",
        node_requires_capabilities="", root=str(tmp_path),
        run_dir=run_dir_eff, plan_path="", task_class=TASK_CLASS,
        db="", run_id=RUN_ID, recipe=RECIPE, workflow="", lane="claude",
        run_dir_eff=run_dir_eff, recipe_dir="", prompt_file="",
        plan_content="", learned="",
        dispatch_fn=lambda *a, **k: "",
        trace=lambda *a, **k: None,
        charge=lambda *a, **k: None)


def _record_state(run_dir, node_id):
    from mini_ork.stores.checkpoints import effect_state
    return effect_state(run_dir, node_id)


# ── _handle_publisher: mark before the effect, refuse when it cannot ───────


def test_marks_pending_before_publisher_node(tmp_path, monkeypatch):
    from mini_ork.cli import execute_handlers as eh
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)
    seen = {}

    def fake_publisher(*args, **kwargs):
        seen["state_at_dispatch"] = _record_state(run_dir, "pub1")
        seen["calls"] = seen.get("calls", 0) + 1
        return 0, "done"

    monkeypatch.setattr(eh, "publisher_node", fake_publisher)
    rc, _finish = eh._handle_publisher(_mk_ctx(run_dir, tmp_path))
    assert rc == 0
    assert seen["calls"] == 1
    # on disk as pending BEFORE publisher_node ran, and the handler leaves it
    # open — closing is _cp's job at node end
    assert seen["state_at_dispatch"] == "pending"
    assert _record_state(run_dir, "pub1") == "pending"


def test_marker_failure_blocks_publisher_node(tmp_path, monkeypatch):
    from mini_ork.cli import execute_handlers as eh
    calls = []
    monkeypatch.setattr(eh, "publisher_node",
                        lambda *a, **k: calls.append(1) or (0, "done"))
    missing = str(tmp_path / "no-such-run-dir")
    rc, finish_reason = eh._handle_publisher(
        _mk_ctx(missing, tmp_path))
    assert rc != 0
    assert finish_reason == "effect_marker_failed"
    assert calls == []  # an effect that cannot be recorded is not performed


def test_failing_or_raising_publisher_leaves_record_pending(tmp_path,
                                                            monkeypatch):
    from mini_ork.cli import execute_handlers as eh
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)

    monkeypatch.setattr(eh, "publisher_node",
                        lambda *a, **k: (1, "provider_error"))
    rc, _finish = eh._handle_publisher(_mk_ctx(run_dir, tmp_path))
    assert rc != 0
    assert _record_state(run_dir, "pub1") == "pending"

    def boom(*args, **kwargs):
        raise RuntimeError("publisher exploded")

    monkeypatch.setattr(eh, "publisher_node", boom)
    with pytest.raises(RuntimeError):
        eh._handle_publisher(_mk_ctx(run_dir, tmp_path))
    assert _record_state(run_dir, "pub1") == "pending"


# ── _cp: close on success in both branches, leave pending otherwise ────────


def _cp_for(db, run_dir):
    from mini_ork.cli.execute import _make_checkpoint_fn
    return _make_checkpoint_fn(db, RUN_ID, run_dir, RECIPE, TASK_CLASS)


def test_closes_record_on_append_branch(db, run_dir):
    # no output_file is the publisher's real shape: append_node_attempt
    from mini_ork.stores.checkpoints import mark_effect_pending
    mark_effect_pending(run_dir, "pub1", "publish")
    _cp_for(db, run_dir)("pub1", "success", "publisher", "")
    assert _record_state(run_dir, "pub1") == "done"


def test_closes_record_on_checkpoint_branch(db, run_dir):
    from mini_ork.stores.checkpoints import mark_effect_pending
    out = os.path.join(run_dir, "out.md")
    with open(out, "w") as fh:
        fh.write("artifact bytes")
    mark_effect_pending(run_dir, "rev1", "review")
    _cp_for(db, run_dir)("rev1", "success", "reviewer", out)
    assert _record_state(run_dir, "rev1") == "done"


@pytest.mark.parametrize("status", ["failure", "error", "skipped"])
def test_leaves_record_pending_on_non_success(db, run_dir, status):
    from mini_ork.stores.checkpoints import mark_effect_pending
    mark_effect_pending(run_dir, "pub1", "publish")
    _cp_for(db, run_dir)("pub1", status, "publisher", "")
    assert _record_state(run_dir, "pub1") == "pending"


def test_success_without_record_creates_nothing(db, run_dir):
    _cp_for(db, run_dir)("ghost", "success", "implementer", "")
    assert not os.path.exists(os.path.join(run_dir, ".effects", "ghost.json"))


def test_closed_record_body_carries_ended_at(db, run_dir):
    from mini_ork.stores.checkpoints import mark_effect_pending
    mark_effect_pending(run_dir, "pub1", "publish")
    _cp_for(db, run_dir)("pub1", "success", "publisher", "")
    with open(os.path.join(run_dir, ".effects", "pub1.json")) as fh:
        record = json.load(fh)
    assert record["state"] == "done"
    assert isinstance(record["ended_at"], int)
