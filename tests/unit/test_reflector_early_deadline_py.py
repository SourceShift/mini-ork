"""Unit tests for the reflector workflow node's ``--deadline`` cap.

Hermetic: every external touch ``_handle_reflector_early`` makes
(``_reflect_timeout``, ``_deadline_state``, ``_module_env``, ``subprocess.run``)
is monkeypatched, so no network, DB or run-local state is involved.

``_early_reflect_bound`` composes the history-derived bound from
``_reflect_timeout`` with the post-verdict cap in ``_cap_reflect_to_deadline``:
an unarmed deadline passes the bound through unchanged, an armed one tightens it
to the remaining budget (never negative) plus ``_REFLECT_DEADLINE_GRACE``.
"""
from __future__ import annotations

import subprocess
import types

import mini_ork.cli.execute_handlers as execute_handlers
from mini_ork.cli.main import _REFLECT_DEADLINE_GRACE

DERIVED_SECONDS = 21600
DERIVED_REASON = "derived"


def _arm(monkeypatch, armed, remaining):
    """Point the bound sources at fixed values for one case."""
    monkeypatch.setattr(
        execute_handlers, "_reflect_timeout",
        lambda *a, **k: (DERIVED_SECONDS, DERIVED_REASON),
    )
    monkeypatch.setattr(
        execute_handlers, "_deadline_state", lambda *a, **k: (armed, remaining)
    )
    monkeypatch.setattr(execute_handlers, "_module_env", lambda *a, **k: {})


class _FakeRun:
    """Stand-in for ``subprocess.run`` that records kwargs and plays an outcome."""

    def __init__(self, outcome):
        self.calls = []
        self._outcome = outcome

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def test_bound_unarmed_is_todays_derived_bound(monkeypatch):
    """With no --deadline armed the bound is exactly today's derived value."""
    _arm(monkeypatch, False, 0)
    assert execute_handlers._early_reflect_bound() == (DERIVED_SECONDS, DERIVED_REASON)


def test_bound_armed_caps_to_remainder_plus_grace(monkeypatch):
    """An armed deadline tightens the bound to the remainder plus the grace."""
    _arm(monkeypatch, True, 300)
    seconds, reason = execute_handlers._early_reflect_bound()
    assert seconds == 300 + _REFLECT_DEADLINE_GRACE
    assert "capped to the --deadline remainder" in reason
    assert reason.endswith(DERIVED_REASON)


def test_bound_armed_with_zero_remaining_is_grace_only(monkeypatch):
    """A spent deadline clamps to the grace alone, never negative."""
    _arm(monkeypatch, True, 0)
    seconds, reason = execute_handlers._early_reflect_bound()
    assert seconds == _REFLECT_DEADLINE_GRACE
    assert "deadline already hit" in reason


def test_bound_armed_with_plenty_of_room_is_unchanged(monkeypatch):
    """A cap above the bound is not a cap — the derived value and reason stay."""
    _arm(monkeypatch, True, 100000)
    assert execute_handlers._early_reflect_bound() == (DERIVED_SECONDS, DERIVED_REASON)


def test_handler_passes_capped_timeout_and_succeeds(monkeypatch, tmp_path):
    """The handler runs the child with the capped bound and still returns done."""
    fake = _FakeRun(types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    monkeypatch.setattr(execute_handlers.subprocess, "run", fake)
    _arm(monkeypatch, True, 300)
    assert execute_handlers._handle_reflector_early(str(tmp_path)) == (0, "done")
    assert [call["timeout"] for call in fake.calls] == [300 + _REFLECT_DEADLINE_GRACE]


def test_handler_unarmed_records_todays_derived_timeout(monkeypatch, tmp_path):
    """Without a deadline the child still gets exactly today's derived bound."""
    fake = _FakeRun(types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    monkeypatch.setattr(execute_handlers.subprocess, "run", fake)
    _arm(monkeypatch, False, 0)
    assert execute_handlers._handle_reflector_early(str(tmp_path)) == (0, "done")
    assert [call["timeout"] for call in fake.calls] == [DERIVED_SECONDS]


def test_handler_timeout_expired_never_fails_the_node(monkeypatch, tmp_path, capfd):
    """A timeout is reported with the bound and still returns done."""
    fake = _FakeRun(subprocess.TimeoutExpired(cmd="reflect", timeout=1))
    monkeypatch.setattr(execute_handlers.subprocess, "run", fake)
    _arm(monkeypatch, True, 300)
    assert execute_handlers._handle_reflector_early(str(tmp_path)) == (0, "done")
    assert f"timed out after {300 + _REFLECT_DEADLINE_GRACE}s" in capfd.readouterr().err
