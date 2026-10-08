"""scrubbed_test_env — safe environment for target-repo test suites.

The framework-edit, code-fix, and recursive-self-improve verifiers spawn
``pytest`` (or another test command) inside a target repository. Inheriting
the operator's full environment leaks two distinct categories of state into
that child:

1. **Provider credentials.** Variables ending in ``_API_KEY``,
   ``_AUTH_TOKEN``, ``_ACCESS_TOKEN``, ``_SECRET``, ``_SECRET_KEY``,
   plus any ``ANTHROPIC_*`` and the ``OPENAI_API_BASE`` /
   ``OPENAI_BASE_URL`` overrides. A target-repo test that accidentally
   invokes a real provider under the gate can charge the operator AND
   corrupt test output with a network error in place of the assertion the
   test meant to check.

2. **mini-ork state pointers.** ``MINI_ORK_SECRETS``, ``MINI_ORK_DB``,
   ``MINI_ORK_HOME``, ``MINI_ORK_PROJECT_HOME``, ``MINI_ORK_RUN_ID``,
   ``MINI_ORK_RUN_DIR``, ``MINI_ORK_PLAN_PATH``, ``MINI_ORK_AGENTS``. A
   target-repo fixture resolved the credential store through
   ``MINI_ORK_SECRETS`` and overwrote a real provider key; the same
   fixture wrote rows into the operator's ``llm_calls`` table through
   ``MINI_ORK_DB`` + ``MINI_ORK_RUN_ID``.

``scrubbed_test_env`` returns a COPY of ``os.environ`` (or the supplied
mapping) with both classes removed. Everything else — ``PATH``, ``HOME``,
``PYTHONPATH``, ``MINI_ORK_ROOT``, ``MINI_ORK_TEST_CMD``, every ``MO_*``
knob — is preserved.

This module deliberately lives outside :mod:`mini_ork.verify`'s
package-level ``__all__``: the public surface is behavioral verification,
and the recipe verifiers import via the full subpath so ``scrubbed_test_env``
stays a private seam between the verify package and the recipe verifiers.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

# Names dropped verbatim (case-sensitive). mini-ork state pointers that
# would re-bind the child to the operator's live run / database / secrets
# store, or to a shadow agent registry that doesn't exist inside the
# sandboxed worktree.
#
# ``MO_TARGET_CWD`` belongs here for the same reason: it names the operator's
# target repo, so a test wrapper that does ``cd "${MO_TARGET_CWD:-$PWD}"``
# escapes the cwd the caller set. The oracle's delta-gate replay runs the SAME
# command twice with an explicit ``cwd`` (base worktree, then candidate); if the
# wrapper re-cds to the leaked candidate path the base side silently re-runs the
# candidate, ``base == candidate``, the touched-test overlap is empty, and every
# patch is refuted as ``tests-do-not-exercise-change``. Scrubbing it lets the
# wrapper's ``:-$PWD`` fall back to the caller's ``cwd`` — the side under test.
_DENY_NAMES: frozenset[str] = frozenset({
    "MINI_ORK_SECRETS",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MINI_ORK_PROJECT_HOME",
    "MINI_ORK_RUN_ID",
    "MINI_ORK_RUN_DIR",
    "MINI_ORK_PLAN_PATH",
    "MINI_ORK_AGENTS",
    "MO_TARGET_CWD",
})

# Credential suffixes. Names fully custom (MINIMAX_API_KEY, GLM_API_KEY,
# KIMI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, gateway BASE_URLs)
# land here, regardless of where the prefix comes from.
_DENY_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SECRET",
    "_SECRET_KEY",
)

# Anthropic publishes a wide set of ANTHROPIC_* knobs (API key, base URL,
# custom headers). Drop the prefix outright.
_DENY_PREFIXES: tuple[str, ...] = ("ANTHROPIC_",)

# OpenAI base-URL overrides gate a compatible endpoint. Dropping these
# forces the child to use the library's default, which fails fast instead
# of silently pointing at an operator-controlled mirror.
_DENY_OPENAI_BASES: frozenset[str] = frozenset({
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
})


def _is_scrubbed(key: str) -> bool:
    """Return True iff ``key`` should be stripped from the child env."""
    if key in _DENY_NAMES or key in _DENY_OPENAI_BASES:
        return True
    if any(key.startswith(p) for p in _DENY_PREFIXES):
        return True
    if any(key.endswith(s) for s in _DENY_SUFFIXES):
        return True
    return False


def scrubbed_test_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (default ``os.environ``) safe to hand a
    target repository's test suite.

    The returned dict removes every variable that names a provider
    credential or that points mini-ork at the operator's live state.
    The input mapping is never mutated — the function iterates the
    source's items into a fresh dict.

    Pass an explicit mapping to scrub a non-default environment, e.g.
    a fixture that simulates a hostile parent shell.
    """
    src = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for k, v in src.items():
        if _is_scrubbed(k):
            continue
        out[k] = v
    return out


__all__ = ["scrubbed_test_env"]
