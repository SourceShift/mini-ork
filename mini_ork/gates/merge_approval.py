"""Per-run merge-approval ledger over the ``mo_inbox_gates`` human-oversight channel.

Every request to merge a run's diff becomes one ``pending`` row in
``mo_inbox_gates`` with ``gate_id = f"merge:{run_id}"`` and
``feature = "review.before_merge"``. The CLI (``mini-ork approvals``) and the
later-wired merge paths both go through this module — there is one ledger.

The ledger reuses ``mini_ork/gates/oversight_inbox.py`` (``enqueue``,
``resolve``, ``pending``, ``get``) plus a few narrow ``SELECT``/``UPDATE``
queries it has to issue itself, because ``oversight_inbox`` exposes no
query-by-``gate_id`` surface.

Failure surface is small and bounded:

* **Refuses inside a run.** ``approve()`` and ``reject()`` raise
  ``ApprovalError`` if ``MINI_ORK_RUN_ID`` or ``MINI_ORK_RUN_DIR`` is set in
  the caller's env. An agent must never approve its own run.
* **Fail closed in secure tier.** Without a signing key, ``approve()`` raises
  ``ApprovalError`` before enqueueing or resolving anything — a row that ends
  up ``approved`` but with no signature is the worst of both worlds.
* **Idempotent on head SHA.** Re-asking for the same diff returns the same
  inbox_id; a different head SHA is a new request, and the old pending row is
  marked ``rejected`` with note ``"superseded: diff changed"``.

The ``is_approved`` check is the single source of truth for "can this run be
merged?" — it matches the head SHA, the base SHA when given, and re-verifies
the HMAC signature in the secure tier. A row that no longer verifies after a
context edit is *not* approved; tampering with the ledger is not a free
approval.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from typing import Any, Mapping

from mini_ork.gates import oversight_inbox
from mini_ork.gates.review_policy import ReviewPolicy, load_policy
from mini_ork.dispatch.vault import (
    EnvBackend,
    SecretStoreError,
    VaultEnvironment,
    VaultError,
    load_vault_config,
)

__all__ = [
    "ApprovalError",
    "request",
    "approve",
    "reject",
    "is_approved",
    "status",
    "require",
]


class ApprovalError(Exception):
    """Raised when an approval action is refused: in-run, stale, missing, or
    the secure tier has no signing key."""


GATE_ID_PREFIX = "merge:"
FEATURE = "review.before_merge"

# Env keys that mark "this is happening inside a running agent". An agent
# must never approve its own run.
_IN_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR")


def _gate_id(run_id: str) -> str:
    return f"{GATE_ID_PREFIX}{run_id}"


def _db(db_path: str | os.PathLike[str] | None) -> str:
    if db_path is not None:
        return os.fspath(db_path)
    return os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"


def _connect(db_path: str | os.PathLike[str] | None) -> sqlite3.Connection:
    """Open a connection and ensure ``mo_inbox_gates`` exists.

    Reuses ``oversight_inbox._connect`` so the ledger's rows live in the same
    schema every other reader of the inbox already understands.
    """
    conn = oversight_inbox._connect(_db(db_path))  # noqa: SLF001 — module-private, intent is to share bootstrap
    conn.row_factory = sqlite3.Row
    return conn


def _latest_for_run(
    run_id: str, db_path: str | os.PathLike[str] | None
) -> sqlite3.Row | None:
    gate = _gate_id(run_id)
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE gate_id = ? "
            "ORDER BY inbox_id DESC LIMIT 1",
            (gate,),
        ).fetchone()
    finally:
        conn.close()


def _pending_for_run(
    run_id: str, db_path: str | os.PathLike[str] | None
) -> sqlite3.Row | None:
    gate = _gate_id(run_id)
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE gate_id = ? AND status = 'pending' "
            "ORDER BY inbox_id DESC LIMIT 1",
            (gate,),
        ).fetchone()
    finally:
        conn.close()


def _approved_for_run(
    run_id: str, head_sha: str, db_path: str | os.PathLike[str] | None
) -> sqlite3.Row | None:
    """The latest ``approved`` row whose stored head matches ``head_sha``.

    ``head_sha`` is part of the match: a row that was approved against one
    head is not a permission slip for a different diff. We match inside
    SQL on the parsed ``context_json``'s ``head_sha`` key — simpler than a
    JSON1 expression, and the row count is small.
    """
    gate = _gate_id(run_id)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mo_inbox_gates "
            "WHERE gate_id = ? AND status = 'approved' "
            "ORDER BY inbox_id DESC LIMIT 1",
            (gate,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        ctx = json.loads(row["context_json"]) or {}
    except (TypeError, ValueError):
        ctx = {}
    if ctx.get("head_sha", "") != head_sha:
        return None
    return row


def _refuse_if_in_run(env: Mapping[str, str]) -> None:
    """The agent that wrote the diff must never be the one clicking Approve."""
    for key in _IN_RUN_KEYS:
        if env.get(key):
            raise ApprovalError(
                f"refuses inside a run: {key} is set in the environment; "
                "a human must approve, not the agent that produced the diff"
            )


def _signing_key(
    tier: str, env: Mapping[str, str]
) -> str | None:
    """Resolve ``MO_REVIEW_SIGNING_KEY`` through the configured vault chain.

    The chain is whatever ``$MINI_ORK_HOME/config/vault.yaml`` declares — the
    default is ``env`` then ``file``, but a secure-tier operator can leave
    ``env`` out so the run environment is never the source. We rebind the
    ``EnvBackend`` to ``env`` locally so the chain shape honours the operator's
    config while the env backend's view reflects the caller's mapping. A key
    only present in ``os.environ`` is invisible when the chain does not
    include the ``env`` backend. A ``VaultError`` (bad config) is re-raised so
    the caller can surface it; a missing key just returns ``None``.

    ``MINI_ORK_HOME`` falls back to ``os.environ`` when not in ``env``, so the
    vault config lookup is the same one ``load_policy`` performs and tests that
    pin ``MINI_ORK_HOME`` via a monkeypatch setenv are hermetic.
    """
    del tier  # the chain is the same shape regardless of tier
    # Hermetic home resolution: env wins, os.environ fills the gap. Matches
    # ``load_policy``'s view of the world and keeps tests that pin
    # ``MINI_ORK_HOME`` via monkeypatch from reading the cwd's vault config.
    effective_env: dict[str, str] = dict(os.environ)
    effective_env.update(env)
    try:
        config = load_vault_config(effective_env)
    except VaultError:
        raise
    except SecretStoreError:
        return None
    backends = [
        EnvBackend(effective_env) if isinstance(b, EnvBackend) else b
        for b in config.backends
    ]
    chain = VaultEnvironment(backends)
    try:
        value = chain.resolve("MO_REVIEW_SIGNING_KEY")
    except VaultError:
        raise
    return value or None


def _signature(key: str, run_id: str, base_sha: str, head_sha: str, approver: str) -> str:
    payload = f"{run_id}|{base_sha}|{head_sha}|{approver}"
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def request(
    run_id: str,
    base_sha: str,
    head_sha: str,
    *,
    branch: str = "",
    base_branch: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Enqueue a merge-approval request, idempotent on ``(run_id, head_sha)``.

    Returns the pending row's ``inbox_id``. A second call with the same head
    SHA returns the same id; a different head SHA marks the previous pending
    row ``rejected`` with note ``"superseded: diff changed"`` and enqueues a
    new one.
    """
    gate = _gate_id(run_id)
    pending = _pending_for_run(run_id, db_path)
    if pending is not None:
        try:
            existing_head = (json.loads(pending["context_json"]) or {}).get("head_sha", "")
        except (TypeError, ValueError):
            existing_head = ""
        if existing_head == head_sha:
            return int(pending["inbox_id"])
        # Different head → supersede.
        oversight_inbox.resolve(
            int(pending["inbox_id"]),
            "rejected",
            review_note="superseded: diff changed",
            db_path=db_path,
        )

    return oversight_inbox.enqueue(
        gate,
        FEATURE,
        context={
            "run_id": run_id,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "branch": branch,
            "base_branch": base_branch,
            "approver": "",
            "signature": "",
            "requested_at": time.time(),
        },
        blocks_dispatch_for=run_id,
        db_path=db_path,
    )


