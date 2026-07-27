"""Standalone unit tests for ``mini_ork.registries.coord_registry``.

Replaces the bash-parity gate (against ``lib/coord_registry.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer drives the LIVE bash function
via ``bash -c 'source lib/coord_registry.sh; coord_acquire ...'`` — it
asserts the port's behaviour directly. The expected values below are the
semantic contract the bash side used to pin (rc semantics, stderr
messages, state-file structure, TTL clamping, deadlock-abort payload),
now asserted on the port's output.

Cases (a)-(l) predate the .sh retirement; (i)-(l) were ported from
tests/unit/test_coord_registry.sh; (m)-(u) subsume the retired
tests/unit/test_coord_registry_ttl.sh (Track B3, 19 assertions):
  (a) acquire-success           — two non-overlapping write leases, both rc=0
  (b) acquire-conflict-W+W      — overlapping prefix → rc=1 + "conflict" stderr
  (c) acquire-path-boundary     — src/api vs src/ap do NOT overlap
  (d) acquire-mode-validation   — invalid mode → rc=2 + usage stderr
  (e) release-roundtrip         — valid→0, double→1, malformed→1
  (f) renew-own extends         — expires_at advances, rc=0
  (g) renew-not-holder          — rc=3 + "not the current holder" stderr
  (h) deadlock-abort            — A→B→A cycle, lower-priority requester aborts
                                  with rc=4 + JSON {status:abort, reason:deadlock,
                                  agent, cycle} payload AND its own wait edge
                                  is removed from the state file.
  (i) conflict-variants         — exact W+W + R+W both request orders → rc=1.
  (j) rr-allow-overlap          — three overlapping readers coexist.
  (k) validation-variants       — empty mode + ttl 0/-1/abc → rc=2.
  (l) higher-priority-no-preempt— inverse of (h): the higher-priority
                                  cycle requester blocks (rc=1), holders untouched.
  (m) default TTL = 120s on acquire (ttl omitted) — exact span
  (n) default TTL = 120s on renew (ttl omitted) — delta window
  (o) max TTL = 3600s; larger values capped silently (acquire + renew)
  (p) expiry self-heal: expired lease frees on next acquire + is pruned
  (q) renew rejects an expired lease (rc=1) and an unknown id (rc=1)
  (r) renew arg-validation → rc=2 (missing args / bad ttl)
  (s) renew holder/non-holder logic on a seeded live lease
  (t) release of an expired lease returns rc=1
  (u) a rejected (non-holder) renew must NOT mutate the lease

TTL timing model: coord_acquire stores acquired_at == now, so
expires_at - acquired_at == effective_ttl EXACTLY (clamp of the input:
120 default, 3600 cap) with zero wall-clock skew -> exact ==. coord_renew
updates expires_at = now + ttl but leaves acquired_at, so the span isn't
the ttl; for renew we capture `now` in-test and assert expires_at - now
within the retired .sh's own window (115..125 / 3595..3605), which is
skew-robust.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.registries import coord_registry as cr


# ─── python-port helpers (stderr captured for message assertions) ────────────


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
    return rc, buf.getvalue()


def _run_py_renew(state_file, agent, lease_id, ttl=None):
    with _captured_stderr() as buf:
        rc = cr.coord_renew(agent, lease_id, ttl, state_file=state_file)
    return rc, buf.getvalue()


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


def _seed_lease(state_file: str, lease_id: str, agent: str) -> None:
    """Write a single live lease into the state file (used when we need a
    deterministic shared lease id for error-path assertions)."""
    now = int(time.time())
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


def _assert_lease_id(value) -> None:
    """rc=0 acquire returns a hex lease_id (secrets.token_hex(8))."""
    assert isinstance(value, str), (
        f"rc=0 acquire should return a lease_id str, got {type(value).__name__}"
    )
    assert re.fullmatch(r"[A-Fa-f0-9]+", value) is not None, (
        f"lease_id shape invalid: {value!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (a) Two non-overlapping write leases — both succeed with distinct hex ids.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_success_two_disjoint_writes(tmp_path):
    sf = str(tmp_path / "leases.json")

    # First write.
    py_rc1, py_out1, py_err1 = _run_py_acquire(sf, "agent-w1", "src/api", "write", 60)
    assert py_rc1 == 0 and py_err1 == ""
    _assert_lease_id(py_out1)

    # Second write (non-overlapping path).
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-w2", "src/db", "write", 60)
    assert py_rc2 == 0 and py_err2 == ""
    _assert_lease_id(py_out2)
    assert py_out1 != py_out2, "lease ids must differ"
    assert _state_lease_count(sf) == 2


# ──────────────────────────────────────────────────────────────────────────────
# (b) Overlapping write+write — second write blocked with rc=1 + "conflict"
#     on stderr. State still has only the first lease.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_conflict_ww_overlap(tmp_path):
    sf = str(tmp_path / "leases.json")

    py_rc1, _, _ = _run_py_acquire(sf, "agent-w1", "src/api", "write", 60)
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-w2", "src/api/x.rs", "write", 60)

    assert py_rc1 == 0
    assert py_rc2 == 1
    assert not py_out2
    assert "conflict" in py_err2.lower()
    state = _read_state(sf)
    assert len(state["leases"]) == 1
    assert "agent-w2" in state.get("waits", {})


# ──────────────────────────────────────────────────────────────────────────────
# (c) Path boundary: writer holds /src/api; reader requests /src/ap. The
#     trailing-slash trick prevents /src/ap from being treated as a prefix of
#     /src/api — read allowed, rc=0.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_path_boundary(tmp_path):
    sf = str(tmp_path / "leases.json")

    py_rc1, _, _ = _run_py_acquire(sf, "agent-w1", "src/api", "write", 60)
    py_rc2, py_out2, py_err2 = _run_py_acquire(sf, "agent-r3", "src/ap", "read", 60)

    assert py_rc1 == 0
    assert py_rc2 == 0 and py_err2 == ""
    _assert_lease_id(py_out2)


# ──────────────────────────────────────────────────────────────────────────────
# (d) Mode validation: unknown mode → rc=2 + mode/usage stderr.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_mode_validation(tmp_path):
    sf = str(tmp_path / "leases.json")

    py_rc, py_out, py_err = _run_py_acquire(sf, "agent-x", "src/api", "weird", 60)
    assert py_rc == 2
    assert not py_out
    assert "read" in py_err and "write" in py_err


# ──────────────────────────────────────────────────────────────────────────────
# (e) Release round-trip: valid → rc=0, double-release → rc=1, malformed → rc=1.
#     The unknown-id stderr message embeds the lease id; a shared seeded id
#     keeps the assertion deterministic.
# ──────────────────────────────────────────────────────────────────────────────
def test_release_roundtrip(tmp_path):
    sf = str(tmp_path / "leases.json")

    # Step 1: valid release (rc=0, stderr="").
    _reset_state(sf)
    _seed_lease(sf, "cafebabe", "agent-r1")
    py_rel1_rc, py_rel1_err = _run_py_release(sf, "cafebabe")
    assert py_rel1_rc == 0 and py_rel1_err == ""

    # Step 2: double-release (rc=1). State is empty after step 1 → stderr
    # mentions the id.
    py_rel2_rc, py_rel2_err = _run_py_release(sf, "cafebabe")
    assert py_rel2_rc == 1
    assert "unknown" in py_rel2_err.lower()
    assert "cafebabe" in py_rel2_err

    # Step 3: malformed id (rc=1) → "malformed lease id" stderr.
    py_rel3_rc, py_rel3_err = _run_py_release(sf, "not-hex!")
    assert py_rel3_rc == 1
    assert "malformed" in py_rel3_err.lower()


# ──────────────────────────────────────────────────────────────────────────────
# (f) renew-own extends expires_at, rc=0.
# ──────────────────────────────────────────────────────────────────────────────
def test_renew_own_extends_expires(tmp_path):
    sf = str(tmp_path / "leases.json")

    py_rc, py_lease, _ = _run_py_acquire(sf, "agent-holder", "src/api", "write", 60)
    assert py_rc == 0
    py_state_before = _read_state(sf)
    py_expires_before = int(py_state_before["leases"][py_lease]["expires_at"])
    py_renew_rc, py_renew_err = _run_py_renew(sf, "agent-holder", py_lease, 600)
    py_state_after = _read_state(sf)
    py_expires_after = int(py_state_after["leases"][py_lease]["expires_at"])

    assert py_renew_rc == 0 and py_renew_err == ""
    py_delta = py_expires_after - py_expires_before
    assert py_delta >= 500, f"renew delta too small: {py_delta}"


# ──────────────────────────────────────────────────────────────────────────────
# (g) renew-not-holder: rc=3 + "not the current holder" on stderr.
# ──────────────────────────────────────────────────────────────────────────────
def test_renew_not_holder(tmp_path):
    sf = str(tmp_path / "leases.json")

    py_rc, py_lease, _ = _run_py_acquire(sf, "agent-holder", "src/api", "write", 60)
    assert py_rc == 0
    py_renew_rc, py_renew_err = _run_py_renew(sf, "agent-other", py_lease, 60)

    assert py_renew_rc == 3
    assert "not the current holder" in py_renew_err.lower()


# ──────────────────────────────────────────────────────────────────────────────
# (h) Deadlock abort: A→B→A cycle. agent-a priority 20, agent-b priority 10.
#     agent-b is the lowest-priority participant → its blocked cycle acquire
#     aborts with rc=4 + JSON payload AND its own wait edge is removed from
#     the state file. agent-a's wait edge (a→b) remains.
# ──────────────────────────────────────────────────────────────────────────────
def test_deadlock_abort(tmp_path, monkeypatch):
    sf = str(tmp_path / "leases.json")
    monkeypatch.setenv("COORD_REGISTRY_AGENT_PRIORITIES", "agent-a=20,agent-b=10")

    _reset_state(sf)
    _, rc1 = cr.coord_acquire("agent-a", "src/a", "write", 60, state_file=sf)
    _, rc2 = cr.coord_acquire("agent-b", "src/b", "write", 60, state_file=sf)
    _, rc3 = cr.coord_acquire("agent-a", "src/b", "write", 60, state_file=sf)
    py_out4, py_rc4 = cr.coord_acquire("agent-b", "src/a", "write", 60, state_file=sf)
    py_state = _read_state(sf)

    assert rc1 == 0 and rc2 == 0 and rc3 == 1, (
        f"setup precondition: a holds, b holds, a-waits-blocked; "
        f"got rc1={rc1} rc2={rc2} rc3={rc3}"
    )
    assert py_rc4 == 4

    # State structure.
    assert len(py_state["leases"]) == 2
    assert "agent-b" not in py_state["waits"]
    assert py_state["waits"].get("agent-a") == ["agent-b"]

    # Payload.
    assert isinstance(py_out4, dict)
    assert py_out4["status"] == "abort"
    assert py_out4["reason"] == "deadlock"
    assert py_out4["agent"] == "agent-b"
    assert sorted(py_out4["cycle"]) == ["agent-a", "agent-b"]


# ──────────────────────────────────────────────────────────────────────────────
# (i) Conflict variants — exact W+W + R+W in both request orders. A WRITE
#     participant on an overlapping OR identical path always conflicts (rc=1),
#     regardless of order; only R+R is exempt (see test (j)).
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_conflict_variants(tmp_path):
    sf = str(tmp_path / "leases.json")

    # (holder_mode, req_agent, req_path, req_mode, label)
    scenarios = [
        ("read", "agent-w2", "src/api/x.rs", "write", "R-holds W-child"),
        ("write", "agent-r2", "src/api/x.rs", "read", "W-holds R-child"),
        ("write", "agent-r2", "src/api", "read", "W-holds R-exact"),
        ("write", "agent-w2", "src/api", "write", "W-holds W-exact"),
    ]
    for holder_mode, req_agent, req_path, req_mode, label in scenarios:
        _reset_state(sf)
        h_rc, _, _ = _run_py_acquire(sf, "agent-h1", "src/api", holder_mode, 60)
        p_rc, p_out, p_err = _run_py_acquire(sf, req_agent, req_path, req_mode, 60)

        assert h_rc == 0, f"[{label}] holder acquire failed rc={h_rc}"
        assert p_rc == 1, f"[{label}] expected conflict rc=1"
        assert not p_out
        assert "conflict" in p_err.lower()
    _reset_state(sf)


# ──────────────────────────────────────────────────────────────────────────────
# (j) R+R allow — many readers may hold overlapping prefixes concurrently:
#     reader1@src/api, reader2@src/api/x.rs (overlapping child), reader3@src/api
#     (exact) all succeed (rc=0) and coexist as three live leases.
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_rr_allow_overlap(tmp_path):
    sf = str(tmp_path / "leases.json")

    _reset_state(sf)
    p_rc1, _, _ = _run_py_acquire(sf, "agent-r1", "src/api", "read", 60)
    p_rc2, p_out2, p_err2 = _run_py_acquire(sf, "agent-r2", "src/api/x.rs", "read", 60)
    p_rc3, p_out3, p_err3 = _run_py_acquire(sf, "agent-r3", "src/api", "read", 60)

    assert p_rc1 == 0
    assert p_rc2 == 0 and p_err2 == ""
    assert p_rc3 == 0 and p_err3 == ""
    _assert_lease_id(p_out2)
    _assert_lease_id(p_out3)
    assert _state_lease_count(sf) == 3
    _reset_state(sf)


# ──────────────────────────────────────────────────────────────────────────────
# (k) Validation variants — every invalid-arg form exits rc=2 with a
#     usage/validation message (empty mode hits the usage branch; bad ttl hits
#     the ttl branch; mode='weird' is also covered standalone by test (d)).
# ──────────────────────────────────────────────────────────────────────────────
def test_acquire_validation_variants(tmp_path):
    sf = str(tmp_path / "leases.json")

    # (mode, ttl, label)
    variants = [
        ("weird", "60", "mode=weird"),
        ("", "60", "mode=empty"),
        ("read", "0", "ttl=0"),
        ("write", "-1", "ttl=-1"),
        ("read", "abc", "ttl=abc"),
    ]
    for mode, ttl, label in variants:
        _reset_state(sf)
        p_rc, p_out, p_err = _run_py_acquire(sf, "agent-x", "src/api", mode, ttl)
        assert p_rc == 2, f"[{label}] expected rc=2, got py={p_rc}"
        assert not p_out
        assert p_err
    _reset_state(sf)


# ──────────────────────────────────────────────────────────────────────────────
# (l) Higher-priority requester does NOT preempt — the inverse of the
#     deadlock-abort (h). Same A→B→A cycle but with the priorities flipped
#     (agent-a=10, agent-b=20) so the cycle-completing requester (agent-b) is
#     the HIGHER-priority participant. Wound-wait names the LOWER-priority
#     agent (agent-a) as victim, so agent-b — not being the victim — simply
#     blocks with rc=1 instead of aborting (rc=4), and the two active holder
#     leases are left untouched.
# ──────────────────────────────────────────────────────────────────────────────
def test_higher_priority_no_preempt(tmp_path, monkeypatch):
    sf = str(tmp_path / "leases.json")
    monkeypatch.setenv("COORD_REGISTRY_AGENT_PRIORITIES", "agent-a=10,agent-b=20")

    _reset_state(sf)
    _, rc1 = cr.coord_acquire("agent-a", "src/a", "write", 60, state_file=sf)
    _, rc2 = cr.coord_acquire("agent-b", "src/b", "write", 60, state_file=sf)
    _, rc3 = cr.coord_acquire("agent-a", "src/b", "write", 60, state_file=sf)
    py_rc4, py_out4, py_err4 = _run_py_acquire(sf, "agent-b", "src/a", "write", 60)
    py_state = _read_state(sf)

    assert rc1 == 0 and rc2 == 0 and rc3 == 1, (
        f"setup precondition failed: rc1={rc1} rc2={rc2} rc3={rc3}"
    )
    assert py_rc4 == 1, f"higher-priority requester must block (rc=1), got rc={py_rc4}"
    assert not py_out4
    assert "conflict" in py_err4.lower()
    # Holders untouched: exactly two active leases (no preemption).
    assert len(py_state["leases"]) == 2


# ══════════════════════════════════════════════════════════════════════════════
# TTL / time-bounded lease tests (subsumes retired
#   tests/unit/test_coord_registry_ttl.sh, Track B3)
# ══════════════════════════════════════════════════════════════════════════════


def _ttl_span(state_file, lease_id):
    """expires_at - acquired_at for a lease (== effective_ttl on acquire), or None."""
    rec = _read_state(state_file).get("leases", {}).get(lease_id)
    if not rec:
        return None
    return int(rec["expires_at"]) - int(rec["acquired_at"])


def _expire_all(state_file):
    """Rewrite every lease's expires_at to the past (mirrors the .sh's _expire_all)."""
    st = _read_state(state_file)
    past = int(time.time()) - 1000
    for rec in st.get("leases", {}).values():
        rec["expires_at"] = past
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(st, f)


def _only_lease(state_file):
    """The single lease_id in the state file (helper for one-lease scenarios)."""
    leases = list(_read_state(state_file).get("leases", {}))
    assert len(leases) == 1, f"expected exactly one lease, got {leases}"
    return leases[0]


# ── (m) default TTL = 120s on acquire (ttl omitted) — exact span ───────────────
def test_ttl_default_120_on_acquire(tmp_path):
    sf = str(tmp_path / "leases.json")

    _reset_state(sf)
    p_rc, p_lease, p_err = _run_py_acquire(sf, "agent-a", "src/api", "write", None)
    p_span = _ttl_span(sf, p_lease)

    assert p_rc == 0 and p_err == ""
    assert p_span == 120, f"default acquire span={p_span} != 120"


# ── (n) default TTL = 120s on renew (ttl omitted) — delta window ───────────────
def test_ttl_default_120_on_renew(tmp_path):
    sf = str(tmp_path / "leases.json")

    _reset_state(sf)
    _run_py_acquire(sf, "agent-a", "src/api", "write", 60)
    p_lease = _only_lease(sf)
    now_p = int(time.time())
    p_rc, p_err = _run_py_renew(sf, "agent-a", p_lease, None)
    p_delta = int(_read_state(sf)["leases"][p_lease]["expires_at"]) - now_p

    assert p_rc == 0 and p_err == ""
    assert 115 <= p_delta <= 125, f"renew default delta={p_delta} not ~120"


# ── (o) max TTL = 3600s; larger values capped silently (acquire + renew) ───────
def test_ttl_max_cap_3600(tmp_path):
    sf = str(tmp_path / "leases.json")

    # acquire ttl=99999 → span exactly 3600.
    _reset_state(sf)
    _run_py_acquire(sf, "agent-a", "src/api", "write", 99999)
    p_span = _ttl_span(sf, _only_lease(sf))
    assert p_span == 3600, f"acquire cap span={p_span} != 3600"

    # renew ttl=99999 → delta ~3600.
    _reset_state(sf)
    _run_py_acquire(sf, "agent-a", "src/api", "write", 60)
    p_lease = _only_lease(sf)
    now_p = int(time.time())
    p_rc, p_err = _run_py_renew(sf, "agent-a", p_lease, 99999)
    p_delta = int(_read_state(sf)["leases"][p_lease]["expires_at"]) - now_p

    assert p_rc == 0 and p_err == ""
    assert 3595 <= p_delta <= 3605, f"renew cap delta={p_delta} not ~3600"


# ── (p) expiry self-heal: a crashed holder's expired lease frees on next acquire
#        and is pruned from state; the competing writer gets a fresh id ─────────
def test_ttl_expiry_self_heal(tmp_path):
    sf = str(tmp_path / "leases.json")

    _reset_state(sf)
    _run_py_acquire(sf, "agent-crashed", "src/api", "write", 60)
    p_old = _only_lease(sf)
    _expire_all(sf)
    p_rc, p_new, p_err = _run_py_acquire(sf, "agent-rescue", "src/api", "write", 30)
    p_pruned = p_old not in _read_state(sf)["leases"]

    assert p_rc == 0 and p_err == "", (
        "competing writer must succeed after holder expiry"
    )
    assert p_new and p_new != p_old, (
        f"rescue must get fresh id (old={p_old} new={p_new})"
    )
    assert p_pruned, "expired lease must be pruned"


# ── (q) renew rejects an expired lease (rc=1) and an unknown id (rc=1) ─────────
def test_renew_rejects_expired_and_unknown(tmp_path):
    sf = str(tmp_path / "leases.json")
    shared = "abc123ef"  # valid hex; shared so stderr (which embeds the id) is deterministic

    # Expired: seed a live lease, expire it, then renew.
    _reset_state(sf); _seed_lease(sf, shared, "agent-h"); _expire_all(sf)
    p_rc, p_err = _run_py_renew(sf, "agent-h", shared, 60)
    assert p_rc == 1, f"renew expired rc={p_rc} (expected 1)"
    assert shared in p_err

    # Unknown id on empty state → rc=1.
    _reset_state(sf)
    p2_rc, p2_err = _run_py_renew(sf, "agent-h", shared, 60)
    assert p2_rc == 1, f"renew unknown rc={p2_rc} (expected 1)"
    assert shared in p2_err


# ── (r) renew arg-validation → rc=2 (missing args / bad ttl) ───────────────────
def test_renew_arg_validation(tmp_path):
    sf = str(tmp_path / "leases.json")

    # (agent, lease_id, ttl, label) — every form the retired .sh's group 8 exercises.
    variants = [
        ("", "", "60", "missing agent+lease"),
        ("agent-x", "", "60", "missing lease_id"),
        ("agent-x", "abcdef", "-1", "negative ttl"),
        ("agent-x", "abcdef", "abc", "non-numeric ttl"),
    ]
    for agent, lease, ttl, label in variants:
        _reset_state(sf)
        p_rc, p_err = _run_py_renew(sf, agent, lease, ttl)
        assert p_rc == 2, f"[{label}] expected rc=2, got py={p_rc}"
        assert p_err


# ── (s) renew holder/non-holder logic on a seeded live lease ───────────────────
def test_renew_holder_and_non_holder(tmp_path):
    sf = str(tmp_path / "leases.json")

    _reset_state(sf)
    _seed_lease(sf, "cafebabe", "agent-w")
    assert cr.coord_renew("agent-w", "cafebabe", 600, state_file=sf) == 0
    assert cr.coord_renew("agent-other", "cafebabe", 60, state_file=sf) == 3


# ── (t) release of an expired lease returns non-zero (rc=1) ───────────────────
def test_expired_release(tmp_path):
    sf = str(tmp_path / "leases.json")
    shared = "0ff1ce00"  # shared hex id so the "unknown lease id <id>" stderr is deterministic

    _reset_state(sf); _seed_lease(sf, shared, "agent-a"); _expire_all(sf)
    p_rc, p_err = _run_py_release(sf, shared)

    assert p_rc == 1, f"expired release rc={p_rc} (expected 1)"
    assert "unknown" in p_err.lower()
    assert shared in p_err


# ── (u) a rejected (non-holder) renew must NOT mutate the lease ────────────────
def test_renew_not_holder_no_mutation(tmp_path):
    sf = str(tmp_path / "leases.json")
    shared = "beadfeed"

    _reset_state(sf); _seed_lease(sf, shared, "agent-holder")
    p_before = int(_read_state(sf)["leases"][shared]["expires_at"])
    p_rc, p_err = _run_py_renew(sf, "agent-other", shared, 600)
    p_after = int(_read_state(sf)["leases"][shared]["expires_at"])

    assert p_rc == 3, f"non-holder renew rc={p_rc} (expected 3)"
    assert "not the current holder" in p_err.lower()
    assert p_before == p_after, f"rejected renew mutated lease {p_before}->{p_after}"
