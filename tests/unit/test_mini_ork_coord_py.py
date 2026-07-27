"""Standalone unit tests for ``mini_ork.orchestration.coord`` (the Python
CLI dispatcher, invoked as ``python -m mini_ork.orchestration.coord``).

Replaces the bash-parity gate (against ``bin/mini-ork-coord``) as part of
the bash→Python migration: the Python CLI is now the sole implementation,
so its coverage no longer invokes the LIVE bash CLI dispatcher as a
subprocess — it drives the Python CLI AS A SUBPROCESS and asserts its
behaviour directly: return codes, stdout shapes (lease_id hex, JSON
payloads), stderr messages, and side-effect state files (leases.json /
metrics.json / audit.json). The expected values below are the semantic
contract the bash side used to pin.

Eight cases:
  (a) acquire+release roundtrip  — rc=0/rc=0, empty stderr, empty final state
  (b) acquire conflict W+W       — overlapping prefix → rc=1 + "conflict" stderr
  (c) acquire invalid mode       — rc=2 + "mode must be" stderr
  (d) release unknown id         — rc=1 + "unknown lease id <id>" stderr
  (e) renew not-holder           — rc=3 + "not the current holder" stderr
  (f) gate advisory no-conflict  — rc=0, empty stdout, empty stderr
  (g) gate strict in-scope deny  — rc=11 + "strict deny" stderr
  (h) metrics on empty state     — 4-key default JSON
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# ── fixtures / helpers ──────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Fresh tmp dir per test. Pin file-path env knobs for the Python CLI.

    HOME / MINI_ORK_HOME / MINI_ORK_RUN_DIR all point at the per-test tmp
    dir so the subprocess resolves its default state-base there too. The
    COORD_*_FILE knobs take precedence over the env-cascade, so the
    registry / metrics / audit JSON files live under the per-test tmp dir
    regardless of which knob the subprocess happens to consult.
    """
    h = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(h))
    monkeypatch.setenv("COORD_REGISTRY_STATE_FILE", str(h / "registry.json"))
    monkeypatch.setenv("COORD_GATE_METRICS_FILE", str(h / "metrics.json"))
    monkeypatch.setenv("COORD_GATE_AUDIT_FILE", str(h / "audit.json"))
    monkeypatch.delenv("COORD_GATE_MODE", raising=False)
    monkeypatch.delenv("COORD_GATE_SCOPE", raising=False)
    monkeypatch.delenv("COORD_GATE_AUDIT_MAX", raising=False)
    return h


def _env(home: Path, **overrides: str) -> dict:
    """Build a subprocess env: os.environ (already monkeypatched) + per-test
    overrides. The ``home`` fixture pins the file-path knobs onto
    os.environ; we pass through to children so they hit the same tmp paths."""
    env = dict(os.environ)
    for k, v in overrides.items():
        env[k] = v
    return env


def _py(*args: str, env: dict | None = None) -> tuple[str, str, int]:
    """Invoke the Python CLI AS A SUBPROCESS: ``python -m
    mini_ork.orchestration.coord``. cwd=REPO so the package is importable.
    Returns (stdout, stderr, rc)."""
    r = subprocess.run(
        [sys.executable, "-m", "mini_ork.orchestration.coord", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )
    return r.stdout, r.stderr, r.returncode


def _read_state(state_file: Path) -> dict:
    """Load a state file; tolerate absent/malformed as empty."""
    if not state_file.exists():
        return {"leases": {}, "waits": {}}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}


def _read_metrics(home: Path) -> dict:
    return _read_state(home / "metrics.json")


def _read_registry(home: Path) -> dict:
    return _read_state(home / "registry.json")


def _read_audit(home: Path) -> dict:
    """Audit file may be a dict-on-disk or a list-of-records depending on
    load path; normalise to {events: [...], max: N, count: N}."""
    f = home / "audit.json"
    if not f.exists():
        return {"events": [], "max": 0, "count": 0}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"events": [], "max": 0, "count": 0}
    if isinstance(data, dict):
        events = data.get("events", [])
        max_seen = data.get("max", 0)
        count = data.get("count", len(events) if isinstance(events, list) else 0)
    elif isinstance(data, list):
        events, max_seen, count = data, 0, len(data)
    else:
        events, max_seen, count = [], 0, 0
    return {"events": events if isinstance(events, list) else [],
            "max": max_seen if isinstance(max_seen, int) else 0,
            "count": count if isinstance(count, int) else 0}


