"""Unit tests: mini_ork.dispatch.throttle_guard (bash parity halves removed; formerly vs lib/throttle-guard.sh).

Each test drives the Python port (no mocks beyond env pinning + a
monkeypatched time.sleep) and asserts classification strings, flag-file
state, cooldown arithmetic, and the systemic-halt threshold/window
contract.

Cases:
  (a) classify_error_capacity             — "Selected model is at capacity"
  (b) classify_error_throttled_glm        — GLM "api_error_status:429"
  (c) classify_error_overloaded_auth_timeout
                                          — three sub-assertions: 529 /
                                            401 / gtimeout
  (d) classify_error_unknown_and_missing  — non-matching content + missing file
  (e) record_failure_backoff_ladder       — capacity×4 (3600s), auth_failed
                                            (0s), unknown (60s)
  (f) check_cooldown_active_and_expired   — cool_until in the future / past
  (g) systemic_halt_check_threshold_window
                                          — 3 fresh flags halt, 2 don't,
                                            3 stale last_seen don't
  (h) classify_run_failures               — scan a run dir's llm-failures/
  (i) wait_for_cooldowns_longest          — longest sleep value across
                                            providers; time.sleep monkeypatch
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import throttle_guard as tg
from mini_ork.dispatch.throttle_guard import BACKOFFS


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _read_flag(path: Path) -> dict[str, int | str]:
    """Read a flag file into a dict of key=value pairs (ints parsed)."""
    parsed: dict[str, int | str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            parsed[k] = int(v)
        except ValueError:
            parsed[k] = v
    return parsed


def _seed_flag(state_dir: Path, provider: str, *,
               cool_down_until: int, consecutive_failures: int,
               last_error: str, last_seen: int) -> Path:
    """Write a flag file directly (used by tests where we want to control the
    inputs precisely, e.g. set ``cool_down_until`` to ``now-1``)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    flag = state_dir / f"throttle-{provider}.flag"
    flag.write_text(
        f"cool_down_until={cool_down_until}\n"
        f"consecutive_failures={consecutive_failures}\n"
        f"last_error={last_error}\n"
        f"last_seen={last_seen}\n"
    )
    return flag


# ─────────────────────────────────────────────────────────────────────────────
# (a) classify_error — "Selected model is at capacity"
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_capacity(tmp_path):
    err = tmp_path / "err.log"
    err.write_text("Selected model is at capacity, retry in 30s\n")
    assert tg.classify_error(str(err)) == "capacity"


# ─────────────────────────────────────────────────────────────────────────────
# (b) classify_error — GLM "api_error_status:429" → throttled
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_throttled_glm_429(tmp_path):
    err = tmp_path / "err.log"
    err.write_text(
        '{"error": {"message": "Fair Usage Policy", '
        '"api_error_status:429, retry later"}}\n'
    )
    assert tg.classify_error(str(err)) == "throttled"

    # Also cover the literal "Request rejected (429)" + rate_limit_exceeded.
    err2 = tmp_path / "err2.log"
    err2.write_text("Request rejected (429): rate_limit_exceeded\n")
    assert tg.classify_error(str(err2)) == "throttled"


# ─────────────────────────────────────────────────────────────────────────────
# (c) classify_error — overloaded / auth_failed / timed_out
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_overloaded_auth_timeout(tmp_path):
    cases = [
        ("529 overloaded_error on opus-4.7\n", "overloaded"),
        ("401 authentication_error: invalid_api_key\n", "auth_failed"),
        ("gtimeout: upstream gtimeout after 30s\n", "timed_out"),
    ]
    for content, expected in cases:
        err = tmp_path / f"err_{expected}.log"
        err.write_text(content)
        assert tg.classify_error(str(err)) == expected, f"content={content!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (d) classify_error — unknown (non-matching) + missing file
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_unknown_and_missing(tmp_path):
    # Non-matching content.
    err = tmp_path / "err.log"
    err.write_text("some unrelated log line\n")
    assert tg.classify_error(str(err)) == "unknown"

    # Missing file.
    missing = tmp_path / "does_not_exist.log"
    assert tg.classify_error(str(missing)) == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# (e) record_failure — backoff ladder + auth (no backoff) + unknown (60s)
