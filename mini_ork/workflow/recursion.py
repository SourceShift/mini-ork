"""Read the ``recursion:`` block a recipe workflow declares.

``schemas/workflow.schema.json`` has enforced this block since the five-key
lock, but nothing consumed it: a recipe could declare ``max_iterations: 5``
and the driver that actually ran the loop would still use its own hardcoded
default. The declaration was decoration — editing it changed nothing.

This module is the reader that closes that gap. It is deliberately pure: one
file in, one immutable config out, no env reads and no defaults. Resolution
order (caller → declared → literal) belongs at the consumption site, not here,
so that "what did the recipe declare?" stays answerable on its own.

**Stricter than the schema.** The schema lists the five properties under
``additionalProperties: false`` but declares no ``required`` array, so a
partial block validates. This loader rejects one: a missing key would otherwise
silently fall back to a hardcoded default at the consumption site, which is the
same "edit the YAML and nothing happens" failure in a smaller costume. Unknown
keys raise too, so a typo is caught at load rather than at 3am.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

# Schema order. The loader checks presence in this order so a block missing
# several keys reports the first one deterministically.
FIELDS: tuple[str, ...] = (
    "max_iterations",
    "convergence_check",
    "budget_cap_per_iter_usd",
    "budget_cap_total_usd",
    "divergence_kill",
)


class RecursionConfigError(ValueError):
    """A declared ``recursion:`` block cannot be honored as written."""


@dataclass(frozen=True)
class RecursionConfig:
    """The five declared caps/conditions, verbatim from the recipe."""

    max_iterations: int
    convergence_check: str
    budget_cap_per_iter_usd: float
    budget_cap_total_usd: float
    divergence_kill: str


def _as_int(value: Any, key: str, path: Path) -> int:
    # bool is an int subclass in Python; True must not read as the integer 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecursionConfigError(
            f"recursion.{key} must be an integer, got {type(value).__name__} in {path}"
        )
    if value < 1:
        raise RecursionConfigError(
            f"recursion.{key} must be >= 1, got {value} in {path}"
        )
    return value


def _as_float(value: Any, key: str, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecursionConfigError(
            f"recursion.{key} must be a number, got {type(value).__name__} in {path}"
        )
    if value < 0:
        raise RecursionConfigError(
            f"recursion.{key} must be >= 0, got {value} in {path}"
        )
    return float(value)


def _as_str(value: Any, key: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecursionConfigError(
            f"recursion.{key} must be a non-empty string in {path}"
        )
    return value


def load_recursion_config(workflow_path: str | Path) -> RecursionConfig | None:
    """Return the workflow's declared recursion config, or ``None`` if it has none.

    ``None`` means the recipe declared no ``recursion:`` block — it does not mean
    "use defaults". Callers decide what absence means; this function only reports
    what is written.
    """
    path = Path(workflow_path)
    if not path.is_file():
        raise RecursionConfigError(f"workflow not found: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RecursionConfigError(f"invalid workflow YAML {path}: {exc}") from exc
    if not isinstance(document, Mapping):
        raise RecursionConfigError(f"workflow root must be an object: {path}")

    raw = document.get("recursion")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RecursionConfigError(f"recursion must be an object in {path}")

    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise RecursionConfigError(
            f"unknown recursion key(s) {unknown} in {path}; "
            f"expected exactly {list(FIELDS)}"
        )
    missing = [key for key in FIELDS if key not in raw]
    if missing:
        raise RecursionConfigError(
            f"recursion block in {path} is missing {missing}; "
            "declare all five keys or none (a partial block would silently "
            "fall back to a hardcoded default)"
        )

    return RecursionConfig(
        max_iterations=_as_int(raw["max_iterations"], "max_iterations", path),
        convergence_check=_as_str(raw["convergence_check"], "convergence_check", path),
        budget_cap_per_iter_usd=_as_float(
            raw["budget_cap_per_iter_usd"], "budget_cap_per_iter_usd", path
        ),
        budget_cap_total_usd=_as_float(
            raw["budget_cap_total_usd"], "budget_cap_total_usd", path
        ),
        divergence_kill=_as_str(raw["divergence_kill"], "divergence_kill", path),
    )