def approve(
    run_id: str,
    head_sha: str,
    *,
    approver: str,
    base_sha: str = "",
    note: str = "",
    branch: str = "",
    base_branch: str = "",
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    policy: ReviewPolicy | None = None,
) -> int:
    """Resolve the pending request ``approved``. Returns the approved ``inbox_id``.

    Refuses inside a run. Refuses on a stale pending head. Resolves or enqueues
    a fresh request as needed, then writes the context with ``approver`` and,
    in secure tier, the HMAC ``signature``.
    """
    effective_env = os.environ if env is None else env
    _refuse_if_in_run(effective_env)

    effective_policy = policy if policy is not None else load_policy(env=effective_env)
    tier = effective_policy.tier

    if tier == "secure":
        key = _signing_key(tier, effective_env)
        if not key:
            raise ApprovalError(
                "secure tier requires MO_REVIEW_SIGNING_KEY through the "
                "configured vault chain; refusing to approve without one"
            )

    # ``existing`` is read in every branch below, including the post-resolve
    # context merge. Initialise it up front so the implicit-enqueue path
    # (no prior request) has a defined value.
    existing: dict = {}

    pending = _pending_for_run(run_id, db_path)
    if pending is None:
        # Human clicked Merge before anyone called request(). Seed
        # ``existing`` with the same context the row is about to be written
        # with, so the post-resolve UPDATE preserves ``branch`` /
        # ``base_branch`` on this path (the previous code dropped them).
        existing = {
            "run_id": run_id,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "branch": branch,
            "base_branch": base_branch,
            "approver": "",
            "signature": "",
            "requested_at": time.time(),
        }
        inbox_id = oversight_inbox.enqueue(
            _gate_id(run_id),
            FEATURE,
            context=existing,
            blocks_dispatch_for=run_id,
            db_path=db_path,
        )
    else:
        try:
            existing = json.loads(pending["context_json"]) or {}
        except (TypeError, ValueError):
            existing = {}
        existing_head = existing.get("head_sha", "")
        if existing_head and existing_head != head_sha:
            raise ApprovalError(
                f"stale: pending head_sha is {existing_head!r}, caller passed {head_sha!r}; "
                "re-request approval for the new diff"
            )
        inbox_id = int(pending["inbox_id"])

    ok = oversight_inbox.resolve(
        inbox_id,
        "approved",
        review_note=f"{approver}: {note}".strip(": "),
        db_path=db_path,
    )
    if not ok:
        raise ApprovalError(
            f"no pending row resolved for {run_id!r}; another agent may have "
            "already rejected or superseded it"
        )

    # Stash the approver + signature so is_approved can verify without
    # re-reading the request body. Use a direct UPDATE because the ledger
    # reuses one row, and oversight_inbox.resolve only writes status/note.
    effective_base_sha = base_sha or existing.get("base_sha", "")
    signature = ""
    if tier == "secure":
        # Caller already proved the key exists (else we'd have raised above);
        # key is necessarily truthy here.
        signature = _signature(key or "", run_id, effective_base_sha, head_sha, approver)

    # Merge into the existing context — branch and base_branch set by
    # request() must survive, and the approver / signature are appended.
    new_ctx: dict[str, Any] = dict(existing)
    new_ctx.update(
        {
            "run_id": run_id,
            "base_sha": effective_base_sha,
            "head_sha": head_sha,
            "approver": approver,
            "signature": signature,
            "approved_at": time.time(),
        }
    )
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE mo_inbox_gates SET context_json = ? WHERE inbox_id = ?",
            (json.dumps(new_ctx), inbox_id),
        )
        conn.commit()
    finally:
        conn.close()
    return inbox_id


