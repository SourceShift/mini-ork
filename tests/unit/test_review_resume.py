"""End-to-end tests for the parked-run resume loop (kickoff hr-review-resume).

Covers six behaviours:

  1. Approving the LAST pending node row spawns ``recover --strategy resume``
     detached, renames ``.review-pause`` → ``.review-pause.resolved``.
  2. Approving while other rows are still pending: NO spawn, sentinel kept.
  3. ``--no-resume`` prints the manual command and skips the spawn.
  4. ``request-changes`` emits an operator_steering row + spawns
     ``recover --from-node <node> --strategy retry``.
  5. The planner refuses ``recover`` on a ``reviewing`` run with pending rows
     (rc 1 + the awaiting-review stderr), and ``--force`` bypasses it.
  6. A ``reviewing`` run with nothing pending is NOT refused by the
     retry-hint gate (no "retry-hint refuses this run" line).

Hermetic: each test pins ``$MINI_ORK_HOME`` to ``tmp_path`` and scrubs the
``MINI_ORK_RUN_*`` keys so the planner's path-resolution and the node-review
DB lookup both land on the per-test DB. ``_spawn_recover`` is monkeypatched
on the CLI module so no real subprocess is forked.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import node_review  # noqa: E402
from mini_ork.recovery import planner as rp  # noqa: E402

# Same DDL as ``tests/unit/test_node_review.py`` (mirrors
# migration 0050_node_dag_checkpoints.sql). ``mo_inbox_gates`` is
# bootstrapped on demand by ``oversight_inbox._connect``.
_NODE_DDL = """
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
CREATE TABLE IF NOT EXISTS task_runs (
    id            TEXT PRIMARY KEY,
    status        TEXT,
    recipe        TEXT,
    task_class    TEXT,
    kickoff_path  TEXT,
    cost_usd      REAL DEFAULT 0,
    created_at    INTEGER,
    updated_at    INTEGER,
    ended_at      INTEGER,
    duration_ms   INTEGER
);
CREATE TABLE IF NOT EXISTS operator_steering (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    role_target     TEXT NOT NULL CHECK (role_target IN ('planner','implementer','reviewer','verifier','any')),
    severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','critical')),
    message         TEXT NOT NULL,
    source          TEXT,
    confidence      REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
    created_at      INTEGER NOT NULL,
    consumed_at     INTEGER,
    expires_at      INTEGER NOT NULL
);
"""

_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_TARGET_CWD")


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
    return tmp_path


def _bootstrap(db_path: str) -> None:
    """Bring up the DDL the tests touch; mirrors test_node_review's setup.

    ``mo_inbox_gates`` is bootstrapped automatically by ``oversight_inbox._connect``
    on first open (it's the inbox ledger the approval + node-review paths
    read AND write). The node_review fixture relies on this too; calling
    ``_connect`` once here avoids the first INSERT from racing against a
    not-yet-bootstrapped schema.
    """
    con = sqlite3.connect(db_path)
    try:
        con.executescript(_NODE_DDL)
        con.commit()
    finally:
        con.close()
    from mini_ork.gates import oversight_inbox
    oversight_inbox._connect(db_path).close()  # noqa: SLF001 — module seam, intentional


def _seed_checkpoint(db_path: str, run_id: str, node_id: str,
                    node_type: str, *, created_at: int | None = None) -> None:
    """Mark ``(run_id, node_id)`` successful so ``reviewable_completed`` matches."""
    if created_at is None:
        created_at = int(time.time())
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO node_checkpoints ("
            "  run_id, node_id, attempt, status, input_hash, recipe_version,"
            "  config_hash, artifact_manifest_json, session_ref, failure_class,"
            "  created_at) "
            "VALUES (?, ?, 1, 'success', '', '', '', '[]', NULL, NULL, ?)",
            (run_id, node_id, int(created_at)),
        )
        con.execute(
            "INSERT INTO node_attempts ("
            "  run_id, node_id, attempt_no, node_type, started_at, ended_at,"
            "  result, checkpoint_used, checkpoint_produced"
            ") VALUES (?, ?, 1, ?, ?, ?, 'success', 0, 0)",
            (run_id, node_id, node_type, int(created_at), int(created_at)),
        )
        con.commit()
    finally:
        con.close()


def _set_task_status(db_path: str, run_id: str, status: str) -> None:
    """Write/overwrite the ``task_runs.status`` for ``run_id``."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO task_runs (id, status, recipe, task_class) "
            "VALUES (?, ?, 'framework-edit', 'framework_edit')",
            (run_id, status),
        )
        con.commit()
    finally:
        con.close()


