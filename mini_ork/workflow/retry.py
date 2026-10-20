"""Per-node ``retry:`` block — LangGraph-matched RetryPolicy semantics for mini-ork.

P1.4 of spec-driven-delivery. The per-node ``retry:`` block in workflow.yaml
governs *infra-level* attempts (transient infrastructure failures). It is
deliberately distinct from the ``retries`` edge: that edge drives revise
rounds when a reviewer/verifier rejects the output, while the ``retry:``
block re-runs the same node when dispatch itself failed (timeout, infra
crash, transient provider error). Their caps are independent — see the
parity table in ``docs/LANGGRAPH_PARITY.md``.

LangGraph defaults (source: langgraph/types.py — see ``LANGGRAPH_DEFAULTS_URL``
below):

  * ``max_attempts``     default 3   (including the first attempt)
  * ``initial_interval`` default 0.5 seconds
  * ``backoff_factor``   default 2.0
  * ``max_interval``     default 128.0 seconds
  * ``jitter``           default True (bounded: ``[interval/2, interval]``)

The five fields are spelled exactly as LangGraph spells them so a recipe that
copies a LangGraph example verbatim is one-to-one edit on the block.

Public API
----------
* :class:`RetryPolicy` — frozen dataclass with the five fields + defaults
* :func:`default_retry_policy` — a fresh copy of LangGraph's defaults
* :func:`parse_retry_policy` — parse one ``retry:`` mapping (None → defaults)
* :func:`load_retry_policies` — parse every node's ``retry:`` from a path
* :func:`compute_backoff` — exponential backoff with bounded jitter (F6)
* :func:`mark_retry_error` — persist an ERROR row on terminal failure (F4)
* :func:`retryable_finish_reason` — which ``finish_reason`` strings qualify

The runtime seam lives in ``mini_ork.cli.execute_handlers.dispatch_node``,
which wraps the handler invocation in :func:`run_with_retry`. The plan
mandates that the executor loop here stay import-light: tests are allowed
to import this module without dragging in the CLI executor.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

# Source of the RetryPolicy default values is LangGraph's own NamedTuple at
# https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py
# (verified 2026-10-12 against the ``RetryPolicy`` NamedTuple definition).
# Any change here MUST be paired with a citation in docs/LANGGRAPH_PARITY.md
# so the parity claim stays auditable.
LANGGRAPH_DEFAULTS_URL = (
    "https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/"
    "langgraph/types.py"
)

# JSON schema field names (kept as module constants so a typo is one diff).
FIELD_MAX_ATTEMPTS = "max_attempts"
FIELD_INITIAL_INTERVAL = "initial_interval"
FIELD_BACKOFF_FACTOR = "backoff_factor"
FIELD_MAX_INTERVAL = "max_interval"
FIELD_JITTER = "jitter"

# Retryable finish_reason values. A "code" rejection (reviewer said the
# change was wrong) is the ``retries`` edge's domain, not this one — the
# retry block handles infra/transient failures. The exhaustive set is kept
# narrow on purpose; an arbitrary string would silently turn a code-shaped
# rejection into an expensive re-run.
RETRYABLE_FINISH_REASONS: frozenset[str] = frozenset({
    "timeout",          # provider call timed out
    "infra_interrupt",  # dispatcher died mid-node
    "provider_limit",   # transient 429 / 5xx
    "network",          # connection reset, DNS failure
    "overloaded",       # provider 529 overloaded
    "rate_limited",     # provider rate-limit signal
    "killed",           # process killed (SIGKILL, OOM, etc.)
    "sigterm",          # SIGTERM caught
    "rc_137",           # killed by SIGKILL (128+9)
    "rc_143",           # killed by SIGTERM (128+15)
    # RBF-22a: a remote node-agent outage (605 s exec read timeout, node
    # unreachable after its own retries). Retryable, but under its own tight
    # budget — see the override below.
    "remote_unavailable",
})

# RBF-22a: the remote-outage budget. Two retries (three attempts total, the
# LangGraph default ``max_attempts``) with a flat 30 s wait: the remote
# transport already retried internally, so compounding exponential backoff on
# top of a 605 s read timeout only lengthens the outage.
REMOTE_UNAVAILABLE_FINISH_REASON = "remote_unavailable"
REMOTE_UNAVAILABLE_MAX_RETRIES = 2
REMOTE_UNAVAILABLE_BACKOFF_SECONDS = 30.0

# Per-finish-reason backoff overrides. A reason absent here keeps the node's
# parsed ``RetryPolicy`` envelope from :func:`compute_backoff`.
RETRY_BACKOFF_OVERRIDES: dict[str, float] = {
    REMOTE_UNAVAILABLE_FINISH_REASON: REMOTE_UNAVAILABLE_BACKOFF_SECONDS,
}


@dataclass(frozen=True)
class RetryPolicy:
    """Per-node retry policy — fields and defaults mirror LangGraph's RetryPolicy.

    Defaults: max_attempts=3, initial_interval=0.5, backoff_factor=2.0,
    max_interval=128.0, jitter=True. See ``LANGGRAPH_DEFAULTS_URL`` for the
    source-of-truth in LangGraph's own codebase.

    The dataclass is frozen so a parsed policy cannot be mutated between
    attempts (the executor logs the same envelope every retry; a mutation
    would silently change the wait schedule).
    """

    max_attempts: int = 3
    initial_interval: float = 0.5
    backoff_factor: float = 2.0
    max_interval: float = 128.0
    jitter: bool = True

    def __post_init__(self) -> None:
        # Field-level invariants. A workflow.yaml author can declare nonsense
        # (max_attempts=0, max_interval=0.0, negative intervals) — the
        # executor would silently loop forever or never retry, so we fail
        # closed at parse time.
        if self.max_attempts < 1:
            raise ValueError(
                f"retry.max_attempts must be >= 1 (got {self.max_attempts})"
            )
        if self.initial_interval < 0:
            raise ValueError(
                f"retry.initial_interval must be >= 0 (got {self.initial_interval})"
            )
        if self.backoff_factor < 1:
            raise ValueError(
                f"retry.backoff_factor must be >= 1 (got {self.backoff_factor})"
            )
        if self.max_interval < 0:
            raise ValueError(
                f"retry.max_interval must be >= 0 (got {self.max_interval})"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view of the policy — for trace payloads and tests."""
        return {
            FIELD_MAX_ATTEMPTS: self.max_attempts,
            FIELD_INITIAL_INTERVAL: self.initial_interval,
            FIELD_BACKOFF_FACTOR: self.backoff_factor,
            FIELD_MAX_INTERVAL: self.max_interval,
            FIELD_JITTER: self.jitter,
        }


