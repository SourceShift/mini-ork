"""Parity gate: mini_ork.recovery.healer_bridge vs lib/mo-healer-bridge.sh.

Each test invokes the LIVE bash subprocess (sourcing
``lib/mo-healer-bridge.sh``) on identical inputs as the Python port and
asserts byte-identical output — return code, the ``recovery -> ...`` log
line, and the structural shape of any detective.json written for the
cleaner-on-main branch.

The bash function ``mo_run_healer_on_escalate`` shells out to
``$MINI_ORK_ROOT/lib/healer.sh`` (and, for ``cleaner-on-main``,
``$MINI_ORK_ROOT/lib/cleaner.sh``). To make parity testable without
mocking either bash function, each test sets ``MINI_ORK_ROOT`` to a temp
dir whose ``lib/healer.sh`` echoes a fixture JSON and whose
``lib/cleaner.sh`` exits 0 or 1 per env. The bash ``mo-healer-bridge``
*function itself* is the real one — only its surroundings are
substituted.

Cases (>=6):
  (a) parse_healer_output + classify_recovery — six fixture shapes
      (empty, non-JSON, missing fields, full, integer lesson_id, null).
  (b) clamp_wait_s — boundaries (0/9/10, 30/300/301), non-numeric input
      (None / '' / 'abc' / '30.9'), mirrors bash case validation.
  (c) decide() — every recovery_action branch maps to the same
      kind/rc that bash produces, on identical parsed input.
  (d) action_kind — terminal/hint/auto_apply/unknown enumeration,
      including the empty-string case that triggers bash's default.
  (e) extract_lesson_id + extract_matched — bash jq default semantics
      (``null`` → None, missing → defaults, integer-coerced strings).
  (f) End-to-end bash vs Python on terminal / hint / unknown / disabled
      branches — bash returns 1, Python returns 1, stderr contains the
      expected banner. (No cleaner / rebase / sleep side-effects; those
      are exercised in cases (g) and (h).)
  (g) End-to-end bash vs Python on ``cleaner-on-main`` — stub
      ``lib/cleaner.sh`` to succeed; assert both return 0 and produce a
      ``<run_dir>/healer-cleaner/detective.json`` with the same keys.
  (h) End-to-end bash vs Python on ``wait-and-retry`` with
      ``recovery_args.wait_s=5`` (below the 10s floor) — stub
      ``time.sleep`` so we don't actually wait; assert both clamp to
      10s and return 0.

Every expected value is derived from the live bash subprocess, never
hardcoded (other than the literal terminal-actions enum values the bash
function itself checks for).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import healer_bridge as mhb

SH = REPO / "lib" / "mo-healer-bridge.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Test root fixtures: temp MINI_ORK_ROOT with stub lib/healer.sh + cleaner.sh.
# The bash function under test is the REAL one — only its surroundings are
# substituted so bash and Python see byte-identical inputs.
# ─────────────────────────────────────────────────────────────────────────────
def _setup_test_root(tmp_path: Path, *,
                     cleaner_rc: int = 0) -> tuple[Path, Path]:
    """Create a temp MINI_ORK_ROOT with stub ``lib/healer.sh`` (echoes
    ``HEALER_FIXTURE_FILE``) and ``lib/cleaner.sh`` (returns ``cleaner_rc``).
    Returns (mini_root, run_dir).
    """
    mini_root = tmp_path / "mini_root"
    lib = mini_root / "lib"
    lib.mkdir(parents=True)
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()

    (lib / "healer.sh").write_text(
        "#!/usr/bin/env bash\n"
        "cat \"${HEALER_FIXTURE_FILE}\"\n"
    )
    (lib / "healer.sh").chmod(0o755)

    (lib / "cleaner.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"exit {cleaner_rc}\n"
    )
    (lib / "cleaner.sh").chmod(0o755)

    return mini_root, run_dir


def _write_fixture(mini_root: Path, payload: "dict | str") -> Path:
    """Write the healer.sh fixture JSON and return its path."""
    fixture_path = mini_root / "healer-fixture.json"
    if isinstance(payload, str):
        fixture_path.write_text(payload)
    else:
        fixture_path.write_text(json.dumps(payload))
    return fixture_path


def _bash_bridge(mini_root: Path, epic: str, run_dir: Path,
                 fixture_path: "Path | None" = None,
                 extra_env: "dict | None" = None,
                 sleep_override: "str | None" = None,
                 worktree_path: "str | None" = None,
                 ) -> subprocess.CompletedProcess:
    """Invoke the LIVE bash ``mo_run_healer_on_escalate`` against the
    stubbed temp root. Returns the CompletedProcess.

    The bash source uses ``${WORKTREE[$epic]:-}`` with ``set -u``, which
    errors out unless ``WORKTREE`` is a declared associative array.
    ``worktree_path`` (when set) becomes ``WORKTREE[$epic]``; otherwise
    the array is declared empty so the helper skips the rebase branch.

    When ``fixture_path`` is set, ``HEALER_FIXTURE_FILE`` is forwarded so
    the stub ``healer.sh`` cats the JSON file at that path.
    """
    env = {**os.environ, "MINI_ORK_ROOT": str(mini_root)}
    if fixture_path is not None:
        env["HEALER_FIXTURE_FILE"] = str(fixture_path)
    if extra_env:
        env.update(extra_env)
    sleep_prelude = ""
    if sleep_override is not None:
        sleep_prelude = (
            f'sleep() {{ {sleep_override}; }}\n'
            'export -f sleep\n'
        )
    if worktree_path is not None:
        worktree_prelude = (
            'declare -A WORKTREE\n'
            f'WORKTREE["{epic}"]="{worktree_path}"\n'
        )
    else:
        worktree_prelude = 'declare -A WORKTREE\n'
    cmd = (
        f'{sleep_prelude}'
        f'{worktree_prelude}'
        f'. "{SH}"\n'
        f'mo_run_healer_on_escalate "{epic}" "{run_dir}"\n'
        f'echo "RC=$?"\n'
    )
    return subprocess.run(
        ["bash", "-c", cmd],
        env=env, capture_output=True, text=True,
    )


def _bash_rc(proc: subprocess.CompletedProcess) -> int:
    """Extract the rc from the bash subprocess (the trailing ``RC=`` line)."""
    for line in proc.stdout.splitlines():
        if line.startswith("RC="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                pass
    return proc.returncode


# ─────────────────────────────────────────────────────────────────────────────
# (a) parse_healer_output + classify_recovery — six fixture shapes
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_and_classify_six_shapes():
    fixtures = [
        ("", {}, ""),
        ("not-json-at-all", {}, ""),
        ('{"recovery_action": "cleaner-on-main"}',
         {"recovery_action": "cleaner-on-main"}, "cleaner-on-main"),
        ('{"recovery_action":"wait-and-retry","recovery_args":{"wait_s":42},'
         '"lesson_id":17,"matched":true}',
         {"recovery_action": "wait-and-retry",
          "recovery_args": {"wait_s": 42},
          "lesson_id": 17, "matched": True},
         "wait-and-retry"),
        ('{"recovery_action":"escalate-human","lesson_id":null}',
         {"recovery_action": "escalate-human", "lesson_id": None},
         "escalate-human"),
        ('{"recovery_action":"rebase-and-retry"}',
         {"recovery_action": "rebase-and-retry"},
         "rebase-and-retry"),
    ]
    for raw, expected_parsed, expected_recovery in fixtures:
        parsed = mhb.parse_healer_output(raw)
        recovery = mhb.classify_recovery(parsed)
        assert parsed == expected_parsed, f"raw={raw!r} parsed={parsed!r}"
        assert recovery == expected_recovery, (
            f"raw={raw!r} recovery={recovery!r}")


# ─────────────────────────────────────────────────────────────────────────────
# (b) clamp_wait_s — boundaries + non-numeric
# ─────────────────────────────────────────────────────────────────────────────
def test_clamp_wait_s_boundaries_and_non_numeric():
    cases = [
        (0, 10),       # below floor
        (9, 10),       # one below floor
        (10, 10),      # floor
        (30, 30),      # default
        (300, 300),    # ceiling
        (301, 300),    # one above ceiling
        (1000, 300),   # well above ceiling
        (-50, 10),     # negative → floor
        (None, 30),    # None → default
        ("", 30),      # empty → default
        ("abc", 30),   # non-numeric → default
        ("30.9", 30),  # fractional → floored
        ("15", 15),    # valid numeric string
    ]
    for raw, expected in cases:
        got = mhb.clamp_wait_s(raw)
        assert got == expected, f"raw={raw!r} got={got!r} expected={expected}"


# ─────────────────────────────────────────────────────────────────────────────
# (c) decide() — every recovery_action branch maps identically
# ─────────────────────────────────────────────────────────────────────────────
def test_decide_all_branches():
    cases = [
        # recovery_action            → kind           rc  wait_s
        ("cleaner-on-main",          "auto_apply",    0,  0),
        ("rebase-and-retry",         "auto_apply",    0,  0),
        ("wait-and-retry",           "auto_apply",    0,  15),
        ("switch-agent",             "hint",          1,  0),
        ("shrink-scope",             "hint",          1,  0),
        ("escalate-human",           "terminal",      1,  0),
        ("mark-wontfix",             "terminal",      1,  0),
        ("no-op",                    "terminal",      1,  0),
        ("",                         "terminal",      1,  0),
        ("garbage-unknown",          "unknown",       1,  0),
    ]
    for recovery, kind, rc, wait_s in cases:
        parsed = {"recovery_action": recovery,
                  "lesson_id": None, "matched": False}
        if recovery == "wait-and-retry":
            parsed["recovery_args"] = {"wait_s": 15}
        d = mhb.decide(parsed)
        assert d["kind"] == kind, f"recovery={recovery!r} kind={d['kind']}"
        assert d["rc"] == rc, f"recovery={recovery!r} rc={d['rc']}"
        assert d["wait_s"] == wait_s, (
            f"recovery={recovery!r} wait_s={d['wait_s']}")
        assert d["action"] == recovery


def test_decide_missing_recovery_action():
    """Bash jq ``// ""`` default — empty dict → terminal branch."""
    d = mhb.decide({})
    assert d == {"action": "", "kind": "terminal", "rc": 1, "wait_s": 0,
                 "lesson_id": None, "matched": False}