def _reset(home: Path) -> None:
    """Wipe leases/metrics/audit state files + sibling lockfiles."""
    for name in ("registry.json", "metrics.json", "audit.json"):
        f = home / name
        if f.exists():
            f.unlink()
        lock = home / (name + ".lock")
        if lock.exists():
            lock.unlink()


def _seed_lease(state_file: Path, lease_id: str, agent: str,
                path: str = "/src/api", mode: str = "write",
                now: int | None = None) -> None:
    """Write a deterministic lease into the registry state file (used when
    a test needs a fixed identifier embedded in subsequent stderr)."""
    n = now if now is not None else int(time.time())
    state = {
        "leases": {
            lease_id: {
                "lease_id": lease_id,
                "agent": agent,
                "path": path,
                "mode": mode,
                "acquired_at": int(n),
                "expires_at": int(n) + 3600,
            }
        },
        "waits": {},
    }
    state_file.write_text(json.dumps(state, sort_keys=True) + "\n")


def _seed_registry(home: Path, leases: list[dict], waits: dict | None = None
                    ) -> None:
    """Seed multiple leases (and optional waits) into the registry state."""
    now = int(time.time())
    out: dict = {"leases": {}, "waits": waits or {}}
    for i, lease in enumerate(leases):
        lid = lease.get("lease_id", f"lid{i}")
        out["leases"][lid] = {
            "lease_id": lid,
            "agent": lease["agent"],
            "path": lease["path"],
            "mode": lease.get("mode", "write"),
            "acquired_at": lease.get("acquired_at", now),
            "expires_at": lease.get("expires_at", now + 300),
        }
    (home / "registry.json").write_text(json.dumps(out, sort_keys=True) + "\n")


