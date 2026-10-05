"""Declared equivalence operators for the behavioral verifier.

A behavioral verdict that compares two values (an observed response body vs a
declared ``expect_body``, or amplified ``idempotent_repeat`` probes against the
first probe) depends on a *comparison relation*. This module makes that relation
explicit: an ``EquivalenceSpec`` names an operator (``exact`` by default) plus
operator-specific ``rules``, and every verdict records which operator produced it.

The relation is anchored on what the code DID — there is no LLM in the comparison
path. Three-valued discipline: ``compare`` returns ``EquivalenceResult.equal`` of
``True`` / ``False`` / ``None`` (could not evaluate), and a caller that cannot
evaluate must abstain (UNVERIFIED), never pass.

Import-time contract: pure stdlib only. The sole import-time effect is registering
the four built-in operators in a dict (the same shape ``behavioral.py`` uses for
surface handlers). ``exact`` reproduces ``behavioral._canonical`` semantics exactly —
key order ignored, list order + type distinctions + string bytes preserved.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = [
    "DEFAULT_OPERATOR",
    "EquivalenceSpec",
    "EquivalenceResult",
    "OperatorFn",
    "register_operator",
    "get_operator",
    "known_operators",
    "spec_error",
    "compare",
]

DEFAULT_OPERATOR = "exact"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _canonical(body: Any) -> str:
    """The hard-coded relation this module replaces, reproduced verbatim.

    Mirrors ``behavioral._canonical``: ``json.dumps(sort_keys=True, default=str)``
    with a ``repr()`` fallback, so key order never matters but list order, type
    distinctions (``1``/``1.0``/``true``) and string bytes all do.
    """
    try:
        return json.dumps(body, sort_keys=True, default=str)
    except Exception:
        return repr(body)


def _trunc(text: str) -> str:
    """Truncate a repr to 120 chars for the ``first diff`` detail."""
    return text if len(text) <= 120 else text[:117] + "..."


@dataclass(frozen=True)
class EquivalenceSpec:
    """A declared equivalence relation: an operator name plus optional rules.

    ``from_raw`` never consults the registry — an unknown operator name is not an
    error here; it becomes a :func:`spec_error` that the verifier surfaces as an
    honest abstention instead of silently falling back to ``exact``.
    """

    operator: str = DEFAULT_OPERATOR
    rules: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any) -> "EquivalenceSpec":
        if raw is None:
            return cls()
        if isinstance(raw, str):
            return cls(operator=raw)
        if isinstance(raw, Mapping):
            extra = sorted(set(raw) - {"operator", "rules"})
            if extra:
                raise ValueError(
                    f"equivalence mapping may only have keys 'operator' and 'rules'; "
                    f"got {extra}"
                )
            if "operator" not in raw:
                raise ValueError("equivalence mapping requires an 'operator' key")
            operator = raw["operator"]
            if not isinstance(operator, str):
                raise ValueError("equivalence 'operator' must be a string")
            rules = raw.get("rules", {})
            if rules is None:
                rules = {}
            if not isinstance(rules, Mapping):
                raise ValueError("equivalence 'rules' must be a mapping")
            return cls(operator=operator, rules=dict(rules))
        raise ValueError(
            f"equivalence must be None, a string, or a mapping; got {type(raw).__name__}"
        )


@dataclass(frozen=True)
class EquivalenceResult:
    """Outcome of one comparison. ``equal=None`` means 'could not evaluate'.

    On ``False``, ``detail`` is ``first diff at <path>: observed <repr> !=
    expected <repr>`` and ``path`` uses ``$`` / ``$.k`` / ``$[i]`` forms over the
    operator-normalized values. On ``None``, ``detail`` holds the reason.
    """

    equal: Optional[bool]
    operator: str
    detail: str = ""
    path: str = ""


OperatorFn = Callable[[Any, Any, Mapping[str, Any]], EquivalenceResult]
ValidatorFn = Callable[[Mapping[str, Any]], str]

_OPERATORS: dict[str, tuple[OperatorFn, Optional[ValidatorFn]]] = {}


def register_operator(
    name: str,
    fn: OperatorFn,
    *,
    validate: ValidatorFn | None = None,
) -> None:
    """Register an equivalence operator (last write wins). The OCP seam.

    ``validate(rules)`` returns ``""`` when the rules are acceptable or a reason
    otherwise. ``None`` means the operator takes no rules.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"operator name must match ^[a-z][a-z0-9_-]*$, got {name!r}"
        )
    if not callable(fn):
        raise ValueError(f"operator fn must be callable, got {type(fn).__name__}")
    _OPERATORS[name] = (fn, validate)