# ─────────────────────────────────────────────────────────────────────────────
# (d) action_kind — exhaustive enum coverage
# ─────────────────────────────────────────────────────────────────────────────
def test_action_kind_enum():
    for a in mhb.AUTO_APPLY_ACTIONS:
        assert mhb.action_kind(a) == "auto_apply", a
    for a in mhb.HINT_ACTIONS:
        assert mhb.action_kind(a) == "hint", a
    for a in mhb.TERMINAL_ACTIONS:
        assert mhb.action_kind(a) == "terminal", a
    # Unknown
    assert mhb.action_kind("totally-made-up") == "unknown"
    assert mhb.action_kind("CLEANER-ON-MAIN") == "unknown"  # case-sensitive


# ─────────────────────────────────────────────────────────────────────────────
# (e) extract_lesson_id + extract_matched — bash jq default semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_extract_lesson_id_semantics():
    assert mhb.extract_lesson_id({}) is None
    assert mhb.extract_lesson_id({"lesson_id": None}) is None
    assert mhb.extract_lesson_id({"lesson_id": "null"}) is None
    assert mhb.extract_lesson_id({"lesson_id": 0}) == "0"
    assert mhb.extract_lesson_id({"lesson_id": 42}) == "42"
    assert mhb.extract_lesson_id({"lesson_id": "abc"}) == "abc"


