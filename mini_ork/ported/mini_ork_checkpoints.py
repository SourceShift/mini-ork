"""Durable DAG checkpoint writer + validity check (E1 of feat/durable-dag).

This module is the **runtime seam** for the per-node checkpoint system
described in ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md``.
It is intentionally distinct from ``mini_ork.ported.checkpoint`` (the
bash-parity status tracker that mirrors ``lib/checkpoint.sh``); that file
answers "is this node already done?" via a JSON sidecar, while this
module answers "is this node's work durably reusable on a future attempt?"
via the ``node_checkpoints`` SQLite table.

The seam's load-bearing contract is the **publication order** in design §4:

    1. caller has already produced every artifact on disk
    2. ``write_checkpoint`` enumerates them, sha256s each one, builds the
       manifest, fsyncs them BEFORE the transaction begins
    3. ONE SQLite transaction inserts the ``node_checkpoints`` row (PK
       on (run_id, node_id) → INSERT OR REPLACE keeps the latest valid
       attempt) AND the ``node_attempts`` row
    4. the row is durable only after the transaction commits

A crash between (1) and (3) leaves no row (window 1 — rerun, correct).
A crash between (3) and a future re-hash leaves a row whose manifest
artifacts may be missing/corrupt (window 2 — validity check rejects,
rerun, correct). Both windows are fail-closed by ``is_node_reusable``.

Public API:
  write_checkpoint(...) -> int
      rc=0 on success, non-zero on failure. No exceptions bubble. Caller
      treats non-zero as "checkpoint not written; node will rerun on next
      attempt" — by design, fail-closed.
  is_node_reusable(...) -> bool
      Always returns a bool. NEVER raises — any exception path is coerced
      to False (the design rule from §3 + risk_notes: "MUST fail closed").
  hash_file(path) -> str
      sha256 hex of a file's bytes; returns "" on any error (caller
      decides what to do).
  sha256_bytes(b) -> str
      Convenience for hashing an in-memory blob.

Reader pattern (E2 will use this, E1 just needs it for tests):
  ``is_node_reusable(db, run_id, node_id, current_input_hash=...,
                     current_recipe_version=..., current_config_hash=...,
                     run_dir=...)`` — all three current-* values are the
  resolution the RUNTIME performed for the new attempt; if any differs
  from the stored value, the node is NOT reusable and the runtime must
  rerun it (and its transitive dependents).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from typing import Iterable

__all__ = [
    "write_checkpoint",
    "is_node_reusable",
    "hash_file",
    "sha256_bytes",
    "compute_artifact_manifest",
]

# Reuse the project-wide pragma default from db_open.mo_sqlite — keep
# writer busy-timeout consistent with every other SQLite write in the
# Python runtime, so a long-running checkpoint doesn't trip
# `database is locked` under concurrent reads.
_DEFAULT_BUSY_MS = 5000


def _log_err(msg: str) -> None:
    sys.stderr.write(f"mini_ork_checkpoints: {msg}\n")


def _open(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=_DEFAULT_BUSY_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_DEFAULT_BUSY_MS}")
    return con


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hash_file(path: str) -> str:
    """sha256 hex of a file's bytes; "" on any error (missing, unreadable).

    Empty-string sentinel (vs raising) keeps the writer's best-effort
    contract: a partial-hash failure on a single artifact fails the whole
    write rather than letting a corrupt row commit. The caller (writer)
    treats "" as "skip this artifact" because manifest hashing is a
    all-or-nothing property.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def compute_artifact_manifest(artifact_paths: Iterable[str], run_dir: str) -> list[dict]:
    """Build the per-node artifact manifest.

    Each entry: ``{"path": <rel>, "sha256": <hex>, "bytes": <n>}`` where
    ``path`` is RELATIVE to ``run_dir`` (no leading '/', no '..') — the
    same portability convention as run_artifacts (0047).

    Non-existent files are OMITTED from the manifest (caller treats empty
    manifest as "no artifacts to checkpoint"). Symlinks are resolved
    once for the bytes count + sha256 (we read the link target, not the
    symlink itself) but their path stays the caller-supplied rel path so
    the row is portable across filesystems.

    Directories are NOT walked — the caller passes a flat list of file
    paths. Walking a directory at write time would require a policy
    decision (ignore globs, depth limit, hidden files) that the E1
    kickoff deliberately leaves to E2/E5.
    """
    if not run_dir:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in artifact_paths:
        if not raw:
            continue
        rel = raw
        if os.path.isabs(rel):
            try:
                rel = os.path.relpath(rel, run_dir)
            except ValueError:
                # different drive on Windows; surface as a separate entry
                rel = raw
        # Path-safety: reject absolute and parent-relative entries
        # (run_artifacts 0047 convention; prevents a stored manifest
        # from ever pointing at a file outside the run dir).
        if os.path.isabs(rel) or rel.startswith("..") or "/.." in rel or rel == "..":
            _log_err(f"skip unsafe artifact path: {raw!r}")
            continue
        if rel in seen:
            continue
        seen.add(rel)
        abs_path = os.path.join(run_dir, rel)
        if not os.path.isfile(abs_path):
            continue
        digest = hash_file(abs_path)
        if not digest:
            continue
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            continue
        out.append({"path": rel, "sha256": digest, "bytes": size})
    return out


