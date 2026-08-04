"""Shared env-boundary policy for scope=agent CLI spawns into an isolation
backend (SE-3). ONE allowlist, imported by BOTH the ``docker`` and ``microvm``
backends so the policy can never drift between them (the maintainability risk the
Builder lens flagged in the Increment-2 consensus — extraction was follow-up #1).

The host process env handed to a backend's ``spawn`` is a grab-bag: the whole
``os.environ`` (host ``PATH``/``HOME`` that don't exist in a Linux image;
unrelated ``SSH``/``AWS``/``npm``/``GitHub`` creds) plus the per-dispatch
overrides. Forwarding it verbatim would both **clobber** the sandbox's own shell
identity AND **leak** every host secret across the boundary. So env crosses
ALLOWLIST-first: only the harness (``MO_*``) + LLM-provider namespaces + a
generous ``*_API_KEY`` suffix are forwarded.

Why allowlist and not denylist: a *dropped* key fails LOUD (the CLI can't auth →
caught by the live docker/microVM smoke), whereas a *leaked* secret would be
SILENT — so we bias to the allowlist on this security axis. The ``*_API_KEY``
suffix deliberately does NOT match AWS ``*_ACCESS_KEY_ID`` /
``*_SECRET_ACCESS_KEY`` (neither ends in ``_API_KEY``), so the catch-all cannot
re-open that leak — the load-bearing invariant asserted by
``test_container_env_api_key_suffix_does_not_match_aws``.

Both backends inject the *surviving* keys ADDITIVELY (docker ``-e KEY=VALUE``;
microVM ``exec(env=)`` — both MERGE onto the sandbox's own env rather than
replacing it), which is why dropping ``PATH``/``HOME`` here is safe: the sandbox
keeps its own.

Decision: docs/decisions/20260804-docker-spawn-env-injection.md
"""
from __future__ import annotations

from typing import Mapping

__all__ = ["container_env", "_container_env", "_AGENT_ENV_PREFIXES", "_AGENT_ENV_SUFFIXES"]

_AGENT_ENV_PREFIXES = (
    "MO_",
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "GEMINI_",
    "GOOGLE_GENAI_",
    "GLM_",
    "ZHIPU_",
    "ZHIPUAI_",
    "KIMI_",
    "MOONSHOT_",
    "MINIMAX_",
    "DEEPSEEK_",
    "OPENROUTER_",
    "GROQ_",
    "XAI_",
    "MISTRAL_",
    "TOGETHER_",
    "FIREWORKS_",
    "CEREBRAS_",
    "PERPLEXITY_",
)
_AGENT_ENV_SUFFIXES = ("_API_KEY",)


def container_env(env: Mapping[str, str]) -> dict[str, str]:
    """Filter a host env down to the keys a scope=agent CLI may carry across an
    isolation boundary (allowlist — see the module note). Case-insensitive on the
    key name; values pass through unchanged. Pure + daemon-free so the boundary
    policy is unit-tested once (``tests/unit/test_workspace_env.py``) and shared
    verbatim by every backend."""
    out: dict[str, str] = {}
    for key, val in env.items():
        upper = key.upper()
        if upper.startswith(_AGENT_ENV_PREFIXES) or upper.endswith(_AGENT_ENV_SUFFIXES):
            out[key] = val
    return out


# Backends + the Increment-2 test import the underscore name; keep it as the
# canonical alias so the extraction did not churn call sites.
_container_env = container_env
