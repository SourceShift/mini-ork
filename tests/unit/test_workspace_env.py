"""Tests for the shared scope=agent env-boundary policy (``_workspace_env``).

This is the ONE allowlist both the ``docker`` and ``microvm`` backends import,
so the policy lives here (relocated from ``test_docker_spawn.py`` when Increment 3
extracted the helper — decision-doc follow-up #1). The boundary is an allowlist
by design (host ``os.environ`` is untrusted at the sandbox edge): see
docs/decisions/20260804-docker-spawn-env-injection.md.
"""
from __future__ import annotations

from mini_ork.runtime.backends import docker as docker_backend
from mini_ork.runtime.backends import microvm as microvm_backend
from mini_ork.runtime.backends._workspace_env import _container_env, container_env


def test_forwards_harness_and_provider_keys():
    env = {
        "MO_SANDBOX_SCOPE": "agent",
        "MO_ROUTING_POLICY": "bandit",
        "OPENAI_API_KEY": "sk-oai",
        "ANTHROPIC_API_KEY": "sk-ant",
        "DEEPSEEK_API_KEY": "ds",
        "OPENROUTER_API_KEY": "or",
    }
    assert _container_env(env) == env  # every key is agent-relevant → all pass


def test_api_key_suffix_catches_unknown_provider():
    # A provider we never enumerated still crosses via the *_API_KEY suffix, so a
    # newly-added key is not silently dropped.
    assert _container_env({"FOO_API_KEY": "v"}) == {"FOO_API_KEY": "v"}


def test_drops_host_shell_identity():
    # PATH/HOME/USER/SHELL/TMPDIR are structurally wrong inside a Linux image; the
    # sandbox must keep its OWN (both backends MERGE, so these never cross).
    env = {
        "PATH": "/Users/admin/.pyenv/bin:/usr/bin",
        "HOME": "/Users/admin",
        "USER": "admin",
        "SHELL": "/bin/zsh",
        "TMPDIR": "/var/folders/xx",
        "PWD": "/somewhere",
        "TERM": "xterm",
    }
    assert _container_env(env) == {}


def test_drops_unrelated_host_secrets():
    # The confused-deputy case: unrelated creds in os.environ must not leak in.
    env = {
        "AWS_ACCESS_KEY_ID": "AKIA-LEAK",
        "AWS_SECRET_ACCESS_KEY": "SECRET-LEAK",
        "SSH_AUTH_SOCK": "/tmp/ssh.sock",
        "GITHUB_TOKEN": "ghp_leak",
        "NPM_TOKEN": "npm_leak",
    }
    assert _container_env(env) == {}


def test_api_key_suffix_does_not_match_aws():
    # The load-bearing security invariant: the generous *_API_KEY catch-all is
    # safe ONLY because AWS names its creds *_ACCESS_KEY_ID / *_SECRET_ACCESS_KEY,
    # neither of which ends in _API_KEY. If this ever changes, the allowlist
    # re-opens the exact leak it exists to prevent.
    out = _container_env(
        {"AWS_SECRET_ACCESS_KEY": "x", "AWS_ACCESS_KEY_ID": "y", "REAL_API_KEY": "z"}
    )
    assert out == {"REAL_API_KEY": "z"}


def test_case_insensitive_on_key_name():
    assert _container_env({"openai_api_key": "v"}) == {"openai_api_key": "v"}
    assert _container_env({"mo_foo": "v"}) == {"mo_foo": "v"}


def test_public_and_underscore_aliases_are_the_same_object():
    # ``container_env`` is the public name; ``_container_env`` the back-compat alias
    # the backends import. They must be the same function, not a copy.
    assert _container_env is container_env


def test_both_backends_share_one_policy_object():
    # The anti-drift guarantee: docker and microvm import the SAME symbol, so a
    # future edit to the allowlist can never apply to one backend and not the
    # other. This is the whole reason the helper was extracted (Builder lens).
    assert docker_backend._container_env is container_env
    assert microvm_backend._container_env is container_env
