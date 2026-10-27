"""Unit tests for the deterministic ``kind: mock`` provider (B6).

Hermetic: a tmp ``MINI_ORK_RUN_DIR`` and ``MINI_ORK_PROVIDERS`` registry, a
nonexistent ``MINI_ORK_HOME`` so the repo's real providers.yaml cannot win, and
no network. The mock dispatch is pure Python — no subprocess, no sockets.
"""

from __future__ import annotations

import json
import socket
import subprocess

import pytest
import yaml

from mini_ork.dispatch import llm_dispatch, mock_provider
from mini_ork.dispatch.models import DispatchRequest
from mini_ork.dispatch.providers import (
    ENGINES,
    ProviderSpec,
    dispatch_model,
    dispatch_with_fallback,
    lane_health,
)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "no-such-home"))
    for var in (
        "MINI_ORK_PROVIDERS",
        "MINI_ORK_ROOT",
        "MO_MOCK_SEED",
        "MO_MOCK_RESET",
        "MO_PLACEMENT",
        "MO_SANDBOX_SCOPE",
        "MO_SANDBOX_BACKEND",
        "MO_TARGET_CWD",
        "MO_ALLOW_FRAMEWORK_CWD",
        "MO_MCP_SERVERS",
        "MO_SKILLS",
        "MO_AGENT_DOC",
        "MO_NODE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return run_dir


def _rules(tmp_path, document, name="rules.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _spec(lane, rules_path):
    return ProviderSpec(
        model=lane,
        command=(),
        env={"MO_MOCK_RULES": str(rules_path)},
        unset_env=frozenset(),
        kind="mock",
    )


def _request(lane, prompt, *, timeout_s=30.0, cwd=None):
    return DispatchRequest(
        model=lane, prompt=prompt, timeout_s=timeout_s, env={}, cwd=cwd
    )


def _registry(tmp_path, lanes, monkeypatch, name="providers-mock.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump({"providers": lanes}), encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(path))
    return path


# 1. Presets land in the classifier categories the kickoff declares.
@pytest.mark.parametrize(
    ("preset", "category"),
    [
        ("rate_limit", "capacity"),
        ("auth", "auth"),
        ("quota", "quota"),
        ("server_error", "provider"),
        ("overloaded", "capacity"),
        ("bad_request", "request"),
        ("stream_cut", "stream"),
    ],
)
def test_preset_text_classifies_as_declared(preset, category):
    rc, text = mock_provider.PRESETS[preset]
    assert llm_dispatch.classify_error(text, rc) == category


def test_timeout_preset_rc_and_classifier():
    rc, text = mock_provider.PRESETS["timeout"]
    assert rc == 124
    # The classifier has no timeout bucket today: "timeout after 30s (mock)"
    # falls through to "unknown".
    assert llm_dispatch.classify_error(text.format(timeout=30), rc) == "unknown"


# 2. Matching prompt + default.
def test_match_prompt_then_default(tmp_path):
    rules = _rules(
        tmp_path,
        {
            "rules": [{"match": {"prompt": "hello"}, "respond": {"text": "matched"}}],
            "default": {"respond": {"text": "default"}},
        },
    )
    spec = _spec("mockA", rules)
    assert mock_provider.dispatch(_request("mockA", "say hello"), spec).text == "matched"
    assert mock_provider.dispatch(_request("mockA", "nope"), spec).text == "default"


# 3. Attempt counter drives fail-then-succeed through dispatch_model.
def test_attempt_one_fails_attempt_two_succeeds(_hermetic, tmp_path, monkeypatch):
    rules = _rules(
        tmp_path,
        {
            "rules": [
                {"match": {"attempt": 1}, "error": {"preset": "rate_limit"}},
                {"respond": {"text": "second-try"}},
            ]
        },
    )
    _registry(tmp_path, {"mockA": {"kind": "mock", "rules": str(rules)}}, monkeypatch)

    first = dispatch_model(_request("mockA", "ping", cwd=str(tmp_path)))
    second = dispatch_model(_request("mockA", "ping", cwd=str(tmp_path)))

    assert first.ok is False and "HTTP 429" in first.error
    assert second.ok is True and second.text == "second-try"
    counter = json.loads((_hermetic / ".mock-attempts.json").read_text(encoding="utf-8"))
    assert counter["mockA"] == 2


# 4. Fallback walk from a dead lane to an answering lane.
def test_fallback_walk_uses_second_lane(tmp_path, monkeypatch):
    rules_a = _rules(tmp_path, {"rules": [{"error": {"preset": "auth"}}]}, "a.yaml")
    rules_b = _rules(tmp_path, {"rules": [{"respond": {"text": "from-b"}}]}, "b.yaml")
    _registry(
        tmp_path,
        {
            "mockA": {"kind": "mock", "rules": str(rules_a)},
            "mockB": {"kind": "mock", "rules": str(rules_b)},
        },
        monkeypatch,
    )

    result = dispatch_with_fallback(
        _request("mockA", "job", cwd=str(tmp_path)), ["mockA", "mockB"]
    )

    assert result.ok is True
    assert result.text == "from-b"
    assert result.model == "mockB"


# 5. A latency past the caller's deadline returns the timeout preset quickly.
def test_latency_over_timeout_returns_timeout_preset(tmp_path):
    rules = _rules(
        tmp_path, {"rules": [{"respond": {"text": "slow"}, "latency_ms": 5000}]}
    )
    spec = _spec("mockA", rules)
    result = mock_provider.dispatch(
        _request("mockA", "slow", timeout_s=0.2), spec
    )
    assert result.ok is False
    assert result.rc == 124
    assert "timeout after" in result.error


# 6. Probability is deterministic per seed and seed-sensitive.
def test_probability_is_deterministic_per_seed(tmp_path, monkeypatch):
    rules = _rules(
        tmp_path,
        {
            "rules": [{"respond": {"text": "rule"}, "probability": 0.5}],
            "default": {"respond": {"text": "default"}},
        },
    )
    spec = _spec("mockA", rules)

    def sequence(run_name, seed):
        run_dir = tmp_path / run_name
        run_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
        monkeypatch.setenv("MO_MOCK_SEED", seed)
        return [mock_provider.dispatch(_request("mockA", "x"), spec).text for _ in range(5)]

    seed_zero = sequence("run-a", "0")
    assert seed_zero == sequence("run-b", "0")
    assert sequence("run-c", "1") != seed_zero


# 7. A bad rules file fails lane preflight and names the file.
def test_bad_rules_file_fails_preflight_naming_the_file(tmp_path, monkeypatch):
    bad = tmp_path / "broken-rules.yaml"
    bad.write_text(
        yaml.safe_dump({"rules": [{"error": {"preset": "nope"}}]}), encoding="utf-8"
    )
    _registry(tmp_path, {"mockA": {"kind": "mock", "rules": str(bad)}}, monkeypatch)

    health = lane_health("mockA")

    assert health.ok is False
    assert "broken-rules.yaml" in health.reason


# 8. The audit trail gets one line per dispatch.
def test_audit_trail_has_one_line_per_dispatch(_hermetic, tmp_path):
    rules = _rules(tmp_path, {"rules": [{"respond": {"text": "ok"}}]})
    spec = _spec("mockA", rules)
    mock_provider.dispatch(_request("mockA", "a"), spec)
    mock_provider.dispatch(_request("mockA", "b"), spec)

    lines = [
        json.loads(line)
        for line in (_hermetic / ".mock-calls.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    assert [line["attempt"] for line in lines] == [1, 2]
    for line in lines:
        assert {"lane", "attempt", "rule", "outcome", "at"} <= set(line)


# 9. The mock engine accepts the full capability envelope (no rc 66).
def test_mock_engine_accepts_capability_envelope(tmp_path, monkeypatch):
    rules = _rules(tmp_path, {"rules": [{"respond": {"text": "envelope-ok"}}]})
    _registry(tmp_path, {"mockA": {"kind": "mock", "rules": str(rules)}}, monkeypatch)
    monkeypatch.setenv("MO_MCP_SERVERS", "fs")
    monkeypatch.setenv("MO_SKILLS", "code-review")
    monkeypatch.setenv("MO_AGENT_DOC", "docs/agents/implementer.md")

    result = dispatch_model(_request("mockA", "job", cwd=str(tmp_path)))

    assert result.rc != 66
    assert result.ok is True
    assert result.text == "envelope-ok"


# A mock lane is pure Python: it never spawns a process or opens a socket.
def test_mock_dispatch_is_pure_python(tmp_path, monkeypatch):
    rules = _rules(tmp_path, {"rules": [{"respond": {"text": "pure"}}]})
    _registry(tmp_path, {"mockA": {"kind": "mock", "rules": str(rules)}}, monkeypatch)

    def _forbidden(*args, **kwargs):
        raise AssertionError(f"mock dispatch must stay in-process: args={args!r}")

    for name in ("Popen", "run", "call", "check_output", "check_call"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "socket", _forbidden)

    result = dispatch_model(_request("mockA", "ping", cwd=str(tmp_path)))

    assert result.ok is True
    assert result.text == "pure"
    # The engine is registered by kind, not by lane name.
    assert ENGINES["mock"].capabilities().mcp_servers is True


def test_unknown_rule_key_is_rejected(tmp_path):
    path = _rules(tmp_path, {"rules": [{"respond": {"text": "x"}, "latenc_ms": 5}]})
    with pytest.raises(ValueError, match="latenc_ms"):
        mock_provider.load_rules(path)


def test_rules_file_broken_after_preflight_is_a_failed_dispatch(tmp_path):
    path = _rules(tmp_path, {"rules": [{"respond": {"text": "x"}}]})
    spec = _spec("mockA", path)
    path.write_text("rules: [ {bad", encoding="utf-8")
    result = mock_provider.dispatch(_request("mockA", "hi"), spec)
    assert result.ok is False and result.rc == 2
    assert "rules file unusable" in result.error


def test_lane_env_seed_wins_over_process_env(tmp_path, monkeypatch):
    doc = {"rules": [{"respond": {"text": "hit"}, "probability": 0.5}],
           "default": {"respond": {"text": "miss"}}}
    path = _rules(tmp_path, doc)
    monkeypatch.setenv("MO_MOCK_SEED", "process-seed")

    def outcomes(spec):
        return [mock_provider.dispatch(_request(spec.model, "p"), spec).text for _ in range(6)]

    lane_seeded = ProviderSpec(model="mockS", command=(),
                               env={"MO_MOCK_RULES": str(path), "MO_MOCK_SEED": "lane-seed"},
                               unset_env=frozenset(), kind="mock")
    first = outcomes(lane_seeded)
    monkeypatch.setenv("MO_MOCK_RESET", "1")
    mock_provider._RESET_DONE = False
    monkeypatch.setenv("MO_MOCK_SEED", "another-process-seed")
    again = outcomes(lane_seeded)
    assert first == again  # the lane's seed decides, not the process env