# ─────────────────────────────────────────────────────────────────────────────
def test_record_failure_backoff_ladder(tmp_path):
    # Sub-case 1: 4 consecutive 'capacity' failures → BACKOFFS[4] = 3600s.
    py_home = tmp_path / "py_home"
    py_home.mkdir()
    for _ in range(4):
        monkey = pytest.MonkeyPatch()
        monkey.setenv("MINI_ORK_HOME", str(py_home))
        try:
            tg.record_failure("glm", "capacity")
        finally:
            monkey.undo()

    py_flag = py_home / "state" / "throttle-glm.flag"
    assert py_flag.is_file()
    p = _read_flag(py_flag)
    assert p["consecutive_failures"] == 4
    assert p["last_error"] == "capacity"
    # cool_down_until = last_seen + cool_seconds
    assert p["cool_down_until"] - p["last_seen"] == 3600

    # Sub-case 2: auth_failed → cool_seconds=0, consecutive=1.
    fresh_py = tmp_path / "py_home2"
    fresh_py.mkdir()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(fresh_py))
    try:
        tg.record_failure("claude", "auth_failed")
    finally:
        monkey.undo()

    p2 = _read_flag(fresh_py / "state" / "throttle-claude.flag")
    assert p2["consecutive_failures"] == 1
    assert p2["last_error"] == "auth_failed"
    assert p2["cool_down_until"] - p2["last_seen"] == 0  # auth_failed: no backoff.

    # Sub-case 3: unknown → cool_seconds=60.
    fresh_py3 = tmp_path / "py_home3"
    fresh_py3.mkdir()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(fresh_py3))
    try:
        tg.record_failure("codex", "unknown")
    finally:
        monkey.undo()

    p3 = _read_flag(fresh_py3 / "state" / "throttle-codex.flag")
    assert p3["consecutive_failures"] == 1
    assert p3["last_error"] == "unknown"
    assert p3["cool_down_until"] - p3["last_seen"] == 60  # unknown: 60s cool-down.

    # Sub-case 4: structural format check — the flag file has the four
    # key=value lines in the documented order (cool_down_until,
    # consecutive_failures, last_error, last_seen).
    expected_keys = ["cool_down_until", "consecutive_failures", "last_error",
                     "last_seen"]
    keys_in_file = []
    for line in py_flag.read_text().splitlines():
        if "=" in line:
            keys_in_file.append(line.split("=", 1)[0])
    assert keys_in_file[:4] == expected_keys


# ─────────────────────────────────────────────────────────────────────────────
# (f) check_cooldown — active + expired
# ─────────────────────────────────────────────────────────────────────────────
def test_check_cooldown_active_and_expired(tmp_path):
    py_home = tmp_path / "py_home"
    py_home.mkdir()
    now = int(time.time())

    # Active: cool_down_until = now + 300.
    (py_home / "state").mkdir(parents=True)
    _seed_flag(py_home / "state", "active", cool_down_until=now + 300,
               consecutive_failures=1, last_error="capacity",
               last_seen=now)

    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_active = tg.check_cooldown("active")
    finally:
        monkey.undo()
    assert 0 < py_active <= 300

    # Expired: cool_down_until = now - 1.
    _seed_flag(py_home / "state", "expired", cool_down_until=now - 1,
               consecutive_failures=1, last_error="capacity",
               last_seen=now - 1)

    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_expired = tg.check_cooldown("expired")
    finally:
        monkey.undo()
    assert py_expired == 0


