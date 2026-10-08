"""D12 — the live mirror must not freeze when it hits its byte budget.

``agent-<node>.live.jsonl`` is the operator's only window into an in-flight
node. The writer used to STOP appending once its byte budget was spent: it wrote
one terminal ``live stream truncated at N bytes`` marker and went silent. On a
node with a large output — the jest fan-out from issue #7 produced tens of MB —
the mirror died mid-run, its mtime froze, and a frozen mirror is
indistinguishable from a hung node.

The writer now ROTATES instead: the full segment is rolled aside to ``<path>.1``
and a fresh one is opened at the live path, so the file always keeps advancing.
A tailer holding a byte offset into the (now shorter) path is not corrupted —
``web/routes/node_live.py`` sees ``offset > size`` and resets to 0, resuming on
the new segment.
"""
from __future__ import annotations

from mini_ork.dispatch.live_stream import LiveWriter


def test_the_live_file_never_freezes_at_the_cap(tmp_path):
    live = tmp_path / "agent-n.live.jsonl"
    w = LiveWriter(str(live), max_bytes=300)
    for i in range(20):
        w.write_line("z" * 40 + f"-{i}")
    for i in range(20, 60):
        w.write_line("z" * 40 + f"-{i}")
    w.close()

    text = live.read_text()
    assert "z" * 40 + "-59" in text          # the newest record landed on disk
    assert w.rotated is True                 # ...because the writer rotated
    assert w.truncated is False              # it never cut the stream
    assert "live stream truncated" not in text  # no terminal marker, ever


def test_rotation_keeps_exactly_one_previous_segment(tmp_path):
    live = tmp_path / "l.jsonl"
    w = LiveWriter(str(live), max_bytes=250)
    for i in range(60):
        w.write_line("y" * 60 + f"-{i}")
    w.close()

    assert (tmp_path / "l.jsonl.1").is_file(), "the previous segment is retained"
    assert not (tmp_path / "l.jsonl.2").exists(), "disk is bounded at two segments"
    assert (tmp_path / "l.jsonl.1").stat().st_size > 0
    # the live path holds only the most recent window, not the whole history
    assert live.stat().st_size < 250 * 4


def test_max_bytes_zero_disables_rotation(tmp_path):
    live = tmp_path / "u.jsonl"
    w = LiveWriter(str(live), max_bytes=0)   # 0 = unbounded, the escape hatch
    for _ in range(200):
        w.write_line("q" * 100)
    w.close()

    assert len(live.read_text().strip().splitlines()) == 200
    assert not (tmp_path / "u.jsonl.1").exists()
    assert w.rotated is False


def test_a_single_oversized_record_is_written_not_dropped(tmp_path):
    # A record larger than the whole budget cannot be split; it is written
    # whole (one-record overshoot) rather than triggering rotation per write.
    live = tmp_path / "big.jsonl"
    w = LiveWriter(str(live), max_bytes=100)
    w.write_line("B" * 5000)
    w.close()

    assert "B" * 5000 in live.read_text()
    assert w.rotated is False



def test_reader_resumes_after_a_rotation(tmp_path):
    # The offset-based reader must treat a shrunken file as a truncation and
    # reset, so a tailer that read the rotated-away segment resumes cleanly
    # instead of skipping into the new file at a stale offset.
    from mini_ork.web.routes.node_live import get_live

    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    live = run_dir / "agent-impl.live.jsonl"
    w = LiveWriter(str(live), max_bytes=400)
    for i in range(60):
        w.write_line("r" * 60 + f"-{i}")
    w.close()

    # a byte offset past the end of the CURRENT (post-rotation) segment — what a
    # tailer that read the rotated-away segment would hold
    stale_offset = live.stat().st_size + 5000
    got = get_live(run_id="r1", node="impl", offset=stale_offset, home=tmp_path)
    assert got["truncated"] is True            # the reader notices and resets
    assert got["offset"] == len(str(got["chunk"]).encode())
    assert w.rotated is True
