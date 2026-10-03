"""JSON-Schema loading + validation for the specdir artifacts.

``spec-card.schema.json`` and ``spec-index.schema.json`` live in the engine's
top-level ``schemas/`` directory. They are located by walking up from this
file (the same parent-walk as :mod:`mini_ork.verify.catalog`), so a vendored
``.mini-ork/`` engine copy resolves its own schemas. Unlike the verifier
catalog, validation here is mandatory: a missing schema raises instead of
silently skipping the check.

    load_schema(name) -> dict
    schema_errors(instance, name) -> list[str]
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from jsonschema.validators import validator_for

SPEC_CARD_SCHEMA = "spec-card.schema.json"
SPEC_INDEX_SCHEMA = "spec-index.schema.json"


def _resolve_schema_path(name: str) -> Path | None:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        candidate = parent / "schemas" / name
        if candidate.exists():
            return candidate
    return None


@cache
def load_schema(name: str) -> dict:
    path = _resolve_schema_path(name)
    if path is None:
        raise FileNotFoundError(f"schemas/{name} not found above {Path(__file__).resolve().parent}")
    return json.loads(path.read_text(encoding="utf-8"))


def schema_errors(instance: object, name: str) -> list[str]:
    """Every violation of schema ``name`` as ``"<json/path>: <message>"``,
    deterministically ordered. Empty list means valid."""
    schema = load_schema(name)
    validator = validator_for(schema)(schema)
    out = []
    for err in validator.iter_errors(instance):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{where}: {err.message}")
    return sorted(out)
