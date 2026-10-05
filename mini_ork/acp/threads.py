"""Append-only JSONL thread store for ACP orchestrator (Zed) sessions (Z9c-2).

Each thread (an ``orch-<epoch>-<hex>`` session id from
``mini_ork.acp.agent._mint_thread_id``) is persisted as one JSON object per
line under ``<home>/acp-threads/<thread_id>.jsonl``. The store is the
authoritative read model for ``session/list`` and ``session/load`` of threads:
``list_sessions`` shows thread rows alongside run rows; ``load_session``
replays them in file order.

Design notes (kickoff §"threads.py"):

* **Append-only.** No mutation, no rotation. A thread is a log, not a table.
* **I/O errors never raise.** Persistence must not break a live thread —
  an unwritable home (read-only FS, full disk, ...) is logged to stderr and
  the call returns. The spec is explicit: a thread going down because the
  user's home is read-only would be a regression.
* **Defensive reads.** ``read`` skips malformed lines and returns ``[]``
  when the file is missing — mirrors ``LiveTail.read_new`` discipline
  (see ``mini_ork.acp.live``) so future readers reach for one mental model.
* **Id whitelist.** ``thread_id`` must match ``^orch-[A-Za-z0-9-]+$`` —
  the kickoff's explicit anti-traversal guard. Path concatenation is
  join-only; an id with ``..`` or ``/`` cannot reach outside the store
  directory because the regex rejects them up front.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ``orch-<epoch>-<hex>`` — same shape ``_mint_thread_id`` produces. The
# hyphen + alphanumeric tail is the subset of ``_is_safe_token``'s charset
# we accept; dots are intentionally excluded.
_THREAD_ID_RE = re.compile(r"^orch-[A-Za-z0-9-]+$")

# Default page size for ``list_threads``. The kickoff mandates 100; we keep
# it as a constant so callers can override.
_DEFAULT_LIST_LIMIT = 100

# Title cap (kickoff: "first line, ≤ 80 chars, fallback 'mini-ork thread'").
_TITLE_CAP = 80
_DEFAULT_TITLE = "mini-ork thread"


def _normalize_iso(value: Any) -> str | None:
    """Mirror ``history._normalize_ts``: ISO-8601 UTC with ``Z`` suffix.

    Duplicated (rather than imported from ``history.py``) because the
    kickoff draws an explicit boundary around ``history.py``: that module
    is the run-history read model; threads are a sibling concern. Ten
    duplicated lines are cheaper than a new import edge.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return text


def title_from_text(text: str) -> str:
    """First non-empty line of ``text``, capped at ``_TITLE_CAP``.

    Same formula as ``history._title_from_kickoff`` but returns the kickoff's
    ``_DEFAULT_TITLE`` fallback instead of ``""`` — the kickoff mandates the
    fallback for thread list rows.
    """
    if not text:
        return _DEFAULT_TITLE
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            out = stripped.lstrip("#").strip()
            if out.startswith("/run "):  # "/run fix x" names the task "fix x"
                out = out[len("/run "):].strip()
            if out:
                return out[:_TITLE_CAP]
    return _DEFAULT_TITLE


