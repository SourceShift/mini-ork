"""SpecCard — the compiled contract for one spec file.

Immutable dataclasses mirroring the design doc's SpecCard YAML block
(docs/plans/2026-10-03-spec-driven-delivery.md) field for field, plus the
JSON round-trip and validation against ``schemas/spec-card.schema.json``.

``from_dict`` is structural: it tolerates absent optional lists (refs,
depends_on, clause buckets, design_sources) and fills defaults, but raises
:class:`SpecCardError` when a required scalar is missing. Value checks
(status/gate-kind enums, id patterns, absolute paths, hash shape) belong to
:func:`validate_card`, which returns every schema violation instead of
stopping at the first.

    SpecCard.from_dict(d) -> SpecCard ; card.to_dict() -> dict
    validate_card(d) -> list[str]   # empty == valid
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mini_ork.specdir.schema import SPEC_CARD_SCHEMA, load_schema, schema_errors

# The schema is the single source of truth for the enums; reading them here
# keeps these constants and the validator from drifting apart.
_SCHEMA = load_schema(SPEC_CARD_SCHEMA)
STATUSES: tuple[str, ...] = tuple(_SCHEMA["properties"]["status"]["enum"])
GATE_KINDS: tuple[str, ...] = tuple(_SCHEMA["$defs"]["gate"]["properties"]["kind"]["enum"])
CLAUSE_KINDS: tuple[str, ...] = tuple(_SCHEMA["properties"]["clauses"]["required"])


class SpecCardError(ValueError):
    """A dict that cannot be shaped into a SpecCard at all."""


def _req(d: dict, key: str, where: str):
    if not isinstance(d, dict):
        raise SpecCardError(f"{where}: expected an object, got {type(d).__name__}")
    if key not in d or d[key] is None:
        raise SpecCardError(f"{where}: missing required field '{key}'")
    return d[key]


def _strs(value) -> tuple[str, ...]:
    return tuple(str(v) for v in (value or ()))


@dataclass(frozen=True)
class Clause:
    id: str
    text: str

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict, where: str = "clause") -> Clause:
        return cls(id=str(_req(d, "id", where)), text=str(_req(d, "text", where)))


@dataclass(frozen=True)
class Clauses:
    functional: tuple[Clause, ...] = ()
    quality: tuple[Clause, ...] = ()
    constitutional: tuple[Clause, ...] = ()
    architectural: tuple[Clause, ...] = ()

    def to_dict(self) -> dict:
        return {kind: [c.to_dict() for c in getattr(self, kind)] for kind in CLAUSE_KINDS}

    @classmethod
    def from_dict(cls, d: dict | None) -> Clauses:
        d = d or {}
        return cls(**{
            kind: tuple(Clause.from_dict(c, f"clauses.{kind}[{i}]") for i, c in enumerate(d.get(kind) or ()))
            for kind in CLAUSE_KINDS
        })


@dataclass(frozen=True)
class Gate:
    kind: str
    probe: str
    expect: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "probe": self.probe, "expect": self.expect}

    @classmethod
    def from_dict(cls, d: dict, where: str = "gate") -> Gate:
        return cls(kind=str(_req(d, "kind", where)), probe=str(_req(d, "probe", where)),
                   expect=str(_req(d, "expect", where)))


@dataclass(frozen=True)
class Acceptance:
    id: str
    text: str
    gate: Gate
    clause_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "clause_refs": list(self.clause_refs), "text": self.text,
                "gate": self.gate.to_dict()}

    @classmethod
    def from_dict(cls, d: dict, where: str = "acceptance") -> Acceptance:
        return cls(id=str(_req(d, "id", where)), text=str(_req(d, "text", where)),
                   gate=Gate.from_dict(_req(d, "gate", where), f"{where}.gate"),
                   clause_refs=_strs(d.get("clause_refs")))


@dataclass(frozen=True)
class Deliverable:
    id: str
    title: str
    acceptance_refs: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "acceptance_refs": list(self.acceptance_refs),
                "depends_on": list(self.depends_on)}

    @classmethod
    def from_dict(cls, d: dict, where: str = "deliverable") -> Deliverable:
        return cls(id=str(_req(d, "id", where)), title=str(_req(d, "title", where)),
                   acceptance_refs=_strs(d.get("acceptance_refs")), depends_on=_strs(d.get("depends_on")))


@dataclass(frozen=True)
class UiCraft:
    required: bool = False
    design_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"required": self.required, "design_sources": list(self.design_sources)}

    @classmethod
    def from_dict(cls, d: dict | None) -> UiCraft:
        d = d or {}
        return cls(required=bool(d.get("required", False)), design_sources=_strs(d.get("design_sources")))


@dataclass(frozen=True)
class SpecCard:
    spec_id: str
    source_path: str
    source_hash: str
    title: str
    clauses: Clauses = field(default_factory=Clauses)
    acceptance: tuple[Acceptance, ...] = ()
    deliverables: tuple[Deliverable, ...] = ()
    ui_craft: UiCraft = field(default_factory=UiCraft)
    status: str = "draft"

    def to_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "title": self.title,
            "clauses": self.clauses.to_dict(),
            "acceptance": [a.to_dict() for a in self.acceptance],
            "deliverables": [d.to_dict() for d in self.deliverables],
            "ui_craft": self.ui_craft.to_dict(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SpecCard:
        where = "spec_card"
        return cls(
            spec_id=str(_req(d, "spec_id", where)),
            source_path=str(_req(d, "source_path", where)),
            source_hash=str(_req(d, "source_hash", where)),
            title=str(_req(d, "title", where)),
            clauses=Clauses.from_dict(d.get("clauses")),
            acceptance=tuple(Acceptance.from_dict(a, f"acceptance[{i}]")
                             for i, a in enumerate(d.get("acceptance") or ())),
            deliverables=tuple(Deliverable.from_dict(x, f"deliverables[{i}]")
                               for i, x in enumerate(d.get("deliverables") or ())),
            ui_craft=UiCraft.from_dict(d.get("ui_craft")),
            status=str(d.get("status") or "draft"),
        )

    def validate(self) -> list[str]:
        return validate_card(self.to_dict())


def validate_card(data: dict) -> list[str]:
    """Schema violations of a SpecCard dict; empty list means valid."""
    return schema_errors(data, SPEC_CARD_SCHEMA)
