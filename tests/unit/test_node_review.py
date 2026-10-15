"""Unit tests: ``node_review`` decision layer + the ``mini-ork approvals`` CLI.

Hermetic per test: an autouse fixture points ``$MINI_ORK_HOME`` at ``tmp_path``
and scrubs every env key the loader, the CLI, and the gate read. The lane that
spawned this run carries ``MINI_ORK_RUN_ID`` / ``MINI_ORK_RUN_DIR`` /
``MO_TARGET_CWD`` in its process env — without scrubbing, DoD 1 would observe
a pre-existing DB path, DoD 7 would refuse-in-run, and ``_ckpt_ref`` would
shell into the live engine.

The DoD tests read ``node_checkpoints`` / ``node_attempts``, production
tables created by migration ``0050_node_dag_checkpoints.sql``. We bootstrap
those tables per-test with a minimal DDL matching the canonical schema; the
``mo_inbox_gates`` table is brought up automatically by
``oversight_inbox._connect``.

The fixture for DoD 9 also seeds a pending node review row so the CLI
``approve --node`` branch hits the existing-pending resolution path. A bare
``approve_node`` call against an empty DB raises ``ApprovalError`` (the
kickoff mandates that posture).
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
from mini_ork.gates.merge_approval import ApprovalError  # noqa: E402
from mini_ork.gates.review_policy import ReviewPolicy  # noqa: E402


_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MO_TARGET_CWD")


# Schema lifted from db/migrations/0050_node_dag_checkpoints.sql, trimmed to
# the columns node_review reads. ``mo_inbox_gates`` is bootstrapped by
# ``oversight_inbox._connect`` on first open.
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
def db(tmp_path):
    """The per-test ``state.db`` path; the node tables are bootstrapped on demand."""
    return str(tmp_path / "state.db")


def _bootstrap(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_CHECKPOINTS_DDL + _ATTEMPTS_DDL)
        conn.commit()
    finally:
        conn.close()


def _insert_checkpoint(
    db_path: str,
    run_id: str,
    node_id: str,
    *,
    created_at: int | None = None,
    status: str = "success",
    attempt: int = 1,
) -> None:
    """``INSERT OR REPLACE`` keeps the PK (run_id, node_id) constant across
    re-runs; ``created_at`` is what changes between attempts."""
    if created_at is None:
        created_at = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO node_checkpoints ("
            "  run_id, node_id, attempt, status, input_hash, recipe_version,"
            "  config_hash, artifact_manifest_json, session_ref, failure_class,"
            "  created_at) "
            "VALUES (?, ?, ?, ?, '', '', '', '[]', NULL, NULL, ?)",
            (run_id, node_id, attempt, status, int(created_at)),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_attempt(
    db_path: str,
    run_id: str,
    node_id: str,
    node_type: str,
    *,
    attempt_no: int = 1,
    started_at: int | None = None,
) -> None:
    if started_at is None:
        started_at = int(time.time())
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO node_attempts ("
            "  run_id, node_id, attempt_no, node_type, started_at, ended_at,"
            "  result, checkpoint_used, checkpoint_produced"
            ") VALUES (?, ?, ?, ?, ?, ?, 'success', 0, 0)",
            (run_id, node_id, attempt_no, node_type, started_at, started_at),
        )
        conn.commit()
    finally:
        conn.close()


def _write_plan(run_dir: Path, decomposition: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"decomposition": decomposition}), encoding="utf-8"
    )


# ── DoD 1: fast path ─────────────────────────────────────────────────────


def test_fast_path_does_not_open_db(tmp_path):
    """``policy.after_nodes`` empty → ``("proceed", {})`` and the DB file is
    never created (matches ``merge_approval.require``'s "policy off" branch)."""
    never = tmp_path / "never.db"
    assert not never.exists()
    decision, payload = node_review.decide(
        "r1", "implementer", db_path=str(never), env={}
    )
    assert decision == "proceed"
    assert payload == {}
    assert not never.exists(), "fast path must not create the DB file"


def test_fast_path_through_loader_empty_after_nodes(tmp_path):
    """An env with no after_nodes still takes the fast path; the loader runs
    but doesn't open the DB."""
    never = tmp_path / "never.db"
    decision, _ = node_review.decide(
        "r1",
        "implementer",
        db_path=str(never),
        env={"MO_REVIEW_BEFORE_MERGE": "off"},
    )
    assert decision == "proceed"
    assert not never.exists()


# ── DoD 2: park ──────────────────────────────────────────────────────────


def test_park_implementer_after_node(tmp_path):
    """``after_nodes=("implementer",)`` with a checkpoint → park exactly once."""
    db = tmp_path / "state.db"
    db_path = str(db)
    _bootstrap(db_path)
    _insert_checkpoint(db_path, "r1", "implementer", created_at=1000)
    _insert_attempt(db_path, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db_path,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "park"
    assert payload["awaiting"] == ["implementer"]
    assert len(payload["inbox_ids"]) == 1

    # A second call must NOT enqueue a duplicate (the existing pending row
    # is reused).
    decision2, payload2 = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db_path,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision2 == "park"
    assert payload2["inbox_ids"] == payload["inbox_ids"]

    pending_rows = node_review.pending_for_run("r1", db_path=db_path)
    assert len(pending_rows) == 1
    assert pending_rows[0]["gate_id"] == "node:r1:implementer:1000"
    assert pending_rows[0]["status"] == "pending"


def test_park_skips_self_node_id(tmp_path):
    """``decide`` for the very node under review returns proceed (parking on
    one's own in-flight output is meaningless)."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    decision, payload = node_review.decide(
        "r1", "implementer", db_path=db, run_dir=str(tmp_path), policy=policy
    )
    assert decision == "proceed"
    assert payload == {}


# ── DoD 3: approve ───────────────────────────────────────────────────────


def test_approve_resolves_park_to_proceed(tmp_path):
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    # Park
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    inbox_id = node_review.approve_node(
        "r1", "implementer", approver="alice", db_path=db, env={}
    )
    assert inbox_id > 0

    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "proceed"
    assert payload == {}


# ── DoD 4: request changes ───────────────────────────────────────────────


def test_request_changes_blocks_with_note(tmp_path):
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    inbox_id = node_review.request_changes(
        "r1",
        "implementer",
        approver="bob",
        note="tighten tests",
        db_path=db,
        env={},
    )
    assert inbox_id > 0

    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "block"
    assert "tighten tests" in payload["note"]
    assert payload["rejected"]["node_id"] == "implementer"


# ── DoD 5: re-run ────────────────────────────────────────────────────────


def test_rerun_with_newer_created_at_parks_again(tmp_path):
    """The same node re-running with a newer ``created_at`` must trigger a
    fresh pending row (gate_id includes ``created_at``)."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    # Park → approve at T=1000.
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    node_review.approve_node(
        "r1", "implementer", approver="alice", db_path=db, env={}
    )
    decision, _ = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "proceed"

    # Re-run: replace the (run_id, node_id) row with a newer created_at.
    _insert_checkpoint(db, "r1", "implementer", created_at=2000)

    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "park"
    assert "implementer" in payload["awaiting"]


# ── DoD 6: matching ──────────────────────────────────────────────────────


def test_match_by_node_id_listed_in_after_nodes(tmp_path):
    """A node_id listed in ``after_nodes`` (not a type) is matched; a
    succeeding node with a different id is ignored."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "static_check_verifier", created_at=1000)
    _insert_attempt(
        db, "r1", "static_check_verifier", "verifier"
    )
    _insert_checkpoint(db, "r1", "implementer", created_at=1100)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(
        before_merge="off", after_nodes=("static_check_verifier",)
    )
    decision, payload = node_review.decide(
        "r1", "next_node", db_path=db, run_dir=str(tmp_path), policy=policy
    )
    assert decision == "park"
    assert payload["awaiting"] == ["static_check_verifier"]
    assert "implementer" not in payload["awaiting"]


def test_star_matches_every_completed_node(tmp_path):
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")
    _insert_checkpoint(db, "r1", "verifier_x", created_at=1100)
    _insert_attempt(db, "r1", "verifier_x", "verifier")

    policy = ReviewPolicy(before_merge="off", after_nodes=("*",))
    decision, payload = node_review.decide(
        "r1", "next_node", db_path=db, run_dir=str(tmp_path), policy=policy
    )
    assert decision == "park"
    assert "implementer" in payload["awaiting"]
    assert "verifier_x" in payload["awaiting"]


def test_type_fallback_to_plan_json(tmp_path):
    """When the latest ``node_attempts`` row has no ``node_type``, the loader
    falls back to ``plan.json``. With that type in ``after_nodes``, the row
    is gated."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", node_type="", started_at=999_999_999)
    _write_plan(
        tmp_path,
        [
            {"id": "implementer", "node_type": "implementer"},
            {"id": "static_check_verifier", "node_type": "verifier"},
        ],
    )

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "park"
    assert "implementer" in payload["awaiting"]


# ── DoD 7: in-run refusal ────────────────────────────────────────────────


def test_approve_node_inside_run_raises(tmp_path):
    """An agent must never approve its own run."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    with pytest.raises(ApprovalError) as ei:
        node_review.approve_node(
            "r1",
            "implementer",
            approver="agent",
            db_path=db,
            env={"MINI_ORK_RUN_ID": "x"},
        )
    assert "refuses inside a run" in str(ei.value)


def test_request_changes_inside_run_raises(tmp_path):
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    with pytest.raises(ApprovalError):
        node_review.request_changes(
            "r1",
            "implementer",
            approver="agent",
            note="nope",
            db_path=db,
            env={"MINI_ORK_RUN_DIR": "/tmp/r"},
        )


def test_approve_with_no_pending_raises(tmp_path):
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    with pytest.raises(ApprovalError) as ei:
        node_review.approve_node(
            "r1", "implementer", approver="alice", db_path=db, env={}
        )
    assert "no pending node review" in str(ei.value)


# ── DoD 8: bad config → block ────────────────────────────────────────────


def test_bad_config_blocks(tmp_path):
    """A loader ``ValueError`` (here from a bogus ``MO_REVIEW_BEFORE_MERGE``)
    is fail-closed → block, regardless of ``after_nodes``."""
    db = str(tmp_path / "state.db")
    decision, payload = node_review.decide(
        "r1",
        "any_node",
        db_path=db,
        env={"MO_REVIEW_BEFORE_MERGE": "bogus"},
    )
    assert decision == "block"
    assert "bogus" in payload.get("error", "")


def test_unexpected_db_failure_with_after_nodes_blocks(tmp_path):
    """An ``OperationalError`` from a DB without ``node_checkpoints`` is also
    block when ``after_nodes`` is non-empty (the fail-closed half of the
    contract)."""
    db = str(tmp_path / "state.db")  # not bootstrapped — table missing

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "block"
    assert "error" in payload


# ── DoD 9: CLI ───────────────────────────────────────────────────────────


def test_cli_approve_node_returns_zero(tmp_path, monkeypatch, capsys):
    """``approvals approve <run> --node <node>`` against an isolated
    ``MINI_ORK_HOME`` returns 0 and emits the resume hint."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    # Seed a pending node review so ``approve_node`` has a row to resolve.
    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    from mini_ork.cli import approvals_cmd

    capsys.readouterr()  # drain any pre-test output
    rc = approvals_cmd.main(["approve", "r1", "--node", "implementer"])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert rc == 0, f"unexpected rc={rc}; out={out!r} err={err!r}"
    assert "resume with: mini-ork recover r1 --strategy resume" in out


def test_cli_request_changes_alias_works(tmp_path, monkeypatch, capsys):
    """``request-changes <run> --node <node> --note …`` returns 0."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    from mini_ork.cli import approvals_cmd

    capsys.readouterr()
    rc = approvals_cmd.main(
        ["request-changes", "r1", "--node", "implementer", "--note", "no good"]
    )
    assert rc == 0


def test_cli_reject_node_missing_note_returns_two(tmp_path, monkeypatch, capsys):
    """Reject with ``--node`` but no ``--note`` is a usage error → rc 2."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd

    capsys.readouterr()
    rc = approvals_cmd.main(["reject", "r1", "--node", "implementer"])
    assert rc == 2


# ── DoD 10: intervention_decision ────────────────────────────────────────


def test_intervention_decision_proceed_when_after_nodes_empty(tmp_path, monkeypatch):
    """``intervention_decision`` with no ``after_nodes`` returns proceed
    without touching the DB — mirrors the fast path the executor hook relies
    on for the common "no review" case."""
    from mini_ork.gates import intervention_gate

    never = tmp_path / "never.db"
    decision, payload = intervention_gate.intervention_decision(
        "r1",
        "implementer",
        "implementer",
        "default",
        "",
        db_path=str(never),
        env={},
    )
    assert decision == "proceed"
    assert payload == {}
    assert not never.exists()


def test_intervention_decision_block_on_db_exception_with_after_nodes(
    tmp_path, monkeypatch
):
    """With ``after_nodes`` set and a DB without the production tables, the
    hook returns block (fail-closed) rather than raising."""
    from mini_ork.gates import intervention_gate

    db = str(tmp_path / "state.db")  # not bootstrapped
    # ``node_review`` imports ``load_policy`` as a module-local name; patch
    # that name so ``decide`` sees the gated policy instead of the default
    # (empty) one.
    monkeypatch.setattr(
        node_review,
        "load_policy",
        lambda env=None, home=None: ReviewPolicy(
            before_merge="off", after_nodes=("implementer",)
        ),
    )

    decision, payload = intervention_gate.intervention_decision(
        "r1",
        "static_check_verifier",
        "verifier",
        "default",
        "",
        db_path=db,
        env={},
    )
    assert decision == "block"
    assert "error" in payload


def test_intervention_decision_proceed_on_db_exception_with_empty_after_nodes(
    tmp_path, monkeypatch
):
    """A DB exception with empty ``after_nodes`` is still proceed — the gate
    is off, keep going."""
    from mini_ork.gates import intervention_gate

    db = str(tmp_path / "state.db")  # not bootstrapped
    monkeypatch.setattr(
        node_review,
        "load_policy",
        lambda env=None, home=None: ReviewPolicy(before_merge="off", after_nodes=()),
    )

    decision, payload = intervention_gate.intervention_decision(
        "r1",
        "static_check_verifier",
        "verifier",
        "default",
        "",
        db_path=db,
        env={},
    )
    assert decision == "proceed"
    assert payload == {}


# ── DoD 11: decide wraps DB exceptions (round-1 fix) ─────────────────────


def test_decide_block_on_ckpt_ref_subprocess_failure(tmp_path, monkeypatch):
    """A failure from the per-node ``_ckpt_ref`` path (here simulated as a
    subprocess raising) is caught and surfaces as block, not exception.

    The cheapest way to force a non-recoverable error inside the post-fast
    path is to make ``_ckpt_ref`` raise ``RuntimeError``; the kickoff mandates
    the whole body is wrapped, not just ``reviewable_completed``.
    """
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ckpt_ref failure")

    monkeypatch.setattr(node_review, "_ckpt_ref", _boom)

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    decision, payload = node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    assert decision == "block"
    assert "simulated ckpt_ref failure" in payload.get("error", "")


# ── DoD 12: concurrent decide enqueues once (round-1 fix) ────────────────


def test_concurrent_decide_enqueues_exactly_one_row(tmp_path):
    """Three parallel ``decide`` calls for the same gate must produce one
    pending row, not three. Uses threads to force the race the SQLite
    ``BEGIN IMMEDIATE`` lock closes."""
    import threading

    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    errors: list[BaseException] = []

    def _call() -> None:
        try:
            node_review.decide(
                "r1",
                "static_check_verifier",
                db_path=db,
                run_dir=str(tmp_path),
                policy=policy,
            )
        except BaseException as exc:  # noqa: BLE001 — record, don't swallow
            errors.append(exc)

    barrier = threading.Barrier(3)
    workers = []
    for _ in range(3):

        def _run(b=barrier) -> None:
            b.wait()
            _call()

        t = threading.Thread(target=_run)
        workers.append(t)
        t.start()
    for t in workers:
        t.join()

    assert not errors, f"workers raised: {errors!r}"
    pending_rows = node_review.pending_for_run("r1", db_path=db)
    assert len(pending_rows) == 1
    assert pending_rows[0]["gate_id"] == "node:r1:implementer:1000"


# ── DoD 13: _ckpt_ref env fallback (round-1 fix) ─────────────────────────


def test_ckpt_ref_uses_mo_target_cwd_when_run_dir_empty(
    tmp_path, monkeypatch
):
    """When ``run_dir`` is empty, ``_ckpt_ref`` falls back to ``$MO_TARGET_CWD``
    — running outside a run tree, the executor still points it at the right
    git toplevel."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # Minimal git repo with a fake ckpt ref so ``show-ref`` returns 0.
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init", "-q"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "x"],
        cwd=str(repo_dir),
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )
    subprocess.run(
        ["git", "update-ref", "refs/mini-ork/ckpt/r1/implementer", "HEAD"],
        cwd=str(repo_dir),
        check=True,
    )

    ref = node_review._ckpt_ref("r1", "implementer", "", env={"MO_TARGET_CWD": str(repo_dir)})
    assert ref == "refs/mini-ork/ckpt/r1/implementer"


def test_ckpt_ref_no_run_dir_no_target_returns_empty(tmp_path):
    """Empty ``run_dir`` AND empty ``MO_TARGET_CWD`` skip git entirely —
    running from the engine repo would otherwise probe the wrong tree."""
    ref = node_review._ckpt_ref("r1", "implementer", "", env={})
    assert ref == ""


# ── DoD 14: review_note preserves trailing whitespace (round-1 fix) ──────


def test_review_note_preserves_trailing_punctuation(tmp_path):
    """``f'{approver}: {note}'`` is stored verbatim — the prior ``.strip(
    ': ')`` swallowed a human's trailing punctuation like 'see: '."""
    db = str(tmp_path / "state.db")
    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )
    node_review.request_changes(
        "r1",
        "implementer",
        approver="bob",
        note="see: ",
        db_path=db,
        env={},
    )
    rows = node_review.pending_for_run("r1", db_path=db)
    assert len(rows) == 0  # the row was rejected, not pending
    # The rejected row's review_note holds the verbatim f"{approver}: {note}".
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT review_note, status FROM mo_inbox_gates ORDER BY inbox_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "rejected"
    assert row["review_note"] == "bob: see: "


