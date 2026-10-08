"""The terminal-failure trigger has exactly ONE owner.

Two loops react to a failed run: the cross-run failure TRIAGE (this hook,
``MO_FAILURE_TRIAGE=1`` → attribute + promote a fix epic) and the peer's in-run
AUTO-REPAIR loop (``MO_AUTO_REPAIR=1`` → ``recover --from-node`` on the same run,
which calls triage itself on give-up). Letting both fire spends two fixes on one
failure, so the repair loop, when it owns the run, sets ``MO_AUTO_REPAIR=1`` and
this hook stands down.

The gate is OPT-IN (``== "1"``, not the peer's proposed ``!= "0"``): an unset
``MO_AUTO_REPAIR`` must leave today's behavior untouched. A default-on gate
would silence triage for every existing ``MO_FAILURE_TRIAGE=1`` run the moment
it landed on main, including runs auto-repair never touches.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.triage import failures as tf  # noqa: E402


def _spy(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_triage(run_id, *, home=None, db=None, root=None,
                    promote=False, dry_run=False):
        calls.append({"run_id": run_id, "promote": promote, "dry_run": dry_run})
        return object()

    monkeypatch.setattr(tf, "triage_run", fake_triage)
    return calls


def _clear(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


def test_triage_fires_when_auto_repair_is_unset(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setenv("MO_FAILURE_TRIAGE", "1")
    _clear(monkeypatch, "MO_AUTO_REPAIR")

    ex._maybe_triage_failed_run("db", "r1", "/home", "/root")

    assert len(calls) == 1 and calls[0]["run_id"] == "r1"


def test_triage_stands_down_when_auto_repair_owns_the_trigger(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setenv("MO_FAILURE_TRIAGE", "1")
    monkeypatch.setenv("MO_AUTO_REPAIR", "1")

    ex._maybe_triage_failed_run("db", "r1", "/home", "/root")

    assert calls == []          # the repair loop's give-up call owns triage


def test_triage_fires_when_auto_repair_is_explicitly_off(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setenv("MO_FAILURE_TRIAGE", "1")
    monkeypatch.setenv("MO_AUTO_REPAIR", "0")

    ex._maybe_triage_failed_run("db", "r1", "/home", "/root")

    assert len(calls) == 1


def test_no_triage_when_mo_failure_triage_unset(monkeypatch):
    calls = _spy(monkeypatch)
    _clear(monkeypatch, "MO_FAILURE_TRIAGE", "MO_AUTO_REPAIR")

    ex._maybe_triage_failed_run("db", "r1", "/home", "/root")

    assert calls == []          # still off by default
