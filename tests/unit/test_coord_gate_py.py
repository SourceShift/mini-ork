"""Parity gate: mini_ork.gates.coord_gate vs lib/coord_gate.sh.

Each test drives the LIVE bash function via
``bash -c 'source lib/coord_gate.sh; coord_gate_check ...'`` against the
SAME JSON-file fixture as the Python port, then deep-compares the two
outputs: stdout/stderr strings byte-equal, return code exact-equal. No
mocks, no hardcoded outputs beyond what bash itself emits.

The bash function reads its knobs from the environment
(``COORD_GATE_MODE`` / ``COORD_GATE_SCOPE`` / ``COORD_GATE_METRICS_FILE`` /
``COORD_GATE_AUDIT_FILE`` / ``COORD_GATE_AUDIT_MAX`` /
``COORD_REGISTRY_STATE_FILE``); the Python port takes them as explicit
kwargs (where applicable) AND reads the same env vars at function entry.
The bash subprocess MUST source ``lib/coord_registry.sh`` too — the gate
auto-sources it at the bottom of the bash file, but the auto-source only
runs when the gate is loaded as a script body (``BASH_SOURCE[0]``); when
the gate is sourced as a library in a subshell, we source the registry
explicitly so the auto-source path matches.

Eight cases (above the kickoff's >=6 floor):
  (a) check advisory + no conflict        → rc=0, empty stdout, empty stderr
  (b) check advisory + active write holder → rc=0, stderr "WAIT before..."
  (c) check strict + in-scope conflict    → rc=11, stderr "strict deny..."
  (d) check strict + out-of-scope conflict → rc=0, stderr "WAIT before...
                                             ...out of strict scope..."
  (e) check bad mode                       → rc=2, stderr "mode must be..."
  (f) metrics() on empty state             → 4-key default JSON
  (g) metrics_field after bump_sequence     → integer equality (no drift)
  (h) audit() bounded ring buffer           → most-recent-first, count/max
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import coord_gate as cg

SH = REPO / "lib" / "coord_gate.sh"
REGISTRY_SH = REPO / "lib" / "coord_registry.sh"

_FLOAT_TOL = 1e-6  # gate output is int-only but the helper applies tolerance


# ── helpers ────────────────────────────────────────────────────────────────


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/coord_gate.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/coord_gate.sh at {SH}")
    if not REGISTRY_SH.exists():
        pytest.skip(f"missing lib/coord_registry.sh at {REGISTRY_SH}")


def _env_for(home: Path, *, gate_mode: str | None = None,
             gate_scope: str | None = None,
             gate_audit_max: int | None = None) -> dict:
    """Build the bash subprocess env. Inherits the per-test file-path pins
    set by the `home` fixture; only overrides the gate-config knobs.
    """
    env = dict(os.environ)
    env.pop("COORD_GATE_MODE", None)
    env.pop("COORD_GATE_SCOPE", None)
    env.pop("COORD_GATE_AUDIT_MAX", None)
    if gate_mode is not None:
        env["COORD_GATE_MODE"] = gate_mode
    if gate_scope is not None:
        env["COORD_GATE_SCOPE"] = gate_scope
    if gate_audit_max is not None:
        env["COORD_GATE_AUDIT_MAX"] = str(gate_audit_max)
    return env


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Fresh home dir per test — no db/init.sh needed (registry is JSON).

    Pins the file-path env knobs for BOTH the bash subprocess and the
    in-process Python port via monkeypatch.setenv, so they always target
    the same per-test temp dir regardless of the host shell's HOME.
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


def _bash_check(home: Path, agent: str, path: str, mode: str, **env_overrides
                ) -> tuple[str, str, int]:
    """Invoke LIVE bash ``coord_gate_check``; return (stdout, stderr, rc)."""
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_check "$1" "$2" "$3"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", agent, path, mode],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    return r.stdout, r.stderr, r.returncode


def _bash_metrics(home: Path, **env_overrides) -> str:
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_metrics\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_"],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash coord_gate_metrics rc={r.returncode} stderr={r.stderr}"
    return r.stdout


def _bash_metrics_field(home: Path, name: str, default: str = "0",
                        **env_overrides) -> int:
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_metrics_field "$1" "$2"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", name, default],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash metrics_field rc={r.returncode} stderr={r.stderr}"
    return int(r.stdout.strip())


def _bash_audit(home: Path, n: int = 0, **env_overrides) -> dict:
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_audit "$1"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", str(n)],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash audit rc={r.returncode} stderr={r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _bash_record_deadlock(home: Path, **env_overrides) -> None:
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_record_deadlock\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_"],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash deadlock rc={r.returncode} stderr={r.stderr}"


def _bash_record_ttl_expiration(home: Path, delta: int = 1,
                                **env_overrides) -> None:
    src = (
        f'source "{REGISTRY_SH}"\n'
        f'source "{SH}"\n'
        f'coord_gate_record_ttl_expiration "$1"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", str(delta)],
        env=_env_for(home, **env_overrides),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash ttl rc={r.returncode} stderr={r.stderr}"


def _py_check(home: Path, agent: str, path: str, mode: str,
              *, gate_mode: str | None = None,
              gate_scope: str | None = None) -> tuple[str, str, int]:
    """Run Python port; return (stdout, stderr, rc).

    `gate_mode` / `gate_scope` are passed as explicit kwargs to the port so
    both sides honour identical gate configuration. File-path env is pinned
    by the `home` fixture.
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


