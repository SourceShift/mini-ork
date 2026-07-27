"""Unit tests: mini_ork.recovery.healer_bridge (bash parity halves removed; formerly vs lib/mo-healer-bridge.sh).

Each test drives the Python bridge with its module seams
(``mhb._healer`` / ``mhb._cleaner``) monkeypatched with fixtures, and
asserts return codes, stderr banners, and the structural shape of any
detective.json written for the cleaner-on-main branch.

Cases:
  (a) parse_healer_output + classify_recovery — six fixture shapes
      (empty, non-JSON, missing fields, full, integer lesson_id, null).
  (b) clamp_wait_s — boundaries (0/9/10, 30/300/301), non-numeric input
      (None / '' / 'abc' / '30.9').
  (c) decide() — every recovery_action branch maps to the same kind/rc.
  (d) action_kind — terminal/hint/auto_apply/unknown enumeration,
      including the empty-string case.
  (e) extract_lesson_id + extract_matched — jq-default semantics
      (``null`` → None, missing → defaults, integer-coerced strings).
  (f) End-to-end on terminal / hint / unknown / disabled branches —
      rc 1, stderr banner.
  (g) End-to-end on ``cleaner-on-main`` — cleaner succeeds; rc 0 and a
      ``<run_dir>/healer-cleaner/detective.json`` with the expected keys.
  (h) End-to-end on ``wait-and-retry`` with ``recovery_args.wait_s=5``
      (below the 10s floor) — stub ``time.sleep``; clamp to 10s, rc 0.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import healer_bridge as mhb


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
    """jq ``// ""`` default — empty dict → terminal branch."""
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
# (e) extract_lesson_id + extract_matched — jq default semantics
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
# (f) End-to-end on terminal / hint / unknown / disabled branches
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
def test_e2e_non_auto_apply_branches(tmp_path, monkeypatch, capsys, recovery,
                                     fixture):
    """Every non-auto-apply branch returns rc=1; the stderr banner matches
    the branch (hint → "healer suggests X"; unknown → "unknown recovery")."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))

    monkeypatch.setattr(
        mhb._healer, "decide",
        lambda *a, **k: (0, fixture if fixture.endswith("\n")
                         else fixture + "\n", ""),
    )
    py_rc = mhb.mo_run_healer_on_escalate("epic-1", str(run_dir))
    assert py_rc == 1, f"recovery={recovery} py_rc={py_rc}"

    err = capsys.readouterr().err
    if recovery in ("switch-agent", "shrink-scope"):
        assert f"healer suggests {recovery}" in err, err
    elif recovery == "unknown-action":
        assert "mo-healer unknown recovery:" in err, err


def test_e2e_disabled_branch(tmp_path, monkeypatch, capsys):
    """When MO_HEALER_BRIDGE_DISABLED=1, return 1 without invoking the
    healer seam (no 'classifying ESCALATE' stderr line)."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setenv("MO_HEALER_BRIDGE_DISABLED", "1")

    def _boom(*a, **k):
        raise AssertionError("healer seam must not be invoked when disabled")
    monkeypatch.setattr(mhb._healer, "decide", _boom)
    py_rc = mhb.mo_run_healer_on_escalate("epic-d", str(run_dir))
    assert py_rc == 1
    err = capsys.readouterr().err
    assert "classifying ESCALATE" not in err


