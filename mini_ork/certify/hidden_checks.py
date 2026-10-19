"""Hidden regenerable ground truth for the solve-time oracle.

WHY THIS EXISTS
───────────────
The reproduction test is visible to the implementer across retries. A patch can
therefore special-case the exact test identity -- the function name, the class
name, the file path -- and pass the visible PoC while fixing nothing general.
The visible test is a single point in the space of *identities*, not only in
the space of inputs.

This module regenerates behaviour-neutral variants of a **precommitted**
parameter set at verification time. `plan_for` derives a seed from committed
key material (a configured secret, the run id, or the patch digest -- in that
order, never an empty key) and `apply_variant` renames the PoC's top-level test
identifiers deterministically. The renamed test asserts exactly the same thing
as the visible PoC; only its identity differs. A patch that depends on the
visible identity passes the first and fails the variant -- evidence, not a
flaky re-run.

`precommit(params)` is the commitment: a canonical-JSON sha256 over the
parameter set the caller precommitted to. It is reproducible from the same
inputs, changes if any value changes, and is carried into the certificate so an
auditor can tie a verdict to the parameters that produced it.

BEHAVIOUR NEUTRALITY IS THE LAW
───────────────────────────────
`apply_variant` rewrites ONLY the declaration line of a top-level
``def test_...`` / ``class Test...`` (plus one prepended marker comment). It
never touches assertions, imports, literals, decorators, ordering, indentation,
or the environment. Nested helpers and indented class methods keep their
identifiers. When no top-level test identifier exists, the source is returned
unchanged -- the caller must mark that trial `unmeasured` rather than pretend a
variant was produced.

Stdlib-only at import time: no mini_ork imports, no provider credentials, no
run-local state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

#: Number of hidden variants generated when the caller does not ask for another
#: count. Kept small on purpose: each trial is one extra sandbox test run.
DEFAULT_TRIALS = 2

#: Env var holding the HMAC key material when the secure tier is configured.
_SECRET_ENV = "MO_HIDDEN_CHECK_SECRET"

#: The fixed HMAC personalisation string (domain separation from other uses of
#: the same key material).
_SEED_PERSONALISATION = b"mini-ork-hidden"

#: Env knobs, read at CALL time.
_ENABLED_ENV = "MO_ASSAY_HIDDEN"
_TRIALS_ENV = "MO_HIDDEN_TRIALS"
_TRUE = frozenset({"1", "true", "yes", "on"})

#: Top-level test declarations. ``^`` with no leading whitespace keeps nested
#: helpers and class methods out of the rename. The optional leading ``+`` lets
#: a unified diff that ADDS a test be treated as source (``plan_for`` receives
#: the patch text); a plain test source is unaffected.
_TEST_DEF_RE = re.compile(
    r"^(?P<lead>\+?)(?P<kw>async\s+def|def)\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_TEST_CLASS_RE = re.compile(
    r"^(?P<lead>\+?)class\s+(?P<name>Test[A-Za-z0-9_]*)\b",
    re.MULTILINE,
)


@dataclass(frozen=True)
class HiddenPlan:
    """A reproducible hidden-check plan.

    ``seed`` is derived from the key material and is the cross-call contract:
    the oracle regenerates variants of the PoC it actually judges from this
    seed, so the plan is reproducible from the same inputs and secret.
    ``key_source`` records WHICH material was used -- a run without a configured
    secret must not claim secret-backed hiding.
    """

    run_id: str
    seed: int
    key_source: str  # "secret" | "run_id" | "patch_digest"
    commitment: str  # sha256 hex of the canonical params
    variants: tuple[str, ...]


def enabled() -> bool:
    """True iff ``MO_ASSAY_HIDDEN`` is truthy. DEFAULT ON; ``0``/``off`` disables."""
    return os.environ.get(_ENABLED_ENV, "1").strip().lower() in _TRUE


def trials_from_env(default: int = DEFAULT_TRIALS) -> int:
    """Parse ``MO_HIDDEN_TRIALS``; reject a malformed value instead of guessing.

    A non-integer is a configuration error: silently defaulting would let a
    typo quietly weaken the hidden layer, so it raises ``ValueError``.
    """
    raw = os.environ.get(_TRIALS_ENV, str(default))
    try:
        trials = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{_TRIALS_ENV} must be an integer, got {raw!r}"
        ) from exc
    if trials < 0:
        raise ValueError(f"{_TRIALS_ENV} must be non-negative, got {trials}")
    return trials


def precommit(params: dict) -> str:
    """sha256 hex of the canonical JSON of ``params``.

    Canonical form is ``json.dumps(sort_keys=True, separators=(",", ":"))`` --
    stable across key order, changed by any value, and reproducible outside
    this module.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key_material(run_id: str | None, patch: str) -> tuple[bytes, str]:
    """Return ``(key_bytes, key_source)``; the key is NEVER empty.

    Precedence: a non-empty ``MO_HIDDEN_CHECK_SECRET`` wins; else a non-empty
    ``run_id``; else the sha256 of the patch. Empty values fall through rather
    than becoming an empty HMAC key.
    """
    secret = os.environ.get(_SECRET_ENV) or ""
    if secret:
        return secret.encode("utf-8"), "secret"
    if run_id:
        return run_id.encode("utf-8"), "run_id"
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    return digest.encode("utf-8"), "patch_digest"