def get_operator(name: str) -> OperatorFn | None:
    entry = _OPERATORS.get(name)
    return entry[0] if entry is not None else None


def known_operators() -> tuple[str, ...]:
    return tuple(sorted(_OPERATORS))


def spec_error(spec: EquivalenceSpec | None) -> str:
    """Return ``""`` when ``spec`` is valid, or a reason otherwise."""
    if spec is None:
        spec = EquivalenceSpec()
    entry = _OPERATORS.get(spec.operator)
    if entry is None:
        registered = ", ".join(known_operators())
        return f"unknown equivalence operator '{spec.operator}'; registered: {registered}"
    _, validate = entry
    if validate is not None:
        return validate(spec.rules) or ""
    if spec.rules:
        return f"operator '{spec.operator}' takes no rules"
    return ""


def compare(
    observed: Any,
    expected: Any,
    spec: EquivalenceSpec | None = None,
) -> EquivalenceResult:
    """Compare two values under a declared operator. Never raises.

    Returns ``equal=None`` on a spec_error, when the operator raises, or when it
    returns something other than an :class:`EquivalenceResult`. The result's
    ``operator`` is always ``spec.operator``.
    """
    spec = spec if spec is not None else EquivalenceSpec()
    err = spec_error(spec)
    if err:
        return EquivalenceResult(None, spec.operator, err)
    fn = get_operator(spec.operator)
    try:
        result = fn(observed, expected, spec.rules)
    except Exception as exc:  # an operator raising must abstain, never pass
        return EquivalenceResult(
            None,
            spec.operator,
            f"operator '{spec.operator}' raised: {type(exc).__name__}: {exc}",
        )
    if not isinstance(result, EquivalenceResult):
        return EquivalenceResult(
            None,
            spec.operator,
            f"operator '{spec.operator}' returned {type(result).__name__}, "
            "not EquivalenceResult",
        )
    return EquivalenceResult(result.equal, spec.operator, result.detail, result.path)


