"""Probe validity — the SDD broken-baseline rules, in core verify (I1).

Moved here from ``recipes/spec-driven-delivery/verifiers/{_sdd_common,
test-validity}.py`` so every recipe can use them (those files now delegate).
A probe is valid when:

- it is not statically vacuous (empty / ``true`` / ``exit 0``; an ``expect``
  that any output satisfies);
- it covers exactly one acceptance criterion and no other probe shares its
  command — one probe aliased across several criteria proves only one (AC1);
- it FAILS on the untouched base tree (AC2), unless tagged ``precondition``
  (must pass now) or its spec is already delivered.

:func:`publish_gate` applies those rules and AC3 — a verify that proved
nothing (no verifier node executed and passed) is never publishable — before
the publisher commits anything. It runs only with ``MO_PROBE_VALIDITY=1``
(default OFF: with the flag off nothing in core changes). Turning it on is a
separate change, after an n >= 30 A/B against the K1 frozen baseline.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

FLAG = "MO_PROBE_VALIDITY"
DEFAULT_PROBE_TIMEOUT_S = 120.0
OUTPUT_TAIL_CHARS = 2000
_REAP_TIMEOUT_S = 5.0
_VACUOUS_PROBES = frozenset({"", "true", ":", "exit", "exit 0", "/bin/true", "/usr/bin/true"})
_EXIT_ONLY_RE = re.compile(
    r"^(?:exit(?:[ _-]?(?:code|status))?|rc|return[ _-]?code)\s*(?:=|==|:|is)?\s*0\.?$", re.I)
# A string no honest probe prints: an expect that matches both it and the
# empty string is satisfied by any output.
_VACUITY_SENTINEL = "\x00sdd-vacuity-sentinel\x00"

# Named reasons the gate reports (task_runs notes, probe-validity.json, [BLOCK]).
VERIFY_VACUOUS = "verify_vacuous"
ALIASED_PROBE = "aliased_probe"
VACUOUS_PROBE = "vacuous_probe"
PASSES_ON_BASE = "probe_passes_on_base"
BASE_TREE_UNAVAILABLE = "base_tree_unavailable"


def enabled(environ: dict | None = None) -> bool:
    if environ is not None:
        return environ.get(FLAG, "0") == "1"
    from mini_ork.context import context_env

    return context_env(FLAG, "0") == "1"


# ── probe execution + pass definition (moved verbatim from _sdd_common) ─────

def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def run_cmd(argv: list[str], *, timeout: float, env: dict | None = None,
            cwd: str | os.PathLike[str] | None = None) -> dict:
    """Run ``argv`` in its own process group with captured output.

    Returns ``{exit_code, timed_out, duration_s, output, error}``; on timeout
    the group is SIGKILLed and ``exit_code`` is None.
    """
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as exc:
        return {"exit_code": None, "timed_out": False, "duration_s": 0.0, "output": "",
                "error": f"spawn failed: {exc}"}
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid)
        try:
            out, _ = proc.communicate(timeout=_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # A descendant escaped the group and still holds the pipe.
            proc.kill()
            if proc.stdout:
                proc.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_REAP_TIMEOUT_S)
            out = b""
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - start, 3),
        "output": (out or b"").decode("utf-8", errors="replace"),
        "error": None,
    }


def run_probe(probe: str, expect: str, *, timeout: float, env: dict | None = None,
              cwd: str | os.PathLike[str] | None = None) -> dict:
    """Run one probe and judge it with :func:`expect_matches`.

    Returns ``{status: PASSED|FAILED, reason, exit_code, timed_out,
    duration_s, output_tail}``.
    """
    res = run_cmd(["bash", "-c", probe], timeout=timeout, env=env, cwd=cwd)
    output = res["output"]
    if res["error"]:
        status, reason = "FAILED", res["error"]
    elif res["timed_out"]:
        status, reason = "FAILED", "timeout"
    elif expect_matches(expect, res["exit_code"], output):
        status, reason = "PASSED", "exit 0 and expect satisfied"
    elif res["exit_code"] != 0:
        status, reason = "FAILED", f"exit {res['exit_code']}"
    else:
        status, reason = "FAILED", "exit 0 but output does not satisfy expect"
    return {"status": status, "reason": reason, "exit_code": res["exit_code"],
            "timed_out": res["timed_out"], "duration_s": res["duration_s"],
            "output_tail": output[-OUTPUT_TAIL_CHARS:]}


def is_exit_only(expect: str) -> bool:
    return bool(_EXIT_ONLY_RE.match((expect or "").strip()))


def expect_matches(expect: str, exit_code, output: str) -> bool:
    """The single probe pass definition: exit 0 AND ``expect`` satisfied."""
    text = (expect or "").strip()
    if exit_code != 0 or not text:
        return False
    if is_exit_only(text):
        return True
    try:
        return re.search(text, output, re.M) is not None
    except re.error:
        return text in output


def is_vacuous_probe(probe) -> bool:
    if not isinstance(probe, str):
        return True
    norm = " ".join(probe.split())
    while norm.endswith(";"):
        norm = norm[:-1].rstrip()
    return norm in _VACUOUS_PROBES


def vacuous_expect(expect) -> str | None:
    """Why ``expect`` cannot discriminate, or None when it can."""
    if not isinstance(expect, str) or not expect.strip():
        return "expect is empty"
    if is_exit_only(expect):
        return None
    if expect_matches(expect, 0, "") and expect_matches(expect, 0, _VACUITY_SENTINEL):
        return "expect is satisfied by any output"
    return None


def classify_base_run(passed: bool, *, precondition: bool, delivered: bool,
                      run_reason: str = "") -> tuple[str, str, str | None]:
    """``(status, reason, violation)`` for a probe run on the untouched tree.

    The broken-baseline rule: a non-precondition probe must FAIL before the
    change; a precondition must PASS; an already-delivered spec may pass.
    ``run_reason`` is the probe run's own reason (kept verbatim in the row).
    """
    if precondition:
        if passed:
            return "PRECONDITION_OK", run_reason, None
        return "PRECONDITION_FAILED", run_reason, f"precondition probe does not pass now ({run_reason})"
    if passed and delivered:
        return "DELIVERED_OK", ("spec already delivered (MO_SDD_DELIVERED_SPECS); passing now "
                                "is the expected state"), None
    if passed:
        return "VACUOUS", "passes on the untouched tree", "probe already passes on the untouched tree (vacuous)"
    return "FAILS_TODAY", run_reason, None


# ── AC1: one probe per acceptance criterion, none shared ───────────────────

def _refs(probe: dict) -> list[str]:
    refs = probe.get("acceptance_refs")
    if isinstance(refs, list):
        return [str(r) for r in refs if r]
    ref = probe.get("acceptance_ref")
    return [str(ref)] if ref else []


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def aliasing_violations(probes: list[dict]) -> list[str]:
    """Probes that stand for more than one acceptance criterion.

    Either one probe lists several criteria, or several probes for different
    criteria are the same command + expect — one check passed once and was
    counted once per criterion.
    """
    out: list[str] = []
    by_body: dict[tuple[str, str], list[dict]] = {}
    for probe in probes:
        refs = _refs(probe)
        if len(refs) > 1:
            out.append(f"{ALIASED_PROBE}: {probe.get('gate_id') or '?'} covers {len(refs)} acceptance "
                       f"criteria ({', '.join(refs)})")
        if probe.get("probe"):
            by_body.setdefault((_norm(probe.get("probe")), _norm(probe.get("expect"))), []).append(probe)
    for (_body, _expect), group in by_body.items():
        refs = sorted({r for p in group for r in _refs(p)})
        if len(refs) > 1:
            ids = ", ".join(str(p.get("gate_id") or "?") for p in group)
            out.append(f"{ALIASED_PROBE}: {ids} share one probe for acceptance criteria ({', '.join(refs)})")
    return out


def coverage_violations(acceptance_ids: list[str], probes: list[dict]) -> list[str]:
    """Every acceptance criterion has exactly one probe; no probe names an unknown one."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for probe in probes:
        for ref in _refs(probe):
            if acceptance_ids and ref not in acceptance_ids:
                out.append(f"acceptance_ref '{ref}' is not a declared acceptance criterion")
            seen[ref] = seen.get(ref, 0) + 1
    for aid in acceptance_ids:
        if seen.get(aid, 0) != 1:
            out.append(f"acceptance '{aid}' has {seen.get(aid, 0)} probes, need exactly 1")
    return out


