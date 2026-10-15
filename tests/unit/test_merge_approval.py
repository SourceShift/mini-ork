"""Unit tests: ``merge_approval`` ledger + the ``mini-ork approvals`` CLI.

Hermetic per test: an autouse fixture points ``$MINI_ORK_HOME`` at ``tmp_path``
and scrubs every key the loader and the CLI read. ``approvals_cmd`` calls into
``os.environ.get`` for ``MINI_ORK_HOME`` and ``MINI_ORK_DB``; the fixture pins
both so a stray read of the live ``state.db`` cannot happen.

``os.environ`` is restored by ``tests/conftest.py::_isolate_process_state``;
the autouse fixture here only *deletes* the keys the implementer reads — it
never alters the rest of the env.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates import merge_approval  # noqa: E402
from mini_ork.gates.merge_approval import (  # noqa: E402
    ApprovalError,
    approve,
    reject,
    request,
    require,
    status,
)
from mini_ork.gates.review_policy import ReviewPolicy  # noqa: E402


_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)
_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Pin the home/DB to ``tmp_path``; scrub every key the ledger reads."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db(_isolated, tmp_path):
    """The per-test ``state.db`` path. Built lazily; ``require(off)`` never
    touches it (the directory may not even contain the file)."""
    return str(tmp_path / "state.db")


def _row(db: str, inbox_id: int) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE inbox_id = ?", (inbox_id,)
        ).fetchone()
        out = dict(row)
        out["context"] = json.loads(out.pop("context_json") or "{}")
    finally:
        conn.close()
    return out


# ── DoD 3: request idempotency + supersession ─────────────────────────────


def test_3a_request_idempotent_on_same_head(db):
    first = request("r1", "baseA", "head1", db_path=db)
    second = request("r1", "baseA", "head1", db_path=db)
    assert first == second


def test_3b_request_different_head_supersedes(db):
    from mini_ork.gates.oversight_inbox import get as inbox_get

    first = request("r1", "baseA", "head1", db_path=db)
    second = request("r1", "baseA", "head2", db_path=db)
    assert first != second
    old = inbox_get(first, db_path=db)
    assert old["status"] == "rejected"
    assert "superseded" in (old["review_note"] or "")
    new = inbox_get(second, db_path=db)
    assert new["status"] == "pending"


# ── DoD 4: approve ───────────────────────────────────────────────────────


def test_4a_approve_matching_head_then_is_approved_true(db):
    request("r1", "baseA", "head1", db_path=db)
    inbox_id = approve(
        "r1",
        "head1",
        approver="alice",
        base_sha="baseA",
        note="looks good",
        db_path=db,
    )
    assert inbox_id > 0
    assert merge_approval.is_approved(
        "r1", "head1", base_sha="baseA", db_path=db
    )


def test_4a_approve_then_different_head_is_false(db):
    request("r1", "baseA", "head1", db_path=db)
    approve("r1", "head1", approver="alice", db_path=db)
    assert not merge_approval.is_approved("r1", "head2", db_path=db)


def test_4b_approve_inside_a_run_raises(db, monkeypatch):
    """An agent must never approve its own run."""
    monkeypatch.setenv("MINI_ORK_RUN_ID", "hr-review-policy-20261015125620")
    request("r1", "baseA", "head1", db_path=db)
    with pytest.raises(ApprovalError):
        approve("r1", "head1", approver="agent", db_path=db)


def test_4c_approve_with_stale_pending_head_raises(db):
    """Approve while a NEW pending row exists with a different head → stale."""
    request("r1", "baseA", "head1", db_path=db)
    # Operator A opens a new request with a different head; the previous row
    # was rejected (superseded), so the new pending is the one approve sees.
    request("r1", "baseA", "head2", db_path=db)
    with pytest.raises(ApprovalError) as ei:
        approve("r1", "head1", approver="alice", db_path=db)
    assert "stale" in str(ei.value).lower()


def test_4d_approve_preserves_branch_and_base_branch(db):
    """``approve`` must not drop the ``branch`` / ``base_branch`` that
    ``request()`` set; the kickoff says the row context holds them."""
    request(
        "r1", "baseA", "head1",
        branch="wt/hr", base_branch="main",
        db_path=db,
    )
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db)
    info = status("r1", db_path=db)
    inbox_id = info["inbox_id"]
    row = _row(db, inbox_id)
    assert row["context"]["branch"] == "wt/hr"
    assert row["context"]["base_branch"] == "main"
    assert row["context"]["approver"] == "alice"


def test_4e_approve_no_pending_enqueues_and_succeeds(db):
    """The kickoff's 'No request yet' path: approve with no prior request
    enqueues a row, marks it approved, and the approver lands in context."""
    inbox_id = approve(
        "r1", "headX", approver="bob", base_sha="baseA", db_path=db
    )
    assert inbox_id > 0
    info = status("r1", db_path=db)
    assert info["state"] == "approved"
    assert info["approver"] == "bob"
    # And the row's head_sha is what was approved, not the empty default.
    row = _row(db, inbox_id)
    assert row["context"]["head_sha"] == "headX"
    assert row["context"]["base_sha"] == "baseA"


# ── DoD 5: require ───────────────────────────────────────────────────────


def test_5a_require_off_returns_true_and_writes_no_row(db):
    """A policy-off path takes the short-circuit; no row is written, and
    ``require()`` never opens the DB (so the file may not even exist)."""
    import os

    ok, msg = require("r1", "baseA", "head1", env={"MO_REVIEW_BEFORE_MERGE": "off"})
    assert ok is True
    assert msg == "review off"
    assert not os.path.exists(db)


def test_5b_require_required_unapproved_writes_row(db, monkeypatch):
    """A ``require`` call with policy=required and no approval enqueues a row."""
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    ok, msg = require("r1", "baseA", "head1", env={"MO_REVIEW_BEFORE_MERGE": "required"}, db_path=db)
    assert ok is False
    assert "approvals approve" in msg
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM mo_inbox_gates WHERE gate_id = 'merge:r1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == 1


def test_5c_require_required_then_approve_returns_true(db, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_BEFORE_MERGE", "required")
    require("r1", "baseA", "head1", env={"MO_REVIEW_BEFORE_MERGE": "required"}, db_path=db)
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db)
    ok, msg = require(
        "r1", "baseA", "head1", env={"MO_REVIEW_BEFORE_MERGE": "required"}, db_path=db
    )
    assert ok is True
    assert msg.startswith("approved by")


def test_5d_require_honours_passed_secure_policy_after_simple_approve(db):
    """An approval made in the simple tier must NOT satisfy a secure-tier
    require() — the policy the caller passed is the one to honour, not the
    loader's reload. Before the fix, ``is_approved`` re-read the policy
    from env/file and the simple tier was the one consulted, so a
    secure-tier require() would say ``approved by alice``."""
    # Approve in the simple tier (no key) — this MUST succeed.
    request("r1", "baseA", "head1", db_path=db)
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db)

    secure = ReviewPolicy(before_merge="required", tier="secure")
    ok, msg = require(
        "r1", "baseA", "head1",
        policy=secure,
        env={"MO_REVIEW_TIER": "simple"},  # loader sees simple
        db_path=db,
    )
    assert ok is False, (
        f"secure tier should not accept a simple-tier approval, got {msg!r}"
    )
    assert "awaiting" in msg


def test_5e_is_approved_filters_by_head_sha(db):
    """An approval for one head must not satisfy a check on a different head."""
    request("r1", "baseA", "head1", db_path=db)
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db)
    # Same run, different head — must NOT be approved.
    assert not merge_approval.is_approved("r1", "head2", db_path=db)
    # And the matching head still approves.
    assert merge_approval.is_approved("r1", "head1", db_path=db)


def test_5f_reject_after_approve_revokes(db):
    """An operator can revoke a prior approval by calling ``reject`` again."""
    request("r1", "baseA", "head1", db_path=db)
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db)
    assert merge_approval.is_approved("r1", "head1", base_sha="baseA", db_path=db)

    reject("r1", approver="bob", note="changed my mind", db_path=db)
    info = status("r1", db_path=db)
    assert info["state"] == "rejected"
    # is_approved is now False — the run is no longer cleared to merge.
    assert not merge_approval.is_approved("r1", "head1", base_sha="baseA", db_path=db)


# ── DoD 6: secure tier ───────────────────────────────────────────────────


def test_6a_secure_without_key_raises(db, monkeypatch):
    monkeypatch.setenv("MO_REVIEW_TIER", "secure")
    monkeypatch.delenv("MO_REVIEW_SIGNING_KEY", raising=False)
    request("r1", "baseA", "head1", db_path=db)
    with pytest.raises(ApprovalError) as ei:
        approve("r1", "head1", approver="alice", db_path=db)
    assert "MO_REVIEW_SIGNING_KEY" in str(ei.value)


def test_6b_secure_with_key_approves_and_verifies(db, monkeypatch):
    """DoD 6 — with the signing key in the env mapping, the row is approved
    and ``is_approved`` says True. The CLI/rest of the code reads the key from
    the same mapping (see §3 item 4 of the lens)."""
    monkeypatch.setenv("MO_REVIEW_TIER", "secure")
    monkeypatch.setenv("MO_REVIEW_SIGNING_KEY", "sekret-key-xyz")
    env = {
        "MO_REVIEW_TIER": "secure",
        "MO_REVIEW_SIGNING_KEY": "sekret-key-xyz",
    }
    request("r1", "baseA", "head1", db_path=db)
    inbox_id = approve(
        "r1", "head1", approver="alice", base_sha="baseA", db_path=db, env=env
    )
    assert inbox_id > 0
    assert merge_approval.is_approved(
        "r1", "head1", base_sha="baseA", db_path=db, env=env
    )


def test_6c_secure_tampered_head_fails_verification(db, monkeypatch):
    """A row whose stored head_sha was edited after approval must no longer
    read as approved — the HMAC over the head no longer matches."""
    monkeypatch.setenv("MO_REVIEW_TIER", "secure")
    monkeypatch.setenv("MO_REVIEW_SIGNING_KEY", "sekret-key-xyz")
    env = {
        "MO_REVIEW_TIER": "secure",
        "MO_REVIEW_SIGNING_KEY": "sekret-key-xyz",
    }
    request("r1", "baseA", "head1", db_path=db)
    approve("r1", "head1", approver="alice", base_sha="baseA", db_path=db, env=env)
    # Tamper: rewrite the row's head_sha so the HMAC no longer matches.
    inbox_id = status("r1", db_path=db)["inbox_id"]
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE mo_inbox_gates SET context_json = ? WHERE inbox_id = ?",
            (
                json.dumps(
                    {
                        "run_id": "r1",
                        "base_sha": "baseA",
                        "head_sha": "headDIFFERENT",
                        "approver": "alice",
                        "signature": "deadbeef" * 8,
                    }
                ),
                inbox_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert not merge_approval.is_approved(
        "r1", "head1", base_sha="baseA", db_path=db, env=env
    )


def test_6d_secure_is_approved_false_when_key_missing_at_check_time(
    db, monkeypatch
):
    """If the key is set at approve() time but missing at check time, the
    ledger cannot verify — fail closed (return False), do not raise."""
    env_with = {
        "MO_REVIEW_TIER": "secure",
        "MO_REVIEW_SIGNING_KEY": "sekret-key-xyz",
    }
    env_without = {"MO_REVIEW_TIER": "secure"}
    request("r1", "baseA", "head1", db_path=db)
    approve(
        "r1", "head1", approver="alice", base_sha="baseA", db_path=db, env=env_with
    )
    assert merge_approval.is_approved(
        "r1", "head1", base_sha="baseA", db_path=db, env=env_with
    )
    assert not merge_approval.is_approved(
        "r1", "head1", base_sha="baseA", db_path=db, env=env_without
    )


def test_6e_secure_chain_excluding_env_does_not_read_key_from_env(
    db, monkeypatch, tmp_path
):
    """A vault.yaml that lists NO ``env`` backend is the secure-tier posture:
    the run environment must not be where the key comes from, even when
    ``MO_REVIEW_SIGNING_KEY`` is set there. The previous code's env-first
    short-circuit returned the value regardless of the chain."""
    # 1) Key set in the process env — a would-be leak.
    monkeypatch.setenv("MO_REVIEW_TIER", "secure")
    monkeypatch.setenv("MO_REVIEW_SIGNING_KEY", "sekret-key-xyz")
    # 2) The vault config deliberately excludes the env backend.
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "vault.yaml").write_text(
        "backends:\n  - type: file\n    file: secrets.local.sh\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))

    # The env mapping has the key. approve() must refuse because the chain
    # does not include the env backend.
    with pytest.raises(ApprovalError) as ei:
        approve(
            "r1", "head1", approver="alice", base_sha="baseA",
            db_path=db,
            env={
                "MO_REVIEW_TIER": "secure",
                "MO_REVIEW_SIGNING_KEY": "sekret-key-xyz",
                "MINI_ORK_HOME": str(tmp_path),
            },
        )
    assert "MO_REVIEW_SIGNING_KEY" in str(ei.value)


# ── DoD 7: CLI ───────────────────────────────────────────────────────────


def test_7a_approve_via_cli_records_approved(tmp_path, monkeypatch):
    """``approvals_cmd.main(['approve','r1','--head','abc'])`` in an isolated
    env returns 0 and the resulting ``status`` is ``approved``."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    from mini_ork.gates.oversight_inbox import enqueue

    # Seed a pending row at the same head so ``approve`` hits the existing
    # path (no implicit enqueue). The CLI is the user, so the seed has to
    # already exist.
    enqueue(
        "merge:r1",
        "review.before_merge",
        context={"head_sha": "abc", "approver": "", "signature": ""},
        blocks_dispatch_for="r1",
        db_path=db,
    )
    # Move the env to a state where approve() will not refuse-in-run and
    # signing keys are absent (simple tier).
    rc = approvals_cmd.main(["approve", "r1", "--head", "abc"])
    assert rc == 0
    info = status("r1", db_path=db)
    assert info["state"] == "approved"


