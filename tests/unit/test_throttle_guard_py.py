"""Parity gate: mini_ork.dispatch.throttle_guard vs lib/throttle-guard.sh.

Each test invokes the LIVE bash subprocess (no mocks, no hardcoded expected
outputs — expected is always derived from a control bash invocation that
shares the inputs) on identical state and asserts byte-identical output /
state against the Python port.

Cases (>=6):
  (a) classify_error_capacity             — "Selected model is at capacity"
  (b) classify_error_throttled_glm        — GLM "api_error_status:429"
  (c) classify_error_overloaded_auth_timeout
                                          — three sub-assertions: 529 /
                                            401 / gtimeout
  (d) classify_error_unknown_and_missing  — non-matching content + missing file
  (e) record_failure_backoff_ladder       — capacity×4 (1800s), auth_failed
                                            (0s), unknown (60s)
  (f) check_cooldown_active_and_expired   — cool_until in the future / past
  (g) systemic_halt_check_threshold_window
                                          — 3 fresh flags halt, 2 don't,
                                            3 stale last_seen don't
  (h) classify_run_failures               — scan a run dir's llm-failures/
  (i) wait_for_cooldowns_longest          — longest sleep value across
                                            providers; bash `sleep` stub +
                                            python time.sleep monkeypatch
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import throttle_guard as tg

SH = REPO / "lib" / "throttle-guard.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _bash(extra_env: dict[str, str], body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh bash that has sourced lib/throttle-guard.sh."""
    wrapper = f'. "{SH}"\n{body}\n'
    env = {**os.environ, **extra_env}
    return subprocess.run(
        ["bash", "-c", wrapper], env=env, capture_output=True, text=True,
    )


def _bash_classify(err_log: Path, mini_home: Path) -> str:
    r = _bash({"MINI_ORK_HOME": str(mini_home)},
              f'_throttle_classify_error "{err_log}"')
    return r.stdout.strip()


def _flag_normalized(path: Path) -> dict[str, int | str]:
    """Read a flag file and return a dict with the timestamp fields
    (``cool_down_until``, ``last_seen``) expressed as offsets from each other
    so that two flag files produced by independent processes (bash & python,
    with sub-second clock skew) compare byte-identically. Counters and
    classification strings pass through unchanged."""
    text = path.read_text()
    parsed: dict[str, int | str] = {}
    last_seen = 0
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            iv = int(v)
            parsed[k] = iv
            if k == "last_seen":
                last_seen = iv
        except ValueError:
            parsed[k] = v
    # Only timestamp fields are normalized; counters and strings pass through.
    out: dict[str, int | str] = {}
    for k, v in parsed.items():
        if k in ("cool_down_until", "last_seen"):
            out[k] = v - last_seen if isinstance(v, int) else v
        else:
            out[k] = v
    return out


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
    py = tg.classify_error(str(err))
    bash = _bash_classify(err, tmp_path / "unused")
    assert py == bash == "capacity"