def default_retry_policy() -> RetryPolicy:
    """A fresh copy of LangGraph's defaults.

    Returns a new instance each call so callers can mutate the fields
    without poisoning later reads. ``RetryPolicy`` is frozen so this is
    only useful when the caller wants ``dataclasses.replace`` semantics —
    the executor always uses ``parse_retry_policy`` on the YAML.
    """
    return RetryPolicy()


def parse_retry_policy(raw: Any, *, node_id: str = "<unknown>") -> RetryPolicy:
    """Parse a single ``retry:`` mapping into a ``RetryPolicy``.

    Accepts ``None`` (use defaults), an empty mapping (use defaults), or a
    mapping that overrides one or more fields. Unknown keys raise so the
    schema's ``additionalProperties: false`` is enforced in code as well
    as on disk — a typo (``initial_inverval: 1``) would otherwise be
    silently dropped and the operator would wonder why a node is not
    retrying.
    """
    if raw is None:
        return default_retry_policy()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"node {node_id} retry must be an object, got {type(raw).__name__}"
        )
    unknown = set(raw.keys()) - {
        FIELD_MAX_ATTEMPTS, FIELD_INITIAL_INTERVAL, FIELD_BACKOFF_FACTOR,
        FIELD_MAX_INTERVAL, FIELD_JITTER,
    }
    if unknown:
        raise ValueError(
            f"node {node_id} retry has unknown fields: {sorted(unknown)}"
        )
    kwargs: dict[str, Any] = {}
    if FIELD_MAX_ATTEMPTS in raw:
        value = raw[FIELD_MAX_ATTEMPTS]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"node {node_id} retry.max_attempts must be an integer "
                f"(got {type(value).__name__})"
            )
        kwargs[FIELD_MAX_ATTEMPTS] = value
    for field in (FIELD_INITIAL_INTERVAL, FIELD_BACKOFF_FACTOR, FIELD_MAX_INTERVAL):
        if field in raw:
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"node {node_id} retry.{field} must be a number "
                    f"(got {type(value).__name__})"
                )
            kwargs[field] = float(value)
    if FIELD_JITTER in raw:
        value = raw[FIELD_JITTER]
        if not isinstance(value, bool):
            raise ValueError(
                f"node {node_id} retry.jitter must be a boolean "
                f"(got {type(value).__name__})"
            )
        kwargs[FIELD_JITTER] = value
    return RetryPolicy(**kwargs)


