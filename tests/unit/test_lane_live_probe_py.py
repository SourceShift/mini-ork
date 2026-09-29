"""lane_health must catch a lane whose key is SET but dead.

deepseek answered HTTP 402 "Insufficient Balance" from ~2026-09-20 while
lane_health reported it healthy (it only checked that the key env was set),
so framework-edit's implementer lane failed every run unnoticed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import providers as p  # noqa: E402


@pytest.fixture
def lane(tmp_path, monkeypatch):
    reg = tmp_path / "providers.yaml"
    reg.write_text(
        "providers:\n"
        "  fakeds:\n"
        "    kind: anthropic-compat\n"
        "    base_url: https://gateway.invalid/anthropic\n"
        "    api_key_env: FAKE_DS_KEY\n"
        "    model: fake-model\n")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    home = tmp_path / "home"
    home.mkdir()
    calls = []

    def env(**extra):
        return {"FAKE_DS_KEY": "sk-test", "MINI_ORK_HOME": str(home), "MO_LANE_PROBE": "1", **extra}

    def answer(status):
        def fake(url, key, model):
            calls.append((url, key, model))
            return status
        monkeypatch.setattr(p, "_http_post_status", fake)

    return env, answer, calls


def test_out_of_credit_lane_is_unhealthy_with_the_real_cause(lane):
    env, answer, calls = lane
    answer(402)

    health = p.lane_health("fakeds", environment=env())

    assert not health.ok
    assert "out of credit (HTTP 402)" in health.reason
    assert calls == [("https://gateway.invalid/anthropic/v1/messages", "sk-test", "fake-model")]


@pytest.mark.parametrize("status", [200, 400, 429, 500])
def test_any_other_http_answer_keeps_the_lane(lane, status):
    """400 = request-shape complaint (key accepted); 429/5xx are transient."""
    env, answer, _ = lane
    answer(status)

    assert p.lane_health("fakeds", environment=env()).ok


def test_no_http_answer_is_unknown_not_dead_and_is_not_cached(lane):
    env, answer, calls = lane
    answer(None)

    assert p.lane_health("fakeds", environment=env()).ok
    assert p.lane_health("fakeds", environment=env()).ok
    assert len(calls) == 2  # an unknown result must be re-probed


def test_verdict_is_cached_per_lane_and_key(lane):
    env, answer, calls = lane
    answer(402)

    p.lane_health("fakeds", environment=env())
    p.lane_health("fakeds", environment=env())
    assert len(calls) == 1  # second check served from the cache

    answer(200)  # operator tops up and rotates the key
    assert p.lane_health("fakeds", environment=env(FAKE_DS_KEY="sk-new")).ok
    assert len(calls) == 2


def test_probe_can_be_disabled(lane):
    env, answer, calls = lane
    answer(402)

    assert p.lane_health("fakeds", environment=env(MO_LANE_PROBE="0")).ok
    assert calls == []


def test_unit_tests_never_probe_unless_opted_in(lane):
    env, answer, calls = lane
    answer(402)
    quiet = env()
    del quiet["MO_LANE_PROBE"]

    assert p.lane_health("fakeds", environment=quiet).ok
    assert calls == []
