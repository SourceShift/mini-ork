"""Standalone unit tests for ``mini_ork.gates.coord_gate``.

Replaces the bash-parity gate (against ``lib/coord_gate.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer drives the LIVE bash function via
``bash -c 'source lib/coord_gate.sh; coord_gate_check ...'`` — it asserts
the port's behaviour directly. The expected values below are the semantic
contract the bash side used to pin (rc semantics, stderr nudge/deny
messages, metrics shape, audit ring buffer), now asserted on the port's
output.

Ten cases:
  (a) check advisory + no conflict        → rc=0, empty stdout, empty stderr
  (b) check advisory + active write holder → rc=0, stderr "WAIT before..."
  (c) check strict + in-scope conflict    → rc=11, stderr "strict deny..."
  (d) check strict + out-of-scope conflict → rc=0, stderr "WAIT before...
                                             ...out of strict scope..."
  (e) check bad mode                       → rc=2, stderr "mode must be..."
  (f) metrics() on empty state             → 4-key default JSON
  (g) metrics_field after bump_sequence     → integer counters
  (h) audit() bounded ring buffer           → most-recent-first, count/max
  (i) check strict + no conflict           → rc=0, empty streams
  (j) metrics coord_leases_held            → observe() counts the LIVE
                                             registry snapshot (1, then 2)

Cases (i) and (j) were ported from tests/integration/test_coord_gate.sh
(strict-mode-no-conflict silence, and the observe()-driven live-lease
count) when that bash fixture was retired.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import coord_gate as cg


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Fresh home dir per test — no DB needed (registry is JSON).

    Pins the file-path env knobs for the in-process Python port via
    monkeypatch.setenv, so it always targets the per-test temp dir
    regardless of the host shell's HOME.
    """
    h = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(h))
    monkeypatch.setenv("COORD_GATE_METRICS_FILE", str(h / "metrics.json"))
    monkeypatch.setenv("COORD_GATE_AUDIT_FILE", str(h / "audit.json"))
    monkeypatch.setenv("COORD_REGISTRY_STATE_FILE", str(h / "registry.json"))
    monkeypatch.delenv("COORD_GATE_MODE", raising=False)
    monkeypatch.delenv("COORD_GATE_SCOPE", raising=False)
    monkeypatch.delenv("COORD_GATE_AUDIT_MAX", raising=False)
    return h


def _seed_registry(home: Path, *, leases: list[dict] | None = None,
                   waits: dict | None = None) -> None:
    """Write a registry state file with the given leases (active or expired).

    Each lease: {lease_id, agent, path, mode, acquired_at?, expires_at}.
    """
    now = int(time.time())
    out: dict = {"leases": {}, "waits": waits or {}}
    for i, lease in enumerate(leases or []):
        rec = {
            "lease_id": lease.get("lease_id", f"lid{i}"),
            "agent": lease["agent"],
            "path": lease["path"],
            "mode": lease.get("mode", "write"),
            "acquired_at": lease.get("acquired_at", now),
            "expires_at": lease.get("expires_at", now + 300),
        }
        out["leases"][rec["lease_id"]] = rec
    (home / "registry.json").write_text(json.dumps(out, sort_keys=True) + "\n")


def _py_check(home: Path, agent: str, path: str, mode: str,
              *, gate_mode: str | None = None,
              gate_scope: str | None = None) -> tuple[str, str, int]:
    """Run Python port; return (stdout, stderr, rc).

    `gate_mode` / `gate_scope` are passed as explicit kwargs to the port.
    File-path env is pinned by the `home` fixture.
    """
    return cg.coord_gate_check(agent, path, mode,
                               mode_override=gate_mode,
                               scope_override=gate_scope)


def _py_metrics(home: Path) -> str:
    return cg.coord_gate_metrics()


def _py_metrics_field(home: Path, name: str, default: int = 0) -> int:
    return cg.coord_gate_metrics_field(name, default)


def _py_audit(home: Path, n: int = 0) -> dict:
    out = cg.coord_gate_audit(n)
    return json.loads(out.strip().splitlines()[-1])


# ───────────────────────────────────────────────────────────────────────────
# (a) check advisory + no conflict → rc=0, empty streams.
# ───────────────────────────────────────────────────────────────────────────
def test_advisory_no_conflict(home):
    _seed_registry(home)  # empty registry
    pso, pse, prc = _py_check(home, "agent-a", "/some/path", "write")
    assert prc == 0
    assert pso == "" and pse == ""


# ───────────────────────────────────────────────────────────────────────────
# (b) check advisory + active write holder → rc=0, stderr WAIT nudge.
# ───────────────────────────────────────────────────────────────────────────
def test_advisory_with_conflict(home):
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-1",
        "path": "/some/path", "mode": "write",
    }])
    pso, pse, prc = _py_check(home, "agent-a", "/some/path", "write",
                              gate_mode="advisory")
    assert prc == 0
    assert "WAIT before editing" in pse
    assert "holder-1" in pse
    assert "/some/path" in pse


# ───────────────────────────────────────────────────────────────────────────
# (c) check strict + in-scope conflict → rc=11, stderr "strict deny".
# ───────────────────────────────────────────────────────────────────────────
def test_strict_in_scope_deny(home):
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-2",
        "path": "/scoped/x", "mode": "write",
    }])
    pso, pse, prc = _py_check(home, "agent-a", "/scoped/x/y", "write",
                              gate_mode="strict", gate_scope="/scoped")
    assert prc == 11
    assert "strict deny" in pse
    assert "/scoped/x/y" in pse


