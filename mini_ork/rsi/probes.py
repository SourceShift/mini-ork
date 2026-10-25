"""Deterministic probes — the cheap, repeatable half of HARVEST.

A probe is a command whose output two cycles can be compared on. That is the
whole point: gates G7 and G9 are *differential* (``post ⊆ pre``), because two of
the probes below are already red on a healthy checkout. A probe that demanded
``rc == 0`` would make auto-merge permanently impossible, and a probe that
reported "no findings" on a crash would make it permanently safe. Both failure
modes are designed out here:

* ``error`` and ``timeout`` are first-class statuses. They never collapse into
  ``ok``, and a required probe that errors sets ``harvest_ok=False`` upstream.
* findings carry a **stable signature** (level + normalised message + path), so
  "did this commit remove a finding?" is a set comparison, not a re-read.

Nothing here decides policy or writes the ledger. ``harvest`` maps results to
candidates; ``cycle`` records them; ``gates`` compares them.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

STATUSES: tuple[str, ...] = ("ok", "findings", "error", "timeout", "skipped")

#: Prefix of a synthetic probe id: ``db:<RULE>`` re-runs one harvest rule against
#: the cycle's DB and is ``ok`` only when the rule now reports nothing. Defined
#: here, not in ``harvest``, so the probe factory below does not import the
#: module that imports it.
SYNTHETIC_PREFIX = "db:"

#: How a probe's exit code is interpreted. ``rc != 0`` alone is ambiguous:
#: pytest returns 5 for "no tests collected" (a broken selection, not a
#: failure) while garden/validate return 1 *with* parseable findings.
POLICIES: tuple[str, ...] = ("ok_only", "reported", "pytest")

Runner = Callable[..., "tuple[int, str, str]"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One parseable defect a probe reported.

    ``signature`` is what gates compare. It deliberately excludes line numbers
    and absolute paths: both drift without the defect changing, and a diff gate
    that keys on them reports churn as progress (or as regression).
    """

    level: str
    message: str
    path: str = ""

    def signature(self) -> str:
        blob = f"{self.level}|{normalize_path(self.path)}|{normalize_message(self.message)}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message, "path": self.path,
                "signature": self.signature()}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Finding":
        return cls(
            level=str(raw.get("level", "")),
            message=str(raw.get("message", "")),
            path=str(raw.get("path", "")),
        )


def normalize_path(path: str) -> str:
    """Repo-relative, forward slashes, no ``./`` — mirrored from candidates."""
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


_WS = re.compile(r"\s+")
#: Absolute filesystem paths inside a message. Replaced with ``<path>`` so two
#: worktrees reporting the same defect at different roots hash identically.
_ABS_PATH = re.compile(r"(?<![\w.-])/(?:[^\s:'\"]+/)*[^\s:'\"]+")