# ─────────────────────────────────────────────────────────────────────────────
# (g) systemic_halt_check — threshold + window
# ─────────────────────────────────────────────────────────────────────────────
def test_systemic_halt_check_threshold_window(tmp_path):
    py_home = tmp_path / "py_home"
    py_home.mkdir()
    now = int(time.time())

    def _seed(home: Path, providers: list[str], *, last_seen_offset: int):
        state = home / "state"
        state.mkdir(parents=True, exist_ok=True)
        for p in providers:
            _seed_flag(state, p, cool_down_until=now + 600,
                       consecutive_failures=1, last_error="throttled",
                       last_seen=now + last_seen_offset)

    # Sub-case 1: 3 fresh flags → halt.
    _seed(py_home, ["a", "b", "c"], last_seen_offset=0)
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        assert tg.systemic_halt_check() is True
    finally:
        monkey.undo()

    # Sub-case 2: only 2 flags → no halt.
    (py_home / "state" / "throttle-c.flag").unlink()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        assert tg.systemic_halt_check() is False
    finally:
        monkey.undo()

    # Sub-case 3: 3 flags but last_seen is older than the 600s window.
    _seed(py_home, ["a", "b", "c"], last_seen_offset=-700)
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        assert tg.systemic_halt_check() is False
    finally:
        monkey.undo()


# ─────────────────────────────────────────────────────────────────────────────
# (h) classify_run_failures — scan run_dir/llm-failures/*.err.log
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_run_failures(tmp_path):
    py_home = tmp_path / "py_home"
    py_home.mkdir()

    py_run = py_home / "run-x"
    (py_run / "llm-failures").mkdir(parents=True)
    (py_run / "llm-failures" / "1000-glm.err.log").write_text(
        "Selected model is at capacity\n")
    (py_run / "llm-failures" / "2000-claude.err.log").write_text(
        "429 rate_limit_exceeded\n")
    (py_run / "llm-failures" / "3000-codex.err.log").write_text(
        "401 authentication_error\n")

    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        tg.classify_run_failures(str(py_run))
    finally:
        monkey.undo()

    # 3 flag files created with the right classifications.
    py_state = py_home / "state"
    expected_class = {"glm": "capacity", "claude": "throttled", "codex": "auth_failed"}
    for provider, klass in expected_class.items():
        pf = py_state / f"throttle-{provider}.flag"
        assert pf.is_file(), f"missing flag for {provider}"
        p = _read_flag(pf)
        assert p["consecutive_failures"] == 1
        assert p["last_error"] == klass
        # cool_seconds varies by classification but must be a ladder entry
        # (or 0 for auth).
        assert p["cool_down_until"] - p["last_seen"] in set(BACKOFFS + (0, 60))


# ─────────────────────────────────────────────────────────────────────────────
# (i) wait_for_cooldowns — captures the longest sleep duration
# ─────────────────────────────────────────────────────────────────────────────
def test_wait_for_cooldowns_longest_value(tmp_path, monkeypatch):
    py_home = tmp_path / "py_home"
    py_home.mkdir()
    now = int(time.time())

    # Three providers with cool_down_until = now + 10, now + 50, now + 200.
    # Longest is 200 (well below the 1800 cap and any reasonable deadline).
    state = py_home / "state"
    state.mkdir(parents=True)
    _seed_flag(state, "short", cool_down_until=now + 10,
               consecutive_failures=1, last_error="throttled",
               last_seen=now)
    _seed_flag(state, "mid", cool_down_until=now + 50,
               consecutive_failures=1, last_error="throttled",
               last_seen=now)
    _seed_flag(state, "long", cool_down_until=now + 200,
               consecutive_failures=1, last_error="throttled",
               last_seen=now)

    # Monkeypatch time.sleep to capture the arg.
    captured: list[int] = []
    monkeypatch.setenv("MINI_ORK_HOME", str(py_home))
    monkeypatch.setattr(time, "sleep", lambda s: captured.append(int(s)))
    py_rc = tg.wait_for_cooldowns(0, "short", "mid", "long")
    assert py_rc == 0
    assert len(captured) == 1
    py_longest = captured[0]
    assert 198 <= py_longest <= 200

    # No-op path: when no providers are throttled, return 0 without sleeping.
    def _boom(_):
        raise AssertionError("should not sleep")
    monkeypatch.setattr(time, "sleep", _boom)
    rc = tg.wait_for_cooldowns(0, "no-such-provider")
    assert rc == 0
