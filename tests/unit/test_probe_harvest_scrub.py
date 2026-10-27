"""The visible probe text never names the hidden oracle tests.

Review follow-up (pre-push review 121): _scrub hid shas, the task id and the test
file paths, but a problem statement that named a fail_to_pass / pass_to_pass test
(or its file by basename) still leaked the hidden oracle into the probe.
"""
from mini_ork.learning import probe_harvest


def _cand():
    return {
        "task_id": "mo-1111111111", "fix_sha": "1" * 40, "base_sha": "c" * 40,
        "test_files": ["tests/unit/test_router_lanes.py"],
        "fail_to_pass": ["tests.unit.test_router_lanes::test_pick_prefers_cheap_lane"],
        "pass_to_pass": ["tests.unit.test_router_lanes::test_pick_keeps_pinned"],
    }


def test_scrub_hides_test_ids_names_modules_and_files():
    text = ("Fix routing. See test_pick_prefers_cheap_lane and "
            "tests.unit.test_router_lanes::test_pick_keeps_pinned in "
            "test_router_lanes.py (module tests.unit.test_router_lanes).")
    out = probe_harvest._scrub(text, _cand())
    for leaked in ("test_pick_prefers_cheap_lane", "test_pick_keeps_pinned",
                   "test_router_lanes", "tests.unit.test_router_lanes"):
        assert leaked not in out, (leaked, out)
    assert "Fix routing." in out


def test_scrub_leaves_unrelated_text():
    assert probe_harvest._scrub("Make add() return a + b.", _cand()) == "Make add() return a + b."