def load_retry_policies(workflow_path: str | Path) -> dict[str, RetryPolicy]:
    """Read every node's ``retry:`` block from ``workflow_path``.

    Returns ``{node_id: RetryPolicy}``. A node with no ``retry:`` block is
    absent from the result — the executor uses the defaults via
    :func:`default_retry_policy` so an absent entry is fine, and the test
    suite relies on "absent == defaults" to keep fixtures tiny.

    Workflow files that don't exist or are unreadable yield an empty
    dict (the caller treats this as "no overrides"). The schema does the
    rest of the validation; the executor never raises a YAML error here
    because ``compile_workflow`` has already validated the workflow.
    """
    path = Path(workflow_path)
    out: dict[str, RetryPolicy] = {}
    if not path.is_file():
        return out
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return out
    if not isinstance(document, Mapping):
        return out
    for raw_node in document.get("nodes") or []:
        if not isinstance(raw_node, Mapping):
            continue
        node_id = str(raw_node.get("name") or "").strip()
        if not node_id:
            continue
        try:
            out[node_id] = parse_retry_policy(raw_node.get("retry"), node_id=node_id)
        except ValueError:
            # Re-raise with context. A YAML that parses fine but a
            # retry block that is malformed must not be silently swallowed.
            raise
    return out


# ── backoff envelope ──────────────────────────────────────────────────────────