class ThreadStore:
    """Append-only JSONL store of one thread session's lifecycle.

    The store holds records of these types (kickoff §"threads.py"):

    * ``meta`` — ``{"thread_id", "cwd"}``; written once by ``new_session``.
    * ``config`` — ``{"mode", "model", "recipe"}``; full current values on
      ``new_session`` and every ``set_config_option``.
    * ``user`` — ``{"text"}``; each prompt the user sent.
    * ``update`` — ``{"update": <ACP update JSON>}``; an ``AgentMessageChunk``,
      ``ToolCallStart``, ``ToolCallProgress``, etc., captured live via
      ``_emit`` (``model_dump(mode="json", by_alias=True, exclude_none=True)``
      — round-trips through ``SessionNotification.model_validate``).
    * ``claude_session`` — ``{"id"}``; the orchestrator's resume id changes.
    * ``costs`` — ``{"costs": {key: usd}}``; the thread's cost map whenever
      its usage is sent (i.e. mirror of ``self._thread_costs``).
    * ``title`` — ``{"title": "<text>"}``; the latest task-state title
      emitted by the agent (Zed S1). ``list_threads`` shows the most
      recent such record; threads with none fall back to the first
      user prompt's text.
    """

    def __init__(self, home: Path | str) -> None:
        self._home = Path(home)
        self._dir = self._home / "acp-threads"

    @property
    def home(self) -> Path:
        """The project home (parent of the store directory)."""
        return self._home

    def _path_for(self, thread_id: str) -> Path:
        """Resolve the JSONL path for ``thread_id`` after validating it.

        The regex is the only defence against path traversal: a rejected id
        raises ``ValueError`` *before* the path is constructed, so ``..`` or
        ``/`` cannot escape ``self._dir`` via concatenation.
        """
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            raise ValueError(f"unsafe thread id: {thread_id!r}")
        return self._dir / f"{thread_id}.jsonl"

    def append(self, thread_id: str, record: dict[str, Any]) -> None:
        """Append ``record`` to ``thread_id``'s JSONL. Never raises on I/O.

        ``record`` must be a JSON-serializable dict; ``"t": time.time()`` is
        added automatically (one monotonic clock value per record, useful for
        list-sorted diagnostics even though ``list_threads`` orders by file
        mtime). The file is opened in append mode and flushed, so an OS
        crash mid-write loses at most the current line — not the whole file.

        ``ValueError`` (bad id) propagates — the kickoff mandates that the
        regex guard never silently swallows a programming error. Only
        ``OSError`` is caught and logged: persistence must not break a
        live thread (read-only FS, full disk, ...).
        """
        path = self._path_for(thread_id)  # ValueError propagates for bad ids
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            payload = dict(record)
            payload.setdefault("t", time.time())
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
                fh.write("\n")
                fh.flush()
        except OSError as exc:
            print(
                f"[mini-ork] ThreadStore.append({thread_id!r}) failed: {exc}",
                file=sys.stderr,
            )

    def read(self, thread_id: str) -> list[dict[str, Any]]:
        """All records for ``thread_id`` in file order, or ``[]`` if missing.

        Bad lines (malformed JSON, not a dict) are skipped — the file may
        have been truncated or partially rotated, and a partial read is
        more useful than a hard crash on reload.

        ``ValueError`` propagates for an unsafe ``thread_id`` (kickoff: the
        regex guard is a hard contract; a missing or unreadable file is a
        different, recoverable, failure mode).
        """
        path = self._path_for(thread_id)  # ValueError propagates for bad ids
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            return []
        return out

    def exists(self, thread_id: str) -> bool:
        """True when the thread has at least one record on disk."""
        try:
            return self._path_for(thread_id).is_file()
        except ValueError:
            return False

    def list_threads(self, limit: int = _DEFAULT_LIST_LIMIT) -> list[dict[str, Any]]:
        """Thread rows newest-first, capped at ``limit``.

        Each row is ``{"thread_id", "cwd", "title", "updated_at"}``. The
        ``updated_at`` is the file's mtime as ISO-8601 UTC with ``Z``; ``title``
        is the first user prompt's first non-empty line (≤ 80 chars,
        fallback ``"mini-ork thread"``); ``cwd`` is the recorded cwd or ``""``
        when no ``meta`` record is present.

        The walk is defensive: a file with a missing JSONL, an unreadable
        header, or a name that fails the regex is skipped — the directory
        may contain stale files from a prior schema.
        """
        if not self._dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        try:
            entries = list(self._dir.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".jsonl"):
                continue
            stem = entry.name[: -len(".jsonl")]
            if not _THREAD_ID_RE.fullmatch(stem):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            meta, title = self._meta_and_title(stem)
            rows.append(
                {
                    "thread_id": stem,
                    "cwd": meta.get("cwd", ""),
                    "title": title,
                    "updated_at": _normalize_iso(stat.st_mtime),
                }
            )
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return rows[: max(0, int(limit))]

    def _meta_and_title(self, thread_id: str) -> tuple[dict[str, Any], str]:
        """The ``meta`` record and the title, in a single scan of the file.

        The title is the last ``title`` record (Zed S1: the live
        task-state title the agent persisted) when present; otherwise
        it falls back to the first user prompt's text — the original
        behaviour. Both ``meta`` and the first-prompt text are usually
        near the top, but a ``title`` record is written later, so the
        scan can no longer early-exit on the first prompt. A single
        pass over the lines keeps memory bounded to the running
        accumulators (three strings + one dict).
        """
        meta: dict[str, Any] = {}
        first_user: str | None = None
        last_title: str | None = None
        try:
            with open(self._path_for(thread_id), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    rtype = rec.get("type")
                    if rtype == "meta" and not meta:
                        meta = rec
                    elif rtype == "user" and first_user is None:
                        text = rec.get("text")
                        if isinstance(text, str):
                            first_user = title_from_text(text)
                    elif rtype == "title":
                        text = rec.get("title")
                        if isinstance(text, str) and text:
                            last_title = text
        except (OSError, ValueError):
            pass
        return meta, last_title or first_user or _DEFAULT_TITLE


__all__ = ["ThreadStore", "title_from_text"]