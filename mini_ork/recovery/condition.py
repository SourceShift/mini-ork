"""Compile-time validation and runtime evaluation of `condition:` blocks on
``retries`` edges.

A ``condition:`` block has three keys:

  - ``pointer`` (string, RFC 6901 JSON pointer into the node output artifact)
  - ``match``   (dict, a JSON Schema fragment validated against the resolved
                 value)
  - ``negate``  (optional bool, default ``False``; when ``True`` the predicate
                 fires when the match FAILS)

Compile-time invariants (C2 of ``p1-1-condition-edges``):

  - ``pointer`` must be a string
  - ``match`` must be a mapping
  - ``negate`` must be a boolean (when present)
  - No unknown keys inside the ``condition:`` block
  - ``match`` must itself be a structurally valid JSON Schema fragment

All four are the violations. We raise :class:`ConditionSchemaError` (a
``ValueError`` subclass) at compile time so the executor surfaces a hard
error rather than silently mis-evaluating at runtime. The exception message
includes the schema path so the recipe author can locate the offending key in
their workflow.yaml.

Runtime contract (F1-F3 of ``p1-1-condition-edges``):

  - The predicate fires when ``jsonschema.validate(resolved, match)`` succeeds,
    or when ``negate: True`` and validation FAILS, or when ``negate: True``
    and the pointer resolves to nothing (F3).
  - When the artifact is missing or not valid JSON, the predicate does NOT
    fire and a warning is reported (F2). The rc channel stays authoritative;
    condition-fired rounds only happen when ``rc == 0``.
  - When the pointer resolves to nothing, the predicate does NOT fire (unless
    ``negate: True``, in which case it does) and a warning is reported (F3).

The evaluator is pure: it does NOT touch the filesystem, the network, or any
secret store. Callers are responsible for loading the artifact and supplying
its text. That keeps the predicate testable in isolation (the AC1 parity
tests exercise this module directly).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any, Mapping

# jsonschema is a core dependency (C1). Import at module load — not lazily —
# so an ImportError surfaces at process startup, not deep inside a revise
# loop after a node has already dispatched.
import jsonschema
import jsonschema.exceptions

__all__ = [
    "ConditionSchemaError",
    "CompiledCondition",
    "PredicateOutcome",
    "compile_condition",
    "evaluate_condition",
    "validate_condition_schema",
]


class ConditionSchemaError(ValueError):
    """Raised at compile time when a ``condition:`` block is malformed.

    Distinct from a runtime predicate failure (``PredicateOutcome.fired is
    False``) and from any ``jsonschema`` exception a runtime mismatch would
    raise. The message includes the schema path so the operator can locate
    the offending key in their workflow.yaml.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"condition.{path}: {message}")


_ALLOWED_CONDITION_KEYS = frozenset({"pointer", "match", "negate"})


@dataclasses.dataclass(frozen=True)
class CompiledCondition:
    """The compiled form of a ``condition:`` block, safe to ship across the
    executor boundary (no YAML, no dicts of unknown shape).

    ``pointer`` is the RFC 6901 JSON pointer into the node output artifact.
    ``match`` is the JSON Schema fragment. ``negate`` inverts the predicate
    sense: ``True`` means "fire when match FAILS".
    """

    pointer: str
    match: Mapping[str, Any]
    negate: bool = False
    # Track the YAML pointer path so error messages from a future round can
    # identify which condition misbehaved without re-parsing the workflow.
    source_path: str = ""

    def to_tuple(self) -> tuple[Any, ...]:
        """Return a tuple representation keyed for dict equality tests."""
        return (self.pointer, self.match, self.negate, self.source_path)


@dataclasses.dataclass(frozen=True)
class PredicateOutcome:
    """The result of evaluating one compiled condition against one artifact.

    ``fired`` is the only field the executor should branch on. ``warning`` is
    surfaced to stderr when truthy so the operator can see why a predicate
    did not fire (or, with ``negate: True``, why it fired for a missing
    value).
    """

    fired: bool
    warning: str = ""
    # Free-form reason the executor can surface in revise logs. Distinct from
    # ``warning``: ``reason`` is the affirmative explanation of WHAT fired
    # (e.g. "match succeeded", "pointer missing + negate"). ``warning`` is
    # the defensive note that something was wrong-but-tolerated.
    reason: str = ""


