"""Append-and-flush live sidecar for one dispatched node's output.

Dispatch buffers today: ``spawn_local`` hands the whole conversation to
``proc.communicate()``, so stdout is only observable *after* the harness exits.
That is why a run shows nothing on a UI until each node is already over — the
output exists, but only at the end, in one lump.

The B0 probe settled whether a live view is even buildable on the lanes we have
(all three working gateways relay ``content_block_delta``: glm spanned 2.8s of
generation, minimax 1.7s, deepseek 4.6s, with first delta at 36-51% of the wall
window). So the missing piece is not provider support; it is a sink.

Two constraints shape this writer:

**A tailer holds a byte offset.** A reader opens this file, records where it got
to, and comes back later for the delta. That makes the file append-only in the
strict sense: the byte budget ROTATES the file when it is spent rather than
truncating a prefix in place, because rewriting a prefix would leave every open
offset pointing at the wrong byte — a silent corruption that looks like
duplicated or garbled output, not like a cap. Rotation replaces the whole file
with a shorter, fresh segment; a reader that finds ``offset > size`` treats it
as a truncation and resets to 0 (``web/routes/node_live.py``), so the live view
resumes on the new segment instead of stalling. The failure this avoids is the
one an earlier version had: the mirror stopped dead at the cap, its mtime froze,
and an operator watching a large node could not tell it apart from a hang.

``MO_MAX_LIVE_BYTES`` (default 16 MiB) bounds ONE SEGMENT — the writer keeps the
current segment plus the one it just rotated aside (``<path>.1``), so disk stays
bounded at two segments per node while the live path always advances. It is
deliberately separate from ``MO_MAX_TRANSCRIPT_BYTES``, which gates
``transcript.json`` and caps a file nobody tail-reads. Setting it to ``0``
disables rotation: the live file then grows unbounded, for a node whose output
is legitimately large and whose operator would rather have one long file than a
rotated pair.

**Records are raw transport lines, not parsed events.** The provider-specific
JSON envelope is the parser callables' business (``dispatch`` injects
``parse_usage``/``parse_text``/… so the core stays provider-agnostic). Folding a
provider's schema in here would put claude's envelope shape in the transport
layer and freeze it for codex, opencode and the openai-chat lanes, which share
this writer but not that schema.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LIVE_FILE_ENV = "MO_LIVE_FILE"
MAX_BYTES_ENV = "MO_MAX_LIVE_BYTES"
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


def live_file_path() -> str:
    """The live sidecar for THIS node, or ``""`` when streaming is off.

    Read at dispatch time rather than import time so a caller can set it per
    node (and so tests can point it at a tmp dir without reloading the module).
    """
    return os.environ.get(LIVE_FILE_ENV, "").strip()


def max_live_bytes() -> int:
    """Byte budget for one live SEGMENT before the writer rotates, or ``0`` for
    unbounded. An unparseable value falls back to the default: this is a budget
    knob on a diagnostic path, and raising here would turn a typo in an env var
    into a failed dispatch."""
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


class LiveWriter:
    """Thread-safe append-and-flush JSONL sink.

    One instance is shared by the stdout and stderr drain threads, so every
    mutation takes the lock. Writes are flushed per record, not buffered: a
    tailer polling this file cannot see a line that is still sitting in the
    writer's buffer, and a flush deferred to close() is exactly the buffering
    this module exists to remove.
    """

    def __init__(self, path: str, *, max_bytes: int | None = None) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._written = 0
        self._rotations = 0
        self._started = time.monotonic()
        self._max_bytes = max_live_bytes() if max_bytes is None else max_bytes
        self._path = path
        self._fh = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            # Append, not truncate: a resumed run keeps its earlier records, and
            # a tailer that already read them must not see them renumbered.
            self._fh = open(path, "a", encoding="utf-8", errors="replace")

    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def truncated(self) -> bool:
        """Retained for callers that predate rotation. The writer no longer cuts
        the stream when the budget is spent — it rotates — so this is always
        False; see :attr:`rotated`."""
        return False

    @property
    def rotated(self) -> bool:
        """True once the writer has rolled a full segment aside at least once."""
        return self._rotations > 0

    def write_line(self, line: str, stream: str = "stdout", *, partial: bool = False) -> None:
        """Record one complete transport line.

        ``partial`` marks a trailing fragment that reached EOF without its
        newline. It is kept rather than dropped (it is often the last line of a
        killed run, i.e. the one that says why) but flagged, so a consumer
        parsing this file as JSONL does not treat a half-line as a malformed
        one.
        """
        line = line.rstrip("\r\n")
        record: dict[str, object] = {
            "seq": 0,  # replaced under the lock
            "stream": stream,
            "t": 0.0,
            "line": line,
        }
        if partial:
            record["partial"] = True
        with self._lock:
            if self._fh is None:
                return
            record["seq"] = self._seq
            record["t"] = round(time.monotonic() - self._started, 3)
            payload = json.dumps(record, ensure_ascii=False) + "\n"
            encoded = len(payload.encode("utf-8", "replace"))
            # ``self._written`` guards the degenerate case: a single record
            # larger than the whole budget must still be written (it cannot be
            # split) rather than triggering a rotation on every write.
            if (self._max_bytes and self._written
                    and self._written + encoded > self._max_bytes):
                self._rotate_locked()
                if self._fh is None:
                    return
            self._seq += 1
            self._written += encoded
            self._fh.write(payload)
            self._fh.flush()

    def _rotate_locked(self) -> None:
        """Roll the full segment aside and continue in a fresh file.

        Called with the lock held, once the current segment's budget is spent.
        The live path ALWAYS keeps advancing: the failure this replaces was a
        mirror that stopped dead at the cap, its mtime frozen, which an operator
        watching a large node cannot tell apart from a hang. A tailer holds a
        byte offset into ``self._path``; after rotation that path is a shorter,
        fresh file, and the reader (``web/routes/node_live.py``) treats
        ``offset > size`` as a truncation and resets to 0 — so the live view
        resumes on the new segment instead of stalling.

        Exactly one previous segment is retained (``<path>.1``): disk stays
        bounded at two segments per node, the mirror keeps the most recent
        window of output, and the authoritative full record remains in the
        node's transcript.
        """
        self._rotations += 1
        marker = json.dumps(
            {
                "seq": self._seq,
                "stream": "meta",
                "t": round(time.monotonic() - self._started, 3),
                "line": f"live segment rotated at {self._max_bytes} bytes",
                "rotated": True,
            }
        ) + "\n"
        if self._fh is not None:
            try:
                self._fh.write(marker)
                self._fh.flush()
            finally:
                self._fh.close()
        self._fh = None
        try:
            os.replace(self._path, self._path + ".1")
        except OSError:
            pass
        try:
            self._fh = open(self._path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None
        self._written = 0

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()
                    self._fh = None

    def __enter__(self) -> "LiveWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_live_writer(path: str | None = None) -> LiveWriter:
    """Writer for ``path`` (the dispatch request's ``MO_LIVE_FILE``) or, when
    ``path`` is None, for the process-level ``MO_LIVE_FILE``; inert when both
    are empty.

    Returning an inert writer rather than ``None`` keeps the drain loop free of
    a null check: streaming is a capability the environment grants, and its
    absence must not fork the transport's control flow.
    """
    return LiveWriter(live_file_path() if path is None else path.strip())