def _write_review_pause(home: Path, run_id: str, sentinel_data: dict | None = None) -> Path:
    """Drop a ``.review-pause`` sentinel so the resume path sees a parked run."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    sentinel = run_dir / ".review-pause"
    payload = sentinel_data or {"waiting_node": "implementer", "awaiting": ["implementer"]}
    sentinel.write_text(json.dumps(payload), encoding="utf-8")
    return sentinel


def _pending_review_rows(db_path: str, run_id: str, node_ids: list[str],
                        *, created_ats: list[int] | None = None) -> list[int]:
    """Create one pending ``node:`` inbox row per ``node_id`` for ``run_id``.

    The exact inbox_id sequence lets later assertions match against
    ``pending_for_run`` without depending on ordering; ``created_ats`` lets
    the row keys differ when more than one pending row targets the same
    node id (a re-run produces a new checkpoint row with a newer
    created_at, hence a different ``gate_id``).
    """
    if created_ats is None:
        created_ats = [int(time.time() * 1000) + i for i in range(len(node_ids))]
    inbox_ids: list[int] = []
    con = sqlite3.connect(db_path)
    try:
        for node_id, created_at in zip(node_ids, created_ats):
            gate = f"node:{run_id}:{node_id}:{int(created_at)}"
            ctx = {"run_id": run_id, "node_id": node_id, "node_type": "implementer",
                   "created_at": int(created_at), "ckpt_ref": ""}
            cur = con.execute(
                "INSERT INTO mo_inbox_gates ("
                "  gate_id, feature, phase, context_json, status, enqueued_at,"
                "  blocks_dispatch_for) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (gate, "review.after_node", "implementer",
                 json.dumps(ctx), int(time.time()), run_id),
            )
            inbox_ids.append(int(cur.lastrowid or 0))
        con.commit()
    finally:
        con.close()
    return inbox_ids


def _setup_parked_run(home: Path, run_id: str, *,
                     node_ids: list[str] | None = None) -> tuple[str, list[int]]:
    """Bootstrap a parked run ready for an approve / reject.

    Returns ``(db_path, pending_inbox_ids)``. Sets ``task_runs.status`` to
    ``reviewing`` and writes the ``.review-pause`` sentinel.
    """
    db_path = str(home / "state.db")
    _bootstrap(db_path)
    node_ids = node_ids or ["implementer"]
    for nid in node_ids:
        _seed_checkpoint(db_path, run_id, nid, "implementer")
    inbox_ids = _pending_review_rows(db_path, run_id, node_ids)
    _set_task_status(db_path, run_id, "reviewing")
    _write_review_pause(home, run_id)
    return db_path, inbox_ids


def _stub_spawn(monkeypatch, approvals_module) -> list[tuple[str, list[str], str]]:
    """Replace ``approvals_cmd._spawn_recover`` with a recording stub.

    Returns the list the stub appends to, so the test can assert what was
    spawned. The stub never forks; it just records ``(home_str, extra_args,
    log_path)`` so a test can verify the executor-resume contract end to end,
    and returns ``(0, "<fake-log>")``.
    """
    calls: list[tuple[str, list[str], str]] = []

    def fake_spawn(home, run_id, extra_args, log_path=None):
        calls.append((str(home), list(extra_args), str(log_path) if log_path else ""))
        return 0, "/tmp/fake.log"

    monkeypatch.setattr(approvals_module, "_spawn_recover", fake_spawn)
    return calls


# ── DoD 1: resume on the last approval ───────────────────────────────────


def test_approve_last_pending_spawns_resume(monkeypatch, tmp_path, capsys):
    """One pending row → approve clears it → ``recover --strategy resume``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-resume-1"
    db_path, _inbox = _setup_parked_run(tmp_path, run_id)
    spawns = _stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "implementer"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(spawns) == 1, "exactly one spawn expected"
    _home_str, extra, _log = spawns[0]
    assert extra == ["--strategy", "resume"]
    assert "resumed " in out and run_id in out
    # The sentinel was renamed, not deleted (audit trail).
    assert not (tmp_path / "runs" / run_id / ".review-pause").exists()
    assert (tmp_path / "runs" / run_id / ".review-pause.resolved").exists()


# ── DoD 2: not the last approval ─────────────────────────────────────────