def validate_condition_schema(edge: Mapping[str, Any]) -> None:
    """Hard-error at compile time when ``edge['condition']`` violates C2.

    The four hard errors (per ``p1-1-condition-edges`` spec C2):

      1. unknown keys inside ``condition``
      2. non-string ``pointer``
      3. non-dict ``match``
      4. non-bool ``negate``

    Each error includes the schema path so the recipe author can find the
    offending key in their workflow.yaml. ``match`` is additionally validated
    as a structurally well-formed JSON Schema fragment so a broken
    ``jsonschema`` ``Draft7Validator`` is reported at compile time rather
    than at the first round of revise.
    """
    raw = edge.get("condition")
    if raw is None:
        # No condition declared — nothing to validate. Callers that require
        # a condition should check ``"condition" in edge`` themselves.
        return
    if not isinstance(raw, Mapping):
        raise ConditionSchemaError(
            "<root>",
            f"must be an object, got {type(raw).__name__}",
        )

    unknown = set(raw.keys()) - _ALLOWED_CONDITION_KEYS
    if unknown:
        # ``pointer`` first (canonical order) then any other unknowns.
        ordered = sorted(unknown)
        raise ConditionSchemaError(
            "<root>",
            f"unknown keys {ordered!r}; allowed keys are {sorted(_ALLOWED_CONDITION_KEYS)!r}",
        )

    pointer = raw.get("pointer", "<missing>")
    if not isinstance(pointer, str):
        raise ConditionSchemaError(
            "pointer",
            f"must be a string, got {type(pointer).__name__}",
        )
    if not pointer:
        # An empty pointer resolves to the entire artifact (RFC 6901 §4),
        # which is almost always a mistake — surface it so the recipe author
        # does not silently match against the whole doc.
        raise ConditionSchemaError(
            "pointer",
            "must be a non-empty RFC 6901 pointer (got empty string)",
        )

    match = raw.get("match", "<missing>")
    if not isinstance(match, Mapping):
        raise ConditionSchemaError(
            "match",
            f"must be an object (JSON Schema fragment), got {type(match).__name__}",
        )

    if "negate" in raw:
        negate = raw["negate"]
        if not isinstance(negate, bool):
            raise ConditionSchemaError(
                "negate",
                f"must be a boolean, got {type(negate).__name__}",
            )

    # Validate that ``match`` is at least a structurally valid JSON Schema.
    # jsonschema raises ``SchemaError`` for a malformed fragment (a number
    # where a string is expected, an unknown top-level key, etc.). Re-raise
    # as ``ConditionSchemaError`` so the error class is uniform for the
    # compile-time path and carries the schema path.
    try:
        jsonschema.Draft7Validator.check_schema(match)
    except jsonschema.exceptions.SchemaError as exc:
        raise ConditionSchemaError("match", f"invalid JSON Schema fragment: {exc.message}") from exc


def compile_condition(edge: Mapping[str, Any], *, source_path: str = "<unknown>") -> CompiledCondition:
    """Compile one ``retries`` edge's ``condition:`` block to a
    :class:`CompiledCondition`.

    Validates the schema first via :func:`validate_condition_schema` so a
    malformed block raises before any field is read. Callers that want
    separate validation and compilation should call :func:`validate_condition_schema`
    directly, then call :func:`compile_condition` on the same edge — the
    second call repeats the validation cheaply (no I/O) and returns the
    compiled dataclass.
    """
    validate_condition_schema(edge)
    raw = edge.get("condition")
    if raw is None:
        raise ConditionSchemaError(
            "<root>",
            "compile_condition called on an edge without a condition block",
        )
    return CompiledCondition(
        pointer=str(raw["pointer"]),
        match=dict(raw["match"]),
        negate=bool(raw.get("negate", False)),
        source_path=source_path,
    )


def _load_artifact(output_artifact: str | None) -> tuple[Any | None, str]:
    """Parse the artifact text as JSON; return ``(None, reason)`` on failure.

    Distinguishes "missing artifact" (empty input) from "non-JSON artifact"
    (parse failure) in the warning text so the operator can fix the right
    thing. The first element is the parsed JSON when parsing succeeds, else
    ``None``. The second element is the warning string (empty on success).
    """
    if output_artifact is None or output_artifact == "":
        return None, "artifact missing"
    try:
        return json.loads(output_artifact), ""
    except json.JSONDecodeError as exc:
        return None, f"artifact is not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"