# ── DoD 15: approvals list output (round-1 fix) ──────────────────────────


def test_cli_list_text_uses_context_run_id(tmp_path, monkeypatch, capsys):
    """The text output's 'run' column is the run id from context, not the
    third ``gate_id.partition(':')`` element. For node rows that would print
    ``r1:implementer:1000`` instead of ``r1``."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    from mini_ork.cli import approvals_cmd

    capsys.readouterr()
    rc = approvals_cmd.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "node   r1" in out
    assert "r1:implementer:1000" not in out


def test_cli_list_json_is_flat_list(tmp_path, monkeypatch, capsys):
    """The JSON shape is a flat list (rows), not ``{'merge': [...], 'node':
    [...]}`` — preserves the existing output contract for any consumer that
    iterated the list directly."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    _bootstrap(db)
    _insert_checkpoint(db, "r1", "implementer", created_at=1000)
    _insert_attempt(db, "r1", "implementer", "implementer")

    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",))
    node_review.decide(
        "r1",
        "static_check_verifier",
        db_path=db,
        run_dir=str(tmp_path),
        policy=policy,
    )

    from mini_ork.cli import approvals_cmd

    capsys.readouterr()
    rc = approvals_cmd.main(["list", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert any(r.get("feature") == "review.after_node" for r in payload)
