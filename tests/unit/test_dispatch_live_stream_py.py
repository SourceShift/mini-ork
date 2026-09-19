"""The live tap: does a node's output become visible WHILE it runs?

Every other test in this file could be passed by a writer that buffers the whole
node and dumps it at exit — that is the exact bug being fixed, so it needs an
assertion that distinguishes the two. The one that does is
``test_lines_appear_while_the_child_is_still_running``: it samples the live file
from a second thread and requires the line count to strictly increase *before*
the child exits. A non-incremental writer fails only there, and passes
everything else, which is why that test exists.

The children here are ``python3`` one-liners emitting JSON lines with a sleep
between them, so this suite proves the tap without an LLM call: the transport is
under test, not the provider.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from mini_ork.dispatch.core import spawn_local
from mini_ork.dispatch.live_stream import (
    DEFAULT_MAX_BYTES,
    LiveWriter,
    live_file_path,
    max_live_bytes,
)

# Emits N JSON lines, sleeping between them, so a reader has a window in which
# the child is provably still alive.
_EMITTER = (
    "import json,sys,time\n"
    "n=int(sys.argv[1])\n"
    "for i in range(n):\n"
    "    print(json.dumps({'i': i}), flush=True)\n"
    "    time.sleep(float(sys.argv[2]))\n"
)


def _emit_lines(n: int, gap: float = 0.05) -> list[str]:
    return [sys.executable, "-c", _EMITTER, str(n), str(gap)]


def _read_records(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def test_lines_appear_while_the_child_is_still_running(tmp_path, monkeypatch):
    """THE assertion: output is readable before the process exits.

    Sampling from a thread is what makes this meaningful — reading the file after
    ``spawn_local`` returns cannot tell a live tap from a buffered dump, because
    both leave every line on disk by then.
    """
    live = tmp_path / "agent-n1.live.jsonl"
    monkeypatch.setenv("MO_LIVE_FILE", str(live))

    samples: list[tuple[float, int]] = []
    stop = threading.Event()

    def sample_until_done() -> None:
        while not stop.is_set():
            samples.append((time.monotonic(), len(_read_records(live))))
            time.sleep(0.02)

    watcher = threading.Thread(target=sample_until_done, daemon=True)
    watcher.start()
    started = time.monotonic()
    rc, stdout, _ = spawn_local(_emit_lines(8, 0.05), stdin="", timeout=30.0, env={}, cwd=None)
    elapsed = time.monotonic() - started
    stop.set()
    watcher.join(timeout=5)

    assert rc == 0
    assert elapsed > 0.3, "child exited too fast to distinguish live from buffered"

    # A partial count strictly between 0 and the total, observed while the child
    # was still running, is the whole point.
    mid = [n for _, n in samples if 0 < n < 8]
    assert mid, f"live file never showed a partial count mid-run; samples={samples[:10]}"
    assert max(mid) < 8 or min(n for _, n in samples) == 0

    # And the child's stdout still reached the caller whole — the tee must not
    # consume what the parsers need.
    assert len(stdout.splitlines()) == 8


def test_records_carry_seq_stream_and_monotonic_time(tmp_path):
    live = tmp_path / "run.live.jsonl"
    writer = LiveWriter(str(live))
    writer.write_line('{"a":1}\n', "stdout")
    writer.write_line("boom\n", "stderr")
    writer.close()

    recs = _read_records(live)
    assert [r["seq"] for r in recs] == [0, 1]
    assert [r["stream"] for r in recs] == ["stdout", "stderr"]
    assert [r["line"] for r in recs] == ['{"a":1}', "boom"]
    assert recs[0]["t"] <= recs[1]["t"], "timestamps must be monotonic"


def test_stderr_is_tapped_too(tmp_path, monkeypatch):
    live = tmp_path / "run.live.jsonl"
    monkeypatch.setenv("MO_LIVE_FILE", str(live))
    rc, _, stderr = spawn_local(
        [sys.executable, "-c", "import sys;sys.stderr.write('bad\\n');sys.stderr.flush()"],
        stdin="",
        timeout=30.0,
        env={},
        cwd=None,
    )
    assert rc == 0
    assert stderr == "bad\n"
    assert [r["line"] for r in _read_records(live)] == ["bad"]


def test_trailing_fragment_is_flagged_not_dropped(tmp_path, monkeypatch):
    """A line killed mid-write is the one that says why the run died."""
    live = tmp_path / "run.live.jsonl"
    monkeypatch.setenv("MO_LIVE_FILE", str(live))
    # No trailing newline: the fragment must survive, marked partial, so a
    # consumer parsing this as JSONL does not read it as a malformed record.
    rc, _, _ = spawn_local(
        [sys.executable, "-c", "import sys;sys.stdout.write('half');sys.stdout.flush()"],
        stdin="",
        timeout=30.0,
        env={},
        cwd=None,
    )
    assert rc == 0
    recs = _read_records(live)
    assert [r["line"] for r in recs] == ["half"]
    assert recs[0].get("partial") is True


def test_stdin_larger_than_the_pipe_buffer_does_not_deadlock(monkeypatch):
    """The prompt is written on its own thread for this reason: a >64 KiB prompt
    written inline would block the thread that owns the timeout, and a child that
    reads stdin slowly would then outlive its own deadline."""
    monkeypatch.delenv("MO_LIVE_FILE", raising=False)
    reader = (
        "import sys\n"
        "data=sys.stdin.read()\n"
        "print(len(data), flush=True)\n"
    )
    big = "x" * 300_000
    rc, stdout, _ = spawn_local(
        [sys.executable, "-c", reader], stdin=big, timeout=30.0, env={}, cwd=None
    )
    assert rc == 0
    assert stdout.strip() == "300000"


def test_timeout_still_returns_124_and_tees_what_was_emitted(tmp_path, monkeypatch):
    """The rc=124 contract is load-bearing (dispatch_with_fallback abandons the
    lane on it), so replacing communicate() must not disturb it. The lines the
    hung child managed to emit before the kill must still be on disk."""
    live = tmp_path / "run.live.jsonl"
    monkeypatch.setenv("MO_LIVE_FILE", str(live))
    rc, stdout, stderr = spawn_local(
        _emit_lines(200, 0.5), stdin="", timeout=1.0, env={}, cwd=None
    )
    assert rc == 124
    assert stdout == ""
    assert stderr == "timeout after 1.0s"
    assert len(_read_records(live)) >= 1, "lines emitted before the kill were lost"


def test_spawn_failure_still_returns_127(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_LIVE_FILE", str(tmp_path / "run.live.jsonl"))
    rc, _, err = spawn_local(["/nonexistent/harness"], stdin="", timeout=5.0, env={}, cwd=None)
    assert rc == 127
    assert "spawn failed" in err


def test_no_live_file_means_no_file_is_created(tmp_path, monkeypatch):
    """Streaming is a capability the environment grants; without it the
    transport must behave exactly as before."""
    monkeypatch.delenv("MO_LIVE_FILE", raising=False)
    rc, stdout, _ = spawn_local(_emit_lines(3, 0.0), stdin="", timeout=30.0, env={}, cwd=None)
    assert rc == 0
    assert len(stdout.splitlines()) == 3
    assert live_file_path() == ""
    assert list(tmp_path.iterdir()) == []


def test_byte_cap_stops_appends_rather_than_rewriting(tmp_path):
    """A tailer holds a byte offset into this file, so the cap must never
    truncate a prefix — it stops, leaving every already-published byte valid."""
    live = tmp_path / "run.live.jsonl"
    writer = LiveWriter(str(live), max_bytes=200)
    for i in range(100):
        writer.write_line(f"line-{i}\n")
    writer.close()

    recs = _read_records(live)
    assert writer.truncated is True
    assert len(recs) < 100
    # Every record but the marker is a real line, and the marker is last.
    marker = [r for r in recs if r.get("truncated")]
    assert len(marker) == 1
    assert recs[-1].get("truncated") is True
    assert marker[0]["seq"] == len(recs) - 1


def test_byte_cap_bounds_the_file(tmp_path):
    """Payload respects the budget; the truncation marker is allowed a bounded
    overshoot so a capped file is never mistaken for a short one."""
    live = tmp_path / "run.live.jsonl"
    writer = LiveWriter(str(live), max_bytes=500)
    for _ in range(50):
        writer.write_line("y" * 100 + "\n")
    writer.close()
    assert writer.truncated is True
    assert live.stat().st_size <= 500 + 256


def test_max_live_bytes_env_is_read_and_bad_values_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_MAX_LIVE_BYTES", "1234")
    assert max_live_bytes() == 1234
    monkeypatch.setenv("MO_MAX_LIVE_BYTES", "not-a-number")
    # A typo in a diagnostic budget must not fail the dispatch that sets it.
    assert max_live_bytes() == DEFAULT_MAX_BYTES
    monkeypatch.delenv("MO_MAX_LIVE_BYTES", raising=False)
    assert max_live_bytes() == DEFAULT_MAX_BYTES


def test_appends_across_writers_so_a_resume_keeps_its_history(tmp_path):
    live = tmp_path / "run.live.jsonl"
    first = LiveWriter(str(live))
    first.write_line("one\n")
    first.close()
    second = LiveWriter(str(live))
    second.write_line("two\n")
    second.close()
    assert [r["line"] for r in _read_records(live)] == ["one", "two"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_concurrent_writers_do_not_interleave_within_a_record(tmp_path, stream):
    """stdout and stderr are drained by two threads sharing one writer; a torn
    record would be a JSON parse error on the tailing side."""
    live = tmp_path / "run.live.jsonl"
    writer = LiveWriter(str(live))
    threads = [
        threading.Thread(
            target=lambda: [writer.write_line(f"payload-{i}" * 20 + "\n", stream) for i in range(200)]
        )
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    recs = _read_records(live)
    assert len(recs) == 800
    assert sorted(r["seq"] for r in recs) == list(range(800))
