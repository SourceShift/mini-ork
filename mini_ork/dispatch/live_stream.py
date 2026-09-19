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
strict sense: the byte cap STOPS appends when the budget is spent rather than
truncating, because rewriting a prefix would leave every open offset pointing at
the wrong byte — a silent corruption that looks like duplicated or garbled
output, not like a cap. ``MO_MAX_LIVE_BYTES`` (default 16 MiB) is deliberately
separate from ``MO_MAX_TRANSCRIPT_BYTES``, which gates ``transcript.json`` and
caps a file nobody tail-reads.

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
    """Byte budget for one live file. An unparseable value falls back to the
    default: this is a budget knob on a diagnostic path, and raising here would
    turn a typo in an env var into a failed dispatch."""
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
        self._truncated = False
        self._started = time.monotonic()
        self._max_bytes = max_live_bytes() if max_bytes is None else max_bytes
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
        return self._truncated

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
            if self._fh is None or self._truncated:
                return
            record["seq"] = self._seq
            record["t"] = round(time.monotonic() - self._started, 3)
            payload = json.dumps(record, ensure_ascii=False) + "\n"
            encoded = len(payload.encode("utf-8", "replace"))
            if self._written + encoded > self._max_bytes:
                self._truncate_locked()
                return
            self._seq += 1
            self._written += encoded
            self._fh.write(payload)
            self._fh.flush()

    def _truncate_locked(self) -> None:
        """Stop appending and record that we did. Called with the lock held.

        The marker is written even when the budget is already spent, so the file
        can overshoot ``max_bytes`` by its size (~140 bytes) once. That is
        deliberate: the budget bounds PAYLOAD, and a capped file with no marker
        is indistinguishable from a short run — a tailer would show the operator
        two lines and let them believe that was all the node said. A bounded
        overshoot is the cheaper wrong answer.
        """
        self._truncated = True
        marker = json.dumps(
            {
                "seq": self._seq,
                "stream": "meta",
                "t": round(time.monotonic() - self._started, 3),
                "line": f"live stream truncated at {self._max_bytes} bytes",
                "truncated": True,
            }
        ) + "\n"
        self._fh.write(marker)  # type: ignore[union-attr]
        self._fh.flush()  # type: ignore[union-attr]

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


def open_live_writer() -> LiveWriter:
    """Writer for the node named by ``MO_LIVE_FILE``, or an inert one.

    Returning an inert writer rather than ``None`` keeps the drain loop free of
    a null check: streaming is a capability the environment grants, and its
    absence must not fork the transport's control flow.
    """
    return LiveWriter(live_file_path())
