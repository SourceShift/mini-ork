"""Tests for the native-lane live probe (:func:`mini_ork.dispatch.providers._live_probe_native`
and the ``anthropic-native`` branch in :func:`lane_health`).

The headline bug this probe closes: a `deepseek` 402 / `opus` "out of credit"
used to be invisible because ``lane_health`` on an ``anthropic-native`` lane
returned ``LaneHealth(True, "ok (unprobed: native lane — auth owned by the CLI)")``
on the basis that there is no ``api_key_env`` to read. The gate cannot ship
until a 402-shaped failure surfaces here, and 200 / 400 / 429 / 5xx / quota
wording behaves distinctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: F401 -- the fixtures below use monkeypatch/parametrize

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import providers as p  # noqa: E402


@pytest.fixture
def lane(tmp_path, monkeypatch):
    reg = tmp_path / "providers.yaml"
    # Two lanes share the same credential so the cache fingerprint
    # ``sha256(f"{url}|{cred}")[:16]`` is identical — ``opus`` and
    # ``sonnet`` on the same key probe once per _PROBE_TTL_S.
    reg.write_text(
        "providers:\n"
        "  fakeopus:\n"
        "    kind: anthropic-native\n"
        "    base_url: https://gateway.invalid/anthropic\n"
        "    model: claude-haiku-4-5\n"
        "  fakesonnet:\n"
        "    kind: anthropic-native\n"
        "    base_url: https://gateway.invalid/anthropic\n"
        "    model: claude-haiku-4-5\n"
        "  fakebare:\n"
        "    kind: anthropic-native\n"
        "    model: claude-haiku-4-5\n")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(reg))
    home = tmp_path / "home"
    home.mkdir()
    calls = []

    def env(**extra):
        return {
            # Set a real credential env var so _live_probe_native enters the
            # HTTP branch (it returns None when NO native credential is set,
            # which is the ambient-login unknown contract — separately tested
            # below). Tests that exercise "no credential" pass through
            # the explicit pop below.
            "ANTHROPIC_API_KEY": "sk-test",
            "MINI_ORK_HOME": str(home),
            "MO_LANE_PROBE": "1",
            **extra,
        }

    def answer(status, body=""):
        def fake(url, headers, model):
            calls.append((url, dict(headers), model))
            return status, body
        # The native probe shares _http_post_result with the compat probe,
        # but tests for the compat probe still monkeypatch _http_post_probe
        # (which keeps a (url, key, model) signature). The native test
        # monkeypatches the new primitive directly.
        monkeypatch.setattr(p, "_http_post_result", fake)

    return env, answer, calls


def test_out_of_credit_lane_is_unhealthy(lane):
    env, answer, calls = lane
    answer(402)

    h = p.lane_health("fakeopus", environment=env())

    assert not h.ok
    assert "out of credit (HTTP 402)" in h.reason
    assert calls and calls[0][0] == "https://gateway.invalid/anthropic/v1/messages"


@pytest.mark.parametrize("status", [200, 400, 404, 429, 500, 503])
def test_any_other_http_answer_keeps_the_native_lane(lane, status):
    """200 = success; 400/404 = wrong-model or request-shape complaint (key
    accepted); bare 429 and 5xx are transient — none of these block."""
    env, answer, _ = lane
    answer(status)

    assert p.lane_health("fakeopus", environment=env()).ok, status


def test_400_with_credit_body_is_a_funding_wall(lane):
    """``deepseek`` and the native providers both send a 400/429 whose body
    carries credit wording. The status alone is ambiguous — the body is the
    classification. The kickoff widens the regex on top of the compat one."""
    env, answer, calls = lane
    answer(400, '{"error":{"message":"Your credit balance is too low."}}')

    h = p.lane_health("fakeopus", environment=env())

    assert not h.ok
    assert "credit balance is too low" in h.reason.lower() or "out of quota" in h.reason
    # ...and it is cached long enough that a re-probe does not burn a call
    # relearning the same wall.
    assert not p.lane_health("fakeopus", environment=env()).ok
    assert len(calls) == 1


def test_no_credential_env_var_is_healthy_and_makes_no_http_call(lane):
    """Per the kickoff: an ambient (credentials-file-only) login is unknown
    and does NOT block. Toggling the three known credential env vars off
    must yield ok=True without touching the network."""
    env, answer, calls = lane
    bare = env()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        bare.pop(key, None)

    assert p.lane_health("fakeopus", environment=bare).ok
    assert calls == []


def test_oauth_token_carries_beta_header_and_not_x_api_key(lane):
    env, answer, calls = lane
    answer(200)
    oauth_env = env(CLAUDE_CODE_OAUTH_TOKEN="sk-oauth-aaa")
    oauth_env.pop("ANTHROPIC_API_KEY", None)
    oauth_env.pop("ANTHROPIC_AUTH_TOKEN", None)

    assert p.lane_health("fakeopus", environment=oauth_env).ok

    headers = calls[0][1]
    assert headers.get("authorization") == "Bearer sk-oauth-aaa"
    assert headers.get("anthropic-beta") == "oauth-2025-04-20"
    assert "x-api-key" not in headers


def test_api_key_uses_x_api_key_and_not_authorization(lane):
    env, answer, calls = lane
    answer(200)
    api_env = env(ANTHROPIC_API_KEY="sk-api-bbb")
    api_env.pop("ANTHROPIC_AUTH_TOKEN", None)
    api_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    assert p.lane_health("fakeopus", environment=api_env).ok

    headers = calls[0][1]
    assert headers.get("x-api-key") == "sk-api-bbb"
    assert "authorization" not in headers
    assert "anthropic-beta" not in headers


def test_auth_token_uses_bearer_not_x_api_key(lane):
    env, answer, calls = lane
    answer(200)
    auth_env = env(ANTHROPIC_AUTH_TOKEN="sk-auth-ccc")
    auth_env.pop("ANTHROPIC_API_KEY", None)
    auth_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    assert p.lane_health("fakeopus", environment=auth_env).ok

    headers = calls[0][1]
    assert headers.get("authorization") == "Bearer sk-auth-ccc"
    assert "x-api-key" not in headers
    assert "anthropic-beta" not in headers


def test_cache_hit_avoids_a_second_http_call_on_same_lane(lane):
    """The cache rides on ``sha256(f"{url}|{cred}")[:16]`` and is keyed per
    model name (each lane gets its own entry). Re-checking the SAME lane
    MUST be served from the cache; rotating the credential invalidates the
    cache via the fingerprint check, not by replaying the network."""
    env, answer, calls = lane
    answer(402)

    h1 = p.lane_health("fakeopus", environment=env())
    h2 = p.lane_health("fakeopus", environment=env())
    h3 = p.lane_health("fakeopus", environment=env())
    assert not h1.ok and not h2.ok and not h3.ok
    assert len(calls) == 1  # second and third check served from cache


def test_mo_lane_probe_zero_disables_native_probe(lane):
    env, answer, calls = lane
    answer(402)

    assert p.lane_health("fakeopus", environment=env(MO_LANE_PROBE="0")).ok
    assert calls == []


def test_native_probe_skipped_under_pytest_without_opt_in(lane):
    """Mirrors the compat-probe contract — pytest runs are dark by default."""
    env, answer, calls = lane
    answer(402)
    quiet = env()
    quiet.pop("MO_LANE_PROBE", None)

    assert p.lane_health("fakeopus", environment=quiet).ok
    assert calls == []


# ── the probe target must be the endpoint dispatch uses ──────────────────────


def test_ambient_anthropic_base_url_is_the_probe_target(lane):
    """A native lane with no ``base_url`` in the registry inherits the ambient
    ``ANTHROPIC_BASE_URL``. That pairing is the normal shape of a Claude Code
    session (or launcher) routed through a gateway: the gateway's token sits in
    ``ANTHROPIC_AUTH_TOKEN`` and api.anthropic.com has never seen it. Probing
    the default host there answers 401 — a false abort for EVERY run in that
    environment, which is why the probe must follow the CLI's own base_url."""
    env, answer, calls = lane
    answer(200)
    e = env(ANTHROPIC_BASE_URL="https://envgw.invalid/anthropic")
    e.pop("ANTHROPIC_API_KEY", None)
    e["ANTHROPIC_AUTH_TOKEN"] = "sk-gateway-token"

    assert p.lane_health("fakebare", environment=e).ok
    assert calls[0][0] == "https://envgw.invalid/anthropic/v1/messages"
    assert calls[0][1].get("authorization") == "Bearer sk-gateway-token"


def test_registry_base_url_outranks_the_ambient_one(lane):
    """Precedence mirrors the CLI: the provider entry's own ``base_url`` wins
    over ``ANTHROPIC_BASE_URL``. Otherwise an operator who pinned a lane to a
    specific gateway could have it silently re-pointed by their shell."""
    env, answer, calls = lane
    answer(200)

    h = p.lane_health(
        "fakeopus",
        environment=env(ANTHROPIC_BASE_URL="https://envgw.invalid/anthropic"))

    assert h.ok
    assert calls[0][0] == "https://gateway.invalid/anthropic/v1/messages"


def test_native_probe_defaults_to_anthropic_with_no_url_anywhere(lane):
    """Neither the entry nor the environment names an endpoint → the default
    host. This is the BYO-key case: a bare ``ANTHROPIC_API_KEY`` against the
    real API, no gateway involved."""
    env, answer, calls = lane
    answer(200)

    assert p.lane_health("fakebare", environment=env()).ok
    assert calls[0][0] == "https://api.anthropic.com/v1/messages"
