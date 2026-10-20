"""Shared live tee for the executor's stdout/stderr (RUN-BUGS RB-31).

``execute.log`` used to be assembled from an ``io.StringIO`` buffer *after*
execute returned. Every path that never got there — a dispatcher that died, a
deadline early-return, a SIGTERM — left the run dir with no log and no
traceback, and the recover path never wrote one at all.

This helper replaces that capture with a tee: each write reaches the in-memory
buffer the lifecycle still reads **and** ``<run_dir>/execute.log`` (a rehearsal
keeps its own ``execute.dryrun.log``), opened when the tee is entered and
line-flushed. The file is closed on every exit path, and a SIGTERM handler
installed for the tee's duration flushes the log before chaining to the
previous behaviour.

Recovery (``mini_ork.recovery.planner.cli_main``'s in-process execute) uses
the same helper with ``recover=True``: attempt *n* tees to
``<run_dir>/execute.recover-<n>.log`` (``n = 1 + existing count``) and appends
one ``recover attempt <n>: see execute.recover-<n>.log`` line to the live
attempt's ``execute.log``, so the dead attempt's log survives.
"""

from __future__ import annotations

import io as _io
import os as _os
import signal as _signal

EXECUTE_LOG = "execute.log"
DRYRUN_LOG = "execute.dryrun.log"
RECOVER_PREFIX = "execute.recover-"
RECOVER_LOG = "execute.recover-{n}.log"
RECOVER_POINTER = "recover attempt {n}: see {name}"


def execute_log_name() -> str:
    """The live run's execute log name; a rehearsal keeps its own sibling."""
    return (DRYRUN_LOG if _os.environ.get("MINI_ORK_DRY_RUN", "0") == "1"
            else EXECUTE_LOG)


def _recover_attempt_count(run_dir: str) -> int:
    """How many ``execute.recover-<n>.log`` files already exist."""
    try:
        names = _os.listdir(run_dir)
    except OSError:
        return 0
    return sum(
        1 for name in names
        if name.startswith(RECOVER_PREFIX) and name.endswith(".log")
    )


class _TeeStream:
    """A text stream that buffers in memory and mirrors to a shared file."""

    def __init__(self, sink=None, buffer=None):
        self._sink = sink
        self._buffer = buffer if buffer is not None else _io.StringIO()

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        self._buffer.write(data)
        sink = self._sink
        if sink is not None:
            try:
                sink.write(data)
                if "\n" in data:
                    sink.flush()
            except (OSError, ValueError):
                pass
        return len(data)

    def flush(self):
        sink = self._sink
        if sink is not None:
            try:
                sink.flush()
            except (OSError, ValueError):
                pass

    def getvalue(self) -> str:
        return self._buffer.getvalue()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return bool(self._sink is not None and self._sink.closed)

    @property
    def encoding(self) -> str:
        return "utf-8"


class _ExecuteTee:
    """Context manager teeing stdout/stderr into ``<run_dir>/execute*.log``.

    Returned by :func:`execute_tee`; entering it opens the file (so it exists
    for the whole execute, not only after it returns), installs the SIGTERM
    flush handler, and exposes the buffers the lifecycle reads afterwards as
    ``stdout`` / ``stderr``.
    """

    def __init__(self, run_dir, *, recover: bool = False):
        self.run_dir = str(run_dir or "")
        self.recover = recover
        self._file = None
        self._closed = False
        self._prev_sigterm = None
        self._sigterm_handler = None
        self.stdout = _TeeStream()
        self.stderr = _TeeStream()

    # -- stream surface (the payload the caller writes to) -----------------

    def write(self, data):
        return self.stdout.write(data)

    def flush(self):
        self.stdout.flush()
        self.stderr.flush()

    def getvalue(self) -> str:
        return self.stdout.getvalue()

    def isatty(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._closed

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _open(self) -> None:
        if not self.run_dir:
            return  # buffer-only: no run dir to write into
        try:
            _os.makedirs(self.run_dir, exist_ok=True)
        except OSError:
            return
        if self.recover:
            n = 1 + _recover_attempt_count(self.run_dir)
            name = RECOVER_LOG.format(n=n)
            self._append_pointer(
                RECOVER_POINTER.format(n=n, name=name) + "\n")
            mode = "w"
        else:
            name = execute_log_name()
            mode = "a"
        path = _os.path.join(self.run_dir, name)
        try:
            # buffering=1 → line-buffered text stream, as the kickoff pins.
            self._file = open(path, mode, encoding="utf-8", buffering=1)
        except OSError:
            self._file = None
            return
        self.stdout._sink = self._file
        self.stderr._sink = self._file
        self._install_sigterm()

    def _append_pointer(self, line: str) -> None:
        try:
            with open(_os.path.join(self.run_dir, EXECUTE_LOG), "a",
                      encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        except OSError:
            pass

    def _install_sigterm(self) -> None:
        try:
            self._prev_sigterm = _signal.getsignal(_signal.SIGTERM)
            self._sigterm_handler = self._handle_sigterm
            _signal.signal(_signal.SIGTERM, self._sigterm_handler)
        except (AttributeError, OSError, ValueError):
            # Not the main thread (no handler install), or no SIGTERM.
            self._sigterm_handler = None

    def _restore_sigterm(self) -> None:
        handler = self._sigterm_handler
        self._sigterm_handler = None
        if handler is None:
            return
        try:
            if _signal.getsignal(_signal.SIGTERM) is handler:
                prev = self._prev_sigterm
                if prev is None:
                    prev = _signal.SIG_DFL
                _signal.signal(_signal.SIGTERM, prev)
        except (OSError, ValueError):
            pass

    def _handle_sigterm(self, signum, frame) -> None:
        """Flush the log, then chain to the behaviour that was installed."""
        self.flush()
        prev = self._prev_sigterm
        self._sigterm_handler = None
        if callable(prev):
            prev(signum, frame)
            return
        if prev == _signal.SIG_IGN:
            return
        try:
            _signal.signal(signum, _signal.SIG_DFL)
        except (OSError, ValueError):
            pass
        _os.kill(_os.getpid(), signum)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._restore_sigterm()
        self.flush()
        fh, self._file = self._file, None
        self.stdout._sink = None
        self.stderr._sink = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def execute_tee(run_dir, *, recover: bool = False) -> _ExecuteTee:
    """Tee execute's stdout/stderr into the run dir for the duration.

    ``recover=False`` writes ``execute.log`` (``execute.dryrun.log`` under
    ``MINI_ORK_DRY_RUN=1``); ``recover=True`` writes the next
    ``execute.recover-<n>.log`` and appends the pointer line to
    ``execute.log``.
    """
    return _ExecuteTee(run_dir, recover=recover)
