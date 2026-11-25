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
``kill_score=None``. The same holds when there is no denominator to score:
an observable no mutation can act on (a ``function`` surface carries no
``body``/``expect_status`` field) yields ``total=0``, and an observable whose
mutants all come back undecided yields ``killed=survived=0``. Both are
``measured=False`` with ``kill_score=None`` — ``measured`` means "a kill score
exists", never "the scoring loop ran", because ``admit(None)`` is ``True``.

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

_PRODUCE_ENV = "MO_VERIFIER_MUTATION_PRODUCE"

_OFF_VALUES = frozenset({"0", "off", "false", "no"})

__all__ = [
    "MUTATIONS",
    "DEFAULT_MIN_KILL",
    "mutate",
    "score",
    "admit",
    "min_kill",
    "gate_enabled",
    "write_score",
    "read_score",
    "card_for_spec",
    "autoproduce",
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
    # ``measured`` means "a kill score exists", not "the scoring loop ran": an
    # observable that every mutation skips (a ``function`` surface carries no
    # ``body``/``expect_status`` field to corrupt) yields an empty denominator,
    # and reporting that as measured would publish a null score as a result.
    return {
        "kill_score": (killed / denominator) if denominator else None,
        "killed": killed,
        "survived": survived,
        "abstained": abstained,
        "total": len(entries),
        "mutations": entries,
        "measured": denominator > 0,
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


def min_kill() -> float:
    """The admission floor in force (``MO_VERIFIER_MUTATION_MIN`` or the default).

    Public so a caller can say *why* it refused a score instead of restating
    the threshold in prose that then drifts from this module.
    """
    return _min_kill_from_env()


def gate_enabled() -> bool:
    """Whether ``MO_VERIFIER_MUTATION`` leaves mutation-discrimination on.

    The master switch shared by the catalog's ranking and the verifier
    dispatcher's admission check, so one knob turns both halves off.
    """
    return os.environ.get("MO_VERIFIER_MUTATION", "1").strip().lower() not in _OFF_VALUES


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


# --------------------------------------------------------------------------- #
# autoproduce()
# --------------------------------------------------------------------------- #

_SPEC_SUFFIXES = (".observable.yaml", ".observable.yml", ".observable.json")


def card_for_spec(spec_path: str) -> str:
    """The catalog card that describes the verifier behind an observable spec.

    The convention is sibling files in one directory:
    ``api_contract.observable.yaml`` → ``api_contract.card.yaml``, and a bare
    ``thing.yaml``/``thing.json`` descriptor → ``thing.card.yaml``.
    """
    directory = os.path.dirname(spec_path) or "."
    base = os.path.basename(spec_path)
    for suffix in _SPEC_SUFFIXES:
        if base.endswith(suffix):
            stem = base[: -len(suffix)]
            break
    else:
        stem = os.path.splitext(base)[0]
    return os.path.join(directory, stem + ".card.yaml")


def _find_card(spec_path: str, card_path: str | None) -> str | None:
    """Resolve the card for a descriptor: explicit, sibling, then ``<ancestor>/verifiers``.

    The catalog's cards live at ``<root>/verifiers/`` while a recipe keeps its
    observable next to its dispatcher, so the sibling convention alone would
    never find the card for a recipe-local spec. The walk is bounded (eight
    levels) and matches the exact stem, so it cannot pick up an unrelated
    project's card from further up the tree.
    """
    if card_path:
        return card_path if os.path.isfile(card_path) else None
    sibling = card_for_spec(spec_path)
    if os.path.isfile(sibling):
        return sibling
    stem = os.path.basename(sibling)[: -len(".card.yaml")]
    directory = os.path.dirname(os.path.abspath(spec_path))
    for _ in range(8):
        candidate = os.path.join(directory, "verifiers", stem + ".card.yaml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


def _load_spec_mapping(spec_path: str) -> dict:
    with open(spec_path, encoding="utf-8") as fh:
        text = fh.read()
    if spec_path.endswith((".yaml", ".yml")):
        import yaml  # lazy: the module's import-time contract stays stdlib-only

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{spec_path}: observable descriptor must be a mapping")
    return data


def autoproduce(
    spec_path: str,
    *,
    card_path: str | None = None,
    runner=None,
) -> dict:
    """Measure a PROVEN observable's verifier and write its sidecar.

    The manual producer is ``mini-ork instruments verifier-mutation``; this is
    the in-run one, called by :func:`mini_ork.verify.behavioral.main` after a
    PROVEN verdict. It costs one baseline probe plus one per applicable
    mutation against the same live target — the price of measuring a verifier
    that is already passing, paid once per PROVEN behavioral verification.

    Never raises: a missing card, an unparseable spec, or a failed write
    returns ``measured=False`` with the reason. A verifier that greened a run
    must not be reddened by the instrument that measures it.

    Returns ``{"measured", "written", "reason", "kill_score"}``.
    """
    if not _produce_enabled():
        return {
            "measured": False,
            "written": None,
            "reason": f"off ({_PRODUCE_ENV})",
            "kill_score": None,
        }
    if not spec_path:
        return {
            "measured": False,
            "written": None,
            "reason": "no observable spec",
            "kill_score": None,
        }
    card = _find_card(spec_path, card_path)
    if card is None:
        # No card means nothing reads the score; writing a sidecar next to a
        # file nobody declared would only leave litter in the tree.
        return {
            "measured": False,
            "written": None,
            "reason": f"no verifier card for {spec_path}",
            "kill_score": None,
        }
    try:
        mapping = _load_spec_mapping(spec_path)
        result = score(mapping, runner=runner)
    except Exception as exc:  # fallback-guard: allow: the instrument must not fail the run it measures
        return {
            "measured": False,
            "written": None,
            "reason": f"{type(exc).__name__}: {exc}",
            "kill_score": None,
        }
    if not result.get("measured"):
        return {
            "measured": False,
            "written": None,
            "reason": str(result.get("reason") or "not measured"),
            "kill_score": None,
        }
    try:
        written = write_score(card, result)
    except OSError as exc:
        return {
            "measured": True,
            "written": None,
            "reason": f"cannot write the sidecar: {exc}",
            "kill_score": result.get("kill_score"),
        }
    return {
        "measured": True,
        "written": written,
        "reason": "",
        "kill_score": result.get("kill_score"),
    }


def _produce_enabled() -> bool:
    """In-run production is on unless ``MO_VERIFIER_MUTATION_PRODUCE`` says no."""
    return os.environ.get(_PRODUCE_ENV, "1").strip().lower() not in _OFF_VALUES
