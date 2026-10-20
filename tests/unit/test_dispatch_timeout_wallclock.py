"""RBF-21 — a lane timeout is bounded after the kill and records its wall clock.

The timeout path in ``mini_ork.dispatch.core.spawn_local`` used to call an
unbounded ``proc.wait()`` after ``_terminate_process_group(proc)``. A child that
survived the group kill could therefore hold a lane for thousands of seconds
after a "timeout after 120.0s" was recorded, and nothing captured when the kill
happened or when the child was reaped. These tests pin the bound and the error
text that makes the gap attributable.

Hermetic: real but tiny local subprocesses, a minimal child environment, no
network, no credentials, no run-local state.
"""

from __future__ import annotations

import re
import sys
import time

import pytest

from mini_ork.dispatch.core import spawn_local

#: The arrow exactly as the kickoff writes it: U+2192 RIGHTWARDS ARROW.
ARROW = "→"


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """No live-file tee into a run-local sidecar, and no inherited reap timeout
    (each test starts from the documented default unless it sets its own)."""
    monkeypatch.delenv("MO_LIVE_FILE", raising=False)
    monkeypatch.delenv("MO_KILL_REAP_TIMEOUT_S", raising=False)


def _child_env(**extra: str) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    env.update(extra)
    return env


def test_sleeping_child_times_out_bounded_with_wallclock():
    """A 30 s sleeper with a 1 s lane timeout returns rc 124 within ~5 s and the
    error text carries the bounded-reap wall clock."""
    start = time.time()
    rc, _out, err = spawn_local(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin="",
        timeout=1,
        env=_child_env(),
        cwd=None,
    )
    elapsed = time.time() - start
    assert rc == 124, f"expected the timeout rc 124, got {rc!r} (err={err!r})"
    assert elapsed < 5.0, f"timeout path not bounded within ~5 s: {elapsed:.1f}s"
    assert err.startswith("timeout after 1"), f"leading prefix changed: {err!r}"
    assert f"spawned{ARROW}kill" in err, f"no spawned→kill timing in: {err!r}"
    assert f"kill{ARROW}reaped" in err, f"no kill→reaped timing in: {err!r}"


def test_timeout_error_text_follows_the_declared_template():
    """The error text is exactly
    ``timeout after {timeout}s (spawned→kill {a:.0f}s, kill→reaped {b:.0f}s)``,
    with plausible wall-clock values (not constants, not a swapped pair)."""
    rc, _out, err = spawn_local(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin="",
        timeout=2,
        env=_child_env(),
        cwd=None,
    )
    assert rc == 124, f"expected the timeout rc 124, got {rc!r} (err={err!r})"
    pattern = r"timeout after 2(\.0)?s \(spawned→kill (\d+)s, kill→reaped (\d+)s\)"
    match = re.fullmatch(pattern, err)
    assert match is not None, f"error text does not follow the template: {err!r}"
    spawned_to_kill = int(match.group(2))
    kill_to_reaped = int(match.group(3))
    # The kill fires when the 2 s lane timeout expires; the reap of a SIGKILLed
    # child is immediate. Constants (always 0) or a swapped pair fail these.
    assert 1 <= spawned_to_kill <= 6, f"implausible spawned→kill {spawned_to_kill}s in {err!r}"
    assert 0 <= kill_to_reaped <= 6, f"implausible kill→reaped {kill_to_reaped}s in {err!r}"


def test_sigterm_ignoring_child_returns_bounded(monkeypatch):
    """A child that ignores SIGTERM still returns within ~15 s when
    ``MO_KILL_REAP_TIMEOUT_S=1``."""
    monkeypatch.setenv("MO_KILL_REAP_TIMEOUT_S", "1")
    start = time.time()
    rc, _out, err = spawn_local(
        ["/bin/bash", "-c", "trap '' TERM; sleep 60"],
        stdin="",
        timeout=1,
        env=_child_env(MO_KILL_REAP_TIMEOUT_S="1"),
        cwd=None,
    )
    elapsed = time.time() - start
    assert elapsed < 15.0, f"SIGTERM-immune child not returned within ~15 s: {elapsed:.1f}s"
    assert rc == 124, f"expected the timeout rc 124, got {rc!r} (err={err!r})"
    assert err.startswith("timeout after 1"), f"leading prefix changed: {err!r}"
    assert f"kill{ARROW}reaped" in err, f"no kill→reaped timing in: {err!r}"


def test_fast_child_rc_and_output_unchanged():
    """A fast child keeps its rc and streams; no timeout text is fabricated."""
    code = (
        "import sys\n"
        "print('fast-out-line')\n"
        "print('fast-err-line', file=sys.stderr)\n"
        "sys.exit(7)\n"
    )
    rc, out, err = spawn_local(
        [sys.executable, "-c", code],
        stdin="",
        timeout=30,
        env=_child_env(),
        cwd=None,
    )
    assert rc == 7, f"fast-child rc changed: {rc!r} (err={err!r})"
    assert out == "fast-out-line\n", f"stdout changed: {out!r}"
    assert "fast-err-line" in err, f"stderr lost: {err!r}"
    assert "timeout after" not in err, f"timeout text on a non-timeout run: {err!r}"