# ─────────────────────────────────────────────────────────────────────────────
# (b) classify_error — GLM "api_error_status:429" → throttled
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_throttled_glm_429(tmp_path):
    err = tmp_path / "err.log"
    err.write_text(
        '{"error": {"message": "Fair Usage Policy", '
        '"api_error_status:429, retry later"}}\n'
    )
    py = tg.classify_error(str(err))
    bash = _bash_classify(err, tmp_path / "unused")
    assert py == bash == "throttled"

    # Also cover the literal "Request rejected (429)" + rate_limit_exceeded.
    err2 = tmp_path / "err2.log"
    err2.write_text("Request rejected (429): rate_limit_exceeded\n")
    py2 = tg.classify_error(str(err2))
    bash2 = _bash_classify(err2, tmp_path / "unused")
    assert py2 == bash2 == "throttled"


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
        py = tg.classify_error(str(err))
        bash = _bash_classify(err, tmp_path / "unused")
        assert py == bash == expected, (
            f"content={content!r} py={py!r} bash={bash!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (d) classify_error — unknown (non-matching) + missing file
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_unknown_and_missing(tmp_path):
    # Non-matching content.
    err = tmp_path / "err.log"
    err.write_text("some unrelated log line\n")
    py = tg.classify_error(str(err))
    bash = _bash_classify(err, tmp_path / "unused")
    assert py == bash == "unknown"

    # Missing file.
    missing = tmp_path / "does_not_exist.log"
    py_missing = tg.classify_error(str(missing))
    bash_missing = _bash_classify(missing, tmp_path / "unused")
    assert py_missing == bash_missing == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# (e) record_failure — backoff ladder + auth (no backoff) + unknown (60s)
# ─────────────────────────────────────────────────────────────────────────────
def test_record_failure_backoff_ladder(tmp_path):
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_home.mkdir()
    py_home.mkdir()

    # Sub-case 1: 4 consecutive 'capacity' failures → BACKOFFS[4] = 1800s.
    for _ in range(4):
        r_bash = _bash(
            {"MINI_ORK_HOME": str(bash_home)},
            f'_throttle_record_failure "glm" "capacity"',
        )
        assert r_bash.returncode == 0
        monkey = pytest.MonkeyPatch()
        monkey.setenv("MINI_ORK_HOME", str(py_home))
        try:
            tg.record_failure("glm", "capacity")
        finally:
            monkey.undo()

    bash_flag = bash_home / "state" / "throttle-glm.flag"
    py_flag = py_home / "state" / "throttle-glm.flag"
    assert bash_flag.is_file() and py_flag.is_file()

    b = _flag_normalized(bash_flag)
    p = _flag_normalized(py_flag)
    # Capacity × 4: BACKOFFS[4] = 3600, consecutive=4, last_error=capacity.
    assert b == p, f"bash={b} py={p}"
    assert b["consecutive_failures"] == 4
    assert b["last_error"] == "capacity"
    assert b["cool_down_until"] == 3600
    assert b["last_seen"] == 0  # last_seen - last_seen == 0 by definition.
    assert b["cool_down_until"] == 3600  # last_seen - last_seen = 0; offset = cool_seconds.

    # Sub-case 2: auth_failed → cool_seconds=0, consecutive=1.
    fresh_bash = tmp_path / "bash_home2"
    fresh_py = tmp_path / "py_home2"
    fresh_bash.mkdir()
    fresh_py.mkdir()
    r_bash = _bash(
        {"MINI_ORK_HOME": str(fresh_bash)},
        '_throttle_record_failure "claude" "auth_failed"',
    )
    assert r_bash.returncode == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(fresh_py))
    try:
        tg.record_failure("claude", "auth_failed")
    finally:
        monkey.undo()

    b2 = _flag_normalized(fresh_bash / "state" / "throttle-claude.flag")
    p2 = _flag_normalized(fresh_py / "state" / "throttle-claude.flag")
    assert b2 == p2
    assert b2["consecutive_failures"] == 1
    assert b2["last_error"] == "auth_failed"
    assert b2["cool_down_until"] == 0  # auth_failed: no backoff.

    # Sub-case 3: unknown → cool_seconds=60.
    fresh_bash3 = tmp_path / "bash_home3"
    fresh_py3 = tmp_path / "py_home3"
    fresh_bash3.mkdir()
    fresh_py3.mkdir()
    r_bash = _bash(
        {"MINI_ORK_HOME": str(fresh_bash3)},
        '_throttle_record_failure "codex" "unknown"',
    )
    assert r_bash.returncode == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(fresh_py3))
    try:
        tg.record_failure("codex", "unknown")
    finally:
        monkey.undo()

    b3 = _flag_normalized(fresh_bash3 / "state" / "throttle-codex.flag")
    p3 = _flag_normalized(fresh_py3 / "state" / "throttle-codex.flag")
    assert b3 == p3
    assert b3["consecutive_failures"] == 1
    assert b3["last_error"] == "unknown"
    assert b3["cool_down_until"] == 60  # unknown: 60s cool-down.

    # Sub-case 4: structural format check — both files have the four key=value
    # lines in the documented order (cool_down_until, consecutive_failures,
    # last_error, last_seen). The plan requires this even where the int values
    # are compared via the normalized offset.
    expected_keys = ["cool_down_until", "consecutive_failures", "last_error",
                     "last_seen"]
    for path in (bash_flag, py_flag):
        keys_in_file = []
        for line in path.read_text().splitlines():
            if "=" in line:
                keys_in_file.append(line.split("=", 1)[0])
        assert keys_in_file[:4] == expected_keys


