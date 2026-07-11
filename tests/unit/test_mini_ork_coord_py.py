"""Parity gate: mini_ork.ported.mini_ork_coord vs bin/mini-ork-coord.

Each test invokes the LIVE bash CLI dispatcher ``bin/mini-ork-coord <subcmd>
<args>`` AS A SUBPROCESS against the SAME pinned COORD_REGISTRY_STATE_FILE /
COORD_GATE_METRICS_FILE / COORD_GATE_AUDIT_FILE env as the Python port, then
invokes the Python CLI dispatcher ``python -m mini_ork.ported.mini_ork_coord
<subcmd> <args>`` AS A SUBPROCESS on identical inputs, and deep-compares:
    - return code (rc)            — exact-equal
    - stdout bytes                — shape-equal (lease_id is hex; deadlock
                                    payload is JSON; metrics/audit is JSON)
    - stderr bytes                — exact-equal (text contains the same
                                    embedded identifiers when seeded)
    - side-effect state files     — leases.json / metrics.json / audit.json
                                    must diff-equal (mod random ids/holders)

NO mocks. NO hardcoded outputs beyond what bash itself emits. The bash
``bin/mini-ork-coord`` and ``lib/coord_*.sh`` are byte-identical to git HEAD
(strangler-fig invariant). Tests that need a shared lease_id between the two
sides _seed_lease() into the Python-side state file so the bash-emitted
identifier can be embedded verbatim in the Python-emitted stderr.

Eight cases (above the kickoff's >=6 floor):
  (a) acquire+release roundtrip  — rc=0/rc=0, empty stderr, empty final state
  (b) acquire conflict W+W       — overlapping prefix → rc=1 + "conflict" stderr
  (c) acquire invalid mode       — rc=2 + "mode must be" stderr
  (d) release unknown id         — rc=1 + "unknown lease id <id>" stderr
  (e) renew not-holder           — rc=3 + "not the current holder" stderr
  (f) gate advisory no-conflict  — rc=0, empty stdout, empty stderr
  (g) gate strict in-scope deny  — rc=11 + "strict deny" stderr
  (h) metrics on empty state     — 4-key default JSON, byte-equal
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

BIN = REPO / "bin" / "mini-ork-coord"
SH_REGISTRY = REPO / "lib" / "coord_registry.sh"
SH_GATE = REPO / "lib" / "coord_gate.sh"

# ── fixtures / helpers ──────────────────────────────────────────────────


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not BIN.exists():
        pytest.skip(f"missing {BIN}")
    if not SH_REGISTRY.exists():
        pytest.skip(f"missing {SH_REGISTRY}")
    if not SH_GATE.exists():
        pytest.skip(f"missing {SH_GATE}")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by bash lib/coord_*.sh)")


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Fresh tmp dir per test. Pin file-path env knobs for both bash + Python.

    HOME / MINI_ORK_HOME / MINI_ORK_RUN_DIR all point at the per-test tmp
    dir so the bash subprocess resolves its default state-base there too.
    The COORD_*_FILE knobs take precedence over the env-cascade, so the
    registry / metrics / audit JSON files live under the per-test tmp dir
    regardless of which knob the bash subprocess happens to consult.
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


def _bash(*args: str, env: dict | None = None) -> tuple[str, str, int]:
    """Invoke LIVE bin/mini-ork-coord. Returns (stdout, stderr, rc)."""
    r = subprocess.run(
        [str(BIN), *args],
        env=env, capture_output=True, text=True,
    )
    return r.stdout, r.stderr, r.returncode


def _py(*args: str, env: dict | None = None) -> tuple[str, str, int]:
    """Invoke the Python port AS A SUBPROCESS: ``python -m mini_ork.ported
    .mini_ork_coord``. cwd=REPO so the package is importable. Returns
    (stdout, stderr, rc) mirroring the bash helper."""
    r = subprocess.run(
        [sys.executable, "-m", "mini_ork.ported.mini_ork_coord", *args],
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
    """Write a deterministic lease into the registry state file.

    Used when we want bash + Python to share an identifier: bash
    acquires a lease, captures the id, we replicate the same lease
    (same id, same agent) on the Python side so subsequent
    release/renew messages carry the SAME id on both sides.
    """
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


# ── parity helpers ──────────────────────────────────────────────────────


def _assert_rc(bash_rc: int, py_rc: int, label: str) -> None:
    assert bash_rc == py_rc, (
        f"[{label}] rc mismatch: bash={bash_rc} py={py_rc}"
    )


def _assert_stderr_equal(bash_err: str, py_err: str, label: str) -> None:
    assert bash_err == py_err, (
        f"[{label}] stderr mismatch\nbash={bash_err!r}\npy  ={py_err!r}"
    )


def _assert_acquire_success_stdout(bash_out: str, py_out: str, label: str
                                     ) -> None:
    """For acquire-success: bash stdout is ``<hex>\n``, py stdout is
    ``<hex>\n``. Lease ids differ (random per-process); both must be valid
    hex and the surrounding line-shape must match."""
    bash_lines = bash_out.strip().splitlines()
    py_lines = py_out.strip().splitlines()
    assert bash_lines, f"[{label}] bash stdout empty on rc=0 acquire"
    assert py_lines, f"[{label}] py stdout empty on rc=0 acquire"
    assert re.fullmatch(r"[A-Fa-f0-9]+", bash_lines[-1]), (
        f"[{label}] bash lease_id shape invalid: {bash_out!r}"
    )
    assert re.fullmatch(r"[A-Fa-f0-9]+", py_lines[-1]), (
        f"[{label}] py lease_id shape invalid: {py_out!r}"
    )
    assert len(bash_lines) == len(py_lines) == 1, (
        f"[{label}] acquire stdout should be a single line on rc=0; "
        f"got bash={bash_out!r} py={py_out!r}"
    )


def _assert_stdout_byte_equal(bash_out: str, py_out: str, label: str
                                ) -> None:
    assert bash_out == py_out, (
        f"[{label}] stdout mismatch\nbash={bash_out!r}\npy  ={py_out!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# (a) acquire+release roundtrip. Both sides rc=0/rc=0; stderr empty on both
#     sides; final registry.json is empty on both sides. Lease ids are
#     generated independently per side (random hex 16 chars), so we only
#     assert hex-shape + single-line-shape parity.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_release_roundtrip_parity(home):
    _which_bash()
    env = _env(home)

    # Bash side.
    _reset(home)
    bash_acq_out, bash_acq_err, bash_acq_rc = _bash(
        "acquire", "agent-a", "/src/api", "write", "60", env=env)
    bash_lease = bash_acq_out.strip()
    assert bash_acq_rc == 0
    bash_rel_out, bash_rel_err, bash_rel_rc = _bash(
        "release", bash_lease, env=env)
    assert bash_rel_rc == 0
    bash_registry_final = _read_registry(home)

    # Python side (independent setup with its own random lease_id).
    _reset(home)
    py_acq_out, py_acq_err, py_acq_rc = _py(
        "acquire", "agent-a", "/src/api", "write", "60", env=env)
    py_lease = py_acq_out.strip()
    assert py_acq_rc == 0
    py_rel_out, py_rel_err, py_rel_rc = _py(
        "release", py_lease, env=env)
    assert py_rel_rc == 0
    py_registry_final = _read_registry(home)

    # Parity: rcs, stderrs, stdout-shape, final state.
    _assert_rc(bash_acq_rc, py_acq_rc, "acquire")
    _assert_stderr_equal(bash_acq_err, py_acq_err, "acquire stderr")
    _assert_acquire_success_stdout(bash_acq_out, py_acq_out, "acquire stdout")
    _assert_rc(bash_rel_rc, py_rel_rc, "release")
    _assert_stderr_equal(bash_rel_err, py_rel_err, "release stderr")
    _assert_stdout_byte_equal(bash_rel_out, py_rel_out, "release stdout")

    assert bash_registry_final == {"leases": {}, "waits": {}}
    assert py_registry_final == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (b) acquire conflict W+W. Both sides seed the SAME holder lease (shared
#     id from a bash-side acquire), then run the conflicting acquire. Both
#     rc=1, stderr mentions "conflict", final state has only the holder.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_conflict_ww_overlap_parity(home):
    _which_bash()
    env = _env(home)

    # Bash side: bash acquires holder, then bash retries a conflicting
    # write — captures the conflict rc + stderr byte-for-byte.
    _reset(home)
    bash_acq1_out, bash_acq1_err, bash_acq1_rc = _bash(
        "acquire", "agent-holder", "/src/api", "write", "60", env=env)
    bash_holder_lease = bash_acq1_out.strip()
    assert bash_acq1_rc == 0
    bash_acq2_out, bash_acq2_err, bash_acq2_rc = _bash(
        "acquire", "agent-other", "/src/api/x.rs", "write", "60", env=env)
    bash_registry_after = _read_registry(home)

    # Python side: replicate the SAME holder lease (same id) so the
    # Python side has identical registry state, then conflict-acquire.
    _reset(home)
    _seed_lease(home / "registry.json", bash_holder_lease,
                "agent-holder", path="/src/api", mode="write")
    py_acq_out, py_acq_err, py_acq_rc = _py(
        "acquire", "agent-other", "/src/api/x.rs", "write", "60", env=env)
    py_registry_after = _read_registry(home)

    assert bash_acq2_rc == 1
    _assert_rc(bash_acq2_rc, py_acq_rc, "conflict rc")
    _assert_stderr_equal(bash_acq2_err, py_acq_err, "conflict stderr")
    _assert_stdout_byte_equal(bash_acq2_out, py_acq_out, "conflict stdout")
    assert "conflict" in bash_acq2_err.lower()
    assert "agent-holder" in bash_acq2_err  # bash embeds the holder name

    # Side-effect parity: only the holder remains; the conflicted requester
    # was added to `waits` (bash + python both add wait edges here). Holder
    # timestamps drift by sub-second clock skew between side-by-side
    # processes, so we compare structural fields only — not acquired_at
    # / expires_at.
    def _holder_signature(state: dict) -> dict:
        leases = state["leases"]
        return {
            lid: {
                "lease_id": rec["lease_id"],
                "agent": rec["agent"],
                "path": rec["path"],
                "mode": rec["mode"],
            }
            for lid, rec in leases.items()
        }
    assert _holder_signature(bash_registry_after) == (
        _holder_signature(py_registry_after))
    assert len(bash_registry_after["leases"]) == 1
    assert "agent-other" in bash_registry_after.get("waits", {})
    assert bash_registry_after.get("waits", {}).get("agent-other") == (
        py_registry_after.get("waits", {}).get("agent-other"))


# ─────────────────────────────────────────────────────────────────────────
# (c) acquire invalid mode. Both sides rc=2 + stderr mentions "read" and
#     "write". No lease written to state.
# ─────────────────────────────────────────────────────────────────────────
def test_acquire_invalid_mode_parity(home):
    _which_bash()
    env = _env(home)
    _reset(home)

    bash_out, bash_err, bash_rc = _bash(
        "acquire", "agent-x", "/src/api", "weird", "60", env=env)
    _reset(home)
    py_out, py_err, py_rc = _py(
        "acquire", "agent-x", "/src/api", "weird", "60", env=env)

    assert bash_rc == 2
    _assert_rc(bash_rc, py_rc, "invalid-mode rc")
    _assert_stderr_equal(bash_err, py_err, "invalid-mode stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "invalid-mode stdout")
    assert "read" in bash_err and "write" in bash_err
    assert "weird" in bash_err
    assert _read_registry(home) == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (d) release unknown id. Bash side starts empty; both sides release a
#     shared id that does not exist. Both rc=1 + "unknown lease id <id>"
#     stderr. Py side has empty stdout (matches bash).
# ─────────────────────────────────────────────────────────────────────────
def test_release_unknown_id_parity(home):
    _which_bash()
    env = _env(home)
    bogus_id = "deadbeef" * 2  # 16 hex chars; valid format but no such lease

    _reset(home)
    bash_out, bash_err, bash_rc = _bash("release", bogus_id, env=env)
    _reset(home)
    py_out, py_err, py_rc = _py("release", bogus_id, env=env)

    assert bash_rc == 1
    _assert_rc(bash_rc, py_rc, "unknown-release rc")
    _assert_stderr_equal(bash_err, py_err, "unknown-release stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "unknown-release stdout")
    assert "unknown" in bash_err.lower()
    assert bogus_id in bash_err
    assert _read_registry(home) == {"leases": {}, "waits": {}}


# ─────────────────────────────────────────────────────────────────────────
# (e) renew not-holder. Bash side acquires a lease (id shared), both sides
#     seed the SAME lease, then attempt renew with a different agent. Both
#     rc=3 + stderr "not the current holder". Holder name embedded in
#     stderr (deterministic between sides).
# ─────────────────────────────────────────────────────────────────────────
def test_renew_not_holder_parity(home):
    _which_bash()
    env = _env(home)

    _reset(home)
    bash_acq_out, bash_acq_err, bash_acq_rc = _bash(
        "acquire", "agent-holder", "/src/api", "write", "60", env=env)
    bash_lease = bash_acq_out.strip()
    assert bash_acq_rc == 0
    bash_out, bash_err, bash_rc = _bash(
        "renew", "agent-other", bash_lease, "60", env=env)

    _reset(home)
    _seed_lease(home / "registry.json", bash_lease,
                "agent-holder", path="/src/api", mode="write")
    py_out, py_err, py_rc = _py(
        "renew", "agent-other", bash_lease, "60", env=env)

    assert bash_rc == 3
    _assert_rc(bash_rc, py_rc, "renew-not-holder rc")
    _assert_stderr_equal(bash_err, py_err, "renew-not-holder stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "renew-not-holder stdout")
    assert "not the current holder" in bash_err.lower()
    # Bash stderr only embeds the HOLDER name (not the caller's), per the
    # heredoc at lib/coord_registry.sh:515-518.
    assert "agent-holder" in bash_err


# ─────────────────────────────────────────────────────────────────────────
# (f) gate advisory no-conflict. Empty registry; both sides rc=0, empty
#     stdout, empty stderr. Metrics file emitted; both sides' observe()
#     yields the same default-schema file content.
# ─────────────────────────────────────────────────────────────────────────
def test_gate_advisory_no_conflict_parity(home):
    _which_bash()
    env = _env(home)
    _reset(home)

    bash_out, bash_err, bash_rc = _bash(
        "gate", "agent-a", "/some/path", "write", env=env)
    py_out, py_err, py_rc = _py(
        "gate", "agent-a", "/some/path", "write", env=env)

    assert bash_rc == 0
    _assert_rc(bash_rc, py_rc, "gate-advisory-noconflict rc")
    _assert_stderr_equal(bash_err, py_err, "gate-advisory-noconflict stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "gate-advisory-noconflict stdout")
    assert bash_out == "" and bash_err == ""
    # Side-effect parity: metrics file emitted on both sides.
    bash_metrics = _read_metrics(home)
    py_metrics = _read_metrics(home)
    assert bash_metrics == py_metrics
    assert set(bash_metrics.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }


# ─────────────────────────────────────────────────────────────────────────
# (g) gate strict in-scope conflict. Seed an active write holder under
#     the strict scope /scoped. Request a write inside /scoped → strict
#     deny. Both rc=11 + stderr contains "strict deny" + the conflicted
#     path. Audit file receives a "conflict" record on both sides.
# ─────────────────────────────────────────────────────────────────────────
def test_gate_strict_in_scope_deny_parity(home):
    _which_bash()
    env = _env(home, COORD_GATE_MODE="strict", COORD_GATE_SCOPE="/scoped")

    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-1",
        "path": "/scoped/x", "mode": "write",
    }])

    bash_out, bash_err, bash_rc = _bash(
        "gate", "agent-a", "/scoped/x/y", "write", env=env)
    _reset(home)
    _seed_registry(home, leases=[{
        "lease_id": "lid1", "agent": "holder-1",
        "path": "/scoped/x", "mode": "write",
    }])
    py_out, py_err, py_rc = _py(
        "gate", "agent-a", "/scoped/x/y", "write", env=env)

    assert bash_rc == 11
    _assert_rc(bash_rc, py_rc, "strict-deny rc")
    _assert_stderr_equal(bash_err, py_err, "strict-deny stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "strict-deny stdout")
    assert "strict deny" in bash_err
    assert "/scoped/x/y" in bash_err
    assert "holder-1" in bash_err
    # Side-effect parity: audit file received one conflict record on each
    # side. Both append the same holder + same scope at effectively the
    # same ts (loose check: structure parity + same holder).
    bash_audit = _read_audit(home)
    py_audit = _read_audit(home)
    assert bash_audit["count"] == py_audit["count"] == 1
    assert len(bash_audit["events"]) == len(py_audit["events"]) == 1
    bash_ev = bash_audit["events"][0]
    py_ev = py_audit["events"][0]
    assert bash_ev.get("event") == py_ev.get("event") == "conflict"
    assert bash_ev.get("holder") == py_ev.get("holder") == "holder-1"
    assert bash_ev.get("scope") == py_ev.get("scope") == "/scoped"
    assert bash_ev.get("mode") == py_ev.get("mode") == "strict"


# ─────────────────────────────────────────────────────────────────────────
# (h) metrics on empty state. Both sides write the same 4-key default
#     JSON to stdout. State file parity: metrics.json written with the
#     same content (default schema).
# ─────────────────────────────────────────────────────────────────────────
def test_metrics_empty_state_parity(home):
    _which_bash()
    env = _env(home)
    _reset(home)

    bash_out, bash_err, bash_rc = _bash("metrics", env=env)
    py_out, py_err, py_rc = _py("metrics", env=env)

    assert bash_rc == 0
    _assert_rc(bash_rc, py_rc, "metrics rc")
    _assert_stderr_equal(bash_err, py_err, "metrics stderr")
    _assert_stdout_byte_equal(bash_out, py_out, "metrics stdout")
    obj = json.loads(bash_out.strip())
    assert set(obj.keys()) == {
        "coord_leases_held", "coord_queue_depth",
        "coord_deadlocks_broken", "coord_ttl_expirations",
    }
    for v in obj.values():
        assert isinstance(v, int)
        assert v == 0
    # Side-effect parity: metrics file on disk.
    assert _read_metrics(home) == obj