def _stable_hash(seed: int, index: int, old_name: str) -> str:
    """8 hex chars of a stable hash of ``(seed, index, old_name)``."""
    material = f"{seed}:{index}:{old_name}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:8]


def plan_for(
    *,
    run_id: str | None,
    patch: str,
    params: dict | None = None,
    trials: int = DEFAULT_TRIALS,
) -> HiddenPlan:
    """Build a reproducible hidden-check plan.

    ``params`` is the precommitted parameter set (default ``{}``); its canonical
    digest becomes ``commitment``. ``variants`` are ``trials`` generated sources
    via :func:`apply_variant` over the source text this function receives
    (``patch`` in the oracle's call shape).
    """
    if params is None:
        params = {}
    if trials < 0:
        raise ValueError(f"trials must be non-negative, got {trials}")

    key, key_source = _key_material(run_id, patch)
    seed = int.from_bytes(
        hmac.new(key, _SEED_PERSONALISATION, hashlib.sha256).digest()[:8], "big"
    )
    commitment = precommit(params)
    variants = tuple(
        apply_variant(patch, seed=seed, index=i) for i in range(trials)
    )
    return HiddenPlan(
        run_id=run_id or "",
        seed=seed,
        key_source=key_source,
        commitment=commitment,
        variants=variants,
    )


def _rename(match: re.Match[str], *, seed: int, index: int) -> str:
    """Append ``_h{hash8}_{index}`` to the declaration's identifier."""
    old = match.group("name")
    suffix = f"_h{_stable_hash(seed, index, old)}_{index}"
    return match.group(0).replace(old, old + suffix, 1)


def apply_variant(test_src: str, *, seed: int, index: int) -> str:
    """Return a behaviour-neutral, identity-renamed variant of ``test_src``.

    Every top-level ``def test_...`` and ``class Test...`` identifier gets a
    deterministic suffix; a marker comment naming the trial is prepended. The
    rest of the source -- bodies, assertions, imports, literals, ordering -- is
    byte-identical. When no top-level test identifier is found the source is
    returned UNCHANGED (no marker, no manufactured difference); the caller must
    treat that trial as unmeasured.
    """
    renamed = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal renamed
        renamed += 1
        return _rename(match, seed=seed, index=index)

    out = _TEST_DEF_RE.sub(_sub, test_src)
    out = _TEST_CLASS_RE.sub(_sub, out)
    if not renamed:
        return test_src
    marker_hash = _stable_hash(seed, index, "")
    return f"# hidden-check trial {index} {marker_hash}\n{out}"


__all__ = [
    "DEFAULT_TRIALS",
    "HiddenPlan",
    "apply_variant",
    "enabled",
    "plan_for",
    "precommit",
    "trials_from_env",
]
