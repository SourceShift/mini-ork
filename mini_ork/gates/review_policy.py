"""Human-review policy loader.

A single, configurable rule for when a human must approve a run's output before
the run (or its merge) continues. The policy is purely declarative: three knobs
(``before_merge``, ``after_nodes``, ``tier``) and three places they come from
(env, ``$MINI_ORK_HOME/config/review.yaml``, in-code defaults), in a fixed
precedence.

Failure mode is **fail closed** for every reason we can foresee:

* Unknown values for ``before_merge`` or ``tier`` raise ``ValueError`` rather
  than silently disabling review — a typo in ``before_merge: maybe`` must never
  read as "review off".
* A misspelled sub-key under ``review:`` (e.g. ``befor_merge:``) raises
  ``ValueError`` naming the bad key; silently dropping it would re-enable the
  same footgun the values check is closing.
* An empty ``$MO_REVIEW_*`` variable is treated as unset.

The resolver never reads the ambient process environment unless the caller
passes ``env=None`` (the CLI / hook entry point). Tests pass an explicit
mapping so a runner-launched test cannot pick up values from a parent shell.

The policy is a frozen dataclass — mutable state in a policy that gates runs
is a recipe for an "approved once, applies forever" cache whose lifetime is
never visible.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

__all__ = ["ReviewPolicy", "load_policy", "node_requires_review"]


VALID_BEFORE_MERGE = ("off", "required")
VALID_TIER = ("simple", "secure")

_VALID_AFTER_NODES_KEYS = {"before_merge", "after_nodes", "tier"}


@dataclass(frozen=True)
class ReviewPolicy:
    """The resolved policy at a point in time.

    ``after_nodes`` is a tuple so the dataclass stays hashable/frozen; the YAML
    spec accepts a list and the loader converts.
    """

    before_merge: str = "off"
    after_nodes: tuple[str, ...] = field(default_factory=tuple)
    tier: str = "simple"

    def __post_init__(self) -> None:
        if self.before_merge not in VALID_BEFORE_MERGE:
            raise ValueError(
                f"before_merge must be one of {VALID_BEFORE_MERGE!r}, "
                f"got {self.before_merge!r}"
            )
        if self.tier not in VALID_TIER:
            raise ValueError(
                f"tier must be one of {VALID_TIER!r}, got {self.tier!r}"
            )
        # after_nodes is already a tuple by dataclass field shape, but callers
        # may pass a list through the loader — dataclass does not convert.
        for node_type in self.after_nodes:
            if not isinstance(node_type, str) or not node_type.strip():
                raise ValueError(
                    f"after_nodes must be a sequence of non-empty strings, "
                    f"got {node_type!r}"
                )


def _coerce_after_nodes(value: object) -> tuple[str, ...]:
    """Accept a YAML list or a comma-separated string. Anything else → ValueError."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        raise ValueError(
            f"after_nodes must be a list or comma-separated string, got {type(value).__name__}"
        )
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError(
                f"after_nodes entries must be strings, got {type(item).__name__}: {item!r}"
            )
        stripped = item.strip()
        if stripped:
            out.append(stripped)
    return tuple(out)


def _normalize_env_value(value: str | None) -> str | None:
    """Empty / whitespace-only env vars are treated as unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _coerce_policy_scalar(value: object, *, key: str) -> object:
    """Coerce YAML 1.1's implicit bools/letters to the strings the loader expects.

    ``before_merge: off`` is parsed by PyYAML's default loader as the boolean
    ``False`` (the YAML 1.1 spec lists ``on/off/yes/no`` as bool aliases). A
    ``tier: yes`` would land the same way. Without this coercion, the
    validator would reject the very values the example file documents as
    valid. The fix is to lower-case, normalise, and accept what the operator
    meant. A value that is neither bool, str, nor recognised NoneType
    propagates to the next validator (``ReviewPolicy.__post_init__``), which
    raises with the field's name.
    """
    if isinstance(value, bool):
        # YAML 1.1 bool: ``on/yes/true`` → True, ``off/no/false`` → False.
        # The example's documented valid values are ``"off"`` and ``"required"``;
        # False is the operator's unquoted ``off`` and the only bool we want
        # to map. Any other bool is a typo and falls through to the validator.
        if not value:
            return "off"
        return value
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _load_from_yaml(path: Path) -> dict:
    """Read ``path``; an empty/missing ``review:`` key returns an empty mapping.

    Raises ``ValueError`` on a YAML parse failure, a non-mapping ``review:`` key,
    or unknown sub-keys.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read review policy at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"review policy at {path} is not valid YAML: {exc}") from exc

    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise ValueError(
            f"review policy at {path} must be a mapping with a 'review:' key, "
            f"got {type(document).__name__}"
        )

    raw_review = document.get("review")
    if raw_review is None:
        # Empty file / ``review:`` not set → use defaults. We still validate
        # that there are no junk keys at the top level (vault's discipline).
        unknown_top = sorted(set(document) - {"review"})
        if unknown_top:
            raise ValueError(
                f"unknown top-level key(s) in {path}: {', '.join(unknown_top)}; "
                "expected 'review:'"
            )
        return {}

    if not isinstance(raw_review, Mapping):
        raise ValueError(
            f"'review:' in {path} must be a mapping, got {type(raw_review).__name__}"
        )

    unknown = sorted(set(raw_review) - _VALID_AFTER_NODES_KEYS)
    if unknown:
        raise ValueError(
            f"unknown key(s) under 'review:' in {path}: {', '.join(unknown)}; "
            f"expected one of {sorted(_VALID_AFTER_NODES_KEYS)}"
        )
    out: dict = {}
    for key in raw_review:
        if key in ("before_merge", "tier"):
            out[key] = _coerce_policy_scalar(raw_review[key], key=key)
        else:
            out[key] = raw_review[key]
    return out


