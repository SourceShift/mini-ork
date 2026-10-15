"""``node`` / ``run`` / ``inbox`` review buttons — kickoff r-ide-review.

Hermetic per-test: an autouse fixture points ``$MINI_ORK_HOME`` at ``tmp_path``
and scrubs the env keys the loader and the gate read (same discipline as
``test_node_review.py``, so an existing installer-level value never bleeds in).
The ``state.db`` is bootstrapped per-test with the canonical migrations so
every ``db_for(...)`` read sees the tables the helpers reach for.

Every ``DoD`` from the kickoff maps to one ``test_*`` here. Builds go through
the public ``build_node(home, run_id, node_id)`` and ``build(home, tab, args)``
entry points — the same calls the IDE / CLI make — so the page contract
cannot drift from the call site the test exercises.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import merge_approval  # noqa: E402
from mini_ork.gates import node_review  # noqa: E402
from mini_ork.ide_pages import build_page  # noqa: E402
from mini_ork.ide_pages.node import build_node  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

RUN = "run-1791000000-abc123"
T0 = 1_791_000_000

_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_TARGET_CWD")


# ── schema helpers (matches ``test_node_review.py``) ─────────────────────

_CHECKPOINTS_DDL = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT    NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash            TEXT    NOT NULL,
    recipe_version        TEXT    NOT NULL,
    config_hash           TEXT    NOT NULL,
    artifact_manifest_json TEXT   NOT NULL,
    session_ref           TEXT,
    failure_class         TEXT,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
"""

_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,
    node_type             TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class         TEXT,
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,
    provider_session_id   TEXT,
    initiator             TEXT
);
"""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Pin the home/DB to ``tmp_path``; scrub every key the code reads."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def home(tmp_path) -> Path:
    """A freshly-initialized mini-ork home; the canonical migrations have run."""
    h = tmp_path / ".mini-ork"
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    (h / "recipes").mkdir(exist_ok=True)
    return h


def _bootstrap_node_tables(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_CHECKPOINTS_DDL + _ATTEMPTS_DDL)
        conn.commit()
    finally:
        conn.close()


def _seed_run(home: Path, *, status: str = "published") -> Path:
    """A run directory with the recipe + DB row the IDE expects."""
    recipe = "demo-recipe"
    recipe_dir = home / "recipes" / recipe
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "workflow.yaml").write_text(_WORKFLOW)
    (recipe_dir / "task_class.yaml").write_text("name: demo\ndescription: demo\n")

    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / f"{RUN}.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Demo\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, recipe, status, 0.0, _iso(T0), _iso(T0 + 60), _iso(T0 + 60),
         "demo", str(kickoff), "latest", "tr-demo-1"),
    )
    events = [
        ("node_start", "implementer", "implementer", "worker", T0 + 10, None),
        ("node_end", "implementer", "implementer", "worker", T0 + 40, "done"),
        ("node_start", "test", "verifier", "verifier", T0 + 40, None),
        ("node_end", "test", "verifier", "verifier", T0 + 50, "done"),
    ]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"ev-{i}", RUN, kind, json.dumps(payload), ts),
        )
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text(json.dumps({
        "objective": "demo",
        "decomposition": [
            {"id": "implementer", "node_type": "implementer"},
            {"id": "test", "node_type": "verifier"},
        ],
    }))
    return run_dir


# Workflow with two completed nodes so the inbox / node tests can pick any.
_WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: test, type: verifier, verifier_ref: verifiers/test.py}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test, edge_type: verifies}
"""


# ── helpers ──────────────────────────────────────────────────────────────


def _actions_for(home: Path, run_id: str, node_id: str) -> list[dict]:
    """Pull the ``acts`` list out of a built node page."""
    page = build_node(home, run_id, node_id)
    return list(page.get("acts") or [])


def _first_action_with_label(actions: list[dict], label: str) -> dict | None:
    for action in actions:
        if str(action.get("label") or "") == label:
            return action
    return None


