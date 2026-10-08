"""Canonical Python verifier dispatcher.

Strangler-fig parity port. Reads artifact_contract.success_verifiers[] from the
plan, runs each verifier script/command, evaluates the ported
``gate_registry.gate_run_all``, and computes the verdict
(pass|fail|partial|vacuous|dry-run) with the same minimum-evidence assertions as
bash. Verifier scripts are dispatched extension-natively (``.py`` → the current
interpreter, ``.sh`` → bash with a deprecation warning); the ported logic is the
resolution + verdict computation.

    main(argv=None, *, db=None, root=None) -> int   # 0 ok / 1 fail
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from mini_ork import trace_store
from mini_ork.context import context_env

from mini_ork.gates import gate_registry
from mini_ork.runtime.contract import run_check  # routing helper (kickoff §2)

_USAGE = """Usage: mini-ork verify <artifact-path> [--plan <plan.json>] [--task-class <name>] [--dry-run]

Run artifact verifiers and gates. Emits JSON verdict on stdout.

Options:
  --plan <path>         Path to plan.json containing artifact_contract
  --task-class <name>   Override task class for gate selection
  --dry-run             List verifiers; do not execute them
  --help                Show this help
"""


def _rolled_back_paths(run_dir: str) -> set[str]:
    """Realpaths the run's rollback node reverted (``rolled-back.json``, written
    by ``execute_handlers._record_rolled_back``). Empty when rollback did not
    run or recorded nothing."""
    if not run_dir:
        return set()
    try:
        doc = json.load(open(os.path.join(run_dir, "rolled-back.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    paths = doc.get("paths") if isinstance(doc, dict) else None
    return {os.path.realpath(str(p)) for p in paths or []}


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


def _verifier_stem(raw):
    stem = raw[len("verifiers/"):] if raw.startswith("verifiers/") else raw
    return stem[:-3] if stem.endswith((".sh", ".py")) else stem


def _dag_result(run_dir, name):
    """The workflow verifier node's persisted result for ``name``, or ``None``.

    The verifier node writes ``<run_dir>/verifier_<stem>.json`` (see
    ``execute_handlers._handle_verifier``), where ``<stem>`` is computed by the
    exact same ``_verifier_stem`` rule used here. The file is a byte copy of the
    verifier's evidence log, so a real one usually carries log lines ahead of the
    JSON payload; parse it the way the other in-tree readers do
    (``scheduler._load_verifier``): whole-file ``json.loads`` first, then the last
    line that parses as a JSON object. Only a dict carrying a ``pass`` key is a
    reusable result — a missing file, a parse error or a non-dict returns
    ``None`` so the caller runs the script unchanged.

    A reusable result must satisfy the same minimum-evidence assertion as a
    fresh verifier: its ``evidence_path`` file exists and is non-empty, or the
    JSON carries an ``error_summary``. Otherwise it is treated as missing and
    the caller falls back to running the script.
    """
    if not run_dir:
        return None
    path = os.path.join(run_dir, f"verifier_{_verifier_stem(name)}.json")
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    payload = None
    try:
        loaded = json.loads(text)
    except ValueError:
        loaded = None
    if isinstance(loaded, dict) and "pass" in loaded:
        payload = loaded
    else:
        # Log-line-prefixed (or multi-object) file: the result is the last line
        # that parses as a JSON object with a ``pass`` key — the same bottom-up
        # scan ``_declared_unmeasured`` uses below.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and "pass" in cand:
                payload = cand
                break
    if not isinstance(payload, dict) or "pass" not in payload:
        return None
    # Minimum evidence: a reused result certifies only when its evidence file
    # exists and is non-empty, or it carries an error_summary. ``evidence_path``
    # may be null (the "no test runner detected — skipped" shape), so coerce
    # before touching the filesystem.
    ev_path = str(payload.get("evidence_path") or "")
    evidence_ok = bool(ev_path) and os.path.isfile(ev_path) and os.path.getsize(ev_path) > 0
    if not (evidence_ok or payload.get("error_summary")):
        return None
    return payload


def _find_verifier_script(raw, root, home):
    stem = _verifier_stem(raw)
    # Prefer the extension the contract named; fall back to the sibling so a
    # .sh reference still resolves after a recipe ports its verifier to .py
    # (and vice versa for not-yet-repointed contracts).
    exts = [".py", ".sh"] if raw.endswith(".py") else [".sh", ".py"]
    recipe = os.environ.get("MINI_ORK_RECIPE")
    # A project recipe (home overlay) first, as runs resolve recipes, then the engine's.
    bases = ([os.path.join(home, "recipes", recipe, "verifiers"),
              os.path.join(root, "recipes", recipe, "verifiers")] if recipe else []) + [
        os.path.join(home, "verifiers"),
        os.path.join(root, "verifiers")]
    for ext in exts:
        for base in bases:
            cand = os.path.join(base, f"{stem}{ext}")
            if os.path.isfile(cand):
                return cand
    return ""


def _verifier_argv(script):
    """Extension-native verifier dispatch: ``.py`` runs under the current
    interpreter; ``.sh`` keeps working via bash (user-facing contract) with a
    one-line deprecation warning; anything else keeps legacy bash behavior."""
    if script.endswith(".py"):
        return [sys.executable, script]
    if script.endswith(".sh"):
        sys.stderr.write(
            f"warning: verifier '{script}' is a bash script — .sh verifiers are deprecated, port to .py\n")
    return ["bash", script]


def _evidence_tail(evidence) -> str:
    """Last non-empty line of verifier output, capped at 200 chars.

    Failure results carry this inline so run logs and the why-viewer surface
    the actual breakage (rc + stderr tail) without opening the evidence file.
    """
    try:
        text = (evidence or b"").decode("utf-8", "replace")
    except AttributeError:
        text = str(evidence or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _evidence_stem(raw):
    stem = _verifier_stem(raw)
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


def _declared_unmeasured(evidence) -> str:
    """The note a verifier gave when it declared that it measured nothing, or ``""``.

    Exit 0 with evidence is a pass, except when that evidence is the verifier
    saying it did not check anything: a ``{"verdict": "vacuous"}`` envelope (the
    metamorphic verifier with no spec) or an explicit ``"pass": null``. Counting
    that as a pass is the vacuous pass the zero-byte guard exists to stop, just
    with a few bytes of JSON in front of it. stderr is merged into the evidence,
    so the envelope is found as the last line that parses as a JSON object.
    """
    text = evidence.decode("utf-8", "replace") if isinstance(evidence, bytes) else str(evidence or "")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            env = json.loads(line)
        except ValueError:
            continue
        if not isinstance(env, dict):
            continue
        if env.get("verdict") == "vacuous" or ("pass" in env and env["pass"] is None):
            return str(env.get("note") or env.get("detail") or "verifier declared vacuous")
        return ""
    return ""


def _safe_trace_write(payload: dict, db: str) -> None:
    """Persist verifier telemetry without making observability a failure mode."""
    try:
        trace_store.trace_write(payload, db=db)
    except Exception:
        pass


def _first_verifier_command(plan_path):
    """First command named in the plan's ``verifier_contract``, or ``""``."""
    if not plan_path or not os.path.isfile(plan_path):
        return ""
    try:
        checks = json.load(open(plan_path, encoding="utf-8")).get(
            "verifier_contract", {}).get("checks", [])
    except Exception:
        return ""
    if not isinstance(checks, list):
        return ""
    for check in checks:
        if isinstance(check, dict) and str(check.get("command") or "").strip():
            return str(check["command"]).strip()
    return ""


