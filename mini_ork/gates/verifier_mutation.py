"""Mutation-discrimination suite for behavioral verifiers.

A verifier that accepts every artifact has a perfect pass record and no
value. This module mutates a valid declared expectation in six named ways,
scores the verifier's kill rate, and reports whether the verifier is
discriminating enough to be admitted to the catalog.

Definitions (all load-bearing):

- **killed** — the verifier returns ``REFUTED`` for the mutant.
- **survived** — the verifier returns ``PROVEN`` for the mutant.
- **abstained** — the verifier returns ``UNVERIFIED`` (or anything else).
  Abstentions are *not* kills and *not* survivals; ``kill_score`` is
  ``killed / (killed + survived)`` only.

A verifier that rejects the valid artifact is never scored: the baseline
must be ``PROVEN`` or the result is ``measured=False`` with
``kill_score=None``.

Import-time contract: stdlib only. :mod:`mini_ork.verify.behavioral` is
imported lazily inside :func:`score` only when no runner is injected.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone

MUTATIONS: tuple[str, ...] = (
    "value_flip",
    "field_drop",
    "type_swap",
    "list_reorder",
    "truncate_string",
    "status_swap",
)

DEFAULT_MIN_KILL = 0.8

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

_MIN_KILL_ENV = "MO_VERIFIER_MUTATION_MIN"

__all__ = [
    "MUTATIONS",
    "DEFAULT_MIN_KILL",
    "mutate",
    "score",
    "admit",
    "write_score",
    "read_score",
]


# --------------------------------------------------------------------------- #
# mutate()
# --------------------------------------------------------------------------- #


def _is_scalar(value) -> bool:
    return isinstance(value, (str, int, float, bool))


def _flip_scalar(value):
    """Return a value of the same JSON type that is semantically different."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        # ``-0`` equals ``0``; pick a non-zero so the defect is real.
        return 1 if value == 0 else -value
    if isinstance(value, str):
        return "mutated" if not value else value + "_mutant"
    raise ValueError(f"cannot flip scalar of type {type(value).__name__}")


def _swap_type(value):
    """Return a value of a different JSON type."""
    if isinstance(value, bool):
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return 0
    if isinstance(value, list):
        return {"mutated": True}
    if isinstance(value, dict):
        return ["mutated"]
    return "null"


def _mutate_value_flip(mutant: dict, body: dict | None, mutation: str) -> None:
    if body:
        for key, value in body.items():
            if _is_scalar(value):
                body[key] = _flip_scalar(value)
                return
    visible = mutant.get("expect_visible")
    if isinstance(visible, list):
        for index, value in enumerate(visible):
            if _is_scalar(value):
                visible[index] = _flip_scalar(value)
                return
    raise ValueError(
        f"mutation {mutation!r} cannot apply: no scalar in expect_body or expect_visible"
    )


def _mutate_field_drop(mutant: dict, body: dict | None, mutation: str) -> None:
    if body:
        body.pop(next(iter(body)))
        return
    schema = mutant.get("expect_json_schema")
    if isinstance(schema, dict):
        required = schema.get("required")
        if isinstance(required, list) and required:
            required.pop(0)
            return
    raise ValueError(
        f"mutation {mutation!r} cannot apply: no key in expect_body or required schema field"
    )


def _mutate_type_swap(mutant: dict, body: dict | None, mutation: str) -> None:
    if body:
        for key, value in body.items():
            body[key] = _swap_type(value)
            return
    raise ValueError(f"mutation {mutation!r} cannot apply: expect_body has no value")


def _mutate_list_reorder(mutant: dict, body: dict | None, mutation: str) -> None:
    if body:
        for key, value in body.items():
            if isinstance(value, list) and len(value) >= 2:
                body[key] = list(reversed(value))
                return
    raise ValueError(
        f"mutation {mutation!r} cannot apply: expect_body has no list of two or more items"
    )


def _mutate_truncate_string(mutant: dict, body: dict | None, mutation: str) -> None:
    if body:
        for key, value in body.items():
            if isinstance(value, str) and value:
                body[key] = value[:-1]
                return
    raise ValueError(
        f"mutation {mutation!r} cannot apply: expect_body has no non-empty string"
    )


def _mutate_status_swap(mutant: dict, body: dict | None, mutation: str) -> None:
    if "expect_status" not in mutant:
        raise ValueError(
            f"mutation {mutation!r} cannot apply: observable has no expect_status"
        )
    current = mutant.get("expect_status")
    if not isinstance(current, list):
        current = []
    if 500 not in current:
        mutant["expect_status"] = [500]
        return
    for candidate in (418, 503, 599):
        if candidate not in current:
            mutant["expect_status"] = [candidate]
            return
    mutant["expect_status"] = [500] if current != [500] else [599]


_MUTATION_HANDLERS = {
    "value_flip": _mutate_value_flip,
    "field_drop": _mutate_field_drop,
    "type_swap": _mutate_type_swap,
    "list_reorder": _mutate_list_reorder,
    "truncate_string": _mutate_truncate_string,
    "status_swap": _mutate_status_swap,
}


