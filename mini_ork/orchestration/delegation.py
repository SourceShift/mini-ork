"""Delegation ledger — record who asked for a run, and where its row lives.

Three write-path gaps made one month of delegations un-joinable: a run launched
from another project writes its ``task_runs`` row into THAT project's
``state.db``; nothing records the initiator; and the delivered artifact was
never written back. This module is the write boundary for all three.

Every public function is **fail-soft**: a delegation must never fail because its
bookkeeping could not be written. ``sqlite3.Error`` / ``OSError`` are caught,
one ``[warn]`` line goes to stderr, and the call returns. The one exception is
deliberate: a pre-0091 schema (no ledger columns yet) returns *silently*, so the
command keeps working on the database it is diagnosing and existing callers that
build a minimal ``task_runs`` table do not suddenly grow stderr output.

Stdlib only — this module is imported by CLI entry points and must not pull in
``mini_ork.cli.*`` (which would create an import cycle).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

_LEDGER_COLUMNS = ("initiator", "kickoff_copy", "kickoff_sha256", "home_db")
_ARTIFACT_COLUMNS = ("artifact_path", "artifact_hash")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INITIATOR_MAX = 120
_ANCESTRY_MAX_HOPS = 20
_HOMES_VERSION = 1
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.05


def _warn(message: str) -> None:
    sys.stderr.write(f"[warn] delegation: {message}\n")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── initiator resolution ────────────────────────────────────────────────────

def _ps_output(argv: list[str]):
    """Run ``ps`` and return its stdout, or ``None`` when it cannot be read.

    ``capture_output``/``text`` keep the test seam (a monkeypatched
    ``subprocess.run``) and the real call on the same shape.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return getattr(proc, "stdout", "") or ""


def _session_id_for(pid: int) -> str:
    """``claude-session:<id>`` from the process environment, else its pid."""
    text = _ps_output(["ps", "eww", "-p", str(pid)])
    if text:
        match = re.search(r"CLAUDE_SESSION_ID=(\S+)", text)
        if match:
            return f"claude-session:{match.group(1)}"
    return f"claude-session:{pid}"


def _claude_ancestor() -> str:
    """Walk the process tree for a ``claude`` ancestor; never raises."""
    try:
        pid = os.getpid()
    except OSError:
        return ""
    hops = 0
    while pid and pid > 0 and hops < _ANCESTRY_MAX_HOPS:
        hops += 1
        ppid_text = _ps_output(["ps", "-o", "ppid=", "-p", str(pid)])
        if not ppid_text:
            return ""
        try:
            ppid = int(ppid_text.splitlines()[0].strip())
        except (ValueError, IndexError):
            return ""
        comm_text = _ps_output(["ps", "-o", "comm=", "-p", str(pid)])
        if comm_text:
            comm = comm_text.strip().splitlines()[0]
            if "claude" in os.path.basename(comm).lower():
                return _session_id_for(pid)
        if ppid == pid or ppid <= 0:
            return ""
        pid = ppid
    return ""


def resolve_initiator(*, env=None) -> str:
    """Return the documented initiator string; first match wins.

    1. ``MO_INITIATOR`` (control characters stripped, capped at 120 chars).
    2. ``MO_SCHEDULER_DISPATCH=1`` -> ``scheduler``.
    3. ``MO_RECOVER_OF`` -> ``recover:<value>``.
    4. ``MO_RSI_CYCLE_ID`` -> ``rsi-cycle:<value>``.
    5. a ``claude`` process ancestor -> ``claude-session:<id|pid>``.
    6. otherwise ``manual`` (``manual:<uid>`` where ``os.getuid`` exists).
    """
    env_map = os.environ if env is None else env

    explicit = env_map.get("MO_INITIATOR") or ""
    if explicit:
        return _CONTROL_RE.sub("", explicit)[:_INITIATOR_MAX]
    if (env_map.get("MO_SCHEDULER_DISPATCH") or "").strip() == "1":
        return "scheduler"
    recover_of = (env_map.get("MO_RECOVER_OF") or "").strip()
    if recover_of:
        return f"recover:{recover_of}"
    rsi_cycle = (env_map.get("MO_RSI_CYCLE_ID") or "").strip()
    if rsi_cycle:
        return f"rsi-cycle:{rsi_cycle}"
    ancestor = _claude_ancestor()
    if ancestor:
        return ancestor
    try:
        return f"manual:{os.getuid()}"
    except AttributeError:  # pragma: no cover — non-POSIX runner
        return "manual"


# ── kickoff copy ────────────────────────────────────────────────────────────

def copy_kickoff(run_dir: str, kickoff_path: str) -> tuple[str, str]:
    """Copy the kickoff to ``<run_dir>/kickoff.md``; return ``(name, sha256)``.

    Idempotent: a destination that already holds the same digest is left
    untouched. Any unusable path yields ``("", "")`` so the caller stores NULLs
    — never a wrong hash for the wrong file, and never a copy into a run dir
    that does not exist.
    """
    dest = os.path.join(run_dir, "kickoff.md") if run_dir else ""
    try:
        if not kickoff_path or not os.path.isfile(kickoff_path):
            return "", ""
        if not run_dir or not os.path.isdir(run_dir):
            return "", ""
        digest = _sha256_file(kickoff_path)
        if dest and os.path.isfile(dest):
            try:
                if _sha256_file(dest) == digest:
                    return "kickoff.md", digest
            except OSError:
                pass
        tmp = dest + ".tmp"
        try:
            with open(kickoff_path, "rb") as src, open(tmp, "wb") as out:
                for chunk in iter(lambda: src.read(65536), b""):
                    out.write(chunk)
            os.replace(tmp, dest)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return "", ""
        return "kickoff.md", digest
    except OSError:
        return "", ""