# ── AC2: probes run against the untouched base tree ────────────────────────

@contextlib.contextmanager
def base_tree(target_repo: str, base_ref: str) -> Iterator[str]:
    """A detached throwaway checkout of ``base_ref`` (removed afterwards)."""
    tmp = tempfile.mkdtemp(prefix="probe-base-")
    os.rmdir(tmp)  # git worktree add wants to create it
    subprocess.run(["git", "-C", target_repo, "worktree", "add", "--detach", "--quiet", tmp, base_ref],
                   check=True, capture_output=True, text=True)
    try:
        yield tmp
    finally:
        subprocess.run(["git", "-C", target_repo, "worktree", "remove", "--force", tmp],
                       capture_output=True, text=True)


def remap(command: str, target_repo: str, base: str) -> str:
    """Point absolute references to the target checkout at the base checkout."""
    out = command
    for path in sorted({target_repo, os.path.realpath(target_repo)}, key=len, reverse=True):
        if path:
            out = out.replace(path, base)
    return out


def run_on_base(probes: list[dict], *, target_repo: str, base_ref: str,
                timeout: float = DEFAULT_PROBE_TIMEOUT_S, env: dict | None = None,
                delivered: frozenset[str] = frozenset()) -> list[dict]:
    """Run each executable probe on the base tree; one row per probe."""
    rows: list[dict] = []
    with base_tree(target_repo, base_ref) as base:
        for probe in probes:
            if probe.get("kind", "cmd") != "cmd" or not probe.get("probe"):
                continue
            res = run_probe(remap(str(probe["probe"]), target_repo, base), str(probe.get("expect") or "exit 0"),
                            timeout=timeout, env=env, cwd=base)
            status, reason, violation = classify_base_run(
                res["status"] == "PASSED", precondition="precondition" in (probe.get("tags") or []),
                delivered=probe.get("spec_id") in delivered, run_reason=res["reason"])
            rows.append({"gate_id": probe.get("gate_id"), "acceptance_refs": _refs(probe), "status": status,
                         "reason": reason, "violation": violation, "exit_code": res["exit_code"],
                         "output_tail": res["output_tail"]})
    return rows