def _assert_parity(bash_out: str, py_out: str,
                   bash_err: str, py_err: str,
                   bash_rc: int, py_rc: int) -> None:
    """Deep-compare: stdout/stderr byte-equal, rc exact-equal."""
    assert bash_rc == py_rc, f"rc mismatch: bash={bash_rc} py={py_rc}"
    assert bash_out == py_out, f"stdout mismatch:\nbash={bash_out!r}\npy  ={py_out!r}"
    assert bash_err == py_err, f"stderr mismatch:\nbash={bash_err!r}\npy  ={py_err!r}"


# ───────────────────────────────────────────────────────────────────────────
# (a) check advisory + no conflict → rc=0, empty streams.
# ───────────────────────────────────────────────────────────────────────────
def test_advisory_no_conflict_parity(home):
    _which_bash()
    _seed_registry(home)  # empty registry
    bso, bse, brc = _bash_check(home, "agent-a", "/some/path", "write")
    pso, pse, prc = _py_check(home, "agent-a", "/some/path", "write")
    _assert_parity(bso, pso, bse, pse, brc, prc)
    assert brc == 0
    assert bso == "" and bse == ""


# ───────────────────────────────────────────────────────────────────────────
# (b) check advisory + active write holder → rc=0, stderr WAIT nudge.
# ───────────────────────────────────────────────────────────────────────────
def test_advisory_with_conflict_parity(home):
    _which_bash()
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-1",
        "path": "/some/path", "mode": "write",
    }])
    bso, bse, brc = _bash_check(home, "agent-a", "/some/path", "write",
                                gate_mode="advisory")
    pso, pse, prc = _py_check(home, "agent-a", "/some/path", "write",
                              gate_mode="advisory")
    _assert_parity(bso, pso, bse, pse, brc, prc)
    assert brc == 0
    assert "WAIT before editing" in bse
    assert "holder-1" in bse
    assert "/some/path" in bse


# ───────────────────────────────────────────────────────────────────────────
# (c) check strict + in-scope conflict → rc=11, stderr "strict deny".
# ───────────────────────────────────────────────────────────────────────────
def test_strict_in_scope_deny_parity(home):
    _which_bash()
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-2",
        "path": "/scoped/x", "mode": "write",
    }])
    bso, bse, brc = _bash_check(home, "agent-a", "/scoped/x/y", "write",
                                gate_mode="strict", gate_scope="/scoped")
    pso, pse, prc = _py_check(home, "agent-a", "/scoped/x/y", "write",
                              gate_mode="strict", gate_scope="/scoped")
    _assert_parity(bso, pso, bse, pse, brc, prc)
    assert brc == 11
    assert "strict deny" in bse
    assert "/scoped/x/y" in bse


# ───────────────────────────────────────────────────────────────────────────
# (d) check strict + out-of-scope conflict → rc=0, advisory fallback nudge.
# ───────────────────────────────────────────────────────────────────────────
def test_strict_out_of_scope_advisory_fallback_parity(home):
    _which_bash()
    # Holder lives under /other — OUTSIDE the strict scope /scoped. The
    # request also targets /other, so the lease DOES overlap (probe fires)
    # but the path is out of scope → advisory fallback (nudge, allow).
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-3",
        "path": "/other", "mode": "write",
    }])
    bso, bse, brc = _bash_check(home, "agent-a", "/other/file", "write",
                                gate_mode="strict", gate_scope="/scoped")
    pso, pse, prc = _py_check(home, "agent-a", "/other/file", "write",
                              gate_mode="strict", gate_scope="/scoped")
    _assert_parity(bso, pso, bse, pse, brc, prc)
    assert brc == 0
    assert "WAIT before editing" in bse
    assert "out of strict scope" in bse
    assert "holder-3" in bse


# ───────────────────────────────────────────────────────────────────────────
# (e) check bad mode → rc=2, stderr usage message.
# ───────────────────────────────────────────────────────────────────────────
def test_bad_mode_usage_error_parity(home):
    _which_bash()
    _seed_registry(home)
    bso, bse, brc = _bash_check(home, "agent-a", "/x", "bogus")
    pso, pse, prc = _py_check(home, "agent-a", "/x", "bogus")
    _assert_parity(bso, pso, bse, pse, brc, prc)
    assert brc == 2
    assert "mode must be" in bse