# ───────────────────────────────────────────────────────────────────────────
# (d) check strict + out-of-scope conflict → rc=0, advisory fallback nudge.
# ───────────────────────────────────────────────────────────────────────────
def test_strict_out_of_scope_advisory_fallback(home):
    # Holder lives under /other — OUTSIDE the strict scope /scoped. The
    # request also targets /other, so the lease DOES overlap (probe fires)
    # but the path is out of scope → advisory fallback (nudge, allow).
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-3",
        "path": "/other", "mode": "write",
    }])
    pso, pse, prc = _py_check(home, "agent-a", "/other/file", "write",
                              gate_mode="strict", gate_scope="/scoped")
    assert prc == 0
    assert "WAIT before editing" in pse
    assert "out of strict scope" in pse
    assert "holder-3" in pse


# ───────────────────────────────────────────────────────────────────────────
# (e) check bad mode → rc=2, stderr usage message.
# ───────────────────────────────────────────────────────────────────────────
def test_bad_mode_usage_error(home):
    _seed_registry(home)
    pso, pse, prc = _py_check(home, "agent-a", "/x", "bogus")
    assert prc == 2
    assert "mode must be" in pse


# ───────────────────────────────────────────────────────────────────────────
# (f) metrics() on empty state → 4-key default JSON.
# ───────────────────────────────────────────────────────────────────────────
def test_metrics_empty_state(home):
    py_out = _py_metrics(home).strip()
    obj = json.loads(py_out)
    assert set(obj.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }
    for v in obj.values():
        assert isinstance(v, int)
        assert v == 0


# ───────────────────────────────────────────────────────────────────────────
# (g) metrics_field after a bump sequence → integer counters.
#     The metrics we check are ones that observe() does NOT overwrite.
# ───────────────────────────────────────────────────────────────────────────
def test_metrics_field_after_bumps(home):
    cg.coord_gate_record_deadlock()
    cg.coord_gate_record_ttl_expiration(3)
    py_deadlocks = _py_metrics_field(home, "coord_deadlocks_broken")
    py_ttl = _py_metrics_field(home, "coord_ttl_expirations")
    assert py_deadlocks == 1
    assert py_ttl == 3


# ───────────────────────────────────────────────────────────────────────────
# (h) audit() bounded ring buffer — most-recent-first, count + max preserved.
#     5 audit records via 5 coord_gate_check calls that each trigger a
#     conflict; COORD_GATE_AUDIT_MAX=3 → ring buffer caps at 3 records.
# ───────────────────────────────────────────────────────────────────────────
def test_audit_bounded_ring_buffer(home, monkeypatch):
    monkeypatch.setenv("COORD_GATE_AUDIT_MAX", "3")
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-a",
        "path": "/x", "mode": "write",
    }])
    for i in range(5):
        _py_check(home, f"agent-{i}", "/x", "write", gate_mode="advisory")

    py_aud = _py_audit(home, n=0)
    # cap honoured
    assert py_aud["count"] == 3
    assert py_aud["max"] == 3
    # Every event is a conflict against the seeded holder on /x.
    for e in py_aud["events"]:
        assert e["event"] == "conflict"
        assert e["path"] == "/x"
        assert e["requested_mode"] == "write"
    # The seeded lease is the only conflicting agent.
    holders = [e["holder"] for e in py_aud["events"]]
    assert holders == ["holder-a", "holder-a", "holder-a"], (
        f"unexpected holders: {holders}"
    )
    # ts strictly non-increasing across the recent records.
    ts_list = [e["ts"] for e in py_aud["events"]]
    assert ts_list == sorted(ts_list, reverse=True), (
        f"ts not non-increasing: {ts_list}"
    )
    assert all(isinstance(v, int) for v in (py_aud["count"], py_aud["max"]))


# ───────────────────────────────────────────────────────────────────────────
# (i) check strict + NO conflict → rc=0, empty streams.
#     Ported from test_coord_gate.sh test 3: strict mode must stay silent
#     and allow (rc=0) when the registry holds no overlapping lease.
# ───────────────────────────────────────────────────────────────────────────
def test_strict_no_conflict_silent(home):
    _seed_registry(home)  # empty registry → nothing to conflict with
    pso, pse, prc = _py_check(home, "agent-a", "/scoped/x/y", "write",
                              gate_mode="strict", gate_scope="/scoped")
    assert prc == 0
    assert pso == "" and pse == ""


# ───────────────────────────────────────────────────────────────────────────
# (j) metrics coord_leases_held reflects the LIVE registry snapshot.
#     Ported from test_coord_gate.sh test 4: a coord_gate_check runs
#     observe(), which snapshots the registry into the metrics file;
#     coord_leases_held must equal the number of active leases (1, then 2).
#     A check on a NON-overlapping path is used so observe() fires without
#     the check itself mutating the registry.
# ───────────────────────────────────────────────────────────────────────────
def test_leases_held_live_snapshot(home):

    def _reset_state() -> None:
        for name in ("metrics.json", "audit.json", "registry.json"):
            p = home / name
            if p.exists():
                p.unlink()

    def _leases(n: int) -> list[dict]:
        return [{"lease_id": f"lid{i}", "agent": f"holder-{i}",
                 "path": f"/held/p{i}", "mode": "write"} for i in range(n)]

    def _py_held(n: int) -> int:
        _reset_state()
        _seed_registry(home, leases=_leases(n))
        # unrelated path → observe() fires, no conflict, no registry mutation
        _py_check(home, "req", "/unrelated/path", "write", gate_mode="advisory")
        return json.loads(_py_metrics(home).strip())["coord_leases_held"]

    assert _py_held(1) == 1
    assert _py_held(2) == 2
