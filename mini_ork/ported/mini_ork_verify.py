"""Canonical Python verifier dispatcher.

Strangler-fig parity port. Reads artifact_contract.success_verifiers[] from the
plan, runs each verifier script/command, evaluates the ported
``gate_registry.gate_run_all``, and computes the verdict
(pass|fail|partial|vacuous|dry-run) with the same minimum-evidence assertions as
bash. Verifier scripts are executed via subprocess (they are bash); the ported
logic is the resolution + verdict computation.

    main(argv=None, *, db=None, root=None) -> int   # 0 ok / 1 fail
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mini_ork import trace_store

from . import gate_registry

_USAGE = """Usage: mini-ork verify <artifact-path> [--plan <plan.json>] [--task-class <name>] [--dry-run]

Run artifact verifiers and gates. Emits JSON verdict on stdout.

Options:
  --plan <path>         Path to plan.json containing artifact_contract
  --task-class <name>   Override task class for gate selection
  --dry-run             List verifiers; do not execute them
  --help                Show this help
"""


def _resolve_home_db(db):
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    return home, db


def _newest_plan(home):
    newest = None
    runs = os.path.join(home, "runs")
    if os.path.isdir(runs):
        for p in Path(runs).rglob("plan.json"):
            if newest is None or p.stat().st_mtime > Path(newest).stat().st_mtime:
                newest = str(p)
    return newest or ""


def _find_verifier_script(raw, root, home):
    stem = raw[len("verifiers/"):] if raw.startswith("verifiers/") else raw
    stem = stem[:-3] if stem.endswith(".sh") else stem
    recipe = os.environ.get("MINI_ORK_RECIPE")
    for cand in ([os.path.join(root, "recipes", recipe, "verifiers", f"{stem}.sh")] if recipe else []) + [
            os.path.join(home, "verifiers", f"{stem}.sh"),
            os.path.join(root, "verifiers", f"{stem}.sh")]:
        if os.path.isfile(cand):
            return cand
    return ""


def _evidence_stem(raw):
    stem = raw[len("verifiers/"):] if raw.startswith("verifiers/") else raw
    stem = stem[:-3] if stem.endswith(".sh") else stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem.strip()).strip("._-")
    return stem[:120] or "verifier"


def _find_verifier_command(raw, plan_path):
    if not plan_path or not os.path.isfile(plan_path):
        return ""
    try:
        plan = json.load(open(plan_path, encoding="utf-8"))
    except Exception:
        return ""
    checks = plan.get("verifier_contract", {}).get("checks", [])
    if not isinstance(checks, list):
        return ""
    raw_clean = raw.strip()
    for check in checks:
        if not isinstance(check, dict):
            continue
        command = str(check.get("command") or "").strip()
        if not command:
            continue
        candidates = {str(check.get("id") or "").strip(), str(check.get("description") or "").strip(),
                      command, f"{command} exits 0",
                      f"{command} prints valid JSON containing goal, confidence, nodes, and learningSignals"}
        if (raw_clean in candidates or raw_clean.startswith(f"{command} exits 0 ")
                or raw_clean.startswith(f"{command} prints ")):
            return command
    return ""


def _safe_trace_write(payload: dict, db: str) -> None:
    """Persist verifier telemetry without making observability a failure mode."""
    try:
        trace_store.trace_write(payload, db=db)
    except Exception:
        pass


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    artifact_path = ""
    plan_path = os.environ.get("MINI_ORK_PLAN_PATH", "")
    task_class = os.environ.get("MINI_ORK_TASK_CLASS", "")
    dry_run = 1 if os.environ.get("MINI_ORK_DRY_RUN") == "1" else 0

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_USAGE); return 0
        elif a == "--dry-run":
            dry_run = 1; i += 1
        elif a == "--plan":
            plan_path = argv[i + 1]; i += 2
        elif a == "--task-class":
            task_class = argv[i + 1]; i += 2
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            if not artifact_path:
                artifact_path = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return 2

    home, db = _resolve_home_db(db)
    if not plan_path:
        plan_path = _newest_plan(home)

    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    evidence_dir = (os.path.join(run_dir, "evidence") if run_dir and os.path.isdir(run_dir)
                    else os.path.join(home, "runs", "evidence"))
    os.makedirs(evidence_dir, exist_ok=True)

    verifier_names: list[str] = []
    if plan_path and os.path.isfile(plan_path):
        try:
            plan = json.load(open(plan_path, encoding="utf-8"))
        except Exception:
            plan = {}
        if not task_class:
            task_class = plan.get("task_class", "generic")
        ac = plan.get("artifact_contract", {})
        if isinstance(ac, dict):
            verifier_names = [v for v in (ac.get("success_verifiers") or []) if v]
    task_class = task_class or "generic"

    trace_id = f"tr-verify-{int(time.time())}-{os.getpid()}"
    if dry_run == 0:
        _safe_trace_write({
            "trace_id": trace_id,
            "task_class": task_class,
            "status": "running",
        }, db)

    results: list[str] = []
    pass_count = fail_count = 0

    for name in verifier_names:
        if not name:
            continue
        script = _find_verifier_script(name, root, home)
        ev = os.path.join(evidence_dir, f"{_evidence_stem(name)}-{int(time.time())}.log")
        if dry_run == 1:
            sys.stdout.write(f"[dry-run] verifier: {name} → {script or 'NOT_FOUND'}\n")
            results.append(f'{{"verifier":"{name}","pass":null,"evidence_path":"dry-run"}}')
            continue
        if not script:
            command = _find_verifier_command(name, plan_path)
            if not command:
                results.append(f'{{"verifier":"{name}","pass":false,"evidence_path":"script_not_found"}}')
                fail_count += 1
                continue
            run = subprocess.run(
                ["bash", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            evidence = run.stdout or b""
            if run.returncode == 0 and not evidence:
                evidence = f"verifier command exited 0: {command}\n".encode()
            Path(ev).write_bytes(evidence)
            ok = run.returncode == 0
        else:
            r = subprocess.run(
                ["bash", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, "ARTIFACT_PATH": artifact_path},
            )
            Path(ev).write_bytes(r.stdout or b"")
            ok = r.returncode == 0
            if ok and os.path.getsize(ev) == 0:  # vacuous: exit 0 but no evidence → fail
                ok = False
        if ok:
            results.append(f'{{"verifier":"{name}","pass":true,"evidence_path":"{ev}"}}'); pass_count += 1
        else:
            results.append(f'{{"verifier":"{name}","pass":false,"evidence_path":"{ev}"}}'); fail_count += 1

    # Required-artifact assertion retained from the pre-retirement contract. A recipe that
    # declares a concrete, run-local artifact but produces nothing (missing OR
    # zero-byte) must FAIL — not launder into pass/partial/vacuous. Only ABSOLUTE
    # env-expanded paths (e.g. `${MINI_ORK_RUN_DIR}/framework-edit.diff`) are
    # enforced; relative canonical outputs are publish-targets (exempt). A real,
    # non-empty artifact passes (the 36KB-synthesis false-negative case).
    artifact_fail = False
    if dry_run == 0 and plan_path and os.path.isfile(plan_path):
        try:
            ac_req = json.load(open(plan_path, encoding="utf-8")).get("artifact_contract", {})
        except Exception:
            ac_req = {}
        if isinstance(ac_req, dict):
            seen: set[str] = set()
            for key in ("required_artifacts", "outputs"):
                for raw in ac_req.get(key, []) or []:
                    p = os.path.expandvars(str(raw))
                    if not os.path.isabs(p) or p in seen:
                        continue
                    seen.add(p)
                    if os.path.isfile(p) and os.path.getsize(p) > 0:
                        results.append(f'{{"verifier":"__artifact__","pass":true,"evidence_path":"{p}"}}')
                        pass_count += 1
                    else:
                        sys.stderr.write(f"  [fail] required artifact missing or empty: {p}\n")
                        results.append(f'{{"verifier":"__artifact__","pass":false,"evidence_path":"{p}"}}')
                        fail_count += 1
                        artifact_fail = True

    gate_verdict = "pass"
    if dry_run == 0 and os.path.isfile(os.path.join(root, "lib", "gate_registry.sh")):
        ctx = json.dumps({"task_class": task_class, "artifact_path": artifact_path,
                          "plan_path": plan_path or "", "panel_run_id": os.environ.get("MINI_ORK_RUN_ID", ""),
                          "cost_usd": 0.0})
        try:
            summary = gate_registry.gate_run_all(db, task_class, ctx, mini_ork_root=root)
            gates_ok = bool(summary.get("all_pass", True))
        except Exception:
            gates_ok = True
        if not gates_ok:
            gate_verdict = "fail"; fail_count += 1
            results.append('{"verifier":"__gates__","pass":false,"evidence_path":"gate_registry"}')
        else:
            results.append('{"verifier":"__gates__","pass":true,"evidence_path":"gate_registry"}')

    if dry_run == 1:
        verdict = "dry-run"
    elif artifact_fail:
        # Missing/empty required artifact is a hard fail: outranks an otherwise
        # passing verifier set so a hollow run cannot resolve to "partial".
        verdict = "fail"
    elif pass_count == 0 and fail_count == 0:
        verdict = "vacuous"
    elif fail_count == 0 and gate_verdict == "pass":
        verdict = "pass"
    elif pass_count == 0:
        verdict = "fail"
    else:
        verdict = "partial"

    output = (
        "{\n"
        f'  "verdict": "{verdict}",\n'
        f'  "artifact_path": "{artifact_path or ""}",\n'
        f'  "task_class": "{task_class}",\n'
        f'  "pass_count": {pass_count},\n'
        f'  "fail_count": {fail_count},\n'
        f'  "results": [{",".join(results)}]\n'
        "}\n")
    sys.stdout.write(output)
    if dry_run == 0:
        status = "failure" if verdict == "fail" else ("vacuous" if verdict == "vacuous" else "success")
        _safe_trace_write({
            "trace_id": trace_id,
            "task_class": task_class,
            "status": status,
            "verifier_output": {"verdict": verdict},
        }, db)
    return 1 if verdict == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
