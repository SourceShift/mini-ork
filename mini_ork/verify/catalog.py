"""Behavioral verifier catalog (P3).

A :class:`VerifierCard` is a static, file-backed description of a single
verifier — its kind, surface, cost, recipe pointer, and IRT-GRM-style quality
statistics. The catalog module exposes:

- :func:`load_cards` — the only I/O boundary; parses one or more
  ``*.card.yaml`` files lazily and validates each against
  ``schemas/verifier_card.schema.json`` if ``jsonschema`` is installed;
- :func:`card_score` — deterministic IRT-GRM-inspired score,
  ``discrimination * consistency * (1 / (1 + cost)) - fuzz_penalty``;
- :func:`rank_verifiers` — sorts by score desc, then cost asc (stable sort
  falls back to file order, which is sorted before parsing).

Import-time contract: pure stdlib. ``yaml`` and ``jsonschema`` are imported
lazily inside :func:`load_cards`, so importing this module never pulls a
third-party dependency and performs no I/O. This mirrors the import-time
contract of :mod:`mini_ork.verify.behavioral`.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "VerifierCard",
    "VerifierStats",
    "load_cards",
    "card_score",
    "rank_verifiers",
]


_KIND_ENUM = ("behavioral", "deterministic", "reviewer", "llm_judge", "external")
_SURFACE_ENUM = ("api", "ui", "journey", "function", "")


@dataclass(frozen=True)
class VerifierStats:
    """Quality statistics for one :class:`VerifierCard`.

    IRT-GRM-inspired heuristic inputs. Values are 0.0–1.0 where 1.0 means
    the verifier maximally discriminates pass/fail (discrimination), is
    self-consistent across reruns (consistency), and incurs no false
    positives from input fuzzing (fuzz_penalty is a *penalty*).
    """

    discrimination: float
    consistency: float
    fuzz_penalty: float
    n_observations: int = 0

    def __post_init__(self) -> None:
        for name in ("discrimination", "consistency", "fuzz_penalty"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or v < 0.0:
                raise ValueError(f"{name} must be a non-negative number, got {v!r}")
        if not isinstance(self.n_observations, int) or self.n_observations < 0:
            raise ValueError(
                f"n_observations must be a non-negative int, got {self.n_observations!r}"
            )


@dataclass(frozen=True)
class VerifierCard:
    """A catalog entry for one verifier.

    ``recipe`` is a path to a dedicated per-verifier recipe (empty string
    means the verifier has no dedicated recipe). ``surface`` is the
    behavioral surface (``api``/``ui``/``journey``/``function``) or ``""`` for
    non-behavioral verifiers. ``cost`` is a unitless 0.0+ number used in
    the IRT-GRM-inspired score.
    """

    name: str
    kind: str
    surface: str
    cost: float
    recipe: str = ""
    stats: VerifierStats = field(default_factory=lambda: VerifierStats(0.0, 0.0, 0.0, 0))
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("VerifierCard.name must be a non-empty string")
        if self.kind not in _KIND_ENUM:
            raise ValueError(
                f"VerifierCard.kind must be one of {_KIND_ENUM!r}, got {self.kind!r}"
            )
        if self.surface not in _SURFACE_ENUM:
            raise ValueError(
                f"VerifierCard.surface must be one of {_SURFACE_ENUM!r}, "
                f"got {self.surface!r}"
            )
        if not isinstance(self.cost, (int, float)) or self.cost < 0.0:
            raise ValueError(f"VerifierCard.cost must be >= 0, got {self.cost!r}")
        if not isinstance(self.recipe, str):
            raise ValueError("VerifierCard.recipe must be a string")


def card_score(card: VerifierCard) -> float:
    """Deterministic IRT-GRM-inspired heuristic.

    ``discrimination * consistency * (1 / (1 + cost)) - fuzz_penalty``.

    Higher score = better. Cost shrinks the score monotonically; fuzz_penalty
    is subtracted directly. No fitted model — these stats are manually
    calibrated per card.
    """
    s = card.stats
    return s.discrimination * s.consistency * (1.0 / (1.0 + card.cost)) - s.fuzz_penalty


def rank_verifiers(cards: Iterable[VerifierCard]) -> list[VerifierCard]:
    """Sort cards by score desc, then cost asc.

    Python's sort is stable, so when score and cost both tie the input order
    is preserved. Callers should pass an already-sorted iterable (e.g. from
    :func:`load_cards`) to make file order the deterministic tie-breaker.
    """
    return sorted(cards, key=lambda c: (-card_score(c), c.cost))


def _parse_one(path: Path) -> VerifierCard:
    """Parse one ``*.card.yaml`` file into a :class:`VerifierCard`.

    Lazy-imports ``yaml`` and ``jsonschema`` so module import stays stdlib.
    Errors include the file path so a malformed card fails the load loudly
    rather than producing a silent default.
    """
    import yaml  # type: ignore

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: failed to parse YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")

    _validate_against_schema(data, path)

    stats_raw = data.get("stats") or {}
    if not isinstance(stats_raw, dict):
        raise ValueError(f"{path}: 'stats' must be a mapping, got {type(stats_raw).__name__}")
    try:
        stats = VerifierStats(
            discrimination=float(stats_raw.get("discrimination", 0.0)),
            consistency=float(stats_raw.get("consistency", 0.0)),
            fuzz_penalty=float(stats_raw.get("fuzz_penalty", 0.0)),
            n_observations=int(stats_raw.get("n_observations", 0)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid stats field: {exc}") from exc

    try:
        return VerifierCard(
            name=str(data["name"]),
            kind=str(data["kind"]),
            surface=str(data.get("surface", "")),
            cost=float(data.get("cost", 0.0)),
            recipe=str(data.get("recipe", "")),
            stats=stats,
            source=str(path),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid card: {exc}") from exc


def _validate_against_schema(data: dict, path: Path) -> None:
    """Optional schema check. ``jsonschema`` is an optional dependency.

    If installed, every card is validated against the schema declared by
    the project (``schemas/verifier_card.schema.json`` is *not* hardcoded —
    we look it up next to this package's caller). If not installed, the
    schema check is skipped and we rely on the dataclass validators.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return

    schema_path = _resolve_schema_path()
    if schema_path is None:
        return
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"{path}: schema validation failed: {exc.message}") from exc


def _resolve_schema_path() -> Path | None:
    """Find ``schemas/verifier_card.schema.json`` next to the repo root.

    Walks up from this file looking for a ``schemas/verifier_card.schema.json``.
    Returns ``None`` if not found — schema check is a strict upgrade, not a
    requirement.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "schemas" / "verifier_card.schema.json"
        if candidate.exists():
            return candidate
    return None


def load_cards(
    directory: str | os.PathLike[str],
    pattern: str = "*.card.yaml",
) -> list[VerifierCard]:
    """Parse every ``*.card.yaml`` file in ``directory`` into a sorted list.

    File order is sorted before parsing so the catalog is deterministic
    across platforms and filesystems. The returned list is the input order
    consumed by :func:`rank_verifiers`; when score and cost both tie, the
    stable sort preserves that order.

    A malformed card raises :class:`ValueError` whose message starts with
    the offending file path — there is no silent default.
    """
    root = Path(directory)
    paths = sorted(root.glob(pattern))
    return [_parse_one(p) for p in paths]