def _build_with_worktree(home: Path, run_dir: Path) -> Path:
    """Make a scratch git worktree with one initial commit; return its root.

    The worktree is what ``mini-ork workspaces`` would have created for a
    live run — checkpoint refs land in ``refs/mini-ork/ckpt/...`` there.
    """
    wt = home / "wt"
    wt.mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(wt), "init", "--initial-branch=main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (wt / "README").write_text("init")
    subprocess.run(["git", "-C", str(wt), "add", "README"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"],
                   check=True, capture_output=True)
    record_dir = home / "worktrees"
    record_dir.mkdir(exist_ok=True)
    (record_dir / f"{RUN}.json").write_text(json.dumps({
        "run_id": RUN,
        "path": str(wt),
        "branch": "wt/demo",
        "base_branch": "main",
        "base_sha": "deadbeef",
        "project": str(wt),
        "home": str(home),
        "adopted": False,
        "clean_at_start": True,
    }))
    return wt


# ── DoD 1: Restore button (ckpt ref present / absent) ───────────────────


def test_restore_button_shown_when_ckpt_ref_exists(home: Path) -> None:
    """A finished node with a ckpt ref → Restore button in the node acts."""
    run_dir = _seed_run(home, status="published")
    wt = _build_with_worktree(home, run_dir)
    # Snapshot on top of the worktree's real history so the ref resolves.
    sha = node_checkpoint_snapshot(home, run_dir, wt, RUN, "implementer")
    assert sha, "snapshot must succeed for a real git worktree"
    # Sanity: the ref now resolves.
    ref = f"refs/mini-ork/ckpt/{RUN}/implementer"
    out = subprocess.run(["git", "-C", str(wt), "show-ref", "--verify", ref],
                         capture_output=True, text=True)
    assert out.returncode == 0, "ckpt ref must exist after snapshot"

    actions = _actions_for(home, RUN, "implementer")
    btn = _first_action_with_label(actions, "Restore to after this node")
    assert btn is not None, f"missing Restore button; actions={actions!r}"
    assert btn.get("kind") == "warn"
    cli = (btn.get("do") or {}).get("cli") or []
    assert cli[:4] == ["board", "restore", RUN, "implementer"]
    assert btn["do"].get("confirm"), "Restore button must carry a confirm"


def test_restore_button_absent_when_no_ckpt_ref(home: Path) -> None:
    """A finished node with no ckpt ref → no Restore button."""
    _seed_run(home, status="published")
    _build_with_worktree(home, home / "runs" / RUN)
    # No snapshot written: nothing under refs/mini-ork/ckpt.
    actions = _actions_for(home, RUN, "implementer")
    assert _first_action_with_label(actions, "Restore to after this node") is None


def node_checkpoint_snapshot(home, run_dir, wt, run_id, node_id):
    """Tiny helper that wraps ``vcs.node_checkpoint.snapshot`` for the test.

    Lives here (not in the file-level imports) because the test fixture's
    autouse fixture is what pins ``MINI_ORK_HOME`` — keeping the import
    local lets the helper run from inside the test body after the env is
    set up.
    """
    from mini_ork.vcs import node_checkpoint
    return node_checkpoint.snapshot(wt, run_id, node_id)


# ── DoD 2: Node-review buttons (Approve / Request changes…) ────────────


def _stage_pending_node_review(home: Path, run_id: str = RUN) -> None:
    """Park the run via ``node_review.decide`` so the inbox row is real.

    The kickoff's DoD 2 says the pending row comes from the same code path
    the dispatcher uses. Insert a ``node_checkpoints`` success row first,
    then call ``decide`` with ``MO_REVIEW_AFTER_NODES=implementer``; the
    policy matches the implementer, so ``decide`` parks the run and enqueues
    the ``review.after_node`` row in one atomic transaction. Tests that
    later just want the inbox to have a row use this helper — never
    ``oversight_inbox.enqueue`` directly, because that bypasses the policy.
    """
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO node_checkpoints "
            "(run_id, node_id, attempt, status, input_hash, recipe_version, "
            " config_hash, artifact_manifest_json, session_ref, failure_class, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "implementer", 1, "success", "h1", "v1", "c1", "[]",
             None, None, T0 + 40),
        )
        con.commit()
    finally:
        con.close()
    node_review.decide(
        run_id, "reviewer",
        db_path=db_path,
        run_dir=str(home / "runs" / run_id),
        env={"MO_REVIEW_AFTER_NODES": "implementer"},
    )