def test_approve_with_other_pending_keeps_sentinel(monkeypatch, tmp_path, capsys):
    """Two pending rows; approving one → no spawn, sentinel kept."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-resume-2"
    db_path, inbox = _setup_parked_run(
        tmp_path, run_id, node_ids=["implementer", "static_check_verifier"]
    )
    spawns = _stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "implementer"])
    out = capsys.readouterr().out
    assert rc == 0
    assert spawns == [], f"no spawn expected while other rows are pending, got {spawns}"
    # Sentinel untouched; the operator still has work to do.
    assert (tmp_path / "runs" / run_id / ".review-pause").exists()
    # The remaining pending row survives.
    pending = node_review.pending_for_run(run_id, db_path=db_path)
    assert len(pending) == 1
    assert pending[0]["context"]["node_id"] == "static_check_verifier"
    assert "approved" in out  # the basic approve line still prints


# ── DoD 3: --no-resume prints the manual command ─────────────────────────


def test_approve_no_resume_prints_command(monkeypatch, tmp_path, capsys):
    """``--no-resume`` skips the spawn and emits the manual resume hint."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-resume-3"
    _db_path, _inbox = _setup_parked_run(tmp_path, run_id)
    spawns = _stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(
        ["approve", run_id, "--node", "implementer", "--no-resume"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert spawns == [], "--no-resume must not spawn"
    assert f"mini-ork recover {run_id} --strategy resume" in out
    # The sentinel is renamed regardless (audit trail matters even when
    # the operator chose to drive the resume by hand).
    assert (tmp_path / "runs" / run_id / ".review-pause.resolved").exists()


# ── DoD 4: request-changes emits steering + spawns retry ─────────────────


def test_request_changes_emits_steering_and_spawns_retry(monkeypatch, tmp_path, capsys):
    """Reject with --node + --note writes an operator_steering row and spawns
    ``recover --from-node <node> --strategy retry`` detached."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-resume-4"
    db_path, _inbox = _setup_parked_run(tmp_path, run_id)
    spawns = _stub_spawn(monkeypatch, approvals_cmd)

    note = "add a test"
    capsys.readouterr()
    rc = approvals_cmd.main(
        ["request-changes", run_id, "--node", "implementer", "--note", note]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert len(spawns) == 1, "exactly one spawn expected"
    _home_str, extra, _log = spawns[0]
    assert extra == ["--from-node", "implementer", "--strategy", "retry"]

    # Steering row exists with the note text.
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT run_id, severity, message, source FROM operator_steering "
            "WHERE run_id = ? ORDER BY id DESC",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    assert rows, "an operator_steering row should have been written"
    last = rows[0]
    assert last[0] == run_id
    assert last[1] == "warn"
    assert note in str(last[2])
    assert last[3] == "review"

    # Sentinel renamed (audit trail for the manual review action).
    assert (tmp_path / "runs" / run_id / ".review-pause.resolved").exists()
    assert "rejected" in out


def test_request_changes_no_resume_skips_spawn(monkeypatch, tmp_path, capsys):
    """``request-changes --no-resume`` still writes the steering row,
    still renames the sentinel, but does not spawn ``recover``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-resume-4b"
    db_path, _inbox = _setup_parked_run(tmp_path, run_id)
    spawns = _stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(
        ["request-changes", run_id, "--node", "implementer",
         "--note", "fix the lint", "--no-resume"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert spawns == []
    assert f"recover {run_id} --from-node implementer --strategy retry" in out
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT COUNT(*) FROM operator_steering WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        con.close()
    assert rows[0] == 1, "the steering row must still be written"


# ── DoD 5: planner refuses a parked run with pending rows ────────────────


def test_planner_refuses_reviewing_with_pending(monkeypatch, tmp_path, capsys):
    """``planner.main`` on a parked run with a pending row → rc 1 + stderr
    line pointing at ``mini-ork approvals list``."""
    run_id = "run-resume-pl-1"
    home = tmp_path
    db_path, _inbox = _setup_parked_run(home, run_id)
    run_dir = home / "runs" / run_id
    # The planner needs a workflow.yaml and an empty run; we don't actually
    # dispatch (lease tables absent + the gate refuses earlier).
    workflow = home / "wf.yaml"
    workflow.write_text(
        "version: '0.1.0'\ntask_class: framework_edit\n"
        "nodes:\n  - name: implementer\n    type: implementer\n    model_lane: minimax_lens\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)

    capsys.readouterr()
    rc = rp.main([run_id, "--strategy", "resume",
                  "--workflow", str(workflow), "--db", db_path])
    out_err = capsys.readouterr().err
    assert rc == 1, f"expected refusal rc=1, got rc={rc}"
    assert f"run {run_id} is awaiting human review" in out_err
    assert "mini-ork approvals list" in out_err


def test_planner_force_bypasses_reviewing_refusal(monkeypatch, tmp_path, capsys):
    """``--force`` lets the planner dispatch a parked run anyway."""
    run_id = "run-resume-pl-2"
    home = tmp_path
    db_path, _inbox = _setup_parked_run(home, run_id)
    run_dir = home / "runs" / run_id
    workflow = home / "wf.yaml"
    workflow.write_text(
        "version: '0.1.0'\ntask_class: framework_edit\n"
        "nodes:\n  - name: implementer\n    type: implementer\n    model_lane: minimax_lens\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)

    capsys.readouterr()
    rc = rp.main([run_id, "--strategy", "resume", "--force",
                  "--workflow", str(workflow), "--db", db_path])
    err = capsys.readouterr().err
    # The parked gate is bypassed; the planner then encounters the hint-gate
    # (no retry-hint file present) and falls through to lease acquisition.
    # Pre-lease schemas (no run_leases / recovery_requests) → rc=0 with the
    # "dispatching closure" line. The KEY assertion is the refusal stderr
    # is ABSENT.
    assert "awaiting human review" not in err, f"--force must bypass the gate; err={err!r}"
    # If the planner made it to the dispatch phase, the rc is 0; if it
    # stopped earlier (no nodes to rerun), the rc is also 0 or a
    # non-gate refusal. We don't pin the exact rc here — just that the
    # parked-run refusal did NOT fire.
    assert rc in (0, 1)  # 1 allowed if a later check refused for another reason


def test_planner_skips_retry_hint_for_reviewing(monkeypatch, tmp_path, capsys):
    """A parked run with no pending row is NOT refused by the retry-hint
    gate even if ``retry-hint.json`` says ``retryable: false``."""
    run_id = "run-resume-pl-3"
    home = tmp_path
    db_path, _inbox = _setup_parked_run(home, run_id)
    run_dir = home / "runs" / run_id
    # Resolve the only pending row, then plant a non-retryable hint.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE mo_inbox_gates SET status = 'approved' "
            "WHERE blocks_dispatch_for = ?", (run_id,),
        )
        con.commit()
    finally:
        con.close()
    # Hint says the run is not retryable + needs a change.
    (run_dir / "retry-hint.json").write_text(json.dumps({
        "retryable": False,
        "strategy": "retry",
        "from_node": None,
        "needs_change": {"kind": "general", "summary": "stale",
                         "detail": "tree out of date", "evidence": ""},
        "command": "",
    }), encoding="utf-8")

    workflow = home / "wf.yaml"
    workflow.write_text(
        "version: '0.1.0'\ntask_class: framework_edit\n"
        "nodes:\n  - name: implementer\n    type: implementer\n    model_lane: minimax_lens\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)

    capsys.readouterr()
    rp.main([run_id, "--strategy", "resume",
             "--workflow", str(workflow), "--db", db_path])
    err = capsys.readouterr().err
    # The parked run must NOT have been refused by the hint gate.
    assert "retry-hint refuses this run" not in err, (
        f"parked runs must skip the hint gate; got err={err!r}"
    )
    assert "Needs a change before retrying" not in err, (
        f"parked runs must skip the needs_change refusal; got err={err!r}"
    )


# ── Bonus: planner does NOT refuse when nothing is parked ────────────────


def test_planner_does_not_refuse_non_parked_run(monkeypatch, tmp_path, capsys):
    """A run whose ``task_runs.status`` is NOT ``reviewing`` is unaffected
    by the new parked-run gate (regression guard for DoD 6: existing tests
    must pass unchanged)."""
    run_id = "run-resume-pl-4"
    home = tmp_path
    db_path, _inbox = _setup_parked_run(home, run_id)
    # Flip the run to a non-parked status; the parked gate must NOT fire.
    _set_task_status(db_path, run_id, "failed")
    run_dir = home / "runs" / run_id
    workflow = home / "wf.yaml"
    workflow.write_text(
        "version: '0.1.0'\ntask_class: framework_edit\n"
        "nodes:\n  - name: implementer\n    type: implementer\n    model_lane: minimax_lens\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)

    capsys.readouterr()
    rp.main([run_id, "--strategy", "resume",
             "--workflow", str(workflow), "--db", db_path])
    err = capsys.readouterr().err
    assert "awaiting human review" not in err


def test_planner_no_task_runs_row_does_not_refuse(monkeypatch, tmp_path, capsys):
    """No ``task_runs`` row at all → the parked gate does NOT fire (the
    planner's existing behaviour for runs that pre-date migration 0013)."""
    run_id = "run-resume-pl-5"
    home = tmp_path
    db_path, _inbox = _setup_parked_run(home, run_id)
    # Drop the task_runs row; bootstrap left it via ``_set_task_status``.
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM task_runs WHERE id = ?", (run_id,))
        con.commit()
    finally:
        con.close()

    run_dir = home / "runs" / run_id
    workflow = home / "wf.yaml"
    workflow.write_text(
        "version: '0.1.0'\ntask_class: framework_edit\n"
        "nodes:\n  - name: implementer\n    type: implementer\n    model_lane: minimax_lens\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)

    capsys.readouterr()
    rp.main([run_id, "--strategy", "resume",
             "--workflow", str(workflow), "--db", db_path])
    err = capsys.readouterr().err
    assert "awaiting human review" not in err, (
        f"no task_runs row must not trigger the parked gate; err={err!r}"
    )