def test_7b_reject_with_nothing_pending_returns_1(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["reject", "r1", "--note", "no diff yet"])
    assert rc == 1


def test_7c_show_json_for_unknown_run(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["show", "does-not-exist", "--json"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "\"state\": \"none\"" in captured


def test_7d_cli_refuses_in_run(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_RUN_ID", "hr-review-policy-20261015125620")

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["approve", "r1", "--head", "abc"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refuses inside a run" in err


def test_7e_cli_usage_error_returns_2(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)
    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main([])
    assert rc == 2


def test_7f_cli_policy_verb(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)
    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["policy"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "before_merge=off" in out


def test_7g_cli_approve_no_prior_request_creates_and_approves(
    tmp_path, monkeypatch, capsys
):
    """The kickoff's DoD 7 command: ``approvals approve r1 --head abc`` with
    no pre-seeded row. The previous code raised ``UnboundLocalError`` on the
    implicit-enqueue path because ``existing`` was only defined in the
    pending branch. After the fix, ``approve`` enqueues a row, resolves it
    approved, and ``show r1 --json`` reports ``state=approved`` with a
    non-empty ``approver``.
    """
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["approve", "r1", "--head", "abc"])
    # Drain both buffers so the next readouterr() starts fresh.
    capsys.readouterr()
    assert rc == 0

    # The CLI returns text output; show --json is the structured shape the
    # kickoff asks for.
    rc = approvals_cmd.main(["show", "r1", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["state"] == "approved"
    assert payload["head_sha"] == "abc"
    assert payload["approver"], (
        f"approver must be set after approve, got {payload!r}"
    )
    # And the ledger agrees: is_approved() returns True for that head.
    assert merge_approval.is_approved("r1", "abc", db_path=db)


def test_7h_cli_top_level_db_overrides_default(tmp_path, monkeypatch, capsys):
    """``approvals --db <path> <verb>`` must honour the top-level ``--db`` —
    the subparser default-None previously shadowed it."""
    target = tmp_path / "alt.db"
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["--db", str(target), "show", "r1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "none"
    # The DB the verb wrote to is the one the user named.
    assert target.is_file()


def test_7i_cli_db_with_no_verb_prints_usage(tmp_path, monkeypatch, capsys):
    """``approvals --db <path>`` (no verb) previously crashed with IndexError
    on ``argv[0]`` after hoisting — the empty argv was indexed out of range.
    After the fix it prints the usage and returns 2, matching the plain
    no-verb path in ``test_7e``."""
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    for key in _POLICY_KEYS + _RUN_KEYS:
        monkeypatch.delenv(key, raising=False)

    from mini_ork.cli import approvals_cmd
    rc = approvals_cmd.main(["--db", str(tmp_path / "alt.db")])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Usage:" in out


# ── Sanity: status before/after, reject with nothing pending ──────────────


def test_status_none_for_unknown_run(db):
    info = status("never-existed", db_path=db)
    assert info == {"state": "none", "head_sha": "", "approver": "", "inbox_id": None}


def test_reject_with_nothing_pending_raises(db):
    with pytest.raises(ApprovalError):
        reject("r-unknown", approver="alice", note="no", db_path=db)


def test_request_enqueues_with_merge_gate_id(db):
    inbox_id = request("r1", "baseA", "head1", db_path=db)
    row = _row(db, inbox_id)
    assert row["gate_id"] == "merge:r1"
    assert row["feature"] == "review.before_merge"
    assert row["status"] == "pending"
    assert row["context"]["head_sha"] == "head1"