def test_node_review_buttons_when_pending(home: Path) -> None:
    """A pending node-review row → Approve + Request changes… in the node acts.

    The pending row is created the same way the dispatcher does: insert a
    ``node_checkpoints`` success row for the node, then ``node_review.decide``
    with ``MO_REVIEW_AFTER_NODES=implementer`` — which parks the run and writes
    the inbox row in one atomic transaction. Going through ``enqueue`` would
    test the inbox INSERT but not the actual ``review.after_node`` flow.
    """
    _seed_run(home, status="executing")
    _bootstrap_node_tables(str(home / "state.db"))
    _stage_pending_node_review(home)

    actions = _actions_for(home, RUN, "implementer")

    approve = _first_action_with_label(actions, "Approve")
    assert approve is not None, f"missing Approve button; actions={actions!r}"
    assert approve.get("kind") == "primary"
    cli = (approve.get("do") or {}).get("cli") or []
    assert cli[:5] == ["approvals", "approve", RUN, "--node", "implementer"], \
        f"unexpected cli argv: {cli!r}"
    assert approve["do"].get("home") is False, "approvals does not accept --home"
    assert approve["do"].get("confirm"), "Approve button must carry a confirm"

    reqc = _first_action_with_label(actions, "Request changes…")
    assert reqc is not None, f"missing Request changes… button; actions={actions!r}"
    assert reqc.get("kind") == "default"
    thread = (reqc.get("do") or {}).get("thread") or ""
    assert thread.startswith(f"/request-changes {RUN} implementer")


# ── DoD 3: Parked run → "Waiting for your review" section ──────────────


def _build_parked_run(home: Path) -> None:
    """Mark the seeded run as parked (status=reviewing + .review-pause)."""
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET status = ? WHERE id = ?", ("reviewing", RUN))
    con.commit()
    con.close()
    run_dir = home / "runs" / RUN
    (run_dir / ".review-pause").write_text("reviewing for tests\n")


def test_parked_run_section_appears(home: Path) -> None:
    """A reviewing run with .review-pause → 'Waiting for your review' section."""
    _seed_run(home, status="reviewing")
    _bootstrap_node_tables(str(home / "state.db"))
    _build_parked_run(home)
    _stage_pending_node_review(home)

    page = build_page(home, "run", "dag", {"run": RUN})
    titles = [str(s.get("title") or "") for s in page.get("sections") or []]
    assert "Waiting for your review" in titles, \
        f"missing parked-run section; sections={titles!r}"
    section = next(s for s in page["sections"]
                   if str(s.get("title") or "") == "Waiting for your review")
    actions = list(section.get("actions") or [])
    labels = [str(a.get("label") or "") for a in actions]
    assert "Approve" in labels and "Request changes…" in labels, \
        f"missing review buttons on parked run; actions={actions!r}"


# ── DoD 4: Merge review ────────────────────────────────────────────────


def test_merge_review_appears_when_policy_required(home: Path, monkeypatch) -> None:
    """``before_merge=required`` + open workspace → 'Merge review' + Approve."""
    run_dir = _seed_run(home, status="published")
    _build_with_worktree(home, run_dir)
    # Resolve policy through env so ``load_policy`` returns required.
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")

    page = build_page(home, "run", "dag", {"run": RUN})
    titles = [str(s.get("title") or "") for s in page.get("sections") or []]
    assert "Merge review" in titles, \
        f"missing Merge review section; sections={titles!r}"
    section = next(s for s in page["sections"]
                   if str(s.get("title") or "") == "Merge review")
    actions = list(section.get("actions") or [])
    labels = [str(a.get("label") or "") for a in actions]
    assert "Approve merge" in labels, \
        f"missing Approve merge button; actions={actions!r}"
    btn = next(a for a in actions if str(a.get("label") or "") == "Approve merge")
    cli = (btn.get("do") or {}).get("cli") or []
    assert cli[:2] == ["approvals", "approve"], f"unexpected cli: {cli!r}"
    assert btn["do"].get("home") is False


def test_merge_review_drops_button_after_approval(home: Path, monkeypatch) -> None:
    """After ``merge_approval.approve(...)`` the section stays but the
    Approve button is gone, and the state shows as approved."""
    run_dir = _seed_run(home, status="published")
    _build_with_worktree(home, run_dir)
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")

    # Reach in and request + approve a merge row directly. The dispatcher's
    # path can't run inside the test (it wants a real ``git write-tree``),
    # so ``merge_approval.request`` + ``merge_approval.approve`` is the
    # closest seam that the page also reads.
    db_path = str(home / "state.db")
    merge_approval.request(
        RUN, "deadbeef", "deadbeef",
        branch="wt/demo", base_branch="main",
        db_path=db_path,
    )
    merge_approval.approve(
        RUN, "deadbeef", base_sha="deadbeef",
        approver="alice:tester", note="test",
        db_path=db_path, env={},
    )

    page = build_page(home, "run", "dag", {"run": RUN})
    titles = [str(s.get("title") or "") for s in page.get("sections") or []]
    assert "Merge review" in titles, "section must stay once approved"
    section = next(s for s in page["sections"]
                   if str(s.get("title") or "") == "Merge review")
    actions = list(section.get("actions") or [])
    labels = [str(a.get("label") or "") for a in actions]
    assert "Approve merge" not in labels, \
        f"Approve merge must disappear after approval; actions={actions!r}"
    rendered = json.dumps(section)
    assert "approved" in rendered.lower(), \
        f"approved state must show in section text; got {rendered!r}"


