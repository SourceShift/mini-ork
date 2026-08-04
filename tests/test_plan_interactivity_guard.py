"""Interactivity-guard tests for the planner entrypoint (SE-3 Phase A.1b).

``plan._can_prompt_profile`` is the level-a complement to the Phase-A.1 spawn
contract: A.1 stops mini-ork from leaking a controlling terminal into the
harnesses it *spawns*; A.1b stops mini-ork *itself* from blocking on a
``/dev/tty`` prompt when a parent (the Node BE, a scheduler, CI) handed it an
inherited controlling terminal but no human to answer.

The load-bearing regression: a headless run whose stdin is a pipe must NOT be
treated as interactive just because the session still owns a ``/dev/tty``. The
positive signal for "a human can type an answer" is ``stdin.isatty()``; opening
``/dev/tty`` is only a capability check, not a presence check. These pin the
new ordering (explicit opt-out → stdin-is-a-tty → /dev/tty reachable) so the
incident can't silently regress.
"""

from __future__ import annotations

import sys

from mini_ork.cli import plan


class _FakeStdin:
    """Minimal stdin stand-in: reports a fixed isatty() verdict."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_headless_piped_stdin_never_prompts(monkeypatch) -> None:
    """THE incident: a controlling terminal is inherited (open('/dev/tty')
    would succeed) but stdin is a pipe. The old probe returned True and hung;
    the new gate returns False on the stdin check before /dev/tty is ever
    touched — even with MINI_ORK_NONINTERACTIVE unset."""
    monkeypatch.delenv("MINI_ORK_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    # Make /dev/tty openable so the ONLY thing keeping us out is the stdin gate.
    monkeypatch.setattr(plan, "open", lambda *a, **k: _nullctx(), raising=False)
    assert plan._can_prompt_profile() is False


def test_explicit_noninteractive_wins_over_a_real_tty(monkeypatch) -> None:
    """An explicit opt-out beats every positive signal — a real interactive
    tty still yields False when MINI_ORK_NONINTERACTIVE=1."""
    monkeypatch.setenv("MINI_ORK_NONINTERACTIVE", "1")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))
    assert plan._can_prompt_profile() is False


def test_interactive_human_can_prompt(monkeypatch) -> None:
    """A real human: stdin is a tty, opt-out unset, and /dev/tty is reachable →
    prompting is allowed. Confirms the fix didn't slam the door on the
    legitimate interactive-CLI path."""
    monkeypatch.delenv("MINI_ORK_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))

    opened: dict = {}

    def fake_open(path, *a, **k):
        opened["path"] = path
        return _nullctx()

    monkeypatch.setattr(plan, "open", fake_open, raising=False)
    assert plan._can_prompt_profile() is True
    assert opened["path"] == "/dev/tty"  # reached the capability check


def test_detached_stdin_without_isatty_is_not_a_human(monkeypatch) -> None:
    """A daemonized process may hand us a stdin object whose isatty() raises
    (closed fd). That is emphatically not a human — treat it as non-interactive
    rather than letting the exception escape."""

    class _BrokenStdin:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.delenv("MINI_ORK_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", _BrokenStdin())
    assert plan._can_prompt_profile() is False


class _nullctx:
    """Tiny context manager so a fake open() works in a ``with`` block."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