# --------------------------------------------------------------------------- #
# Diff walkers (compare scalars by canonical form, never by ==)
# --------------------------------------------------------------------------- #
def _diff(obs: Any, exp: Any, path: str) -> tuple[str, str, str] | None:
    """Return ``(path, obs_repr, exp_repr)`` for the first difference, or None."""
    if isinstance(obs, dict) and isinstance(exp, dict):
        if set(obs) != set(exp):
            return (path, _trunc(repr(obs)), _trunc(repr(exp)))
        for key in sorted(set(obs)):
            found = _diff(obs[key], exp[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(obs, list) and isinstance(exp, list):
        if len(obs) != len(exp):
            return (path, _trunc(repr(obs)), _trunc(repr(exp)))
        for i in range(len(obs)):
            found = _diff(obs[i], exp[i], f"{path}[{i}]")
            if found is not None:
                return found
        return None
    if _canonical(obs) != _canonical(exp):
        return (path, _trunc(repr(obs)), _trunc(repr(exp)))
    return None


def _is_number(value: Any) -> bool:
    """A real number, excluding bool (bool is a subclass of int in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Built-in operators
# --------------------------------------------------------------------------- #
def _exact(observed: Any, expected: Any, rules: Mapping[str, Any]) -> EquivalenceResult:
    """Byte-exact under the canonical form. Takes no rules."""
    diff = _diff(observed, expected, "$")
    if diff is None:
        return EquivalenceResult(True, "exact")
    path, obs_repr, exp_repr = diff
    return EquivalenceResult(
        False, "exact", f"first diff at {path}: observed {obs_repr} != expected {exp_repr}", path
    )


def _sort_lists(value: Any) -> Any:
    """Sort every list at any depth by the canonical form of its elements."""
    if isinstance(value, list):
        return sorted((_sort_lists(item) for item in value), key=_canonical)
    if isinstance(value, dict):
        return {key: _sort_lists(item) for key, item in value.items()}
    return value


def _set_op(observed: Any, expected: Any, rules: Mapping[str, Any]) -> EquivalenceResult:
    """Multiset equality: sort every list at any depth, then apply ``exact``."""
    return _exact(_sort_lists(observed), _sort_lists(expected), rules)


def _canonicalize(
    value: Any,
    ignore_keys: set[str],
    strip_ws: bool,
    numeric: bool,
) -> Any:
    """Apply the ``canonical`` rules at every depth."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ignore_keys:
                continue
            out[key] = _canonicalize(item, ignore_keys, strip_ws, numeric)
        return out
    if isinstance(value, list):
        return [_canonicalize(item, ignore_keys, strip_ws, numeric) for item in value]
    if strip_ws and isinstance(value, str):
        return " ".join(value.split())
    if numeric and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _canonical_op(
    observed: Any, expected: Any, rules: Mapping[str, Any]
) -> EquivalenceResult:
    """Drop ``ignore_keys``, strip whitespace, and/or fold integral floats, then exact."""
    ignore_keys = set(rules.get("ignore_keys") or [])
    strip_ws = bool(rules.get("strip_whitespace"))
    numeric = bool(rules.get("numeric"))
    return _exact(
        _canonicalize(observed, ignore_keys, strip_ws, numeric),
        _canonicalize(expected, ignore_keys, strip_ws, numeric),
        rules,
    )


def _tolerant_diff(
    obs: Any,
    exp: Any,
    path: str,
    rel_tol: float,
    abs_tol: float,
) -> tuple[str, str, str] | None:
    if isinstance(obs, dict) and isinstance(exp, dict):
        if set(obs) != set(exp):
            return (path, _trunc(repr(obs)), _trunc(repr(exp)))
        for key in sorted(set(obs)):
            found = _tolerant_diff(obs[key], exp[key], f"{path}.{key}", rel_tol, abs_tol)
            if found is not None:
                return found
        return None
    if isinstance(obs, list) and isinstance(exp, list):
        if len(obs) != len(exp):
            return (path, _trunc(repr(obs)), _trunc(repr(exp)))
        for i in range(len(obs)):
            found = _tolerant_diff(obs[i], exp[i], f"{path}[{i}]", rel_tol, abs_tol)
            if found is not None:
                return found
        return None
    if _is_number(obs) and _is_number(exp):
        if math.isclose(obs, exp, rel_tol=rel_tol, abs_tol=abs_tol):
            return None
        return (path, _trunc(repr(obs)), _trunc(repr(exp)))
    if _canonical(obs) != _canonical(exp):
        return (path, _trunc(repr(obs)), _trunc(repr(exp)))
    return None


def _tolerant_op(
    observed: Any, expected: Any, rules: Mapping[str, Any]
) -> EquivalenceResult:
    """Numeric tolerance for non-bool numbers; every other leaf is exact."""
    abs_tol = rules.get("abs_tol", 0.0)
    rel_tol = rules.get("rel_tol", 0.0)
    diff = _tolerant_diff(observed, expected, "$", rel_tol, abs_tol)
    if diff is None:
        return EquivalenceResult(True, "tolerant")
    path, obs_repr, exp_repr = diff
    return EquivalenceResult(
        False, "tolerant", f"first diff at {path}: observed {obs_repr} != expected {exp_repr}", path
    )


# --------------------------------------------------------------------------- #
# Rule validators for the operators that accept rules
# --------------------------------------------------------------------------- #
def _validate_canonical(rules: Mapping[str, Any]) -> str:
    allowed = {"ignore_keys", "strip_whitespace", "numeric"}
    unknown = sorted(set(rules) - allowed)
    if unknown:
        return (
            f"unknown rule(s) for 'canonical': {unknown}; "
            "allowed: ignore_keys, strip_whitespace, numeric"
        )
    ignore_keys = rules.get("ignore_keys")
    if ignore_keys is not None and (
        not isinstance(ignore_keys, list)
        or any(not isinstance(key, str) for key in ignore_keys)
    ):
        return "'ignore_keys' must be a list of strings"
    for name in ("strip_whitespace", "numeric"):
        if name in rules and not isinstance(rules[name], bool):
            return f"'{name}' must be a bool"
    return ""


def _validate_tolerant(rules: Mapping[str, Any]) -> str:
    allowed = {"abs_tol", "rel_tol"}
    unknown = sorted(set(rules) - allowed)
    if unknown:
        return (
            f"unknown rule(s) for 'tolerant': {unknown}; "
            "allowed: abs_tol, rel_tol"
        )
    abs_tol = rules.get("abs_tol", 0.0)
    rel_tol = rules.get("rel_tol", 0.0)
    for name, value in (("abs_tol", abs_tol), ("rel_tol", rel_tol)):
        if isinstance(value, bool):
            return f"'{name}' must be a number, not a bool"
        if not isinstance(value, (int, float)):
            return f"'{name}' must be a number"
        if not math.isfinite(value):
            return f"'{name}' must be finite"
        if value < 0:
            return f"'{name}' must be >= 0"
    if abs_tol == 0 and rel_tol == 0:
        return "at least one of abs_tol, rel_tol must be > 0"
    return ""


# Built-in operators. Registering them in a dict is the only import-time effect
# (no I/O, no env mutation) — the same contract behavioral.py states.
register_operator("exact", _exact)
register_operator("set", _set_op)
register_operator("canonical", _canonical_op, validate=_validate_canonical)
register_operator("tolerant", _tolerant_op, validate=_validate_tolerant)