# ── AC3 + the pre-publish gate ─────────────────────────────────────────────

def verify_proven(db: str, run_id: str) -> tuple[bool, dict]:
    """Did any verifier node of this run execute and pass?

    A verifier ``node_end`` that is ``done`` after > 0 ms proved something; one
    that errored in 0 ms never ran (K0.5c) and proves nothing.
    """
    detail = {"verifier_nodes": 0, "passed": 0, "never_ran": 0}
    if not db or not run_id or not os.path.isfile(db):
        return False, detail
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT finish_reason, payload_json FROM run_events WHERE run_id = ? AND event_type = 'node_end'",
            (run_id,)).fetchall()
    finally:
        con.close()
    for finish_reason, payload in rows:
        try:
            p = json.loads(payload or "{}")
        except ValueError:
            continue
        if not isinstance(p, dict) or p.get("node_type") != "verifier":
            continue
        detail["verifier_nodes"] += 1
        ms = p.get("duration_ms")
        reason = finish_reason or p.get("finish_reason")
        if reason == "done" and isinstance(ms, (int, float)) and ms > 0:
            detail["passed"] += 1
        elif ms == 0:
            detail["never_ran"] += 1
    return detail["passed"] > 0, detail


def load_probes(run_dir: str, plan: dict) -> tuple[list[str], list[dict]]:
    """Acceptance ids + probes the run declared.

    Sources: plan ``verifier_contract.checks`` entries that name an
    acceptance criterion (``acceptance_ref`` / ``acceptance_refs``) and carry
    a ``command``; SDD gate files ``<run_dir>/gates/*.json``. Checks with no
    acceptance mapping are not probes of a criterion and are not judged here.
    """
    acceptance: list[str] = []
    for key in ("acceptance",):
        for item in (plan.get(key) or (plan.get("artifact_contract") or {}).get(key) or []):
            if isinstance(item, dict) and item.get("id"):
                acceptance.append(str(item["id"]))
    probes: list[dict] = []
    for check in ((plan.get("verifier_contract") or {}).get("checks") or []):
        if isinstance(check, dict) and check.get("command") and (check.get("acceptance_ref") or check.get("acceptance_refs")):
            probes.append({"gate_id": check.get("id"), "acceptance_ref": check.get("acceptance_ref"),
                           "acceptance_refs": check.get("acceptance_refs"), "probe": check["command"],
                           "expect": check.get("expect") or "exit 0", "tags": check.get("tags") or [],
                           "kind": "cmd"})
    gates_dir = Path(run_dir) / "gates"
    if gates_dir.is_dir():
        for gf in sorted(gates_dir.glob("*.json")):
            try:
                data = json.loads(gf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for probe in (data.get("probes") or []) if isinstance(data, dict) else []:
                if isinstance(probe, dict):
                    probes.append({**probe, "spec_id": data.get("spec_id")})
    return acceptance, probes


def record_note(db: str, run_id: str, note: str) -> None:
    """Append ``note`` to ``task_runs.notes`` (warns, never masks the refusal)."""
    if not db or not run_id or not os.path.isfile(db):
        return
    try:
        con = sqlite3.connect(db, timeout=15.0)
        try:
            con.execute("PRAGMA busy_timeout = 15000")
            con.execute("UPDATE task_runs SET notes = COALESCE(notes || '; ', '') || ? WHERE id = ?", (note, run_id))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        import sys

        print(f"  [warn] could not record '{note}' in task_runs.notes: {exc}", file=sys.stderr)


def publish_gate(*, run_dir: str, db: str, run_id: str, target_repo: str, plan: dict,
                 timeout: float = DEFAULT_PROBE_TIMEOUT_S) -> tuple[bool, str, dict]:
    """``(ok, reason, report)``; writes ``<run_dir>/probe-validity.json``."""
    report: dict[str, Any] = {"flag": FLAG}
    ok, reason = True, ""
    proven, report["verify"] = verify_proven(db, run_id)
    if not proven:
        ok, reason = False, VERIFY_VACUOUS
    acceptance, probes = load_probes(run_dir, plan)
    report["acceptance"], report["probes"] = acceptance, len(probes)
    if ok and probes:
        static = [f"{VACUOUS_PROBE}: {p.get('gate_id') or '?'}" for p in probes
                  if is_vacuous_probe(p.get("probe")) or vacuous_expect(p.get("expect"))]
        aliased = aliasing_violations(probes)
        coverage = coverage_violations(acceptance, probes)
        report["violations"] = static + aliased + coverage
        if aliased:
            ok, reason = False, ALIASED_PROBE
        elif static or coverage:
            ok, reason = False, VACUOUS_PROBE
    if ok and probes:
        base_ref_file = Path(run_dir) / "pre-implementer-ref"
        base_ref = base_ref_file.read_text(encoding="utf-8").strip() if base_ref_file.is_file() else ""
        if not base_ref or not target_repo:
            ok, reason = False, BASE_TREE_UNAVAILABLE
        else:
            try:
                rows = run_on_base(probes, target_repo=target_repo, base_ref=base_ref, timeout=timeout)
            except subprocess.CalledProcessError as exc:
                ok, reason = False, BASE_TREE_UNAVAILABLE
                report["base_error"] = (exc.stderr or str(exc)).strip()[-500:]
            else:
                report["base_runs"] = rows
                if any(r["violation"] for r in rows):
                    ok, reason = False, PASSES_ON_BASE
    report["ok"], report["reason"] = ok, reason
    with contextlib.suppress(OSError):
        (Path(run_dir) / "probe-validity.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return ok, reason, report