# ───────────────────────────────────────────────────────────────────────────
# (f) metrics() on empty state → 4-key default JSON.
# ───────────────────────────────────────────────────────────────────────────
def test_metrics_empty_state_parity(home):
    _which_bash()
    bash_out = _bash_metrics(home).strip()
    py_out = _py_metrics(home).strip()
    assert bash_out == py_out, (
        f"metrics mismatch:\nbash={bash_out!r}\npy  ={py_out!r}"
    )
    obj = json.loads(py_out)
    assert set(obj.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }
    for v in obj.values():
        assert isinstance(v, int)
        assert v == 0


# ───────────────────────────────────────────────────────────────────────────
# (g) metrics_field after a bump_sequence → integer equality.
#     Bash runs the bump sequence first, Python runs it second; we capture
#     the post-bump counter values via metrics_field on each side.
#     The metric we check is one that observe() does NOT overwrite.
# ───────────────────────────────────────────────────────────────────────────
def test_metrics_field_after_bumps_parity(home):
    _which_bash()
    # Bump via bash.
    _bash_record_deadlock(home)
    _bash_record_ttl_expiration(home, delta=3)
    bash_deadlocks = _bash_metrics_field(home, "coord_deadlocks_broken")
    bash_ttl = _bash_metrics_field(home, "coord_ttl_expirations")
    # Reset file state so the Python side starts clean and bumps the same.
    for name in ("metrics.json", "audit.json", "registry.json"):
        p = home / name
        if p.exists():
            p.unlink()
    # Bump via Python.
    cg.coord_gate_record_deadlock()
    cg.coord_gate_record_ttl_expiration(3)
    py_deadlocks = _py_metrics_field(home, "coord_deadlocks_broken")
    py_ttl = _py_metrics_field(home, "coord_ttl_expirations")
    assert bash_deadlocks == py_deadlocks == 1, (
        f"deadlocks drifted: bash={bash_deadlocks} py={py_deadlocks}"
    )
    assert bash_ttl == py_ttl == 3, (
        f"ttl drifted: bash={bash_ttl} py={py_ttl}"
    )


# ───────────────────────────────────────────────────────────────────────────
# (h) audit() bounded ring buffer — most-recent-first, count + max preserved.
#     Bash seeds 5 audit records via 5 separate coord_gate_check calls that
#     each trigger a conflict. Python does the same. Both honour
#     COORD_GATE_AUDIT_MAX=3 → ring buffer caps at 3 records.
# ───────────────────────────────────────────────────────────────────────────
def test_audit_bounded_ring_buffer_parity(home, monkeypatch):
    _which_bash()
    monkeypatch.setenv("COORD_GATE_AUDIT_MAX", "3")
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-a",
        "path": "/x", "mode": "write",
    }])
    bash_env = {"gate_mode": "advisory", "gate_audit_max": 3}
    # Generate 5 conflicts on each side. Both sides read the audit-max from
    # the env (which monkeypatch.setenv pinned for the py side; the bash
    # subprocess inherits via _env_for + os.environ).
    for i in range(5):
        _bash_check(home, f"agent-{i}", "/x", "write", **bash_env)
    for i in range(5):
        _py_check(home, f"agent-{i}", "/x", "write", gate_mode="advisory")

    bash_aud = _bash_audit(home, n=0, **bash_env)
    py_aud = _py_audit(home, n=0)
    # cap honoured
    assert bash_aud["count"] == 3 == py_aud["count"], (
        f"audit count drifted: bash={bash_aud['count']} py={py_aud['count']}"
    )
    assert bash_aud["max"] == 3 == py_aud["max"], (
        f"audit max drifted: bash={bash_aud['max']} py={py_aud['max']}"
    )
    # Most-recent-first ordering — most-recent record is from the LAST call.
    bash_first = bash_aud["events"][0]
    py_first = py_aud["events"][0]
    assert bash_first == py_first, (
        f"most-recent audit record drifted:\nbash={bash_first}\npy  ={py_first}"
    )
    # Both sides appended the same holder (the seeded lease is the only
    # conflicting agent) — the audit event schema is what parity must hold.
    holders = [e["holder"] for e in bash_aud["events"]]
    assert holders == ["holder-a", "holder-a", "holder-a"], (
        f"unexpected holders: {holders}"
    )
    # ts strictly non-increasing across the recent records.
    ts_list = [e["ts"] for e in bash_aud["events"]]
    assert ts_list == sorted(ts_list, reverse=True), (
        f"ts not non-increasing: {ts_list}"
    )
    # Float-tolerance no-op for int counters but exercise the helper.
    assert all(isinstance(v, int) for v in (
        bash_aud["count"], bash_aud["max"]))
    assert abs(float(bash_aud["count"]) - float(py_aud["count"])) <= _FLOAT_TOL