def mutate(observable: dict, mutation: str) -> dict:
    """Return a deep copy of ``observable`` with exactly one semantic defect.

    Raises :class:`ValueError` naming ``mutation`` when the observable has no
    field that mutation can act on. Silently returning the original would make
    the suite score a verifier it never tested.
    """
    handler = _MUTATION_HANDLERS.get(mutation)
    if handler is None:
        raise ValueError(f"unknown mutation {mutation!r}; known: {MUTATIONS!r}")
    if not isinstance(observable, dict):
        raise ValueError(
            f"mutation {mutation!r} requires an observable mapping, "
            f"got {type(observable).__name__}"
        )
    mutant = copy.deepcopy(observable)
    body = mutant.get("expect_body")
    body = body if isinstance(body, dict) else None
    handler(mutant, body, mutation)
    return mutant


# --------------------------------------------------------------------------- #
# score()
# --------------------------------------------------------------------------- #


def _status_of(verdict) -> str:
    status = getattr(verdict, "status", None)
    if status is None and isinstance(verdict, dict):
        status = verdict.get("status")
    if status is None and isinstance(verdict, str):
        status = verdict
    return str(status) if status is not None else ""


def _behavioral_runner():
    """Build the default runner, importing behavioral lazily."""
    from mini_ork.verify import behavioral

    def _run(observable):
        if isinstance(observable, behavioral.Observable):
            return behavioral.run(observable)
        return behavioral.run(behavioral.Observable.from_mapping(observable))

    return _run


def score(observable: dict, *, runner=None) -> dict:
    """Score one observable's verifier by its mutation kill rate.

    ``runner`` is a callable ``observable -> verdict`` (a duck-typed object
    with ``status``). When omitted, :func:`mini_ork.verify.behavioral.run`
    is used on a parsed :class:`Observable`.
    """
    run = runner if runner is not None else _behavioral_runner()

    baseline = _status_of(run(copy.deepcopy(observable)))
    if baseline != PROVEN:
        return {
            "kill_score": None,
            "measured": False,
            "reason": f"baseline not proven (status={baseline or 'missing'})",
            "killed": 0,
            "survived": 0,
            "abstained": 0,
            "total": 0,
            "mutations": [],
        }

    killed = 0
    survived = 0
    abstained = 0
    entries: list[dict] = []
    for name in MUTATIONS:
        try:
            mutant = mutate(observable, name)
        except ValueError:
            continue
        status = _status_of(run(mutant))
        if status == REFUTED:
            killed += 1
            outcome = "killed"
        elif status == PROVEN:
            survived += 1
            outcome = "survived"
        else:
            abstained += 1
            outcome = "abstained"
        entries.append({"name": name, "outcome": outcome, "status": status})

    denominator = killed + survived
    return {
        "kill_score": (killed / denominator) if denominator else None,
        "killed": killed,
        "survived": survived,
        "abstained": abstained,
        "total": len(entries),
        "mutations": entries,
        "measured": True,
    }


# --------------------------------------------------------------------------- #
# admit()
# --------------------------------------------------------------------------- #


def _min_kill_from_env() -> float:
    raw = os.environ.get(_MIN_KILL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MIN_KILL
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_KILL


def admit(kill_score: float | None, *, min_kill: float | None = None) -> bool:
    """Return whether ``kill_score`` clears the admission floor.

    ``None`` (unmeasured) never blocks. ``min_kill`` wins when supplied;
    otherwise ``MO_VERIFIER_MUTATION_MIN`` sets the threshold, defaulting to
    :data:`DEFAULT_MIN_KILL`.
    """
    if kill_score is None:
        return True
    threshold = _min_kill_from_env() if min_kill is None else float(min_kill)
    return float(kill_score) >= threshold


# --------------------------------------------------------------------------- #
# sidecar score persistence
# --------------------------------------------------------------------------- #


def _sidecar_path(card_path: str) -> str:
    return f"{card_path}.mutation.json"


def write_score(card_path: str, result: dict) -> str:
    """Atomically write the mutation score next to ``card_path``.

    The sidecar is ``<card_path>.mutation.json`` and holds only the declared
    fields. ``os.replace`` makes an interrupted write invisible: readers see
    either the old sidecar or the new one, never a torn file.
    """
    payload = {
        "kill_score": result.get("kill_score"),
        "killed": int(result.get("killed") or 0),
        "survived": int(result.get("survived") or 0),
        "abstained": int(result.get("abstained") or 0),
        "measured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    sidecar = _sidecar_path(card_path)
    directory = os.path.dirname(card_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".mutation-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.replace(tmp_path, sidecar)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return sidecar


def read_score(card_path: str) -> float | None:
    """Read the sidecar kill score; missing or corrupt sidecars are ``None``."""
    try:
        with open(_sidecar_path(card_path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("kill_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