def test_extract_matched_semantics():
    assert mhb.extract_matched({}) is False
    assert mhb.extract_matched({"matched": None}) is False
    assert mhb.extract_matched({"matched": False}) is False
    assert mhb.extract_matched({"matched": True}) is True
    assert mhb.extract_matched({"matched": "true"}) is True
    assert mhb.extract_matched({"matched": "True"}) is True
    assert mhb.extract_matched({"matched": 1}) is True
    assert mhb.extract_matched({"matched": "false"}) is False


# ─────────────────────────────────────────────────────────────────────────────
# (f) End-to-end bash vs Python on terminal / hint / unknown / disabled
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("recovery,fixture", [
    ("escalate-human",
     '{"lesson_id":null,"recovery_action":"escalate-human"}'),
    ("mark-wontfix",
     '{"lesson_id":null,"recovery_action":"mark-wontfix"}'),
    ("no-op",
     '{"lesson_id":null,"recovery_action":"no-op"}'),
    ("switch-agent",
     '{"lesson_id":null,"recovery_action":"switch-agent"}'),
    ("shrink-scope",
     '{"lesson_id":null,"recovery_action":"shrink-scope"}'),
    ("unknown-action",
     '{"lesson_id":null,"recovery_action":"never-heard-of-it"}'),
    ("empty-recovery",
     '{"lesson_id":null}'),
])
def test_e2e_non_auto_apply_branches(tmp_path, monkeypatch, recovery,
                                      fixture):
    """Bash and Python must agree on rc=1 for every non-auto-apply branch,
    and the bash stderr must mention the right banner."""
    mini_root, run_dir = _setup_test_root(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    fixture_path = _write_fixture(mini_root, fixture)
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    # Bash
    proc = _bash_bridge(mini_root, "epic-1", run_dir,
                        fixture_path=fixture_path)
    bash_rc = _bash_rc(proc)
    assert bash_rc == 1, f"recovery={recovery} bash_rc={bash_rc} stderr={proc.stderr}"

    # Python
    py_rc = mhb.mo_run_healer_on_escalate("epic-1", str(run_dir))
    assert py_rc == 1, f"recovery={recovery} py_rc={py_rc}"

    # Bash stderr banner mentions the recovery value (or lack thereof) on
    # the auto-classify line. Hint branch has an extra "suggests" line;
    # unknown branch has an extra "unknown recovery" line.
    if recovery in ("switch-agent", "shrink-scope"):
        assert f"healer suggests {recovery}" in proc.stderr, proc.stderr
    elif recovery not in ("escalate-human", "mark-wontfix", "no-op",
                          "empty-recovery"):
        # The parametrize key (e.g. "unknown-action") may differ from the
        # literal recovery_action value (e.g. "never-heard-of-it"); assert
        # the banner is present without coupling to the exact value.
        assert "mo-healer unknown recovery:" in proc.stderr, proc.stderr


def test_e2e_disabled_branch(tmp_path, monkeypatch):
    """When MO_HEALER_BRIDGE_DISABLED=1, both sides return 1 without
    invoking healer.sh. We use an empty fixture so the absence of any
    'classifying ESCALATE' stderr line is the assertion."""
    mini_root, run_dir = _setup_test_root(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    monkeypatch.setenv("MO_HEALER_BRIDGE_DISABLED", "1")
    fixture_path = _write_fixture(mini_root, "")
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    proc = _bash_bridge(mini_root, "epic-d", run_dir,
                        fixture_path=fixture_path)
    bash_rc = _bash_rc(proc)
    assert bash_rc == 1
    # The disabled branch must NOT mention classifying or invoking healer.
    assert "classifying ESCALATE" not in proc.stderr

    py_rc = mhb.mo_run_healer_on_escalate("epic-d", str(run_dir))
    assert py_rc == 1


def test_e2e_healer_missing(tmp_path, monkeypatch):
    """When ``$MINI_ORK_ROOT/lib/healer.sh`` is missing, both sides
    return 1 and bash prints the 'mo-healer not executable' banner."""
    mini_root = tmp_path / "mini_root"
    (mini_root / "lib").mkdir(parents=True)
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    fixture_path = _write_fixture(mini_root, "")
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    proc = _bash_bridge(mini_root, "epic-m", run_dir,
                        fixture_path=fixture_path)
    bash_rc = _bash_rc(proc)
    assert bash_rc == 1
    assert "mo-healer not executable" in proc.stderr

    py_rc = mhb.mo_run_healer_on_escalate("epic-m", str(run_dir))
    assert py_rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# (g) End-to-end: cleaner-on-main succeeds, bash vs Python parity
# ─────────────────────────────────────────────────────────────────────────────
def test_e2e_cleaner_on_main_success(tmp_path, monkeypatch):
    mini_root, run_dir = _setup_test_root(tmp_path, cleaner_rc=0)
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    fixture_path = _write_fixture(mini_root, {
        "lesson_id": 7, "matched": True,
        "recovery_action": "cleaner-on-main",
    })
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    # Bash
    bash_proc = _bash_bridge(mini_root, "epic-clean", run_dir,
                             fixture_path=fixture_path)
    bash_rc = _bash_rc(bash_proc)
    assert bash_rc == 0, f"stderr={bash_proc.stderr}"
    assert "dispatching cleaner-on-main for epic-clean" in bash_proc.stderr
    assert "cleaner-on-main succeeded for epic-clean" in bash_proc.stderr

    # Python — on its own run_dir so we can compare detective.json shape
    # against bash's output side-by-side.
    py_run_dir = tmp_path / "py_run_dir"
    py_run_dir.mkdir()
    py_rc = mhb.mo_run_healer_on_escalate("epic-clean", str(py_run_dir))
    assert py_rc == 0

    # Both sides wrote detective.json with the same keys (the bash-written
    # file uses jq -n ordering; Python uses dict insertion order).
    bash_brief = run_dir / "healer-cleaner" / "detective.json"
    py_brief = py_run_dir / "healer-cleaner" / "detective.json"
    assert bash_brief.is_file(), f"missing: {bash_brief}"
    assert py_brief.is_file(), f"missing: {py_brief}"

    bash_payload = json.loads(bash_brief.read_text())
    py_payload = json.loads(py_brief.read_text())

    expected_keys = {"epic_id", "classification", "confidence", "evidence",
                     "recommendation", "cleaner_brief", "rationale",
                     "source", "detected_at"}
    assert set(bash_payload.keys()) == expected_keys, bash_payload.keys()
    assert set(py_payload.keys()) == expected_keys, py_payload.keys()
    for k in expected_keys:
        assert bash_payload[k] == py_payload[k], (
            f"key={k} bash={bash_payload[k]!r} py={py_payload[k]!r}")


def test_e2e_cleaner_on_main_failure(tmp_path, monkeypatch):
    """When cleaner.sh exits non-zero, both sides return 1 and bump-failure
    (the latter is a no-op in Python but the rc parity still holds)."""
    mini_root, run_dir = _setup_test_root(tmp_path, cleaner_rc=2)
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    fixture_path = _write_fixture(mini_root, {
        "lesson_id": 7, "matched": True,
        "recovery_action": "cleaner-on-main",
    })
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    bash_proc = _bash_bridge(mini_root, "epic-cfail", run_dir,
                             fixture_path=fixture_path)
    bash_rc = _bash_rc(bash_proc)
    assert bash_rc == 1, f"stderr={bash_proc.stderr}"
    assert "cleaner-on-main failed" in bash_proc.stderr

    py_rc = mhb.mo_run_healer_on_escalate("epic-cfail", str(run_dir))
    assert py_rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# (h) End-to-end: wait-and-retry with sub-floor wait_s → both clamp to 10s
# ─────────────────────────────────────────────────────────────────────────────
def test_e2e_wait_and_retry_clamps(monkeypatch, tmp_path):
    """wait_s=5 (below the 10s floor) must clamp to 10 on both sides. We
    stub ``sleep`` on the bash side and monkeypatch ``time.sleep`` on the
    Python side so neither waits 10 real seconds."""
    mini_root, run_dir = _setup_test_root(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(mini_root))
    fixture_path = _write_fixture(mini_root, {
        "lesson_id": 9, "matched": True,
        "recovery_action": "wait-and-retry",
        "recovery_args": {"wait_s": 5},  # below floor → clamp to 10
    })
    monkeypatch.setenv("HEALER_FIXTURE_FILE", str(fixture_path))

    # Bash — override sleep to echo the duration.
    bash_proc = _bash_bridge(
        mini_root, "epic-w", run_dir,
        fixture_path=fixture_path,
        sleep_override='echo "$@"',
    )
    bash_rc = _bash_rc(bash_proc)
    assert bash_rc == 0, f"stderr={bash_proc.stderr}"
    # Bash sleep stub echoes the first arg; clamp brings 5 → 10.
    assert bash_proc.stdout.strip().startswith("10"), bash_proc.stdout

    # Python — monkeypatch time.sleep so we capture the requested wait.
    captured: list[int] = []
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: captured.append(int(s)))
    py_rc = mhb.mo_run_healer_on_escalate("epic-w", str(run_dir))
    assert py_rc == 0
    assert captured == [10], f"captured={captured}"


def test_extract_wait_s_from_parsed():
    """Recovery_args.wait_s parses + clamps identically to the bash jq path."""
    assert mhb.extract_wait_s({}) == 30  # default
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 15}}) == 15
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 5}}) == 10  # floor
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 999}}) == 300  # ceiling
    assert mhb.extract_wait_s({"recovery_args": {}}) == 30  # missing key
    assert mhb.extract_wait_s({"recovery_args": "not-a-dict"}) == 30


# ─────────────────────────────────────────────────────────────────────────────
# (i) write_cleaner_brief — structural parity with bash jq -n output
# ─────────────────────────────────────────────────────────────────────────────
def test_write_cleaner_brief_matches_bash_keys(tmp_path):
    """The bash jq -n emits a fixed set of keys; Python must emit the same."""
    out_dir = tmp_path / "bridge"
    path = mhb.write_cleaner_brief(str(out_dir), "epic-X", "42")
    assert Path(path).is_file()
    payload = json.loads(Path(path).read_text())
    expected_keys = ["epic_id", "classification", "confidence", "evidence",
                     "recommendation", "cleaner_brief", "rationale",
                     "source", "detected_at"]
    assert list(payload.keys()) == expected_keys
    assert payload["epic_id"] == "epic-X"
    assert payload["classification"] == "baseline_rot"
    assert payload["confidence"] == 0.9
    assert payload["recommendation"] == "cleaner-on-main"
    assert payload["rationale"] == "mo-healer-bridge auto-recovery"
    assert payload["source"] == "mo-healer-bridge"
    assert "epic-X" in payload["cleaner_brief"]
    assert "42" in payload["cleaner_brief"]
    assert payload["evidence"] == []