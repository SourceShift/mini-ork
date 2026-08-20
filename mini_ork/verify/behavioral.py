"""Behavioral verifier — execution-anchored live checks of observable surfaces.

A *behavioral* verifier exercises a user- or data-journey surface **live** and
returns a three-valued, execution-anchored verdict:

    PROVEN     — every declared check and metamorphic relation held live.
    REFUTED    — a check failed against a *reachable* surface (the code is wrong).
    UNVERIFIED — the surface could not be exercised (unreachable, no observable
                 declared, budget exhausted, or a relation we cannot yet
                 evaluate). This is abstention — NOT a pass.

Why three-valued and not binary: a verifier that turns "I couldn't check" into a
green is a false-complete, and false-completes are exactly what the verifier is
built to eliminate. Abstaining loudly protects precision (cf. the measured
0-false-completions guarantee) at the cost of recall, which is the correct trade
for a gate.

P0 implements ``surface="api"`` only: probe a staging endpoint with httpx (the
requester is injectable so tests need no network), assert status + an optional
JSON shape, and check metamorphic relations that need no gold output — a single
request/response is enough to anchor an oracle (RESTOR, 2607.23963; metamorphic
REST testing, 2605.28321). ``ui`` / ``journey`` surfaces return UNVERIFIED here;
they register their handlers in P1/P2 via :func:`register_surface_handler`.

Process-seam contract: ``mini_ork/cli/verify.py`` dispatches a ``verifiers/*.py``
script as a subprocess and treats **exit-0-with-evidence as PASS**. So a
behavioral verifier must print its verdict JSON to stdout (non-empty evidence)
and exit 0 ONLY on PROVEN. UNVERIFIED must not exit 0 — :func:`main` maps
PROVEN->0, REFUTED->1, UNVERIFIED->``MO_BEHAV_ABSTAIN_EXIT`` (default 1 =
conservative; set to 0 only for an advisory-only verifier). The full
three-valued verdict always lands in the emitted JSON for the ranking scorecard
(P3).

Import-time contract: pure stdlib. ``httpx`` and ``yaml`` are imported lazily
inside the functions that need them, so importing this module never pulls a
third-party dependency and does no I/O beyond registering the built-in ``api``
surface handler in a dict.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = [
    "PROVEN",
    "REFUTED",
    "UNVERIFIED",
    "Observable",
    "ObservableError",
    "Check",
    "BehavioralVerdict",
    "HttpResult",
    "Requester",
    "UiResult",
    "UiDriver",
    "run",
    "run_api_check",
    "run_ui_check",
    "run_journey_check",
    "run_function_check",
    "observable_from_env",
    "main",
    "register_surface_handler",
    "get_surface_handler",
]

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

_SURFACES = ("api", "ui", "journey", "function")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_METAMORPHIC = ("idempotent_repeat", "order_invariant", "filtered_subset_of_unfiltered")


class ObservableError(ValueError):
    """A malformed ``observable`` descriptor (bad surface, path escape, …)."""


def _is_json_data(value: Any) -> bool:
    """Return whether ``value`` is composed only of plain JSON data."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_data(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_data(item)
            for key, item in value.items()
        )
    return False


@dataclass
class HttpResult:
    """Transport-agnostic response envelope the requester returns.

    ``ok_transport`` separates "the surface answered (even with a 500)" from
    "we could not reach the surface at all" — the former can REFUTE, the latter
    can only ABSTAIN.
    """

    status_code: int
    body: Any
    text: str
    ok_transport: bool
    error: str = ""


# A requester runs one HTTP exchange. Injected in tests; the default uses httpx.
Requester = Callable[..., HttpResult]


@dataclass
class UiResult:
    """Browser-driver result envelope for a live UI surface."""

    ok_transport: bool
    url: str
    visible_text: str
    current_url: str
    error: str = ""


UiDriver = Callable[..., UiResult]


@dataclass
class Check:
    """One evaluated acceptance check. ``ok=None`` means 'could not evaluate'."""

    name: str
    ok: Optional[bool]
    detail: str = ""


@dataclass
class _SchemaCheck:
    """Result of :func:`_validate_json_schema` (lazy-import guarded).

    Returned shape stays uniform whether the validator runs on real JSON Schema
    (jsonschema installed) or falls back to the stdlib-only ``type``+``required``
    probe — callers only ever see ``ok`` + ``reason``.
    """

    ok: bool
    reason: str = ""