def reject(
    run_id: str,
    *,
    approver: str,
    note: str,
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Reject the pending or most-recently-approved request for ``run_id``.

    Revoke an already-approved row by rejecting it (status flips to
    ``rejected``; the row's ``context_json`` records the revoker). Raises
    ``ApprovalError`` only when there is no row at all for the run, or the
    caller is in-run.
    """
    effective_env = os.environ if env is None else env
    _refuse_if_in_run(effective_env)

    # Prefer a pending row; fall back to the latest row so an operator can
    # revoke an approval that no longer reflects intent.
    target = _pending_for_run(run_id, db_path) or _latest_for_run(run_id, db_path)
    if target is None:
        raise ApprovalError(f"no merge request for {run_id!r}")

    inbox_id = int(target["inbox_id"])
    if target["status"] == "pending":
        ok = oversight_inbox.resolve(
            inbox_id,
            "rejected",
            review_note=f"{approver}: {note}".strip(": "),
            db_path=db_path,
        )
        if not ok:
            raise ApprovalError(
                f"pending row for {run_id!r} was already resolved; nothing to reject"
            )
    elif target["status"] == "approved":
        # Revoke: write the row's status directly. ``oversight_inbox.resolve``
        # only matches ``status='pending'`` by design, so a transition out of
        # ``approved`` must bypass it.
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE mo_inbox_gates "
                "   SET status = 'rejected', "
                "       resolved_at = ?, "
                "       review_note = ? "
                " WHERE inbox_id = ?",
                (time.time(), f"{approver}: {note}".strip(": "), inbox_id),
            )
            conn.commit()
        finally:
            conn.close()
    else:
        raise ApprovalError(
            f"latest row for {run_id!r} is {target['status']!r}; nothing to reject"
        )

    try:
        existing = json.loads(target["context_json"]) or {}
    except (TypeError, ValueError):
        existing = {}
    existing.update(
        {
            "approver": approver,
            "rejected_at": time.time(),
        }
    )
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE mo_inbox_gates SET context_json = ? WHERE inbox_id = ?",
            (json.dumps(existing), inbox_id),
        )
        conn.commit()
    finally:
        conn.close()
    return inbox_id


def is_approved(
    run_id: str,
    head_sha: str,
    *,
    base_sha: str = "",
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    policy: ReviewPolicy | None = None,
) -> bool:
    """True iff there is an approved, head-matching, signature-verifying row.

    In the simple tier, signature verification is skipped. In the secure tier,
    a row whose stored ``head_sha`` was tampered with after approval fails
    verification (``hmac.compare_digest`` catches a body edit).

    ``policy`` is honoured when passed — useful for ``require(policy=...)``,
    which must not re-read the loader's answer (the simple tier's policy at
    approve time can differ from the policy at check time).
    """
    effective_env = os.environ if env is None else env
    effective_policy = policy if policy is not None else load_policy(env=effective_env)

    row = _approved_for_run(run_id, head_sha, db_path)
    if row is None:
        return False
    try:
        ctx = json.loads(row["context_json"]) or {}
    except (TypeError, ValueError):
        ctx = {}

    # Matchers — head_sha always; base_sha only when caller passed one.
    if ctx.get("head_sha", "") != head_sha:
        return False
    if base_sha and ctx.get("base_sha", "") != base_sha:
        return False
    if not ctx.get("approver"):
        # Approver is the human signature — a row without one was written by
        # a non-policy path (e.g. the generic ``oversight --resolve`` command)
        # and must not gate a real merge.
        return False

    if effective_policy.tier == "secure":
        key = _signing_key("secure", effective_env)
        if not key:
            # Without a key, no verification is possible — fail closed.
            return False
        expected = _signature(
            key,
            run_id,
            ctx.get("base_sha", ""),
            ctx.get("head_sha", ""),
            ctx.get("approver", ""),
        )
        if not hmac.compare_digest(ctx.get("signature", ""), expected):
            return False
    return True


def status(
    run_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict:
    """The latest row's state, decoded for the CLI."""
    row = _latest_for_run(run_id, db_path)
    if row is None:
        return {"state": "none", "head_sha": "", "approver": "", "inbox_id": None}
    try:
        ctx = json.loads(row["context_json"]) or {}
    except (TypeError, ValueError):
        ctx = {}
    return {
        "state": row["status"],
        "head_sha": ctx.get("head_sha", ""),
        "approver": ctx.get("approver", ""),
        "inbox_id": int(row["inbox_id"]),
    }


def require(
    run_id: str,
    base_sha: str,
    head_sha: str,
    *,
    policy: ReviewPolicy | None = None,
    branch: str = "",
    base_branch: str = "",
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """The single policy-aware merge check.

    * policy off → ``(True, "review off")`` — no DB access.
    * already approved (per the policy's tier) → ``(True, f"approved by {approver}")``.
    * otherwise: enqueue a request and return ``(False, "awaiting human approval: …")``.

    The resolved policy is forwarded to ``is_approved`` so a caller-supplied
    policy (``require(policy=ReviewPolicy(before_merge='required', tier='secure'))``)
    is honoured on the check, not just on the before_merge short-circuit.
    """
    effective_policy = policy if policy is not None else load_policy(env=env)
    if effective_policy.before_merge == "off":
        return True, "review off"
    if is_approved(
        run_id,
        head_sha,
        base_sha=base_sha,
        db_path=db_path,
        env=env,
        policy=effective_policy,
    ):
        # Re-read the approver name without re-querying DB twice.
        approver = status(run_id, db_path=db_path).get("approver") or "human"
        return True, f"approved by {approver}"
    request(
        run_id,
        base_sha,
        head_sha,
        branch=branch,
        base_branch=base_branch,
        db_path=db_path,
    )
    return False, f"awaiting human approval: mini-ork approvals approve {run_id}"