def _resolve_pointer(pointer: str, document: Any) -> tuple[Any, str]:
    """Resolve an RFC 6901 JSON pointer against ``document``.

    Returns ``(resolved, warning)`` — ``resolved`` is the dereferenced value
    (or ``None`` when the pointer does not address a real member) and
    ``warning`` is the diagnostic when resolution fails. We do NOT raise:
    the predicate must handle a missing key gracefully per F3, and the
    operator needs to see one stderr line rather than a stack trace.

    RFC 6901 grammar (the bits that matter here):
      - ``""`` addresses the whole document
      - ``"/foo"`` addresses the member named ``"foo"``
      - ``"/foo/0"`` addresses the first element of the array at ``foo``
      - ``"~0"`` and ``"~1"`` are the escapes for ``~`` and ``/``
    """
    # Hand-rolled resolution to avoid an extra dependency on jsonpointer for
    # what amounts to a 10-line traversal. Keeps the predicate pure (no I/O,
    # no global state) and the predicate testable in isolation.
    if pointer == "":
        return document, ""
    if not pointer.startswith("/"):
        return None, f"pointer {pointer!r} must start with '/' (RFC 6901)"

    parts = pointer.split("/")[1:]
    current: Any = document
    for raw_token in parts:
        # RFC 6901 §4: unescape ``~1`` → ``/`` and ``~0`` → ``~``.
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return None, f"pointer {pointer!r} does not address an existing member"
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return None, f"pointer {pointer!r} addresses non-integer array index {token!r}"
            if index < 0 or index >= len(current):
                return None, f"pointer {pointer!r} addresses out-of-range array index {index}"
            current = current[index]
        else:
            return None, f"pointer {pointer!r} cannot traverse scalar value"
    return current, ""


def evaluate_condition(
    compiled: CompiledCondition,
    output_artifact: str | None,
) -> PredicateOutcome:
    """Evaluate one compiled condition against one artifact body.

    The artifact is supplied as the *text* of the node's output JSON (the
    same text the verifier / publisher would consume) — NOT as a parsed
    dict, so this function can report a parse failure distinctly from a
    schema mismatch. The function is pure: it does not read the filesystem
    or call any side-effecting API.

    Returned :class:`PredicateOutcome`:

      - ``fired=False, warning="artifact missing", reason=""`` when no
        artifact text was provided (F2).
      - ``fired=False, warning="artifact is not valid JSON: …", reason=""``
        when the artifact text is present but not parseable (F2). With
        ``negate=True`` the predicate still does NOT fire — we cannot
        reliably say "match failed" when we never ran the match.
      - ``fired=False, warning="pointer … does not address …"`` when the
        pointer resolves to nothing AND ``negate`` is False (F3).
      - ``fired=True, warning="pointer … does not address …",
        reason="negated: missing value counts as match failure"`` when the
        pointer resolves to nothing AND ``negate=True`` (F3).
      - Otherwise: ``jsonschema.validate(resolved, compiled.match)`` runs;
        fires on success, or on failure when ``negate=True``.
    """
    document, artifact_warning = _load_artifact(output_artifact)
    if document is None and artifact_warning:
        # F2: missing / non-JSON artifact — do not fire, surface a warning.
        # We deliberately do NOT honour ``negate`` here: the predicate was
        # never actually evaluated, so we cannot claim "match failure".
        return PredicateOutcome(fired=False, warning=artifact_warning)

    resolved, pointer_warning = _resolve_pointer(compiled.pointer, document)
    if resolved is None and pointer_warning:
        if compiled.negate:
            # F3: with negate, "nothing to match" counts as match failure
            # and the predicate fires. The warning is still useful so the
            # operator can see the predicate is being satisfied by absence.
            return PredicateOutcome(
                fired=True,
                warning=pointer_warning,
                reason="negated: missing value counts as match failure",
            )
        return PredicateOutcome(fired=False, warning=pointer_warning)

    validator = jsonschema.Draft7Validator(compiled.match)
    errors = sorted(validator.iter_errors(resolved), key=lambda e: list(e.absolute_path))
    match_ok = not errors
    if compiled.negate:
        # Fires when match would have failed.
        if match_ok:
            return PredicateOutcome(
                fired=False,
                warning="",
                reason="negated: match succeeded",
            )
        return PredicateOutcome(
            fired=True,
            warning="",
            reason="negated: match failed",
        )
    if match_ok:
        return PredicateOutcome(fired=True, warning="", reason="match succeeded")
    return PredicateOutcome(
        fired=False,
        warning="",
        reason=f"match failed: {errors[0].message}",
    )


# Mark jsonschema as a hard runtime dependency for packaging tools that
# introspect __all__/imports (the import at the top of this module is the
# authoritative check — see the module docstring).
_NEVER_USED = sys.modules.get("jsonschema")  # pragma: no cover