@dataclass
class Observable:
    """Parsed ``observable`` block from a behavioral verifier_contract."""

    surface: str
    target: str = ""
    staging_url: str = ""
    method: str = "GET"
    checklist: list[str] = field(default_factory=list)
    expect_status: list[int] = field(default_factory=lambda: [200])
    expect_json_schema: dict = field(default_factory=dict)
    metamorphic: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    form: list[dict[str, str]] = field(default_factory=list)
    submit: str = ""
    expect_visible: list[str] = field(default_factory=list)
    expect_url: list[str] = field(default_factory=list)
    waits: list[str] = field(default_factory=list)
    filter: str = ""
    steps: list["Observable"] = field(default_factory=list)
    extract: dict = field(default_factory=dict)
    module: str = ""
    function: str = ""
    seed_inputs: list = field(default_factory=list)
    relations: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Any) -> "Observable":
        if not isinstance(data, dict):
            raise ObservableError(f"observable must be a mapping, got {type(data).__name__}")
        surface = str(data.get("surface") or "").strip()
        if surface not in _SURFACES:
            raise ObservableError(
                f"observable.surface must be one of {_SURFACES}, got {surface!r}"
            )
        target = str(data.get("target") or "")
        _guard_path_escape(target)
        method = str(data.get("method") or "GET").upper()
        if method not in _METHODS:
            raise ObservableError(f"observable.method must be one of {_METHODS}, got {method!r}")
        metamorphic = [str(m) for m in (data.get("metamorphic") or [])]
        for m in metamorphic:
            if m not in _METAMORPHIC:
                raise ObservableError(
                    f"unknown metamorphic relation {m!r}; known: {_METAMORPHIC}"
                )
        seed_inputs_raw = data.get("seed_inputs", [])
        seed_inputs = [] if seed_inputs_raw is None else seed_inputs_raw
        if not isinstance(seed_inputs, list):
            raise ObservableError("observable.seed_inputs must be a list")
        if any(not _is_json_data(seed) for seed in seed_inputs):
            raise ObservableError("observable.seed_inputs must contain only plain JSON data")
        relations_raw = data.get("relations", [])
        relations = [] if relations_raw is None else relations_raw
        if not isinstance(relations, list) or any(
            not isinstance(name, str) for name in relations
        ):
            raise ObservableError("observable.relations must be a list of strings")
        if surface == "function":
            from mini_ork.learning import metamorphic as mm

            unknown = [name for name in relations if name not in mm.RELATION_LIBRARY]
            if unknown:
                known = tuple(mm.RELATION_LIBRARY)
                raise ObservableError(
                    f"unknown function relation {unknown[0]!r}; known: {known}"
                )
        expect_status = [int(s) for s in (data.get("expect_status") or [200])]
        schema = data.get("expect_json_schema") or {}
        if not isinstance(schema, dict):
            raise ObservableError("observable.expect_json_schema must be a mapping")
        budget = data.get("budget") or {}
        if not isinstance(budget, dict):
            raise ObservableError("observable.budget must be a mapping")
        form = data.get("form") or []
        if not isinstance(form, list) or any(not isinstance(item, dict) for item in form):
            raise ObservableError("observable.form must be a list of mappings")
        parsed_form = [
            {"selector": str(item.get("selector") or ""), "value": str(item.get("value") or "")}
            for item in form
        ]
        expect_url_raw = data.get("expect_url") or []
        expect_url = (
            [str(expect_url_raw)]
            if isinstance(expect_url_raw, str)
            else [str(value) for value in expect_url_raw]
        )
        filter_value = str(data.get("filter") or "")
        extract_raw = data.get("extract") or {}
        if not isinstance(extract_raw, dict):
            raise ObservableError("observable.extract must be a mapping")
        extract = {
            "name": str(extract_raw.get("name") or ""),
            "path": str(extract_raw.get("path") or ""),
        }
        steps_raw = data.get("steps") or []
        if not isinstance(steps_raw, list):
            raise ObservableError("observable.steps must be a list")
        steps = [cls.from_mapping(item) for item in steps_raw]
        return cls(
            surface=surface,
            target=target,
            staging_url=str(data.get("staging_url") or ""),
            method=method,
            checklist=[str(c) for c in (data.get("checklist") or [])],
            expect_status=expect_status,
            expect_json_schema=schema,
            metamorphic=metamorphic,
            budget=budget,
            form=parsed_form,
            submit=str(data.get("submit") or ""),
            expect_visible=[str(value) for value in (data.get("expect_visible") or [])],
            expect_url=expect_url,
            waits=[str(value) for value in (data.get("waits") or [])],
            filter=filter_value,
            steps=steps,
            extract=extract,
            module=str(data.get("module") or ""),
            function=str(data.get("function") or ""),
            seed_inputs=seed_inputs,
            relations=relations,
        )


@dataclass
class BehavioralVerdict:
    """The three-valued verdict plus the checks that produced it."""

    status: str
    surface: str
    checks: list[Check] = field(default_factory=list)
    evidence: str = ""
    target: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "verifier": "behavioral",
                "surface": self.surface,
                "target": self.target,
                "status": self.status,
                "pass": self.status == PROVEN,
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
                ],
                "evidence": self.evidence,
            },
            indent=2,
        )


def _guard_path_escape(target: str) -> None:
    """Reject a target that tries to climb out of its surface (``..``).

    Mirrors the SharedDrive path-escape guard: a verifier target is untrusted
    recipe input, so a ``..`` segment (which could redirect a probe to an
    unintended host/path) is refused loudly rather than normalized away.
    """
    if not target:
        return
    parts = target.replace("\\", "/").split("/")
    if ".." in parts:
        raise ObservableError(f"observable.target must not contain '..': {target!r}")