def _assert_acquire_success_stdout(out: str, label: str) -> None:
    """Acquire-success stdout is a single ``<hex>`` line."""
    lines = out.strip().splitlines()
    assert lines, f"[{label}] stdout empty on rc=0 acquire"
    assert re.fullmatch(r"[A-Fa-f0-9]+", lines[-1]), (
        f"[{label}] lease_id shape invalid: {out!r}"
    )
    assert len(lines) == 1, (
        f"[{label}] acquire stdout should be a single line on rc=0; got {out!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# (a) acquire+release roundtrip. rc=0/rc=0; stderr empty; final
#     registry.json is empty. Lease ids are random hex 16 chars, so we
#     only assert hex-shape + single-line shape.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_release_roundtrip(home):
    env = _env(home)

    _reset(home)
    py_acq_out, py_acq_err, py_acq_rc = _py(
        "acquire", "agent-a", "/src/api", "write", "60", env=env)
    py_lease = py_acq_out.strip()
    assert py_acq_rc == 0
    _assert_acquire_success_stdout(py_acq_out, "acquire stdout")
    assert py_acq_err == ""
    py_rel_out, py_rel_err, py_rel_rc = _py(
        "release", py_lease, env=env)
    assert py_rel_rc == 0
    assert py_rel_out == "" and py_rel_err == ""
    assert _read_registry(home) == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (b) acquire conflict W+W. Seed a holder lease, then run the conflicting
#     acquire. rc=1, stderr mentions "conflict" + the holder name, final
#     state has only the holder + a wait edge for the requester.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_conflict_ww_overlap(home):
    env = _env(home)

    _reset(home)
    _seed_lease(home / "registry.json", "cafebabe",
                "agent-holder", path="/src/api", mode="write")
    py_acq_out, py_acq_err, py_acq_rc = _py(
        "acquire", "agent-other", "/src/api/x.rs", "write", "60", env=env)
    py_registry_after = _read_registry(home)

    assert py_acq_rc == 1
    assert py_acq_out == ""
    assert "conflict" in py_acq_err.lower()
    assert "agent-holder" in py_acq_err  # stderr embeds the holder name

    # Side-effect: only the holder remains; the conflicted requester was
    # added to `waits`.
    assert len(py_registry_after["leases"]) == 1
    holder = py_registry_after["leases"]["cafebabe"]
    assert holder["agent"] == "agent-holder"
    assert holder["path"] == "/src/api"
    assert holder["mode"] == "write"
    assert "agent-other" in py_registry_after.get("waits", {})


# ─────────────────────────────────────────────────────────────────────────
# (c) acquire invalid mode. rc=2 + stderr mentions "read" and "write".
#     No lease written to state.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_invalid_mode(home):
    env = _env(home)
    _reset(home)

    py_out, py_err, py_rc = _py(
        "acquire", "agent-x", "/src/api", "weird", "60", env=env)

    assert py_rc == 2
    assert py_out == ""
    assert "read" in py_err and "write" in py_err
    assert "weird" in py_err
    assert _read_registry(home) == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (d) release unknown id. Empty registry; release a well-formed id that
#     does not exist → rc=1 + "unknown lease id <id>" stderr.
# ─────────────────────────────────────────────────────────────────────────
def test_release_unknown_id(home):
    env = _env(home)
    bogus_id = "deadbeef" * 2  # 16 hex chars; valid format but no such lease

    _reset(home)
    py_out, py_err, py_rc = _py("release", bogus_id, env=env)

    assert py_rc == 1
    assert py_out == ""
    assert "unknown" in py_err.lower()
    assert bogus_id in py_err
    assert _read_registry(home) == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (e) renew not-holder. Seed a lease, then attempt renew with a different
#     agent → rc=3 + stderr "not the current holder" naming the HOLDER.
# ─────────────────────────────────────────────────────────────────────────
def test_renew_not_holder(home):
    env = _env(home)

    _reset(home)
    _seed_lease(home / "registry.json", "cafebabe",
                "agent-holder", path="/src/api", mode="write")
    py_out, py_err, py_rc = _py(
        "renew", "agent-other", "cafebabe", "60", env=env)

    assert py_rc == 3
    assert py_out == ""
    assert "not the current holder" in py_err.lower()
    # stderr only embeds the HOLDER name (not the caller's).
    assert "agent-holder" in py_err


# ─────────────────────────────────────────────────────────────────────────
# (f) gate advisory no-conflict. Empty registry; rc=0, empty stdout,
#     empty stderr. Metrics file emitted with the default schema.
# ─────────────────────────────────────────────────────────────────────────
def test_gate_advisory_no_conflict(home):
    env = _env(home)
    _reset(home)

    py_out, py_err, py_rc = _py(
        "gate", "agent-a", "/some/path", "write", env=env)

    assert py_rc == 0
    assert py_out == "" and py_err == ""
    # Side-effect: metrics file emitted.
    metrics = _read_metrics(home)
    assert set(metrics.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }


# ─────────────────────────────────────────────────────────────────────────
# (g) gate strict in-scope conflict. Seed an active write holder under
#     the strict scope /scoped. Request a write inside /scoped → strict
#     deny. rc=11 + stderr contains "strict deny" + the conflicted path.
#     Audit file receives a "conflict" record.
# ─────────────────────────────────────────────────────────────────────────
def test_gate_strict_in_scope_deny(home):
    env = _env(home, COORD_GATE_MODE="strict", COORD_GATE_SCOPE="/scoped")

    _reset(home)
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-1",
        "path": "/scoped/x", "mode": "write",
    }])
    py_out, py_err, py_rc = _py(
        "gate", "agent-a", "/scoped/x/y", "write", env=env)

    assert py_rc == 11
    assert py_out == ""
    assert "strict deny" in py_err
    assert "/scoped/x/y" in py_err
    assert "holder-1" in py_err
    # Side-effect: audit file received one conflict record.
    audit = _read_audit(home)
    assert audit["count"] == 1
    assert len(audit["events"]) == 1
    ev = audit["events"][0]
    assert ev.get("event") == "conflict"
    assert ev.get("holder") == "holder-1"
    assert ev.get("scope") == "/scoped"
    assert ev.get("mode") == "strict"


# ─────────────────────────────────────────────────────────────────────────
# (h) metrics on empty state. 4-key default JSON on stdout; metrics.json
#     on disk matches.
# ─────────────────────────────────────────────────────────────────────────
def test_metrics_empty_state(home):
    env = _env(home)
    _reset(home)

    py_out, py_err, py_rc = _py("metrics", env=env)

    assert py_rc == 0
    assert py_err == ""
    obj = json.loads(py_out.strip())
    assert set(obj.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }
    for v in obj.values():
        assert isinstance(v, int)
        assert v == 0
    # Side-effect: metrics file on disk.
    assert _read_metrics(home) == obj