def test_e2e_healer_missing(tmp_path, monkeypatch):
    """When the healer port is unavailable (raises), the bridge treats it
    as empty output → rc 1."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("healer port unavailable")
    monkeypatch.setattr(mhb._healer, "decide", _boom)
    py_rc = mhb.mo_run_healer_on_escalate("epic-m", str(run_dir))
    assert py_rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# (g) End-to-end: cleaner-on-main succeeds
# ─────────────────────────────────────────────────────────────────────────────
def test_e2e_cleaner_on_main_success(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    fixture = json.dumps({
        "lesson_id": 7, "matched": True,
        "recovery_action": "cleaner-on-main",
    })

    monkeypatch.setattr(
        mhb._healer, "decide",
        lambda *a, **k: (0, fixture + "\n", ""),
    )
    cleaner_calls = []
    monkeypatch.setattr(
        mhb._cleaner, "main",
        lambda argv: (cleaner_calls.append(list(argv)), 0)[1],
    )
    py_rc = mhb.mo_run_healer_on_escalate("epic-clean", str(run_dir))
    assert py_rc == 0
    err = capsys.readouterr().err
    assert "dispatching cleaner-on-main for epic-clean" in err
    assert "cleaner-on-main succeeded for epic-clean" in err
    # The native cleaner seam received (brief_path, bridge_dir).
    assert cleaner_calls == [
        [str(run_dir / "healer-cleaner" / "detective.json"),
         str(run_dir / "healer-cleaner")]
    ]

    # detective.json shape
    py_brief = run_dir / "healer-cleaner" / "detective.json"
    assert py_brief.is_file(), f"missing: {py_brief}"
    py_payload = json.loads(py_brief.read_text())

    expected_keys = {"epic_id", "classification", "confidence", "evidence",
                     "recommendation", "cleaner_brief", "rationale",
                     "source", "detected_at"}
    assert set(py_payload.keys()) == expected_keys, py_payload.keys()
    assert py_payload["epic_id"] == "epic-clean"
    assert py_payload["classification"] == "baseline_rot"
    assert py_payload["confidence"] == 0.9
    assert py_payload["recommendation"] == "cleaner-on-main"
    assert py_payload["source"] == "mo-healer-bridge"
    import re as _re
    assert _re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                     py_payload["detected_at"])


def test_e2e_cleaner_on_main_failure(tmp_path, monkeypatch, capsys):
    """When the cleaner returns non-zero, the bridge returns 1."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    fixture = json.dumps({
        "lesson_id": 7, "matched": True,
        "recovery_action": "cleaner-on-main",
    })

    monkeypatch.setattr(
        mhb._healer, "decide",
        lambda *a, **k: (0, fixture + "\n", ""),
    )
    monkeypatch.setattr(mhb._cleaner, "main", lambda argv: 2)
    py_rc = mhb.mo_run_healer_on_escalate("epic-cfail", str(run_dir))
    assert py_rc == 1
    err = capsys.readouterr().err
    assert "cleaner-on-main failed" in err


# ─────────────────────────────────────────────────────────────────────────────
# (h) End-to-end: wait-and-retry with sub-floor wait_s → clamp to 10s
# ─────────────────────────────────────────────────────────────────────────────
def test_e2e_wait_and_retry_clamps(monkeypatch, tmp_path):
    """wait_s=5 (below the 10s floor) must clamp to 10. We monkeypatch
    ``time.sleep`` so we don't actually wait."""
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    fixture = json.dumps({
        "lesson_id": 9, "matched": True,
        "recovery_action": "wait-and-retry",
        "recovery_args": {"wait_s": 5},  # below floor → clamp to 10
    })

    monkeypatch.setattr(
        mhb._healer, "decide",
        lambda *a, **k: (0, fixture + "\n", ""),
    )
    captured: list[int] = []
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: captured.append(int(s)))
    py_rc = mhb.mo_run_healer_on_escalate("epic-w", str(run_dir))
    assert py_rc == 0
    assert captured == [10], f"captured={captured}"


def test_extract_wait_s_from_parsed():
    """Recovery_args.wait_s parses + clamps through the jq-shaped path."""
    assert mhb.extract_wait_s({}) == 30  # default
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 15}}) == 15
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 5}}) == 10  # floor
    assert mhb.extract_wait_s({"recovery_args": {"wait_s": 999}}) == 300  # ceiling
    assert mhb.extract_wait_s({"recovery_args": {}}) == 30  # missing key
    assert mhb.extract_wait_s({"recovery_args": "not-a-dict"}) == 30


# ─────────────────────────────────────────────────────────────────────────────
# (i) write_cleaner_brief — structural contract
# ─────────────────────────────────────────────────────────────────────────────
def test_write_cleaner_brief_matches_expected_keys(tmp_path):
    """write_cleaner_brief emits the fixed key set in order."""
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