def compute_backoff(
    policy: RetryPolicy,
    *,
    attempt: int,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with bounded jitter (F6).

    ``attempt`` is the attempt number that JUST failed (1 = the first
    attempt; the wait before attempt 2 is ``initial_interval``). The
    envelope is ``initial_interval * backoff_factor ** (attempt - 1)``
    clamped at ``max_interval``; with jitter on, the actual sleep is
    sampled from ``[interval / 2, interval]`` (LangGraph-style half-jitter).

    A ``rng`` argument is exposed for deterministic tests (seed with
    ``random.Random(42)``); production code passes ``None`` and the
    module uses ``random.SystemRandom``. Bounded jitter — every output
    of this function is in ``[0, max_interval]`` and the un-jittered
    base is never exceeded. A previous draft used ``random.uniform(0,
    interval)`` which would occasionally overshoot ``max_interval`` under
    jitter; that bug is the reason this function exists.
    """
    if attempt < 1:
        return 0.0
    base = policy.initial_interval * (policy.backoff_factor ** (attempt - 1))
    if policy.max_interval > 0:
        base = min(base, policy.max_interval)
    if not policy.jitter:
        return base
    rng = rng or random.SystemRandom()
    # Half-jitter: [base/2, base]. ``SystemRandom`` is the production
    # source; the optional ``rng`` argument is the test hook.
    return rng.uniform(base / 2.0, base)


def retryable_finish_reason(finish_reason: str) -> bool:
    """True when ``finish_reason`` is one the retry loop will re-dispatch on.

    The set is narrow on purpose — see :data:`RETRYABLE_FINISH_REASONS`.
    A reviewer ``reject`` or verifier ``refute`` is a *code* rejection
    and goes through the ``retries`` edge, not this loop.
    """
    return finish_reason in RETRYABLE_FINISH_REASONS


def backoff_for_finish_reason(
    policy: RetryPolicy,
    finish_reason: str,
    *,
    attempt: int,
    rng: random.Random | None = None,
) -> float:
    """The wait before the next attempt after ``finish_reason`` failed.

    A per-reason override (``RETRY_BACKOFF_OVERRIDES``) wins over the policy
    envelope: ``remote_unavailable`` waits a flat 30 s because the remote
    transport already spent its own retry budget, while every other retryable
    reason keeps the node's configured exponential backoff.
    """
    override = RETRY_BACKOFF_OVERRIDES.get(str(finish_reason))
    if override is not None:
        return float(override)
    return compute_backoff(policy, attempt=attempt, rng=rng)


# ── ERROR checkpoint row (F4) ────────────────────────────────────────────────
#
# P1.2 ships a ``mini_ork/checkpoints/`` module with completion rows; P1.3
# ships ``mini-ork run --resume <id>``. Neither is merged yet, so the row
# written here is the best the current schema can carry: a sidecar JSON
# file in the run directory PLUS a best-effort ``node_attempts`` row
# (when the ``node_attempts`` table from migration 0050 exists). Both
# forms are non-overlapping with P1.2's planned contract — the JSON
# sidecar's path is namespaced under ``retry-exhausted/`` so a later
# merge that lands ``mini_ork/checkpoints/`` cannot mistake a retry
# failure for a successful completion.

ERROR_ROW_DIR = "retry-exhausted"
ERROR_ROW_FILENAME = "error.json"


@dataclass(frozen=True)
class ErrorRow:
    """One terminal retry-exhaustion row for a node.

    The ``attempt`` field is the number of attempts that ran before the
    terminal failure (1-indexed). The ``finish_reasons`` field records
    what each attempt said it failed with so a future operator can read
    why the loop gave up without grepping the run dir.
    """

    node_id: str
    attempt: int
    max_attempts: int
    finish_reasons: tuple[str, ...]
    policy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "finish_reasons": list(self.finish_reasons),
            "policy": self.policy,
            "marker": "retry_exhausted",
        }


def mark_retry_error(
    run_dir: str | Path,
    row: ErrorRow,
    *,
    db_path: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, str]:
    """Persist the terminal-failure row (F4) and return its locations.

    Two JSON actions:
      * Always: write ``<run_dir>/retry-exhausted/<node>.json`` (the
        sidecar the P1.2/P1.3 resume path will read). The file is the
        source of truth; if the DB write below fails the resume still
        finds the row.
      * Best-effort: append a ``node_attempts`` row with
        ``result='failure'``, ``failure_class='retry_exhausted'`` when
        ``db_path`` is provided AND the ``node_attempts`` table exists
        (migration 0050). The DB write uses a plain ``sqlite3``
        connection (same pattern as ``mini_ork.recovery.auto_repair``)
        because ``web.db.StateDB`` opens read-only.

    Returns a dict ``{"sidecar": ..., "attempt_row": ...}`` naming the
    written paths (empty string on miss). The function never raises —
    a half-written run dir, missing table, or unwritable disk leaves the
    sidecar (the sidecar is the authoritative copy).
    """
    run_dir_eff = Path(run_dir)
    out: dict[str, str] = {"sidecar": "", "attempt_row": ""}
    sidecar_dir = run_dir_eff / ERROR_ROW_DIR
    try:
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sidecar_dir / f"{row.node_id}{ERROR_ROW_FILENAME}.tmp"
        sidecar.write_text(
            json.dumps(row.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(sidecar, sidecar_dir / f"{row.node_id}{ERROR_ROW_FILENAME}")
        out["sidecar"] = str(sidecar_dir / f"{row.node_id}{ERROR_ROW_FILENAME}")
    except OSError:
        # Best-effort. The dispatch path returns rc != 0 regardless.
        pass
    if db_path and run_id:
        try:
            db = Path(db_path)
            if db.is_file():
                con = sqlite3.connect(str(db), timeout=5.0)
                try:
                    con.execute("PRAGMA busy_timeout = 5000")
                    # migration 0050 creates node_attempts; missing table
                    # is the legacy path and is silently skipped.
                    con.execute(
                        "SELECT attempt_no FROM node_attempts LIMIT 0",
                    )
                    now = int(time.time())
                    con.execute(
                        "INSERT INTO node_attempts ("
                        " run_id, node_id, attempt_no, node_type,"
                        " started_at, ended_at, result, failure_class,"
                        " checkpoint_used, checkpoint_produced, initiator"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'failure',"
                        " 'retry_exhausted', 0, 0, 'retry-policy')",
                        (run_id, row.node_id, row.attempt, "", now, now),
                    )
                    con.commit()
                    out["attempt_row"] = str(db)
                finally:
                    con.close()
        except sqlite3.Error:
            # Legacy DB / no migration: the sidecar is enough.
            pass
    return out


# ── attempt loop ─────────────────────────────────────────────────────────────


def should_retry(
    rc: int,
    finish_reason: str,
    attempt: int,
    policy: RetryPolicy,
) -> bool:
    """True when the loop should attempt another dispatch.

    ``rc != 0`` is required (a green dispatch never retries) AND the
    finish reason must be one the retry block recognises. An
    unrecognised reason (``verifier_refute``, ``reviewer_reject``, …)
    falls through to ``False`` — that is a *code* failure, owned by
    the ``retries`` edge, and re-running the same dispatch would not
    produce a different answer.
    """
    if rc == 0:
        return False
    if attempt >= policy.max_attempts:
        return False
    return retryable_finish_reason(finish_reason)


def load_attempt_log(run_dir: str | Path, node_id: str) -> list[dict[str, Any]]:
    """The attempts this node has already made in the current run, if any.

    Reads ``<run_dir>/retry-exhausted/<node>.attempts.json`` when present.
    A later attempt's policy loop uses this to seed the attempt counter
    on a re-dispatch so the cap (``max_attempts``) is preserved across
    a process split. Empty list when the file is absent or unreadable.
    """
    path = Path(run_dir) / ERROR_ROW_DIR / f"{node_id}.attempts.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def record_attempt(
    run_dir: str | Path,
    node_id: str,
    attempt: int,
    finish_reason: str,
    rc: int,
) -> None:
    """Append one attempt to ``<run_dir>/retry-exhausted/<node>.attempts.json``.

    Best-effort. Used by the runtime seam so a recovered resume can see
    what attempts already happened and not start the loop over. Fails
    silent — the sidecar is the audit trail, not this log.
    """
    path = Path(run_dir) / ERROR_ROW_DIR / f"{node_id}.attempts.json"
    try:
        existing = load_attempt_log(run_dir, node_id)
    except Exception:  # noqa: BLE001
        existing = []
    existing.append({
        "attempt": int(attempt),
        "rc": int(rc),
        "finish_reason": str(finish_reason),
        "ts": int(time.time()),
    })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ── iteration helper for tests ─────────────────────────────────────────────────


def attempt_schedule(
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> list[float]:
    """The wait schedule across ``policy.max_attempts`` attempts.

    Returns ``[w1, w2, …, w_{max_attempts-1}]`` where ``wi`` is the
    wait BEFORE attempt ``i+1`` (the wait after attempt ``i`` failed).
    Length is ``max_attempts - 1`` because the first attempt needs no
    wait. Pure function — tests use this to assert the envelope is
    respected (F1, F6).
    """
    return [compute_backoff(policy, attempt=i, rng=rng) for i in range(1, policy.max_attempts)]


__all__ = [
    "RetryPolicy",
    "default_retry_policy",
    "parse_retry_policy",
    "load_retry_policies",
    "compute_backoff",
    "retryable_finish_reason",
    "RETRYABLE_FINISH_REASONS",
    "REMOTE_UNAVAILABLE_FINISH_REASON",
    "REMOTE_UNAVAILABLE_MAX_RETRIES",
    "REMOTE_UNAVAILABLE_BACKOFF_SECONDS",
    "RETRY_BACKOFF_OVERRIDES",
    "backoff_for_finish_reason",
    "ErrorRow",
    "mark_retry_error",
    "should_retry",
    "load_attempt_log",
    "record_attempt",
    "attempt_schedule",
    "LANGGRAPH_DEFAULTS_URL",
]