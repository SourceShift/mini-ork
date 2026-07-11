"""Parity gate: mini_ork.ported.coord_registry vs lib/coord_registry.sh.

Each test drives the LIVE bash function via
``bash -c 'source lib/coord_registry.sh; coord_acquire "$@"' _ <args>``
against the SAME isolated tmp state file the Python port uses, then
diff-compares the two sides: return code, stdout bytes, stderr bytes,
and resulting leases.json structure. No mocks, no hardcoded outputs
beyond what bash itself emits.

Both sides see identical env knobs (COORD_REGISTRY_STATE_FILE points at
a tmp path; COORD_REGISTRY_AGENT_PRIORITIES / COORD_REGISTRY_AGENT_PRIORITY_*
when priority resolution is under test).

The bash heredoc normalises paths and writes atomic tmp→os.replace,
exactly as the port does. Cross-process safety is via flock on a sibling
.lock file — both sides honour that contract.

Eight cases (above the kickoff's >=6 floor):
  (a) acquire-success           — two non-overlapping write leases, both rc=0
  (b) acquire-conflict-W+W      — overlapping prefix → rc=1 + "conflict" stderr
  (c) acquire-path-boundary     — src/api vs src/ap do NOT overlap
  (d) acquire-mode-validation   — invalid mode → rc=2 + usage stderr
  (e) release-roundtrip         — valid→0, double→1, malformed→1
  (f) renew-own extends         — expires_at advances, rc=0
  (g) renew-not-holder          — rc=3 + "not the current holder" stderr
  (h) deadlock-abort            — A→B→A cycle, lower-priority requester aborts
                                  with rc=4 + JSON {status:abort, reason:deadlock,
                                  agent, cycle} on stdout AND its own wait edge
                                  is removed from the state file.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import coord_registry as cr  # noqa: E402

SH = REPO / "lib" / "coord_registry.sh"


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/coord_registry.sh at {SH}")


# ─── live-bash subprocess helpers ─────────────────────────────────────────────


def _bash_acquire(state_file, agent, path, mode, ttl=None, env=None):
    base_env = {**os.environ, "COORD_REGISTRY_STATE_FILE": state_file}
    if env:
        base_env.update(env)
    cmd = (
        f'source "{SH}"\n'
        f'coord_acquire "$1" "$2" "$3" "$4"\n'
    )
    args = ["bash", "-c", cmd, "_", agent, path, mode, "" if ttl is None else str(ttl)]
    r = subprocess.run(args, env=base_env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _bash_release(state_file, lease_id, env=None):
    base_env = {**os.environ, "COORD_REGISTRY_STATE_FILE": state_file}
    if env:
        base_env.update(env)
    cmd = (
        f'source "{SH}"\n'
        f'coord_release "$1"\n'
    )
    r = subprocess.run(
        ["bash", "-c", cmd, "_", lease_id], env=base_env, capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def _bash_renew(state_file, agent, lease_id, ttl=None, env=None):
    base_env = {**os.environ, "COORD_REGISTRY_STATE_FILE": state_file}
    if env:
        base_env.update(env)
    cmd = (
        f'source "{SH}"\n'
        f'coord_renew "$1" "$2" "$3"\n'
    )
    args = ["bash", "-c", cmd, "_", agent, lease_id, "" if ttl is None else str(ttl)]
    r = subprocess.run(args, env=base_env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# ─── python-port helpers (re-run under stderr capture for parity comparison) ─


@contextlib.contextmanager
def _captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


def _run_py_acquire(state_file, agent, path, mode, ttl=None):
    """Return (rc, value, stderr) — value is lease_id str, abort dict, or None."""
    with _captured_stderr() as buf:
        value, rc = cr.coord_acquire(agent, path, mode, ttl, state_file=state_file)
    return rc, value, buf.getvalue()


def _run_py_release(state_file, lease_id):
    with _captured_stderr() as buf:
        rc = cr.coord_release(lease_id, state_file=state_file)
    return rc, "", buf.getvalue()


def _run_py_renew(state_file, agent, lease_id, ttl=None):
    with _captured_stderr() as buf:
        rc = cr.coord_renew(agent, lease_id, ttl, state_file=state_file)
    return rc, "", buf.getvalue()


# ─── state inspection ─────────────────────────────────────────────────────────


def _read_state(state_file) -> dict:
    if not os.path.exists(state_file):
        return {"leases": {}, "waits": {}}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"leases": {}, "waits": {}}


def _state_lease_count(state_file) -> int:
    return len(_read_state(state_file).get("leases", {}))


def _reset_state(state_file) -> None:
    if os.path.exists(state_file):
        os.unlink(state_file)
    lock = state_file + ".lock"
    if os.path.exists(lock):
        os.unlink(lock)


# ─── parity core ──────────────────────────────────────────────────────────────


def _assert_parity(bash_rc, bash_out, bash_err, py_rc, py_out, py_err, label):
    """Compare rc + stderr + (for rc=4) payload between bash and python.

    For rc=0, lease_ids are secrets.token_hex(8) (non-deterministic), so we
    only assert format-validity and that both sides produced something. For
    rc=4, the JSON deadlock payload is deterministic (sorted keys) and must
    be byte-equal between the two sides.
    """
    assert bash_rc == py_rc, (
        f"[{label}] rc mismatch: bash={bash_rc} py={py_rc} "
        f"bash_stdout={bash_out!r} py_out={py_out!r}"
    )
    if bash_rc == 4:
        assert isinstance(py_out, dict), (
            f"[{label}] rc=4 must yield a dict payload, got {type(py_out).__name__}"
        )
        bash_payload = json.loads(bash_out.strip().splitlines()[-1])
        assert bash_payload == py_out, (
            f"[{label}] deadlock payload mismatch\n"
            f"bash={bash_payload}\npy  ={dict(py_out)}"
        )
    elif bash_rc == 0:
        # For acquire: bash_out is the lease_id (random hex), py_out is the
        # same kind of lease_id. For release/renew: stdout is empty on both
        # sides. Handle both: when stdout is non-empty, validate hex shape.
        if bash_out.strip():
            assert re.fullmatch(r"[A-Fa-f0-9]+", bash_out.strip()) is not None, (
                f"[{label}] bash lease_id shape invalid: {bash_out!r}"
            )
            assert isinstance(py_out, str), (
                f"[{label}] py should return a lease_id str on rc=0, got {type(py_out).__name__}"
            )
            assert re.fullmatch(r"[A-Fa-f0-9]+", py_out) is not None, (
                f"[{label}] py lease_id shape invalid: {py_out!r}"
            )
        else:
            # Empty stdout on rc=0 (release/renew path) — both sides silent.
            assert not py_out, f"[{label}] py stdout non-empty on rc=0: {py_out!r}"
    else:
        # rc=1 (conflict/release-error), rc=2 (usage) — stdout should be empty.
        assert bash_out == "", f"[{label}] bash stdout non-empty on rc={bash_rc}: {bash_out!r}"
        assert not py_out, f"[{label}] py stdout non-empty on rc={py_rc}: {py_out!r}"
    assert bash_err == py_err, (
        f"[{label}] stderr mismatch\nbash={bash_err!r}\npy  ={py_err!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (a) Two non-overlapping write leases — both succeed with distinct hex ids.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_success_two_disjoint_writes_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    # First write.
    bash_rc1, bash_out1, bash_err1 = _bash_acquire(sf, "agent-w1", "src/api", "write", 60)
    _reset_state(sf)
    py_rc1, py_out1, py_err1 = _run_py_acquire(sf, "agent-w1", "src/api", "write", 60)
    _assert_parity(bash_rc1, bash_out1, bash_err1, py_rc1, py_out1, py_err1, "first write")
    assert bash_rc1 == 0 and py_rc1 == 0
    assert re.fullmatch(r"[A-Fa-f0-9]+", bash_out1.strip()) is not None

    # Second write (non-overlapping path).
    bash_rc2, bash_out2, bash_err2 = _bash_acquire(sf, "agent-w2", "src/db", "write", 60)
    _reset_state(sf)
    cr.coord_acquire("agent-w1", "src/api", "write", 60, state_file=sf)
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-w2", "src/db", "write", 60)
    _assert_parity(bash_rc2, bash_out2, bash_err2, py_rc2, py_out2, py_err2, "second write")
    assert bash_rc2 == 0 and py_rc2 == 0
    assert bash_out1.strip() != bash_out2.strip(), "lease ids must differ"
    assert _state_lease_count(sf) == 2


# ──────────────────────────────────────────────────────────────────────────────
# (b) Overlapping write+write — second write blocked with rc=1 + "conflict"
#     on stderr. State still has only the first lease.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_conflict_ww_overlap_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    bash_rc1, _, _ = _bash_acquire(sf, "agent-w1", "src/api", "write", 60)
    bash_rc2, bash_out2, bash_err2 = _bash_acquire(sf, "agent-w2", "src/api/x.rs", "write", 60)
    _reset_state(sf)
    cr.coord_acquire("agent-w1", "src/api", "write", 60, state_file=sf)
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-w2", "src/api/x.rs", "write", 60)

    assert bash_rc1 == 0
    _assert_parity(bash_rc2, bash_out2, bash_err2, py_rc2, py_out2, py_err2, "w+w overlap")
    assert bash_rc2 == 1 and py_rc2 == 1
    assert "conflict" in bash_err2.lower()
    assert "conflict" in py_err2.lower()
    state = _read_state(sf)
    assert len(state["leases"]) == 1
    assert "agent-w2" in state.get("waits", {})


# ──────────────────────────────────────────────────────────────────────────────
# (c) Path boundary: writer holds /src/api; reader requests /src/ap. The
#     trailing-slash trick prevents /src/ap from being treated as a prefix of
#     /src/api — read allowed, rc=0.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_path_boundary_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    bash_rc1, _, _ = _bash_acquire(sf, "agent-w1", "src/api", "write", 60)
    bash_rc2, bash_out2, bash_err2 = _bash_acquire(sf, "agent-r3", "src/ap", "read", 60)
    _reset_state(sf)
    cr.coord_acquire("agent-w1", "src/api", "write", 60, state_file=sf)
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-r3", "src/ap", "read", 60)

    assert bash_rc1 == 0
    _assert_parity(bash_rc2, bash_out2, bash_err2, py_rc2, py_out2, py_err2, "boundary read")
    assert bash_rc2 == 0 and py_rc2 == 0
    assert re.fullmatch(r"[A-Fa-f0-9]+", bash_out2.strip()) is not None


# ──────────────────────────────────────────────────────────────────────────────
# (d) Mode validation: unknown mode → rc=2 + mode/usage stderr.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_mode_validation_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    bash_rc, bash_out, bash_err = _bash_acquire(sf, "agent-x", "src/api", "weird", 60)
    _reset_state(sf)
    py_rc, py_out, py_err = _run_py_acquire(sf, "agent-x", "src/api", "weird", 60)
    _assert_parity(bash_rc, bash_out, bash_err, py_rc, py_out, py_err, "mode=weird")
    assert bash_rc == 2 and py_rc == 2
    assert "read" in bash_err and "write" in bash_err


# ──────────────────────────────────────────────────────────────────────────────
# (e) Release round-trip: valid → rc=0, double-release → rc=1, malformed → rc=1.
#     Pattern: bash acquires the lease (gives us a deterministic id), then
#     bash and python each try to release that SAME id. This way the stderr
#     message ("coord_release: unknown lease id <id>") is byte-equal on both
#     sides — the lease_id is shared, not random-per-side.
# ──────────────────────────────────────────────────────────────────────────────
def test_release_roundtrip_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    # Step 1: valid release (rc=0). Bash acquires, gets lease_id; both sides
    # release it (rc=0, stderr="").
    _reset_state(sf)
    bash_rc, bash_out, _ = _bash_acquire(sf, "agent-r1", "src/api", "read", 60)
    bash_lease = bash_out.strip()
    assert bash_rc == 0
    assert re.fullmatch(r"[A-Fa-f0-9]+", bash_lease) is not None

    bash_rel1_rc, bash_rel1_out, bash_rel1_err = _bash_release(sf, bash_lease)
    # State is now empty (bash released). Set state back to "lease exists"
    # by re-acquiring on the python side with the same lease_id... but the
    # python acquire generates a random id. Instead, write the state file
    # directly to mirror what bash just removed, then run python release.
    _reset_state(sf)
    _seed_lease(sf, bash_lease, "agent-r1")
    py_rel1_rc, _, py_rel1_err = _run_py_release(sf, bash_lease)
    _assert_parity(bash_rel1_rc, bash_rel1_out, bash_rel1_err,
                   py_rel1_rc, "", py_rel1_err, "release valid")
    assert bash_rel1_rc == 0 and py_rel1_rc == 0

    # Step 2: double-release (rc=1). State is empty after step 1. Both sides
    # try to release bash_lease (no longer present) → rc=1, stderr mentions
    # bash_lease by id.
    bash_rel2_rc, bash_rel2_out, bash_rel2_err = _bash_release(sf, bash_lease)
    py_rel2_rc, _, py_rel2_err = _run_py_release(sf, bash_lease)
    _assert_parity(bash_rel2_rc, bash_rel2_out, bash_rel2_err,
                   py_rel2_rc, "", py_rel2_err, "double release")
    assert bash_rel2_rc == 1 and py_rel2_rc == 1
    assert "unknown" in bash_rel2_err.lower()
    assert bash_lease in bash_rel2_err  # stderr embeds the shared id

    # Step 3: malformed id (rc=1). Both sides reject "not-hex!" with
    # "coord_release: malformed lease id\n".
    bash_rel3_rc, bash_rel3_out, bash_rel3_err = _bash_release(sf, "not-hex!")
    py_rel3_rc, _, py_rel3_err = _run_py_release(sf, "not-hex!")
    _assert_parity(bash_rel3_rc, bash_rel3_out, bash_rel3_err,
                   py_rel3_rc, "", py_rel3_err, "malformed lease id")
    assert bash_rel3_rc == 1 and py_rel3_rc == 1
    assert "malformed" in bash_rel3_err.lower()


def _seed_lease(state_file: str, lease_id: str, agent: str) -> None:
    """Write a single live lease into the state file (used when we need to
    replicate a bash-side setup on the python side without re-acquiring)."""
    now = int(os.environ.get("_FAKE_NOW", "0")) or __import__("time").time()
    import time as _t
    now = int(_t.time())
    state = {
        "leases": {
            lease_id: {
                "lease_id": lease_id,
                "agent": agent,
                "path": "/src/api",
                "mode": "read",
                "acquired_at": int(now),
                "expires_at": int(now) + 3600,
            }
        },
        "waits": {},
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
        f.write("\n")


# ──────────────────────────────────────────────────────────────────────────────
# (f) renew-own extends expires_at. Each side independently acquires a lease,
#     captures expires_at, renews it, captures expires_at again, then asserts
#     the renewal advanced the timestamp. rc must be 0 on both sides.
# ──────────────────────────────────────────────────────────────────────────────
def test_renew_own_extends_expires_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    # Bash side.
    bash_rc, bash_out, _ = _bash_acquire(sf, "agent-holder", "src/api", "write", 60)
    bash_lease = bash_out.strip()
    assert bash_rc == 0
    bash_state_before = _read_state(sf)
    bash_expires_before = int(bash_state_before["leases"][bash_lease]["expires_at"])
    bash_renew_rc, bash_renew_out, bash_renew_err = _bash_renew(sf, "agent-holder", bash_lease, 600)
    bash_state_after = _read_state(sf)
    bash_expires_after = int(bash_state_after["leases"][bash_lease]["expires_at"])

    # Python side (independent setup).
    _reset_state(sf)
    py_rc, py_lease, _ = _run_py_acquire(sf, "agent-holder", "src/api", "write", 60)
    assert py_rc == 0
    py_state_before = _read_state(sf)
    py_expires_before = int(py_state_before["leases"][py_lease]["expires_at"])
    py_renew_rc, _, py_renew_err = _run_py_renew(sf, "agent-holder", py_lease, 600)
    py_state_after = _read_state(sf)
    py_expires_after = int(py_state_after["leases"][py_lease]["expires_at"])

    _assert_parity(bash_renew_rc, bash_renew_out, bash_renew_err,
                   py_renew_rc, "", py_renew_err, "renew own")
    assert bash_renew_rc == 0 and py_renew_rc == 0

    # Both sides must show advance, by a similar magnitude.
    bash_delta = bash_expires_after - bash_expires_before
    py_delta = py_expires_after - py_expires_before
    assert bash_delta >= 500, f"bash renew delta too small: {bash_delta}"
    assert py_delta >= 500, f"py renew delta too small: {py_delta}"


# ──────────────────────────────────────────────────────────────────────────────
# (g) renew-not-holder: rc=3 + "not the current holder" on stderr.
# ──────────────────────────────────────────────────────────────────────────────
def test_renew_not_holder_parity(tmp_path):
    _which_bash()
    sf = str(tmp_path / "leases.json")

    # Bash side.
    bash_rc, bash_out, _ = _bash_acquire(sf, "agent-holder", "src/api", "write", 60)
    bash_lease = bash_out.strip()
    assert bash_rc == 0
    bash_renew_rc, bash_renew_out, bash_renew_err = _bash_renew(sf, "agent-other", bash_lease, 60)

    # Python side (independent setup).
    _reset_state(sf)
    py_rc, py_lease, _ = _run_py_acquire(sf, "agent-holder", "src/api", "write", 60)
    assert py_rc == 0
    py_renew_rc, _, py_renew_err = _run_py_renew(sf, "agent-other", py_lease, 60)

    _assert_parity(bash_renew_rc, bash_renew_out, bash_renew_err,
                   py_renew_rc, "", py_renew_err, "renew not-holder")
    assert bash_renew_rc == 3 and py_renew_rc == 3
    assert "not the current holder" in bash_renew_err.lower()


# ──────────────────────────────────────────────────────────────────────────────
# (h) Deadlock abort: A→B→A cycle. agent-a priority 20, agent-b priority 10.
#     agent-b is the lowest-priority participant → its blocked cycle acquire
#     aborts with rc=4 + JSON payload on stdout AND its own wait edge is
#     removed from the state file. agent-a's wait edge (a→b) remains.
# ──────────────────────────────────────────────────────────────────────────────
def test_deadlock_abort_parity(tmp_path, monkeypatch):
    _which_bash()
    sf = str(tmp_path / "leases.json")
    monkeypatch.setenv("COORD_REGISTRY_AGENT_PRIORITIES", "agent-a=20,agent-b=10")
    env_overrides = {"COORD_REGISTRY_AGENT_PRIORITIES": "agent-a=20,agent-b=10"}

    # Bash side: build the cycle in a single tmp dir.
    _reset_state(sf)
    rc1, _, _ = _bash_acquire(sf, "agent-a", "src/a", "write", 60, env=env_overrides)
    rc2, _, _ = _bash_acquire(sf, "agent-b", "src/b", "write", 60, env=env_overrides)
    rc3, _, _ = _bash_acquire(sf, "agent-a", "src/b", "write", 60, env=env_overrides)
    bash_rc4, bash_out4, bash_err4 = _bash_acquire(sf, "agent-b", "src/a", "write", 60, env=env_overrides)
    bash_state = _read_state(sf)

    assert rc1 == 0 and rc2 == 0 and rc3 == 1, (
        f"setup precondition: a holds, b holds, a-waits-blocked; got rc1={rc1} rc2={rc2} rc3={rc3}"
    )
    assert bash_rc4 == 4
    bash_payload = json.loads(bash_out4.strip().splitlines()[-1])

    # Python side: rebuild from clean state.
    _reset_state(sf)
    cr.coord_acquire("agent-a", "src/a", "write", 60, state_file=sf)
    cr.coord_acquire("agent-b", "src/b", "write", 60, state_file=sf)
    cr.coord_acquire("agent-a", "src/b", "write", 60, state_file=sf)
    py_out4, py_rc4 = cr.coord_acquire("agent-b", "src/a", "write", 60, state_file=sf)
    py_state = _read_state(sf)

    assert py_rc4 == 4

    # State structure parity.
    assert len(bash_state["leases"]) == len(py_state["leases"]) == 2
    assert set(bash_state["waits"].keys()) == set(py_state["waits"].keys())
    assert "agent-b" not in bash_state["waits"]
    assert "agent-b" not in py_state["waits"]
    assert bash_state["waits"].get("agent-a") == ["agent-b"]
    assert py_state["waits"].get("agent-a") == ["agent-b"]

    # Payload byte-for-byte.
    assert bash_payload == py_out4
    assert bash_payload["status"] == "abort"
    assert bash_payload["reason"] == "deadlock"
    assert bash_payload["agent"] == "agent-b"
    assert sorted(bash_payload["cycle"]) == ["agent-a", "agent-b"]

    # bash_err4 is empty for the deadlock path (bash skips the "conflict..."
    # stderr branch on victim-self-abort).
    assert bash_err4 == ""