def normalize_message(message: str) -> str:
    text = _WS.sub(" ", str(message or "")).strip()
    return _ABS_PATH.sub("<path>", text)


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """A probe declaration. ``fn`` wins over ``argv`` when both are set.

    ``fn`` is for in-process probes (``verifier_audit.audit`` has no CLI verb);
    it returns a mapping that is treated as the probe payload with ``rc = 0``.
    """

    probe_id: str
    description: str = ""
    argv: tuple[str, ...] = ()
    fn: Callable[..., Mapping[str, Any]] | None = None
    policy: str = "ok_only"
    parser: str = "none"
    required: bool = False
    timeout_s: int = 900

    def __post_init__(self) -> None:
        if not self.probe_id:
            raise ValueError("probe_id is required")
        if self.policy not in POLICIES:
            raise ValueError(f"unknown probe policy {self.policy!r}; expected one of {POLICIES}")
        if not self.argv and self.fn is None:
            raise ValueError(f"probe {self.probe_id!r} has neither argv nor fn")

    @property
    def cmd(self) -> str:
        if self.fn is not None:
            return f"python:{getattr(self.fn, '__module__', '?')}:{getattr(self.fn, '__name__', '?')}"
        return " ".join(self.argv)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    cmd: str
    status: str
    rc: int | None
    duration_ms: int
    findings: tuple[Finding, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    stderr_digest: str = ""
    #: Carried through from the spec so downstream cannot re-guess it.
    required: bool = False

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown probe status {self.status!r}; expected one of {STATUSES}")

    @property
    def failed(self) -> bool:
        return self.status in ("error", "timeout")

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "cmd": self.cmd,
            "status": self.status,
            "rc": self.rc,
            "duration_ms": self.duration_ms,
            "findings": [f.to_dict() for f in self.findings],
            "payload": dict(self.payload),
            "stderr_digest": self.stderr_digest,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProbeResult":
        return cls(
            probe_id=str(raw["probe_id"]),
            cmd=str(raw.get("cmd", "")),
            status=str(raw["status"]),
            rc=None if raw.get("rc") is None else int(raw["rc"]),
            duration_ms=int(raw.get("duration_ms", 0)),
            findings=tuple(Finding.from_dict(f) for f in raw.get("findings") or ()),
            payload=dict(raw.get("payload") or {}),
            stderr_digest=str(raw.get("stderr_digest", "")),
            required=bool(raw.get("required", False)),
        )


# --------------------------------------------------------------------------
# Parsers. Every one returns findings for *reported defects only*; "info"
# lines are dropped on purpose. garden emits an info line per stale run
# directory — including those would make every diff gate noisy for reasons
# that have nothing to do with the commit under test.
# --------------------------------------------------------------------------

_BRACKET = re.compile(r"^\[(error|warning|info)\]\s+(.*)$")
_PYTEST_FAILED = re.compile(r"^(FAILED|ERROR)\s+([^\s:]+(?:::[^\s]+)?)\s*(?:-\s*(.*))?$")


def parse_garden(text: str) -> tuple[Finding, ...]:
    return _parse_bracket(text)


def parse_validate(text: str) -> tuple[Finding, ...]:
    return _parse_bracket(text)


def _parse_bracket(text: str) -> tuple[Finding, ...]:
    out: list[Finding] = []
    for line in text.splitlines():
        match = _BRACKET.match(line.strip())
        if not match:
            continue
        level, message = match.group(1), match.group(2).strip()
        if level == "info":
            continue
        out.append(Finding(level=level, message=message, path=_embedded_path(message)))
    return tuple(out)


#: Top-level directories that make a suffix a repo path. garden prints absolute
#: paths, and ``Candidate`` rejects them (its scope is matched against
#: ``git diff``), so a finding's path is cut back to this root.
_REPO_ROOTS: tuple[str, ...] = (
    "mini_ork/", "recipes/", "tests/", "config/", "docs/", "db/",
    "scripts/", "schemas/", "deploy/", "web/",
)


def _embedded_path(message: str) -> str:
    """First repo-relative token in a message, e.g. ``recipes/x/workflow.yaml``."""
    for token in re.findall(r"[\w./-]+\.(?:py|yaml|yml|json|md|toml|sh)", message):
        token = normalize_path(token)
        if token.startswith("/"):
            for root in _REPO_ROOTS:
                index = token.find("/" + root)
                if index >= 0:
                    token = token[index + 1:]
                    break
            else:
                continue  # absolute, but not recognisably inside the repo
        if "/" in token:
            return token
    return ""


def parse_pytest(text: str) -> tuple[Finding, ...]:
    out: list[Finding] = []
    for line in text.splitlines():
        match = _PYTEST_FAILED.match(line.strip())
        if not match:
            continue
        level, nodeid, message = match.group(1), match.group(2), (match.group(3) or "").strip()
        path = nodeid.split("::", 1)[0]
        out.append(Finding(level="error" if level == "ERROR" else "failure",
                           message=message or nodeid, path=normalize_path(path)))
    return tuple(out)


PARSERS: Mapping[str, Callable[[str], tuple[Finding, ...]]] = {
    "none": lambda _: (),
    "garden": parse_garden,
    "validate": parse_validate,
    "pytest": parse_pytest,
}


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def subprocess_runner(argv: Sequence[str], *, timeout_s: int, cwd: str,
                      env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """Default runner: no shell, no retry, output captured as text.

    ``env`` exists so a cycle can pin ``MINI_ORK_DB``/``MINI_ORK_ROOT`` for a
    probe. Without it every probe resolves its own DB, and a cycle given an
    explicit ``--db`` would harvest one database while its probes report on
    another.
    """
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def run_probe(
    spec: ProbeSpec,
    *,
    root: str | Path,
    timeout_s: int | None = None,
    runner: Runner = subprocess_runner,
) -> ProbeResult:
    """Run one probe. Never raises for a probe-side failure — that is a status."""
    root_s = str(root)
    limit = int(timeout_s if timeout_s is not None else spec.timeout_s)
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if spec.fn is not None:
        try:
            payload = dict(spec.fn() or {})
        except Exception as exc:  # noqa: BLE001 - a probe crash is data, not a crash
            return ProbeResult(
                probe_id=spec.probe_id, cmd=spec.cmd, status="error", rc=1,
                duration_ms=elapsed(),
                payload={"error": f"{type(exc).__name__}: {exc}"},
                stderr_digest=_digest(f"{type(exc).__name__}: {exc}"),
                required=spec.required,
            )
        findings = _findings_from_payload(payload)
        return ProbeResult(
            probe_id=spec.probe_id, cmd=spec.cmd,
            status="findings" if findings else "ok", rc=0,
            duration_ms=elapsed(), findings=findings, payload=payload,
            required=spec.required,
        )

    try:
        rc, stdout, stderr = runner(spec.argv, timeout_s=limit, cwd=root_s)
    except subprocess.TimeoutExpired:
        return ProbeResult(probe_id=spec.probe_id, cmd=spec.cmd, status="timeout",
                           rc=None, duration_ms=elapsed(), payload={"timeout_s": limit},
                           required=spec.required)
    except OSError as exc:
        return ProbeResult(probe_id=spec.probe_id, cmd=spec.cmd, status="error", rc=None,
                           duration_ms=elapsed(), payload={"error": str(exc)},
                           stderr_digest=_digest(str(exc)), required=spec.required)

    parser = PARSERS.get(spec.parser)
    if parser is None:
        return ProbeResult(probe_id=spec.probe_id, cmd=spec.cmd, status="error", rc=rc,
                           duration_ms=elapsed(),
                           payload={"error": f"unknown parser {spec.parser!r}"},
                           stderr_digest=_digest(stderr), required=spec.required)
    findings = parser(stdout) + parser(stderr)
    status = _classify(spec, rc, findings)

    return ProbeResult(
        probe_id=spec.probe_id, cmd=spec.cmd, status=status, rc=rc,
        duration_ms=elapsed(), findings=findings,
        payload={"stdout_tail": _tail(stdout, 40), "summary": _pytest_summary(stdout)},
        stderr_digest=_digest(stderr), required=spec.required,
    )


def _classify(spec: ProbeSpec, rc: int, findings: tuple[Finding, ...]) -> str:
    if spec.policy == "ok_only":
        return "ok" if rc == 0 else "error"
    if spec.policy == "pytest":
        # 0 green, 1 failures, 5 no-tests-collected. Treating 5 as "findings"
        # would turn a broken path list into a fake red; it is an error.
        if rc == 0:
            return "ok"
        if rc == 1:
            return "findings"
        return "error"
    # "reported": the tool says something and we parsed it, or we call it an error.
    if rc == 0:
        return "findings" if findings else "ok"
    return "findings" if findings else "error"


def _findings_from_payload(payload: Mapping[str, Any]) -> tuple[Finding, ...]:
    """Detectors return ``{"flags": [...]}`` / per-detector dicts; keep it simple."""
    out: list[Finding] = []
    flags = payload.get("flags")
    if isinstance(flags, list):
        for flag in flags:
            if isinstance(flag, str):
                out.append(Finding(level="warning", message=flag))
            elif isinstance(flag, Mapping):
                out.append(Finding(level=str(flag.get("level", "warning")),
                                   message=str(flag.get("message") or flag.get("name") or flag)))
    return tuple(out)


def _tail(text: str, lines: int) -> str:
    rows = text.strip().splitlines()
    return "\n".join(rows[-lines:])


_PYTEST_TAIL = re.compile(r"^=+\s+(.*?)\s+=+$")


def _pytest_summary(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        match = _PYTEST_TAIL.match(line.strip())
        if match and any(word in match.group(1) for word in ("passed", "failed", "error")):
            return match.group(1)
    return ""


def run_probes(
    specs: Iterable[ProbeSpec],
    *,
    root: str | Path,
    runner: Runner = subprocess_runner,
) -> tuple[ProbeResult, ...]:
    """Run serially.

    Serial is a load decision, not a simplification: probe flakiness under
    concurrent load is a known hazard in this repo, and running twelve
    processes at once would manufacture it.
    """
    return tuple(run_probe(spec, root=root, runner=runner) for spec in specs)


def _verifier_audit_spec(*, root: str | Path, db: str | Path | None,
                         timeout_s: int) -> ProbeSpec:
    """``verifier_audit.audit`` has no CLI verb, so it runs in-process.

    The DB is passed explicitly when the caller knows it (the cycle does) and
    otherwise resolved the way every other consumer resolves it. Best-effort
    per detector is the audit's own contract: a detector that cannot read
    contributes evidence, never an exception.
    """
    root_s = str(root)

    def _check() -> Mapping[str, Any]:
        import os

        from mini_ork.context import RunContext
        from mini_ork.learning import verifier_audit

        resolved = str(db) if db else (
            os.environ.get("MINI_ORK_DB")
            or RunContext.from_env().db_or_default()
            or os.path.join(root_s, ".mini-ork", "state.db")
        )
        report = verifier_audit.audit(None, resolved)
        return {
            "ok": bool(report.get("ok")),
            "db": resolved,
            "evidence": report.get("evidence") or {},
            "flags": [{"level": "error", "message": f"verifier-audit: {flag}"}
                      for flag in report.get("flags") or ()],
        }

    return ProbeSpec("verifier-audit", "verifier detectors (hack / collapse / gate fuzz)",
                     fn=_check, policy="ok_only", required=False, timeout_s=timeout_s)


def default_specs(
    *,
    root: str | Path,
    pytest_paths: Sequence[str] = (),
    timeout_s: int = 900,
    db: str | Path | None = None,
) -> tuple[ProbeSpec, ...]:
    """The default probe set, all deterministic. No network, no model.

    ``pytest_paths`` comes from config; an empty list omits the tests probe
    rather than silently running the whole suite (~60 min here). ``db`` pins
    the in-process probe (``verifier-audit``) to the database the caller
    decided on, so a cycle given ``--db`` does not audit a different one.

    ``lane-preflight`` is deliberately *not* here: its health check is a
    provider network probe, and the deterministic set must stay network-free.
    Dead lanes are caught by the dispatch-time gate instead.
    """
    bin_path = str(Path(root) / "bin" / "mini-ork")
    specs: list[ProbeSpec] = [
        ProbeSpec("garden", "recipe/naming/output drift", argv=(bin_path, "garden"),
                  policy="reported", parser="garden", required=True, timeout_s=timeout_s),
        ProbeSpec("validate", "static workflow validation", argv=(bin_path, "validate", "--strict"),
                  policy="reported", parser="validate", required=True, timeout_s=timeout_s),
        ProbeSpec("metrics", "trajectory metrics", argv=(bin_path, "metrics", "--format", "json"),
                  policy="ok_only", required=False, timeout_s=timeout_s),
        ProbeSpec("calibrate", "router calibration", argv=(bin_path, "calibrate", "--json"),
                  policy="ok_only", required=False, timeout_s=timeout_s),
        ProbeSpec("oversight", "oversight backlog", argv=(bin_path, "oversight", "--calibrate", "--json"),
                  policy="ok_only", required=False, timeout_s=timeout_s),
        _verifier_audit_spec(root=root, db=db, timeout_s=timeout_s),
    ]
    if pytest_paths:
        import sys

        specs.append(ProbeSpec(
            "tests", "targeted pytest",
            argv=(sys.executable, "-m", "pytest", "-q", *pytest_paths),
            policy="pytest", parser="pytest", required=True, timeout_s=timeout_s,
        ))
    return tuple(specs)


def synthetic_spec(rule_id: str) -> ProbeSpec:
    """The ``db:<RULE>`` success check for one harvest rule.

    A candidate's ``success_command`` is either a real probe id or one of these.
    The check re-runs the rule and passes only when it produces no candidate:
    that is the *same* predicate the harvest used to find the gap, so a fix
    "stays fixed" exactly when the gap stops firing.

    An unknown rule id raises here rather than at run time — a candidate naming
    a rule that does not exist has no success state at all, and the two places
    that construct these (``select`` by validating the candidate, ``cycle`` by
    running it) should both see the same failure.
    """
    from mini_ork.rsi.harvest import rule_ids  # local: harvest imports this module

    if rule_id not in rule_ids():
        raise LookupError(f"unknown harvest rule {rule_id!r}")

    def _check() -> Mapping[str, Any]:
        import os
        import sqlite3

        from mini_ork.rsi.harvest import run_rule

        db = os.environ.get("MINI_ORK_DB")
        if not db:
            raise RuntimeError("MINI_ORK_DB is not set; a db: probe needs the cycle's DB")
        root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
        con = sqlite3.connect(db, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            remaining = run_rule(rule_id, root, con, now=int(time.time()))
        finally:
            con.close()
        return {
            "rule": rule_id,
            "remaining": len(remaining),
            # ``flags`` is the payload shape run_probe turns into findings; one
            # flag per still-firing candidate keeps the probe's finding count
            # equal to the gap count, which is what G8 compares.
            "flags": [
                {"level": "error", "message": f"{rule_id}: {cand.title}"}
                for cand in remaining
            ],
        }

    return ProbeSpec(
        probe_id=f"{SYNTHETIC_PREFIX}{rule_id}",
        description=f"harvest rule {rule_id} reports nothing",
        fn=_check,
        policy="ok_only",
    )


def run_synthetic(
    rule_id: str,
    *,
    root: str | Path,
    db: str | Path,
    now: int,
) -> ProbeResult:
    """Run one ``db:<RULE>`` check against an explicit DB, without touching env.

    ``synthetic_spec`` reads ``MINI_ORK_DB``/``MINI_ORK_ROOT`` because the probe
    runner gives it no other channel. Tests and the cycle already hold both
    values, so they use this instead of mutating process env.
    """
    import sqlite3

    from mini_ork.rsi.harvest import run_rule

    spec = synthetic_spec(rule_id)
    started = time.monotonic()
    try:
        con = sqlite3.connect(str(db), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            remaining = run_rule(rule_id, root, con, now=int(now))
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - a probe failure is a status
        return ProbeResult(
            probe_id=spec.probe_id, cmd=spec.cmd, status="error", rc=1,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload={"error": f"{type(exc).__name__}: {exc}"},
            stderr_digest=_digest(f"{type(exc).__name__}: {exc}"),
        )
    findings = tuple(
        Finding(level="error", message=f"{rule_id}: {cand.title}") for cand in remaining
    )
    return ProbeResult(
        probe_id=spec.probe_id, cmd=spec.cmd,
        status="findings" if findings else "ok", rc=0,
        duration_ms=int((time.monotonic() - started) * 1000),
        findings=findings, payload={"rule": rule_id, "remaining": len(remaining)},
    )


__all__ = [
    "Finding",
    "PARSERS",
    "POLICIES",
    "ProbeResult",
    "ProbeSpec",
    "Runner",
    "STATUSES",
    "SYNTHETIC_PREFIX",
    "default_specs",
    "normalize_message",
    "normalize_path",
    "parse_garden",
    "parse_pytest",
    "parse_validate",
    "run_probe",
    "run_probes",
    "run_synthetic",
    "synthetic_spec",
    "subprocess_runner",
]