# ── DoD 5: Inbox awaiting-review list ───────────────────────────────────


def test_inbox_awaiting_review_lists_pending_node(home: Path) -> None:
    """The inbox surfaces the pending node row under 'Awaiting review'."""
    _seed_run(home, status="executing")
    _bootstrap_node_tables(str(home / "state.db"))
    _stage_pending_node_review(home)

    page = build_page(home, "inbox", None, {})
    sections = page.get("sections") or []
    titles = [str(s.get("title") or "") for s in sections]
    assert "Awaiting review" in titles, \
        f"missing Awaiting review section; titles={titles!r}"
    section = next(s for s in sections
                   if str(s.get("title") or "") == "Awaiting review")
    items = list(section.get("items") or [])
    assert items, "Awaiting review section must have at least one row"
    blob = json.dumps(items)
    assert "implementer" in blob, f"node id missing from awaiting row: {blob!r}"
    assert "node" in blob.lower(), f"kind missing from row label: {blob!r}"


def test_inbox_without_db_raises_no_exception(tmp_path, monkeypatch) -> None:
    """A home with no ``state.db`` file → no Awaiting review section, no exception.

    Distinct from the empty-DB case: the older ``test_inbox_without_db_*`` ran
    against the fixture DB that ``mig.init_db`` had already set up. The DoD 5
    contract is "missing DB means no section, no side effect" — so the DB file
    must not even exist when the page builds. Use a fresh ``tmp_path`` directly
    and skip the ``home`` fixture entirely.
    """
    home = tmp_path / "no-db-home"
    home.mkdir()
    db_path = home / "state.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db_path))

    page = build_page(home, "inbox", None, {})

    titles = [str(s.get("title") or "") for s in page.get("sections") or []]
    assert "Awaiting review" not in titles, \
        f"missing-DB inbox must show no Awaiting review section; titles={titles!r}"
    # And the side effect the lens warned about: opening the page must not
    # create the DB file. ``_connect`` (in ``gates.oversight_inbox``) runs an
    # unprompted ``CREATE TABLE IF NOT EXISTS`` on connect; the guard in
    # ``_awaiting_review_rows`` is what stops it from being called.
    assert not db_path.exists(), \
        f"page build must not create state.db; got {db_path.exists()=}"


def test_inbox_with_empty_db_raises_no_exception(home: Path) -> None:
    """An empty-but-present ``state.db`` (the ``mo_inbox_gates`` table) →
    no Awaiting review section, no exception.

    Distinct from the missing-DB case: ``mig.init_db`` already created the
    table, but no row was enqueued. The page build must neither raise nor
    list a section.
    """
    page = build_page(home, "inbox", None, {})
    titles = [str(s.get("title") or "") for s in page.get("sections") or []]
    assert "Awaiting review" not in titles, \
        f"empty inbox must show no section; titles={titles!r}"


# ── DoD 6 (smoke): existing pages still build ───────────────────────────


def test_existing_pages_still_build(home: Path, monkeypatch) -> None:
    """Building every named page on a parked + reviewed run must not raise.

    This is the safety net: the kickoff forbids touching anything outside
    the 4-file scope, but a refactor here could still break a downstream
    tab. Building each tab twice (level 1 / level 2) catches the contract
    drift without spreading the test suite.
    """
    run_dir = _seed_run(home, status="reviewing")
    _bootstrap_node_tables(str(home / "state.db"))
    _build_parked_run(home)
    _build_with_worktree(home, run_dir)
    _stage_pending_node_review(home)

    # Spec level 1 — explicit unset via monkeypatch.delenv (autouse covers
    # any installer leak, this asserts the unset state).
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    build_page(home, "run", None, {"run": RUN})
    build_page(home, "inbox", None, {})
    # Spec level 2.
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    for tab in ("story", "graph", "kickoff", "agents", "learnings", "artifacts"):
        build_page(home, "run", tab, {"run": RUN})
    build_page(home, "inbox", None, {})