# --------------------------------------------------------------------------- #
# HTTP requester (default = httpx, imported lazily; injectable in tests)
# --------------------------------------------------------------------------- #
def _default_requester(method: str, url: str, *, timeout: int = 30) -> HttpResult:
    try:
        import httpx  # lazy: keeps import-time dependency-free
    except Exception as e:  # pragma: no cover - environment-dependent
        return HttpResult(0, None, "", ok_transport=False, error=f"httpx unavailable: {e}")
    try:
        resp = httpx.request(method, url, timeout=timeout)
    except Exception as e:
        return HttpResult(0, None, "", ok_transport=False, error=f"{type(e).__name__}: {e}")
    body: Any = None
    try:
        body = resp.json()
    except Exception:
        body = None
    return HttpResult(resp.status_code, body, resp.text, ok_transport=True)


def _canonical(body: Any) -> str:
    try:
        return json.dumps(body, sort_keys=True, default=str)
    except Exception:
        return repr(body)


def _shape_ok(body: Any, schema: dict) -> tuple[bool, str]:
    """Minimal JSON-shape check (P0). Full JSON-Schema validation is P2.

    Checks only ``type`` (object/array) and top-level ``required`` keys — enough
    to catch a wrong-shape response without pulling a jsonschema dependency.
    """
    if not schema:
        return True, ""
    t = schema.get("type")
    if t == "object" and not isinstance(body, dict):
        return False, f"expected object, got {type(body).__name__}"
    if t == "array" and not isinstance(body, list):
        return False, f"expected array, got {type(body).__name__}"
    required = schema.get("required") or []
    if required and isinstance(body, dict):
        missing = [k for k in required if k not in body]
        if missing:
            return False, f"missing required keys: {missing}"
    return True, ""


def _validate_json_schema(body: Any, schema: dict) -> _SchemaCheck:
    """Validate ``body`` against a JSON Schema (Draft 2020-12).

    Tries to use :mod:`jsonschema` if it is importable (full Draft 2020-12
    coverage — ``$defs``, ``oneOf``, ``format``, etc.). The import is
    **lazy and per-call**: environments without ``jsonschema`` fall back to the
    stdlib-only :func:`_shape_ok` probe (type + top-level ``required``) so this
    module stays import-time-pure.

    Returns a :class:`_SchemaCheck` whose ``reason`` always names the first
    failure on the most informative path the validator exposes. Never raises
    on a well-formed schema; bad schemas surface as a one-line REFUTED with the
    jsonschema exception class name + message.
    """
    if not schema:
        return _SchemaCheck(ok=True)
    try:
        import jsonschema  # type: ignore[import-not-found]
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]
    except Exception:  # ImportError or sys.modules['jsonschema']=None guard
        ok, reason = _shape_ok(body, schema)
        return _SchemaCheck(ok=ok, reason=reason or "shape matches (fallback)")
    try:
        Draft202012Validator(schema).validate(body)
        return _SchemaCheck(ok=True)
    except jsonschema.ValidationError as e:
        return _SchemaCheck(ok=False, reason=f"{e.message} (path {list(e.path)})")
    except jsonschema.SchemaError as e:
        return _SchemaCheck(ok=False, reason=f"schema invalid: {e.message}")


_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _substitute(text: str, bindings: dict) -> str:
    """Replace ``${name}`` occurrences in ``text`` from ``bindings``.

    Unbound names are left verbatim (no KeyError, no silent empty string) so a
    missing extract in step 1 surfaces as a literal ``${user_id}`` in step 2's
    URL — easier to debug than a silent empty string.
    """
    if not text:
        return text

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        return str(bindings.get(name, match.group(0)))

    return _VAR_RE.sub(repl, text)


