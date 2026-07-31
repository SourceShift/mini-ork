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
    "observable_from_env",
    "main",
    "register_surface_handler",
    "get_surface_handler",
]

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

_SURFACES = ("api", "ui", "journey")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_METAMORPHIC = ("idempotent_repeat", "order_invariant", "filtered_subset_of_unfiltered")


class ObservableError(ValueError):
    """A malformed ``observable`` descriptor (bad surface, path escape, …)."""


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


def _eval_metamorphic(relation: str, primary: HttpResult, secondary: HttpResult) -> Check:
    """Evaluate one metamorphic relation from two exchanges of the surface.

    Relations that P0 cannot yet evaluate (they need an input transformation we
    do not synthesize here) return ``ok=None`` so the overall verdict abstains
    rather than falsely passing.
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
        ok, detail = _shape_ok(primary.body, obs.expect_json_schema)
        checks.append(Check("json_shape", ok, detail or "body matches declared shape"))

    if obs.metamorphic:
        secondary = req(obs.method, url)
        for relation in obs.metamorphic:
            checks.append(_eval_metamorphic(relation, primary, secondary))

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
register_surface_handler("journey", _unimplemented_surface)


if __name__ == "__main__":
    raise SystemExit(main())