def _resolve_home(home: str | Path | None) -> Path:
    if home is not None:
        return Path(home)
    return Path(os.environ.get("MINI_ORK_HOME") or ".mini-ork")


def load_policy(
    home: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ReviewPolicy:
    """Resolve the policy from env, file, and defaults — highest precedence first.

    Precedence (env wins, defaults lose):

    1. ``MO_REVIEW_BEFORE_MERGE``, ``MO_REVIEW_AFTER_NODES``, ``MO_REVIEW_TIER``
       — empty values are treated as unset.
    2. ``$home/config/review.yaml`` (where ``home`` defaults to ``$MINI_ORK_HOME``
       and finally ``.mini-ork``). An absent file is the defaults.
    3. In-code defaults: ``before_merge="off"``, ``after_nodes=()``, ``tier="simple"``.

    Reads no env var other than the three ``MO_REVIEW_*`` policy keys (plus the
    standard ``MINI_ORK_HOME`` for the file path). A runner-launched process
    that exports unrelated ``MO_REVIEW_*`` knobs (``MO_REVIEW_PANEL``,
    ``MO_REVIEW_ROUND_AWARE``, …) never leaks into a policy decision here.
    """
    effective_env = os.environ if env is None else env

    env_before = _normalize_env_value(effective_env.get("MO_REVIEW_BEFORE_MERGE"))
    env_after = _normalize_env_value(effective_env.get("MO_REVIEW_AFTER_NODES"))
    env_tier = _normalize_env_value(effective_env.get("MO_REVIEW_TIER"))

    file_path = _resolve_home(home) / "config" / "review.yaml"
    file_policy: dict = {}
    if file_path.is_file():
        file_policy = _load_from_yaml(file_path)

    # Build a candidate dict per layer; the FIRST non-None value for each knob
    # wins. Errors in a higher-precedence layer (unknown value) propagate and
    # short-circuit the file/default fallback — the typo is what the operator
    # needs to see first.
    out: dict = {
        "before_merge": "off",
        "after_nodes": (),
        "tier": "simple",
    }

    if "before_merge" in file_policy:
        out["before_merge"] = file_policy["before_merge"]
    if "after_nodes" in file_policy:
        out["after_nodes"] = _coerce_after_nodes(file_policy["after_nodes"])
    if "tier" in file_policy:
        out["tier"] = file_policy["tier"]

    if env_before is not None:
        out["before_merge"] = env_before.strip().lower()
    if env_after is not None:
        lowered = env_after.strip().lower()
        if lowered == "all":
            out["after_nodes"] = ("*",)
        else:
            out["after_nodes"] = _coerce_after_nodes(lowered)
    if env_tier is not None:
        out["tier"] = env_tier.strip().lower()

    return ReviewPolicy(**out)


def node_requires_review(
    policy: ReviewPolicy,
    node_type: str,
    node_flag: str | None = None,
) -> bool:
    """Decide whether the named node's output is gated by a human.

    Returns True when any of these holds:

    * ``node_flag == "required"`` — the workflow node itself asks for review.
    * ``"*"`` is in ``policy.after_nodes`` — every node is gated.
    * ``node_type`` is in ``policy.after_nodes`` — exact match.

    A ``node_flag`` of ``"off"`` turns this node's review **off**, even when
    the policy would otherwise gate it — but the secure tier ignores that
    override (the operator who set ``tier=secure`` wants every node reviewed,
    full stop).
    """
    if policy.tier == "secure":
        # Secure tier is opt-out disabled; a workflow node cannot say
        # ``review: off`` and bypass the gate an operator armed.
        if node_flag == "required":
            return True
        return "*" in policy.after_nodes or node_type in policy.after_nodes

    # Simple tier: an explicit off wins.
    if node_flag == "off":
        return False

    if node_flag == "required":
        return True
    return "*" in policy.after_nodes or node_type in policy.after_nodes