def _extract(body: Any, path: str) -> Any:
    """Walk a dotted path through a dict/list body. Returns None on miss."""
    if body is None or not path:
        return None
    cur: Any = body
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _amplify(
    relation: str,
    method: str,
    url: str,
    requester: Requester,
    obs: Observable,
    *,
    n: int = 3,
) -> Check:
    """Run amplified probes for a metamorphic relation (P2).

    - ``idempotent_repeat``: ``n`` probes must agree on the canonical body.
    - ``order_invariant``: two probes must agree on the set of elements (abstains
      if either body is not a list).
    - ``filtered_subset_of_unfiltered``: an unfiltered probe plus a probe with
      ``observable.filter`` applied; the filtered set must be a subset of the
      unfiltered set. Abstains when ``filter`` is not declared.

    ``n`` is capped against ``observable.budget.max_turns`` so a runaway
    relation cannot burn unbounded budget (naive computer-use verification =
    2.6–7.4M tokens/instance, WebTestBench 2603.25226). The cap is reported in
    the check detail when it fires so the user knows amplification was
    constrained.
    """
    budget_max = int((obs.budget or {}).get("max_turns") or 12)
    requested = max(1, int(n))
    capped = min(requested, max(1, budget_max))
    cap_note = "" if capped == requested else f" (capped at budget.max_turns={budget_max})"

    if relation == "idempotent_repeat":
        probes = [requester(method, url) for _ in range(capped)]
        if not all(p.ok_transport for p in probes):
            bad = next(p for p in probes if not p.ok_transport)
            return Check(relation, None, f"unreachable probe: {bad.error}")
        canonicals = {_canonical(p.body) for p in probes}
        if len(canonicals) == 1:
            return Check(relation, True, f"{capped} probes identical{cap_note}")
        return Check(
            relation,
            False,
            f"{capped} probes diverged across {len(canonicals)} distinct bodies{cap_note}",
        )

    if relation == "order_invariant":
        a = requester(method, url)
        b = requester(method, url)
        if not (a.ok_transport and b.ok_transport):
            err = a.error if not a.ok_transport else b.error
            return Check(relation, None, f"unreachable probe: {err}")
        if isinstance(a.body, list) and isinstance(b.body, list):
            sa = sorted(_canonical(x) for x in a.body)
            sb = sorted(_canonical(x) for x in b.body)
            if sa == sb:
                return Check(relation, True, "same elements across probes (order-agnostic)")
            return Check(relation, False, "element set differed across probes")
        return Check(relation, None, "order_invariant needs list responses")

    if relation == "filtered_subset_of_unfiltered":
        if not obs.filter:
            return Check(
                relation,
                None,
                "filtered_subset_of_unfiltered needs observable.filter (no filter declared)",
            )
        unfiltered = requester(method, url)
        filtered = requester(method, url, params={"filter": obs.filter})
        if not (unfiltered.ok_transport and filtered.ok_transport):
            err = unfiltered.error if not unfiltered.ok_transport else filtered.error
            return Check(relation, None, f"unreachable probe: {err}")
        if isinstance(unfiltered.body, list) and isinstance(filtered.body, list):
            uf_set = {_canonical(x) for x in unfiltered.body}
            f_set = {_canonical(x) for x in filtered.body}
            if f_set <= uf_set:
                return Check(relation, True, "filtered ⊆ unfiltered")
            return Check(relation, False, "filtered not a subset of unfiltered")
        return Check(relation, None, "filtered_subset_of_unfiltered needs list responses")

    return Check(relation, None, f"unknown metamorphic relation {relation!r}")


def _eval_metamorphic(relation: str, primary: HttpResult, secondary: HttpResult) -> Check:
    """P0 compatibility shim — two-probe evaluation of a single relation.

    Kept so any external caller pinning to the P0 signature still works; the
    live verifier path goes through :func:`run_api_check`, which calls
    :func:`_amplify` directly for richer probing and budget-aware n.
    """
    if not secondary.ok_transport:
        return Check(relation, None, f"second probe unreachable: {secondary.error}")
    if relation == "idempotent_repeat":
        same_status = primary.status_code == secondary.status_code
        same_body = _canonical(primary.body) == _canonical(secondary.body)
        if same_status and same_body:
            return Check(relation, True, "repeat produced identical status+body")
        return Check(
            relation,
            False,
            f"repeat diverged (status {primary.status_code}->{secondary.status_code}, "
            f"body_equal={same_body})",
        )
    if relation == "order_invariant":
        if isinstance(primary.body, list) and isinstance(secondary.body, list):
            a = sorted(_canonical(x) for x in primary.body)
            b = sorted(_canonical(x) for x in secondary.body)
            if a == b:
                return Check(relation, True, "same elements across probes (order-agnostic)")
            return Check(relation, False, "element set differed across probes")
        return Check(relation, None, "order_invariant needs list responses (P0 cannot evaluate)")
    # filtered_subset_of_unfiltered needs a declared filter parameter to build the
    # transformed input; P0 does not synthesize one, so it abstains (honest None).
    return Check(relation, None, f"{relation} not evaluable in P0")


def _propose_relations(module: str, function: str, fn: Callable) -> list:
    """Ask the configured model to select vetted relations for ``fn``.

    This path is runtime-only and opt-in. The proposer returns data, and the
    relation library resolves that data to audited predicates; model-authored
    code is never evaluated.
    """
    import inspect

    import mini_ork.dispatch as mo_dispatch
    from mini_ork.learning import metamorphic as mm
    from mini_ork.learning import metamorphic_proposer

    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return []
    prompt = metamorphic_proposer.build_proposer_prompt(
        f"{module}.{function}", source
    )
    model = os.environ.get("MO_BEHAV_FN_MODEL") or os.environ.get(
        "MINI_ORK_DEFAULT_MODEL", "sonnet"
    )
    try:
        result = mo_dispatch.dispatch_model(
            mo_dispatch.DispatchRequest(model=model, prompt=prompt)
        )
    except Exception:  # noqa: BLE001 - an unavailable optional lane means abstain
        return []
    if not result.ok:
        return []
    proposal = metamorphic_proposer.parse_proposal(result.text)
    return mm.resolve_relations(proposal["relations"])