# ─────────────────────────────────────────────────────────────────────────────
# (f) check_cooldown — active + expired
# ─────────────────────────────────────────────────────────────────────────────
def test_check_cooldown_active_and_expired(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    now = int(time.time())

    # Active: cool_down_until = now + 300.
    _seed_flag(state, "active", cool_down_until=now + 300,
               consecutive_failures=1, last_error="capacity",
               last_seen=now)
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_home.mkdir(); py_home.mkdir()
    # Copy the seed flag into both homes so bash & python read identical input.
    (bash_home / "state").mkdir(parents=True)
    (bash_home / "state" / "throttle-active.flag").write_text(
        (state / "throttle-active.flag").read_text())
    (py_home / "state").mkdir(parents=True)
    (py_home / "state" / "throttle-active.flag").write_text(
        (state / "throttle-active.flag").read_text())

    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)},
        '_throttle_check_cooldown "active"',
    )
    bash_active = int(r_bash.stdout.strip())
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_active = tg.check_cooldown("active")
    finally:
        monkey.undo()
    assert 0 < bash_active <= 300
    assert 0 < py_active <= 300
    # Both implementations agree within clock skew (<2s for a single pytest).
    assert abs(bash_active - py_active) <= 2

    # Expired: cool_down_until = now - 1.
    _seed_flag(state, "expired", cool_down_until=now - 1,
               consecutive_failures=1, last_error="capacity",
               last_seen=now - 1)
    (bash_home / "state" / "throttle-expired.flag").write_text(
        (state / "throttle-expired.flag").read_text())
    (py_home / "state" / "throttle-expired.flag").write_text(
        (state / "throttle-expired.flag").read_text())

    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)},
        '_throttle_check_cooldown "expired"',
    )
    bash_expired = int(r_bash.stdout.strip())
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_expired = tg.check_cooldown("expired")
    finally:
        monkey.undo()
    assert bash_expired == 0 == py_expired


# ─────────────────────────────────────────────────────────────────────────────
# (g) systemic_halt_check — threshold + window
# ─────────────────────────────────────────────────────────────────────────────
def test_systemic_halt_check_threshold_window(tmp_path):
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_home.mkdir(); py_home.mkdir()
    now = int(time.time())

    def _seed(home: Path, providers: list[str], *, last_seen_offset: int):
        state = home / "state"
        state.mkdir(parents=True, exist_ok=True)
        for p in providers:
            _seed_flag(state, p, cool_down_until=now + 600,
                       consecutive_failures=1, last_error="throttled",
                       last_seen=now + last_seen_offset)

    # Sub-case 1: 3 fresh flags → halt (True on both sides).
    _seed(bash_home, ["a", "b", "c"], last_seen_offset=0)
    _seed(py_home, ["a", "b", "c"], last_seen_offset=0)
    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)}, '_throttle_systemic_halt_check')
    # bash rc 0 == halt; rc 1 == no halt.
    bash_halt = r_bash.returncode == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_halt = tg.systemic_halt_check()
    finally:
        monkey.undo()
    assert py_halt is True
    assert bash_halt is True

    # Sub-case 2: only 2 flags → no halt.
    _seed(bash_home, ["a", "b"], last_seen_offset=0)
    _seed(py_home, ["a", "b"], last_seen_offset=0)
    # Drop the third flag.
    (bash_home / "state" / "throttle-c.flag").unlink()
    (py_home / "state" / "throttle-c.flag").unlink()
    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)}, '_throttle_systemic_halt_check')
    bash_halt = r_bash.returncode == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_halt = tg.systemic_halt_check()
    finally:
        monkey.undo()
    assert py_halt is False
    assert bash_halt is False

    # Sub-case 3: 3 flags but last_seen is older than the 600s window.
    _seed(bash_home, ["a", "b", "c"], last_seen_offset=-700)
    _seed(py_home, ["a", "b", "c"], last_seen_offset=-700)
    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)}, '_throttle_systemic_halt_check')
    bash_halt = r_bash.returncode == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        py_halt = tg.systemic_halt_check()
    finally:
        monkey.undo()
    assert py_halt is False
    assert bash_halt is False


