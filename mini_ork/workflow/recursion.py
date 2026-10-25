"""Read the ``recursion:`` block a recipe workflow declares.

``schemas/workflow.schema.json`` has enforced this block since the recursion
lock, but nothing consumed it: a recipe could declare ``max_iterations: 5``
and the driver that actually ran the loop would still use its own hardcoded
default. The declaration was decoration — editing it changed nothing.

This module is the reader that closes that gap. It is deliberately pure: one
file in, one immutable config out, no env reads and no defaults. Resolution
order (caller → declared → literal) belongs at the consumption site, not here,
so that "what did the recipe declare?" stays answerable on its own.

**Only what runs.** The block declares the two declared caps a reader actually
honors: ``max_iterations`` and ``budget_cap_total_usd``. Three keys —
``budget_cap_per_iter_usd``, ``convergence_check`` and ``divergence_kill`` —
were dropped in 2026-10 because nothing read them; a block that still carries
one raises with a message saying so rather than passing silently. The schema
declares the same two properties and requires both, and this loader agrees: a
partial block is rejected so it cannot silently fall back to a hardcoded
default at the consumption site. Unknown keys raise too, so a typo is caught at
load rather than at 3am.
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
    "budget_cap_total_usd",
)

# Keys that used to be declared and were read by nothing. Kept out of FIELDS so
# a block still carrying one gets a removal notice instead of a generic
# unknown-key error that names the key without saying why it is gone.
REMOVED_FIELDS: tuple[str, ...] = (
    "budget_cap_per_iter_usd",
    "convergence_check",
    "divergence_kill",
)


class RecursionConfigError(ValueError):
    """A declared ``recursion:`` block cannot be honored as written."""


@dataclass(frozen=True)
class RecursionConfig:
    """The two declared caps, verbatim from the recipe."""

    max_iterations: int
    budget_cap_total_usd: float


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

    # A key that was removed must say so before the generic unknown-key check:
    # "unknown recursion key" names the key but not why it is invalid. Sorted so
    # a block carrying several removed keys reports the same one every time.
    removed = sorted(set(raw) & set(REMOVED_FIELDS))
    if removed:
        raise RecursionConfigError(
            f"recursion.{removed[0]} was removed in 2026-10 (nothing read it); "
            f"delete it from {path}"
        )
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
            "declare both keys or none (a partial block would silently "
            "fall back to a hardcoded default)"
        )

    return RecursionConfig(
        max_iterations=_as_int(raw["max_iterations"], "max_iterations", path),
        budget_cap_total_usd=_as_float(
            raw["budget_cap_total_usd"], "budget_cap_total_usd", path
        ),
    )