def run_function_check(obs: Observable) -> BehavioralVerdict:
    """Run function-level metamorphic checks with three-valued discipline."""
    target = ".".join(part for part in (obs.module, obs.function) if part)
    if not obs.module or not obs.function:
        checks = [Check("declared", None, "no module/function declared")]
        return BehavioralVerdict(
            UNVERIFIED,
            "function",
            checks,
            evidence=_summarize(UNVERIFIED, target, checks),
            target=target,
        )

    import copy
    import importlib

    from mini_ork.learning import metamorphic as mm

    try:
        module = importlib.import_module(obs.module)
        fn = getattr(module, obs.function)
    except (ImportError, AttributeError) as exc:
        checks = [Check("reachable", None, f"{type(exc).__name__}: {exc}")]
        return BehavioralVerdict(
            UNVERIFIED,
            "function",
            checks,
            evidence=_summarize(UNVERIFIED, target, checks),
            target=target,
        )
    if not callable(fn):
        checks = [Check("reachable", None, f"target {target!r} is not callable")]
        return BehavioralVerdict(
            UNVERIFIED,
            "function",
            checks,
            evidence=_summarize(UNVERIFIED, target, checks),
            target=target,
        )

    if obs.relations:
        relations = mm.resolve_relations(obs.relations)
    elif os.environ.get("MO_BEHAV_FN_PROPOSE") == "1":
        proposed = _propose_relations(obs.module, obs.function, fn)
        proposed_names = [
            item if isinstance(item, str) else getattr(item, "name", "")
            for item in proposed
        ]
        relations = mm.resolve_relations(proposed_names)
    else:
        relations = mm.resolve_relations(
            relation.name for relation in mm.UNIVERSAL_RELATIONS
        )

    if not obs.seed_inputs:
        checks = [Check("seed_inputs", None, "no seed inputs - cannot anchor")]
        return BehavioralVerdict(
            UNVERIFIED,
            "function",
            checks,
            evidence=_summarize(UNVERIFIED, target, checks),
            target=target,
        )
    if not relations:
        checks = [Check("relations", None, "no whitelisted relations resolved")]
        return BehavioralVerdict(
            UNVERIFIED,
            "function",
            checks,
            evidence=_summarize(UNVERIFIED, target, checks),
            target=target,
        )

    result = mm.check(
        fn,
        copy.deepcopy(obs.seed_inputs),
        relations,
        check_immutability=True,
    )
    if result.checks == 0:
        checks = [Check("relations_exercised", None, "no metamorphic relation ran")]
    else:
        checks = []
        for name, slot in result.per_relation.items():
            violations = int(slot.get("violations", 0))
            relation_counterexamples = [
                counterexample
                for counterexample in result.counterexamples
                if counterexample.get("relation") == name
            ]
            if violations:
                detail = (
                    f"{violations} violation(s) across {slot.get('checks', 0)} check(s); "
                    f"counterexamples={json.dumps(relation_counterexamples, sort_keys=True)}"
                )
            else:
                detail = f"{slot.get('checks', 0)} check(s) passed"
            checks.append(Check(name, violations == 0, detail))

        relation_checks = sum(
            int(slot.get("checks", 0))
            for name, slot in result.per_relation.items()
            if name != "input_immutability"
        )
        if relation_checks == 0:
            checks.append(
                Check("relations_exercised", None, "all metamorphic relations were skipped")
            )

    status = _resolve(checks)
    return BehavioralVerdict(
        status,
        "function",
        checks,
        evidence=_summarize(status, target, checks),
        target=target,
    )


def run_api_check(obs: Observable, *, requester: Requester | None = None) -> BehavioralVerdict:
    """Probe a staging API surface and return a three-valued verdict."""
    req = requester or _default_requester
    base = os.path.expandvars(obs.staging_url)
    path = os.path.expandvars(obs.target)
    url = base + path if base else path
    if not url:
        return BehavioralVerdict(
            UNVERIFIED,
            "api",
            [Check("declared", None, "no staging_url/target declared")],
            evidence="UNVERIFIED: nothing to probe — declare observable.staging_url/target",
            target=url,
        )

    primary = req(obs.method, url)
    if not primary.ok_transport:
        return BehavioralVerdict(
            UNVERIFIED,
            "api",
            [Check("reachable", None, primary.error)],
            evidence=f"UNVERIFIED: surface unreachable at {url} ({primary.error})",
            target=url,
        )

    checks: list[Check] = []
    status_ok = primary.status_code in obs.expect_status
    checks.append(
        Check(
            "status",
            status_ok,
            f"got {primary.status_code}, expected one of {obs.expect_status}",
        )
    )
    if obs.expect_json_schema:
        sc = _validate_json_schema(primary.body, obs.expect_json_schema)
        checks.append(
            Check("json_shape", sc.ok, sc.reason or "body matches declared schema")
        )

    if obs.metamorphic:
        for relation in obs.metamorphic:
            checks.append(_amplify(relation, obs.method, url, req, obs, n=3))

    status = _resolve(checks)
    return BehavioralVerdict(
        status,
        "api",
        checks,
        evidence=_summarize(status, url, checks),
        target=url,
    )