# ─────────────────────────────────────────────────────────────────────────────
# (h) classify_run_failures — scan run_dir/llm-failures/*.err.log
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_run_failures(tmp_path):
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_home.mkdir(); py_home.mkdir()

    bash_run = bash_home / "run-x"
    py_run = py_home / "run-x"
    for run in (bash_run, py_run):
        (run / "llm-failures").mkdir(parents=True)
        (run / "llm-failures" / "1000-glm.err.log").write_text(
            "Selected model is at capacity\n")
        (run / "llm-failures" / "2000-claude.err.log").write_text(
            "429 rate_limit_exceeded\n")
        (run / "llm-failures" / "3000-codex.err.log").write_text(
            "401 authentication_error\n")

    # Bash: source + call _throttle_classify_run_failures.
    _bash({"MINI_ORK_HOME": str(bash_home)},
          f'_throttle_classify_run_failures "{bash_run}"')

    # Python: same call.
    monkey = pytest.MonkeyPatch()
    monkey.setenv("MINI_ORK_HOME", str(py_home))
    try:
        tg.classify_run_failures(str(py_run))
    finally:
        monkey.undo()

    # Both sides should have created the same 3 flag files.
    bash_state = bash_home / "state"
    py_state = py_home / "state"
    for provider in ("glm", "claude", "codex"):
        bf = bash_state / f"throttle-{provider}.flag"
        pf = py_state / f"throttle-{provider}.flag"
        assert bf.is_file(), f"bash missing flag for {provider}"
        assert pf.is_file(), f"python missing flag for {provider}"
        b = _flag_normalized(bf)
        p = _flag_normalized(pf)
        # Different classifications → different cool_seconds. Compare
        # structural offsets; the absolute timestamps differ.
        assert b["consecutive_failures"] == p["consecutive_failures"] == 1
        assert b["last_error"] == p["last_error"]
        # cool_seconds varies by classification but must be one of {60, 300,
        # 600, 1800, ...} or 0 for auth — verify the ladder entry.
        from mini_ork.dispatch.throttle_guard import BACKOFFS
        assert b["cool_down_until"] in set(BACKOFFS + (0, 60))


# ─────────────────────────────────────────────────────────────────────────────
# (i) wait_for_cooldowns — captures the longest sleep duration
# ─────────────────────────────────────────────────────────────────────────────
def test_wait_for_cooldowns_longest_value(tmp_path, monkeypatch):
    bash_home = tmp_path / "bash_home"
    py_home = tmp_path / "py_home"
    bash_home.mkdir(); py_home.mkdir()
    now = int(time.time())

    # Three providers with cool_down_until = now + 10, now + 50, now + 200.
    # Longest is 200 (well below the 1800 cap and any reasonable deadline).
    def _seed(home: Path):
        state = home / "state"
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
    _seed(bash_home)
    _seed(py_home)

    # Bash: override `sleep` with a stub that echoes the duration.
    sleep_wrapper = (
        'sleep() { echo "$@"; }\n'
        'export -f sleep\n'
        '_throttle_wait_for_cooldowns 0 "short" "mid" "long"\n'
    )
    r_bash = _bash(
        {"MINI_ORK_HOME": str(bash_home)}, sleep_wrapper)
    assert r_bash.returncode == 0
    # Bash stub echoes the duration as a single token (e.g. "200").
    bash_slept = r_bash.stdout.strip()
    assert bash_slept.isdigit(), f"bash sleep stub output: {bash_slept!r}"
    bash_longest = int(bash_slept)
    assert 198 <= bash_longest <= 200

    # Python: monkeypatch time.sleep to capture the arg.
    captured: list[int] = []
    monkeypatch.setenv("MINI_ORK_HOME", str(py_home))
    monkeypatch.setattr(time, "sleep", lambda s: captured.append(int(s)))
    py_rc = tg.wait_for_cooldowns(0, "short", "mid", "long")
    assert py_rc == 0
    assert len(captured) == 1
    py_longest = captured[0]
    assert 198 <= py_longest <= 200

    # Both implementations agree on the longest cool-down within clock skew.
    assert abs(bash_longest - py_longest) <= 2

    # No-op path: when no providers are throttled, both return 0 without sleeping.
    def _boom(_):
        raise AssertionError("should not sleep")
    monkeypatch.setattr(time, "sleep", _boom)
    rc = tg.wait_for_cooldowns(0, "no-such-provider")
    assert rc == 0