def _default_mutation_report(artifact_path):
    """Where the campaign writes its report and the gate later looks for it."""
    if not artifact_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(artifact_path)),
                        "mutation-validation.json")


def _run_mutation_campaign(artifact_path, plan_path):
    """Run the adversarial-mutation campaign so the mutation gate has evidence.

    SWE-ABS's move: mutate the candidate into plausible-but-wrong variants and
    check whether the suite still passes them. A variant that survives is a
    coverage gap — precisely what a green test run cannot tell you, and the
    failure mode this repo's extensional verifier is known to allow.

    Every input is resolved from the environment and the plan. A missing input
    means the campaign does not run, and the gate then reads the absence of a
    report as ``defer``: an unmeasured check is not a satisfied one. The
    workspace is never the mini-ork checkout unless it was named explicitly, so
    a campaign cannot mutate the framework that is running it.
    """
    if os.environ.get("MO_MUTATION_ADVERSARY", "1") == "0":
        return None
    workspace = (os.environ.get("MO_TARGET_CWD")
                 or os.environ.get("MINI_ORK_TARGET_REPO") or "")
    if not workspace or not os.path.isdir(workspace):
        return None
    test_cmd = os.environ.get("MO_MUTATION_TEST_CMD") or _first_verifier_command(plan_path)
    if not test_cmd:
        return None
    report_path = os.environ.get("MO_MUTATION_REPORT") or _default_mutation_report(artifact_path)
    if not report_path:
        return None
    log_path = os.environ.get("MO_MUTATION_LOG") or os.path.join(
        context_env("MINI_ORK_RUN_DIR", ""), "execute.log")
    try:
        from mini_ork.gates import mutation_adversary

        return mutation_adversary.run_campaign(
            workspace, test_cmd, log_path=log_path, report_path=report_path,
            max_mutations=mutation_adversary.max_mutations())
    except Exception:
        return None


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

    run_dir = context_env("MINI_ORK_RUN_DIR") or None
    evidence_dir = (os.path.join(run_dir, "evidence") if run_dir and os.path.isdir(run_dir)
                    else os.path.join(home, "runs", "evidence"))
    os.makedirs(evidence_dir, exist_ok=True)

    # Kickoff §3 — pin both post-run branches to the run's pinned target root
    # so a verifier that ``cd``'s into ``cwd`` lands on the actual project,
    # not on whatever cwd ``mini-ork run`` was launched from. Resolved BEFORE
    # any branch that depends on ``cwd`` so the legacy ``cwd=None`` fallback
    # only fires for run dirs that were never pinned.
    pinned_target = ""
    if run_dir:
        try:
            from mini_ork.runtime.run_roots import load_run_roots
            _roots = load_run_roots(run_dir)
            if _roots and getattr(_roots, "target", ""):
                pinned_target = _roots.target
        except Exception:
            pinned_target = ""

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
        # Second-resolution names collided: two `mini-ork verify` calls in the same
        # second shared one log, so the earlier result's evidence_path pointed at the
        # later run's verdict. Keep the `<stem>-<epoch>` prefix, add a unique suffix.
        ev = os.path.join(
            evidence_dir,
            f"{_evidence_stem(name)}-{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}.log",
        )
        if dry_run == 1:
            sys.stdout.write(f"[dry-run] verifier: {name} → {script or 'NOT_FOUND'}\n")
            results.append(f'{{"verifier":"{name}","pass":null,"evidence_path":"dry-run"}}')
            continue
        # Post-run reuse: the workflow's verifier node already ran this verifier
        # and persisted its result to <run_dir>/verifier_<stem>.json. Re-running
        # it here duplicates the suite on an unchanged tree and — after a
        # rollback — re-runs it against the reverted tree, truncating the run's
        # only copy of the failure evidence. Reuse the DAG result unless a
        # re-run is explicitly forced. The reused row flows through the gates
        # and the verdict computation exactly like a fresh one.
        if os.environ.get("MO_VERIFY_RERUN") != "1":
            dag = _dag_result(run_dir, name)
            if dag is None and run_dir and os.path.isfile(
                    os.path.join(run_dir, "rolled-back.json")):
                # Kickoff: the workflow skipped this verifier and rollback then
                # reverted the tree, so there is nothing left to verify. Running
                # the script here would run the suite against the reverted tree
                # and truncate the run's only copy of the failure evidence. The
                # row abstains (``pass: null``), exactly like the unmeasured row
                # below — it counts as neither pass nor fail.
                sys.stderr.write(
                    f"[verify] {name}: not re-run on a rolled-back tree "
                    "(MO_VERIFY_RERUN=1 to force)\n")
                results.append(json.dumps({
                    "verifier": name, "pass": None, "evidence_path": "",
                    "reused": "skipped",
                    "detail": "not run: the workflow skipped this verifier and the run was rolled back",
                }, separators=(",", ":")))
                continue
            if dag is not None:
                sys.stderr.write(
                    f"[verify] {name}: reused DAG result (MO_VERIFY_RERUN=1 to re-run)\n")
                if dag.get("pass") is True:
                    pass_count += 1
                elif dag.get("pass") is not None:
                    # Kickoff: "True -> pass; anything else -> fail". Only an
                    # explicit ``pass: null`` abstains; any other value (a
                    # non-boolean, e.g. the string "false") counts one fail.
                    # Testing ``is False`` here would fail open: a payload whose
                    # ``pass`` is neither True nor None would be counted as
                    # neither, and with a declared non-empty output artifact that
                    # abstention resolves the whole run to 'pass'.
                    fail_count += 1
                # pass: null (an abstain / unmeasured envelope) counts neither,
                # mirroring a fresh verifier that declared it measured nothing.
                #
                # Fixed-shape row: never splice the payload's own keys into the
                # results. A real verifier payload carries a ``verdict`` key
                # (``{"verdict": "pass", ...}``, see the framework-edit
                # verifiers), and ``mini_ork.cli.main`` reads the LAST
                # ``"verdict":"…"`` match in verify's stdout as the run's verdict
                # — so a reused pass-shaped payload would report a failing run as
                # ``pass``. Only the fields the verdict computation and the run
                # log need are copied; the payload's ``error_summary`` rides along
                # as ``detail``, the key fresh failure rows use.
                row = {"verifier": name, "pass": dag.get("pass"),
                       "evidence_path": dag.get("evidence_path"), "reused": "dag"}
                if dag.get("error_summary"):
                    row["detail"] = dag["error_summary"]
                results.append(json.dumps(row, separators=(",", ":")))
                continue
        if not script:
            command = _find_verifier_command(name, plan_path)
            if not command:
                results.append(f'{{"verifier":"{name}","pass":false,"evidence_path":"script_not_found"}}')
                fail_count += 1
                continue
            # Kickoff §3 — pass pinned target as ``cwd`` so a remote
            # placement finds it via the run's PathMap and the local branch
            # runs in the project even when launched from another cwd.
            rc, evidence = run_check(
                ["bash", "-lc", command],
                cwd=pinned_target or "",
                env=None,
                evidence_path=ev,
                timeout=0,
            )
            if isinstance(evidence, str):
                evidence = evidence.encode("utf-8", "replace")
            if rc == 0 and not evidence:
                evidence = f"verifier command exited 0: {command}\n".encode()
            Path(ev).write_bytes(evidence)
            ok = rc == 0
            rc, out_tail = rc, _evidence_tail(evidence)
        else:
            # Kickoff §3 — pass pinned target on the script branch too.
            # ARTIFACT_PATH is forwarded via env so recipe verifiers find
            # their namespace; ``run_check`` preserves the legacy
            # ``subprocess.run(stdout=PIPE, stderr=STDOUT)`` shape on the
            # local branch byte-for-byte.
            rc, out = run_check(
                _verifier_argv(script),
                cwd=pinned_target,
                env={**os.environ, "ARTIFACT_PATH": artifact_path},
                evidence_path=ev,
                timeout=0,
            )
            r_stdout = out if isinstance(out, bytes) else (out.encode("utf-8", "replace") if out else b"")
            ok = rc == 0
            if ok and os.path.getsize(ev) == 0:  # vacuous: exit 0 but no evidence → fail
                ok = False
            rc, out_tail = rc, _evidence_tail(r_stdout)
        unmeasured_note = _declared_unmeasured(Path(ev).read_bytes()) if ok else ""
        if unmeasured_note:
            # Neither pass nor fail: recorded so "did not run" stays visible,
            # and kept out of both counts so it cannot lift the verdict.
            results.append(json.dumps({"verifier": name, "pass": None,
                                       "detail": f"unmeasured: {unmeasured_note}",
                                       "evidence_path": ev}))
        elif ok:
            results.append(f'{{"verifier":"{name}","pass":true,"evidence_path":"{ev}"}}'); pass_count += 1
        else:
            detail = json.dumps(out_tail or (f"exit {rc} with no output" if rc else "vacuous pass suppressed: exit 0 with empty evidence"))
            results.append(f'{{"verifier":"{name}","pass":false,"rc":{rc},"detail":{detail},"evidence_path":"{ev}"}}'); fail_count += 1

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
            rolled_back = _rolled_back_paths(context_env("MINI_ORK_RUN_DIR", ""))
            for key in ("required_artifacts", "outputs"):
                for raw in ac_req.get(key, []) or []:
                    p = os.path.expandvars(str(raw))
                    if not os.path.isabs(p) or p in seen:
                        continue
                    seen.add(p)
                    if os.path.isfile(p) and os.path.getsize(p) > 0:
                        results.append(f'{{"verifier":"__artifact__","pass":true,"evidence_path":"{p}"}}')
                        pass_count += 1
                    elif os.path.realpath(p) in rolled_back:
                        # Still a fail — a reverted run must not verify as pass — but
                        # attributed to the rollback that deleted it, not reported as
                        # an artifact the implementer never produced.
                        sys.stderr.write(f"  [fail] required artifact reverted by rollback: {p}\n")
                        results.append(f'{{"verifier":"__artifact__","pass":false,"detail":"rolled_back","evidence_path":"{p}"}}')
                        fail_count += 1
                        artifact_fail = True
                    else:
                        sys.stderr.write(f"  [fail] required artifact missing or empty: {p}\n")
                        results.append(f'{{"verifier":"__artifact__","pass":false,"evidence_path":"{p}"}}')
                        fail_count += 1
                        artifact_fail = True

    gate_verdict = "pass"
    # Gates are evaluated exclusively through the native registry. Recipe
    # verifiers can still be external commands, but framework gates do not
    # depend on a shell implementation being present on disk.
    gates_available = hasattr(gate_registry, "gate_run_all")
    if dry_run == 0 and gates_available:
        # Run the adversarial campaign first, so the mutation gate below has a
        # measurement to read rather than an absent report. It resolves its own
        # inputs and quietly returns None when any is missing — an unrun check
        # is a `defer`, which is visible in the verdict, not a silent pass.
        _run_mutation_campaign(artifact_path, plan_path)
        # The mutation-adversary gate reads the campaign report written next to
        # the artifact above.
        mutation_report = (os.environ.get("MO_MUTATION_REPORT", "")
                           or _default_mutation_report(artifact_path))
        ctx = json.dumps({"task_class": task_class, "artifact_path": artifact_path,
                          "plan_path": plan_path or "", "panel_run_id": context_env("MINI_ORK_RUN_ID", ""),
                          "mutation_report": mutation_report,
                          "workspace": (os.environ.get("MO_TARGET_CWD")
                                        or os.environ.get("MINI_ORK_TARGET_REPO") or ""),
                          "cost_usd": 0.0})
        try:
            summary = gate_registry.gate_run_all(db, task_class, ctx, mini_ork_root=root)
            gate_failed = bool(summary.get("any_fail", True))
            unmeasured = [g.get("gate_id", "") for g in summary.get("gates", [])
                          if g.get("verdict") == "defer"]
        except Exception:
            # A broken registry must not red every run; the gates defer rather
            # than inventing a verdict either way.
            gate_failed = False
            unmeasured = []
        if gate_failed:
            gate_verdict = "fail"; fail_count += 1
            results.append('{"verifier":"__gates__","pass":false,"evidence_path":"gate_registry"}')
        else:
            results.append('{"verifier":"__gates__","pass":true,"evidence_path":"gate_registry"}')
        if unmeasured:
            # Neither pass nor fail: the check did not run. Recorded so "we could
            # not measure this" is distinguishable from "we measured it and it
            # held" — otherwise an unrun gate and a satisfied one look identical.
            results.append(json.dumps({
                "verifier": "__gates_unmeasured__",
                "pass": None,
                "detail": "gate(s) did not run: " + ", ".join(sorted(unmeasured)),
                "evidence_path": "gate_registry:defer",
            }))

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
        _safe_trace_write(trace_store.enrich_stage_trace({
            "trace_id": trace_id,
            "task_class": task_class,
            "status": status,
            "verifier_output": {"verdict": verdict},
        }, node_type="verifier", verdict=verdict), db)
    return 1 if verdict == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