def _default_ui_driver(
    url: str,
    *,
    form: list[dict[str, str]],
    submit: str,
    waits: list[str],
) -> UiResult:
    import shutil
    import subprocess

    executable = shutil.which("agent-browser")
    if executable is None:
        return UiResult(False, url, "", "", error="agent-browser unavailable")

    def invoke(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    try:
        opened = invoke("open", url)
        if opened.returncode != 0:
            error = opened.stderr.strip() or opened.stdout.strip() or "agent-browser open failed"
            return UiResult(False, url, "", "", error=error)
        for item in form:
            filled = invoke("fill", item["selector"], item["value"])
            if filled.returncode != 0:
                error = filled.stderr.strip() or filled.stdout.strip() or "agent-browser fill failed"
                return UiResult(False, url, "", "", error=error)
        if submit:
            clicked = invoke("click", submit)
            if clicked.returncode != 0:
                error = clicked.stderr.strip() or clicked.stdout.strip() or "agent-browser click failed"
                return UiResult(False, url, "", "", error=error)
        for wait in waits:
            waited = invoke("wait", wait)
            if waited.returncode != 0:
                error = waited.stderr.strip() or waited.stdout.strip() or "agent-browser wait failed"
                return UiResult(False, url, "", "", error=error)
        snapshot = invoke("snapshot")
        if snapshot.returncode != 0:
            error = snapshot.stderr.strip() or snapshot.stdout.strip() or "agent-browser snapshot failed"
            return UiResult(False, url, "", "", error=error)
        current_url_result = invoke("get", "url")
        current_url = current_url_result.stdout.strip() if current_url_result.returncode == 0 else url
        return UiResult(True, url, snapshot.stdout, current_url)
    except (OSError, subprocess.SubprocessError) as e:
        return UiResult(False, url, "", "", error=f"{type(e).__name__}: {e}")


def run_ui_check(obs: Observable, *, driver: UiDriver | None = None) -> BehavioralVerdict:
    """Drive a live UI surface and evaluate declared text and URL assertions."""
    drive = driver or _default_ui_driver
    base = os.path.expandvars(obs.staging_url)
    path = os.path.expandvars(obs.target)
    _guard_path_escape(path)
    url = base + path if base else path
    if not url:
        return BehavioralVerdict(
            UNVERIFIED,
            "ui",
            [Check("declared", None, "no staging_url/target declared")],
            evidence="UNVERIFIED: nothing to probe — declare observable.staging_url/target",
            target=url,
        )

    for expected in obs.expect_url:
        _guard_path_escape(expected)

    result = drive(url, form=obs.form, submit=obs.submit, waits=obs.waits)
    if not result.ok_transport:
        return BehavioralVerdict(
            UNVERIFIED,
            "ui",
            [Check("reachable", None, result.error)],
            evidence=f"UNVERIFIED: surface unreachable at {url} ({result.error})",
            target=url,
        )

    checks = [
        Check(
            f"visible:{expected}",
            expected in result.visible_text,
            f"expected visible text {expected!r}",
        )
        for expected in obs.expect_visible
    ]
    checks.extend(
        Check(
            f"url:{expected}",
            expected in result.current_url,
            f"current URL {result.current_url!r} must contain {expected!r}",
        )
        for expected in obs.expect_url
    )
    status = _resolve(checks)
    return BehavioralVerdict(
        status,
        "ui",
        checks,
        evidence=_summarize(status, result.current_url or url, checks),
        target=result.current_url or url,
    )


def _run_journey_step(
    step: Observable,
    bindings: dict,
    *,
    requester: Optional[Requester],
    driver: Optional[UiDriver],
) -> tuple[BehavioralVerdict, Any, dict]:
    """Run one journey step after ``${var}`` substitution; return (verdict, body, new_bindings).

    Dispatches the step to its registered surface handler via :func:`run` so a
    journey can mix api and ui steps. Re-issues the primary probe once after
    the handler returns to grab the response body for extraction (the handler
    returns a verdict, not the body — re-probing is the cheapest way to keep the
    surface-handler API unchanged).
    """
    target = _substitute(step.target, bindings)
    staging_url = _substitute(step.staging_url, bindings)
    _guard_path_escape(target)  # every resolved target MUST pass the guard
    # Build a substituted observable without mutating the caller's copy.
    substituted = Observable(
        surface=step.surface,
        target=target,
        staging_url=staging_url,
        method=step.method,
        checklist=list(step.checklist),
        expect_status=list(step.expect_status),
        expect_json_schema=dict(step.expect_json_schema),
        metamorphic=list(step.metamorphic),
        budget=dict(step.budget),
        form=list(step.form),
        submit=step.submit,
        expect_visible=list(step.expect_visible),
        expect_url=list(step.expect_url),
        waits=list(step.waits),
        filter=step.filter,
        steps=list(step.steps),
        extract=dict(step.extract),
    )
    verdict = run(substituted, requester=requester, driver=driver)
    body: Any = None
    if step.surface == "api" and step.extract and step.extract.get("name") and step.extract.get("path"):
        # Only re-probe when an extraction is actually needed; otherwise the
        # dispatcher's primary call is sufficient and a second probe would
        # double the surface traffic (and any budget cap).
        req = requester or _default_requester
        url = staging_url + target if staging_url else target
        if url:
            primary = req(step.method, url)
            if primary.ok_transport:
                body = primary.body
    new_bindings = dict(bindings)
    if step.extract and step.extract.get("name") and step.extract.get("path"):
        val = _extract(body, step.extract["path"])
        if val is not None:
            new_bindings[step.extract["name"]] = val
    return verdict, body, new_bindings


def run_journey_check(
    obs: Observable,
    *,
    requester: Requester | None = None,
    driver: UiDriver | None = None,
) -> BehavioralVerdict:
    """Run a multi-step journey (api/ui sequence with ${var} threading).

    Each step is a nested :class:`Observable` whose ``target`` and
    ``staging_url`` may reference ``${name}`` placeholders. Names are bound
    from prior steps via ``extract: {name, path}`` (dotted path into the step's
    response body). Every substituted target passes through
    :func:`_guard_path_escape` so a malicious or buggy extraction cannot route
    a later probe out of the surface (``..`` segments are rejected loudly).

    Short-circuits at the first REFUTED step (no point burning budget on later
    steps when an earlier one has already broken the contract).
    """
    if not obs.steps:
        return BehavioralVerdict(
            UNVERIFIED,
            "journey",
            [Check("declared", None, "no journey steps declared")],
            evidence="UNVERIFIED: declare observable.steps[] for a journey surface",
            target=obs.target,
        )

    bindings: dict = {}
    checks: list[Check] = []
    for idx, step in enumerate(obs.steps):
        verdict, _body, bindings = _run_journey_step(
            step, bindings, requester=requester, driver=driver,
        )
        step_label = f"step[{idx}]:{step.surface}"
        if verdict.status == PROVEN:
            checks.append(Check(step_label, True, f"step {idx} PROVEN"))
        elif verdict.status == REFUTED:
            checks.append(Check(step_label, False, f"step {idx} REFUTED: {verdict.evidence}"))
            return BehavioralVerdict(
                REFUTED,
                "journey",
                checks,
                evidence=_summarize(REFUTED, obs.target, checks),
                target=obs.target,
            )
        else:
            checks.append(Check(step_label, None, f"step {idx} UNVERIFIED: {verdict.evidence}"))
            return BehavioralVerdict(
                UNVERIFIED,
                "journey",
                checks,
                evidence=_summarize(UNVERIFIED, obs.target, checks),
                target=obs.target,
            )

    status = _resolve(checks)
    return BehavioralVerdict(
        status,
        "journey",
        checks,
        evidence=_summarize(status, obs.target, checks),
        target=obs.target,
    )


def _resolve(checks: list[Check]) -> str:
    """REFUTED if any check failed; else UNVERIFIED if any abstained; else PROVEN.

    Order matters: a genuine failure (False) outranks an abstention (None), so a
    surface that is both broken and partly-unevaluable still REFUTES rather than
    hiding the break behind an abstain.
    """
    if any(c.ok is False for c in checks):
        return REFUTED
    if any(c.ok is None for c in checks):
        return UNVERIFIED
    return PROVEN


def _summarize(status: str, url: str, checks: list[Check]) -> str:
    lines = [f"{status}: {url}"]
    for c in checks:
        mark = {True: "PASS", False: "FAIL", None: "ABSTAIN"}[c.ok]
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    return "\n".join(lines)


def _unimplemented_surface(obs: Observable, *, requester: Requester | None = None) -> BehavioralVerdict:
    return BehavioralVerdict(
        UNVERIFIED,
        obs.surface,
        [Check("surface_supported", None, f"surface {obs.surface!r} not implemented in P0")],
        evidence=f"UNVERIFIED: surface {obs.surface!r} lands in a later phase (P0 = api only)",
        target=obs.target,
    )


# --------------------------------------------------------------------------- #
# Surface-handler registry (OCP seam: P1 ui / P2 journey register here)
# --------------------------------------------------------------------------- #
SurfaceHandler = Callable[..., BehavioralVerdict]

_SURFACE_HANDLERS: dict[str, SurfaceHandler] = {}


def register_surface_handler(surface: str, handler: SurfaceHandler) -> None:
    """Register a handler for an observable surface (last write wins).

    Mirrors ``register_workspace_backend`` / ``register_node_handler``: P1 adds
    ``ui`` (agentbrowser) and P2 adds ``journey`` without editing this module.
    """
    _SURFACE_HANDLERS[surface] = handler


def get_surface_handler(surface: str) -> SurfaceHandler:
    handler = _SURFACE_HANDLERS.get(surface)
    if handler is None:
        known = ", ".join(sorted(_SURFACE_HANDLERS)) or "(none)"
        raise ObservableError(f"unknown observable surface {surface!r}; registered: {known}")
    return handler


def run(
    obs: Observable,
    *,
    requester: Requester | None = None,
    driver: UiDriver | None = None,
) -> BehavioralVerdict:
    """Dispatch an observable to its registered surface handler."""
    import inspect

    handler = get_surface_handler(obs.surface)
    parameters = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    if "requester" in parameters:
        kwargs["requester"] = requester
    if "driver" in parameters:
        kwargs["driver"] = driver
    return handler(obs, **kwargs)


# --------------------------------------------------------------------------- #
# Env → Observable, and the CLI entrypoint used by verifiers/*.py
# --------------------------------------------------------------------------- #
def _load_spec_file(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # lazy

        return yaml.safe_load(text) or {}
    return json.loads(text)


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def _csv(raw: str) -> list[str]:
    return [x for x in (s.strip() for s in raw.split(",")) if x]


def _json_seed_inputs(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservableError(
            f"MO_BEHAV_SEED_INPUTS must be valid JSON: {exc.msg}"
        ) from exc


def observable_from_env(env: Mapping[str, str] | None = None) -> Observable | None:
    """Build an Observable from the environment, or None if none is declared.

    Precedence: ``MO_OBSERVABLE_SPEC`` (a yaml/json descriptor file) wins;
    otherwise the ``MO_BEHAV_*`` vars. Returning None (no ``MO_OBSERVABLE_SPEC``
    and no ``MO_BEHAV_SURFACE``) is the opt-in no-op: a recipe that never
    configures a behavioral surface never runs one.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    spec = source.get("MO_OBSERVABLE_SPEC")
    if spec:
        return Observable.from_mapping(_load_spec_file(spec))
    surface = source.get("MO_BEHAV_SURFACE")
    if not surface:
        return None
    return Observable.from_mapping(
        {
            "surface": surface,
            "staging_url": source.get("MO_BEHAV_STAGING_URL", ""),
            "target": source.get("MO_BEHAV_TARGET", ""),
            "method": source.get("MO_BEHAV_METHOD", "GET"),
            "expect_status": _int_list(source.get("MO_BEHAV_EXPECT_STATUS", "200")),
            "metamorphic": _csv(source.get("MO_BEHAV_METAMORPHIC", "")),
            "module": source.get("MO_BEHAV_MODULE", ""),
            "function": source.get("MO_BEHAV_FUNCTION", ""),
            "seed_inputs": _json_seed_inputs(
                source.get("MO_BEHAV_SEED_INPUTS", "[]")
            ),
            "relations": _csv(source.get("MO_BEHAV_RELATIONS", "")),
        }
    )


def _exit_code(status: str, abstain_exit: int) -> int:
    if status == PROVEN:
        return 0
    if status == REFUTED:
        return 1
    return abstain_exit  # UNVERIFIED


def main(argv: list[str] | None = None, *, requester: Requester | None = None) -> int:
    """CLI entrypoint for ``verifiers/*.py`` behavioral verifiers.

    Reads the observable from the environment, runs it, prints the verdict JSON
    to stdout (always non-empty so the dispatcher never treats it as vacuous),
    and returns the exit code per the honest-abstain mapping.
    """
    abstain_exit = 0 if os.environ.get("MO_BEHAV_ABSTAIN_EXIT") == "0" else 1
    try:
        obs = observable_from_env()
    except ObservableError as e:
        verdict = BehavioralVerdict(
            UNVERIFIED, "unknown", [Check("descriptor", None, str(e))],
            evidence=f"UNVERIFIED: bad observable descriptor — {e}",
        )
        sys.stdout.write(verdict.to_json() + "\n")
        return abstain_exit
    if obs is None:
        verdict = BehavioralVerdict(
            UNVERIFIED, "none", [Check("declared", None, "no observable declared")],
            evidence="UNVERIFIED: no observable declared (set MO_OBSERVABLE_SPEC or MO_BEHAV_*)",
        )
        sys.stdout.write(verdict.to_json() + "\n")
        return abstain_exit

    verdict = run(obs, requester=requester)
    sys.stdout.write(verdict.to_json() + "\n")
    return _exit_code(verdict.status, abstain_exit)


# Built-in surfaces. Registering handlers in a dict is the only import-time
# effect (no I/O, no env mutation) — the same shape sandbox.py uses.
register_surface_handler("api", run_api_check)
register_surface_handler("ui", run_ui_check)
register_surface_handler("journey", run_journey_check)
register_surface_handler("function", run_function_check)


if __name__ == "__main__":
    raise SystemExit(main())