# ── homes registry ──────────────────────────────────────────────────────────

def homes_registry_path(*, env=None) -> str:
    """``MO_HOMES_REGISTRY`` when set, else ``~/.mini-ork/homes.json``."""
    env_map = os.environ if env is None else env
    override = (env_map.get("MO_HOMES_REGISTRY") or "").strip()
    if override:
        return os.path.expanduser(override)
    home = env_map.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".mini-ork", "homes.json")


def _read_homes(path: str) -> list[str]:
    """Read the registry, treating a corrupt/unreadable file as empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    homes = data.get("homes") if isinstance(data, dict) else None
    if not isinstance(homes, list):
        return []
    return [home for home in homes if isinstance(home, str) and home]


def _write_homes(path: str, homes: list[str]) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".homes.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": _HOMES_VERSION, "homes": homes}, fh)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _acquire_lock(lockdir: str) -> bool:
    """mkdir-lock with a bounded wait (same convention as the worktree driver)."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            os.mkdir(lockdir)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL_S)
        except OSError:
            return False


def _release_lock(lockdir: str) -> None:
    try:
        os.rmdir(lockdir)
    except OSError:
        pass


def add_home(path: str, *, env=None) -> None:
    """Append ``path`` to the homes registry, deduped by absolute path.

    Simple tier: ``~/.mini-ork/homes.json``. Secure tier: point
    ``MO_HOMES_REGISTRY`` at a shared location. A corrupt registry is rewritten
    from empty, and a lock timeout is a warning, not an error.
    """
    if not path:
        return
    env_map = os.environ if env is None else env
    try:
        target = os.path.abspath(path)
    except (OSError, ValueError):
        return
    registry = homes_registry_path(env=env_map)
    try:
        os.makedirs(os.path.dirname(registry) or ".", exist_ok=True)
    except OSError:
        return
    lockdir = registry + ".lockdir"
    locked = _acquire_lock(lockdir)
    if not locked:
        _warn(f"homes registry lock timeout: {lockdir} (writing without it)")
    try:
        homes = _read_homes(registry)
        if target not in homes:
            homes.append(target)
        _write_homes(registry, homes)
    except OSError as exc:
        _warn(f"add_home({target}): {exc}")
    finally:
        if locked:
            _release_lock(lockdir)


# ── ledger writers ──────────────────────────────────────────────────────────

def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _has_columns(con: sqlite3.Connection, required: tuple[str, ...]) -> bool:
    columns = _table_columns(con, "task_runs")
    return bool(columns) and all(name in columns for name in required)


def record_dispatch(db_path: str, run_id: str, *, kickoff_path: str,
                    run_dir: str = "", db_path_abs: str = "",
                    env=None, now: int | None = None) -> dict:
    """Fill the ledger columns on an EXISTING ``task_runs`` row.

    Updates, never inserts: both call sites already own the row, and a second
    INSERT could race the first. A SELECT first, so a run whose row has not
    landed yet returns ``{}`` rather than silently writing nothing. Returns the
    dict it wrote so a caller (or test) can assert on it.

    Fail-soft on a real write error (one ``[warn]`` line, ``{}`` returned) and
    silent on a pre-0091 schema (no ledger columns yet).
    """
    if not db_path or not run_id:
        return {}
    env_map = os.environ if env is None else env
    home_db = os.path.abspath(db_path_abs or db_path or "")
    updated_at = int(time.time()) if now is None else int(now)
    try:
        con = sqlite3.connect(db_path, timeout=15.0)
        try:
            # Probe BEFORE copying: a pre-0091 schema is a silent no-op, so the
            # diagnosing caller sees no stderr and no stray run-dir file.
            if not _has_columns(con, _LEDGER_COLUMNS):
                return {}
            if con.execute("SELECT id FROM task_runs WHERE id=?", (run_id,)).fetchone() is None:
                return {}
            copy_name, copy_digest = copy_kickoff(run_dir, kickoff_path)
            values = {
                "initiator": resolve_initiator(env=env_map),
                "kickoff_copy": copy_name or None,
                "kickoff_sha256": copy_digest or None,
                "home_db": home_db,
                "updated_at": updated_at,
            }
            con.execute(
                "UPDATE task_runs SET initiator=?, kickoff_copy=?, kickoff_sha256=?,"
                " home_db=?, updated_at=? WHERE id=?",
                (values["initiator"], values["kickoff_copy"], values["kickoff_sha256"],
                 values["home_db"], values["updated_at"], run_id),
            )
            con.commit()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        _warn(f"record_dispatch({run_id}): {exc}")
        return {}

    add_home(home_db, env=env_map)
    return values


def set_artifact_path(db_path: str, run_id: str, path: str, *,
                      now: int | None = None) -> None:
    """Record the delivered artifact's path and sha256 on the run's row.

    A path that does not exist records NULL for both rather than a path that
    cannot be opened. Fail-soft, and silent when the row predates the artifact
    columns.
    """
    if not db_path or not run_id:
        return
    try:
        artifact_path = path if path and os.path.isfile(path) else None
        artifact_hash = _sha256_file(artifact_path) if artifact_path else None
        updated_at = int(time.time()) if now is None else int(now)
        con = sqlite3.connect(db_path, timeout=15.0)
        try:
            if not _has_columns(con, _ARTIFACT_COLUMNS):
                return
            if con.execute("SELECT id FROM task_runs WHERE id=?", (run_id,)).fetchone() is None:
                return
            con.execute(
                "UPDATE task_runs SET artifact_path=?, artifact_hash=?, updated_at=?"
                " WHERE id=?",
                (artifact_path, artifact_hash, updated_at, run_id),
            )
            con.commit()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        _warn(f"set_artifact_path({run_id}): {exc}")