def write_checkpoint(
    db: str,
    run_id: str,
    node_id: str,
    *,
    status: str,
    input_hash: str,
    recipe_version: str,
    config_hash: str,
    artifact_paths: Iterable[str],
    run_dir: str,
    node_type: str = "",
    started_at: int,
    ended_at: int,
    cost_usd: float | None = None,
    provider_session_id: str = "",
    initiator: str = "python",
    failure_class: str | None = None,
    attempt: int | None = None,
    owner_token: str | None = None,
) -> int:
    """Crash-safe per-node checkpoint publication. Returns 0 on success.

    Publication order (design §4, encoded for the verifier to assert):

      a) compute manifest (hashing every artifact's bytes — this is the
         "verify hashes" gate; the sha256 is computed from the live file,
         not from a stored claim, so a partial write cannot escape
         detection)
      b) fsync each artifact file — once the row is committed, on
         recovery we MUST be able to re-hash these files and get the
         same digest. os.fsync is the only guarantee for that.
      c) open a single SQLite transaction and INSERT OR REPLACE into
         node_checkpoints (PK on (run_id, node_id)) and INSERT into
         node_attempts. The transaction commits only if BOTH inserts
         succeed — a partial commit is impossible (this is what makes
         the publication order "atomic" from the read side).

    Best-effort contract: a failure at any step returns non-zero and
    logs to stderr. The caller treats the node as "not checkpointed"
    and the runtime will rerun it on the next attempt — exactly the
    fail-closed behavior design §4 requires for crash window 1.
    """
    if not db or not run_id or not node_id:
        _log_err("write_checkpoint: db, run_id, node_id are required")
        return 1
    if status not in ("success", "failure", "skipped"):
        _log_err(f"write_checkpoint: status must be success|failure|skipped, got {status!r}")
        return 1
    if not input_hash or not recipe_version or not config_hash:
        _log_err("write_checkpoint: input_hash, recipe_version, config_hash are required")
        return 1
    if not os.path.isfile(db):
        _log_err(f"write_checkpoint: db not found: {db}")
        return 1

    # (E3 fencing) When a caller presents an owner_token it is asserting it
    # holds the run's single-writer lease. Reject the publish if that token is
    # NOT the current LIVE holder — this is what stops a stale worker (whose
    # lease expired and was re-acquired by a newer recovery) from committing a
    # checkpoint over the top of live work (design §7). When no token is passed
    # (legacy non-recovery path) fencing is disabled — preserves E1's contract.
    if owner_token is not None:
        try:
            from mini_ork.ported.lease import is_lease_holder  # lazy: avoid load-order coupling
            if not is_lease_holder(db, run_id, owner_token):
                _log_err(
                    f"write_checkpoint: fence rejected — token {owner_token!r} is not the "
                    f"live lease holder of run {run_id!r}; refusing to publish {node_id!r}"
                )
                return 2
        except Exception as e:  # noqa: BLE001 — fence must fail closed
            _log_err(f"write_checkpoint: fence check errored ({e}); refusing to publish")
            return 2

    # (a) + (b) build manifest and fsync artifacts BEFORE the row.
    # If we crash between (a) and (c) the next recovery sees no row
    # (window 1) and reruns — correct.
    manifest = compute_artifact_manifest(artifact_paths, run_dir)
    for entry in manifest:
        abs_path = os.path.join(run_dir, entry["path"])
        try:
            with open(abs_path, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError as e:
            _log_err(f"write_checkpoint: fsync failed for {entry['path']}: {e}")
            return 1

    manifest_json = json.dumps(manifest, separators=(",", ":"))
    attempt_no = int(attempt) if attempt is not None else 1
    now = int(ended_at) if ended_at else 0

    # (E4) Persist the provider's session transcript into the run dir so a
    # future recovery can `claude --resume <session_id>` even after the worker
    # sandbox dies. Best-effort: a failure here leaves session_ref NULL and the
    # node simply re-runs from scratch on recovery (still correct). This does
    # NOT affect is_node_reusable — session_ref is never read by the validity
    # check (design §6).
    session_ref: str | None = None
    if provider_session_id and run_dir:
        try:
            from mini_ork.ported.session_store import persist_session  # lazy
            ref = persist_session(run_dir, provider_session_id)
            session_ref = ref or None
        except Exception as e:  # noqa: BLE001 — persistence is best-effort
            _log_err(f"write_checkpoint: session persist skipped ({e})")

    # (c) one transaction for both rows.
    con = None
    try:
        con = _open(db)
        try:
            con.execute("BEGIN")
            con.execute(
                """
                INSERT OR REPLACE INTO node_checkpoints (
                    run_id, node_id, attempt, status, input_hash,
                    recipe_version, config_hash, artifact_manifest_json,
                    session_ref, failure_class, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, node_id, attempt_no, status, input_hash,
                 recipe_version, config_hash, manifest_json,
                 session_ref, failure_class, now),
            )
            con.execute(
                """
                INSERT INTO node_attempts (
                    run_id, node_id, attempt_no, node_type, started_at,
                    ended_at, result, failure_class, checkpoint_used,
                    checkpoint_produced, cost_usd, provider_session_id, initiator
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
                """,
                (run_id, node_id, attempt_no, node_type or "",
                 int(started_at) if started_at else now, now,
                 status, failure_class, cost_usd,
                 provider_session_id or None, initiator or "python"),
            )
            con.execute("COMMIT")
        finally:
            con.close()
        return 0
    except sqlite3.Error as e:
        _log_err(f"write_checkpoint: sqlite error: {e}")
        if con is not None:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        return 1
    except Exception as e:  # noqa: BLE001 — best-effort seam
        _log_err(f"write_checkpoint: unexpected error: {e}")
        return 1


def is_node_reusable(
    db: str,
    run_id: str,
    node_id: str,
    *,
    current_input_hash: str,
    current_recipe_version: str,
    current_config_hash: str,
    run_dir: str = "",
) -> bool:
    """Decide whether a prior node's checkpoint is safe to reuse.

    Design §3 rules, all three must hold:
      1. input_hash matches the current run's resolved inputs.
      2. recipe_version and config_hash match the current run.
      3. every artifact in artifact_manifest_json exists and its sha256
         verifies against the live file's bytes.

    Any other outcome (no row, hash mismatch, missing file, corrupt file,
    malformed manifest, sqlite error) returns False. The function NEVER
    raises — the design rule is "fail closed". Callers can rely on
    ``bool(is_node_reusable(...))`` being safe in any context.

    The run_dir is optional but REQUIRED for rule 3; if a caller omits
    it, rule 3 is treated as failing (returned False) so the runtime
    cannot accidentally re-use a checkpoint whose artifacts it cannot
    re-verify.
    """
    if not db or not run_id or not node_id:
        return False
    if not current_input_hash or not current_recipe_version or not current_config_hash:
        return False
    if not os.path.isfile(db):
        return False
    try:
        con = _open(db)
        try:
            row = con.execute(
                """
                SELECT input_hash, recipe_version, config_hash,
                       artifact_manifest_json, status
                FROM node_checkpoints
                WHERE run_id = ? AND node_id = ?
                """,
                (run_id, node_id),
            ).fetchone()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — fail closed
        _log_err(f"is_node_reusable: sqlite error: {e}")
        return False

    if row is None:
        return False
    stored_input, stored_recipe, stored_config, manifest_json, status = row
    # Only success-status checkpoints are reusable. failure/skipped are
    # observable but the runtime must NOT skip work it has to redo.
    if status != "success":
        return False
    if stored_input != current_input_hash:
        return False
    if stored_recipe != current_recipe_version:
        return False
    if stored_config != current_config_hash:
        return False

    if not run_dir or not os.path.isdir(run_dir):
        return False
    try:
        manifest = json.loads(manifest_json) if manifest_json else []
    except (TypeError, ValueError):
        return False
    if not isinstance(manifest, list):
        return False
    if not manifest:
        # success with no artifacts is reusable (the node ran but emitted
        # nothing to disk; e.g. a planner that wrote only into state.db)
        return True
    for entry in manifest:
        if not isinstance(entry, dict):
            return False
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str):
            return False
        abs_path = os.path.join(run_dir, rel)
        if os.path.isabs(rel) or rel.startswith("..") or "/.." in rel or rel == "..":
            return False
        if not os.path.isfile(abs_path):
            return False
        actual = hash_file(abs_path)
        if not actual or actual != expected:
            return False
    return True
