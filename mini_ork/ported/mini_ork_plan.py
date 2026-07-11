"""Python port of bin/mini-ork-plan — the Planner node.

Strangler-fig parity port. Reads task_class + kickoff, builds the planner prompt
(recipe > workflow > built-in), injects learnings/CN context (best-effort),
dispatches the planner LLM, then extracts + validates the plan JSON through a
chain of quality gates (D-011/016/052 extraction, D-008b node-type check,
placeholder/parse rejection), retries recoverable invalid planner output with a
repair prompt, overlays the recipe artifact_contract, and writes plan.json + the
task_runs row.

Recoverable planner verdicts default to repair-then-fail: the port retries up
to MO_PLAN_MAX_REPAIRS times (default: 2), then rejects the plan. The old
deterministic recipe fallback is only used when MO_PLAN_DETERMINISTIC_FALLBACK=1.

The one non-deterministic seam is the LLM dispatch (``dispatch=``); MO_GIVEN_PLAN
and --dry-run skip it. Every embedded-python block (extraction, validation,
fallback, dry-run/gate placeholders, overlay, DB write) is transcribed verbatim.

    main(argv=None, *, root=None, dispatch=None) -> int
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_NODE_TYPES = {"planner", "researcher", "implementer", "reviewer", "verifier",
               "reflector", "publisher", "rollback"}
_RECOVERABLE_VERDICTS = {"parse_error", "missing_verifier_contract",
                         "bad_artifact_contract", "bad_node_types"}

# Verbatim heredoc from bin/mini-ork-plan (dry-run placeholder, inline objects).
_DRY_RUN_PLACEHOLDER = """{
  "objective": "<dry-run: not generated>",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "run_profile_path": "",
  "artifact_contract": { "outputs": [], "success_verifiers": [] },
  "verifier_contract": { "checks": [{ "id": "dry-run", "description": "dry-run placeholder" }] }
}
"""

_USAGE = """Usage: mini-ork plan <kickoff.md> [--task-class <name>] [--out <plan.json>] [--dry-run]

Generate a structured plan JSON from a kickoff file.

The plan MUST include a verifier_contract (checks[]) — planning fails if missing.

Outputs:
  <out-file>   JSON plan written to .mini-ork/runs/<run>/plan.json (or --out path)
  stdout       plan path on success

Options:
  --task-class <name>   Override task_class (default: $MINI_ORK_TASK_CLASS)
  --out <path>          Write plan to this path instead of default
  --dry-run             Print plan JSON to stdout; do not write files or DB
  --help                Show this help
"""

_BUILTIN_PROMPT = """You are a meticulous task planner. Given the kickoff document below, produce a
structured plan in JSON.

The JSON MUST have these top-level keys:
  objective          (string)
  assumptions        (string[])
  decomposition      ({id, description, node_type, depends_on[]}[])
    node_type must be one of: planner | researcher | implementer | reviewer |
                               verifier | reflector | publisher | rollback
  dependencies       ({from, to}[])
  risk_notes         (string[])
  artifact_contract  ({outputs: string[], success_verifiers: string[]})
  verifier_contract  ({checks: {id, description, command?}[]})

IMPORTANT: verifier_contract.checks must contain at least one item.
A plan without a verifier_contract is INVALID.

Respond with ONLY valid JSON. No markdown fences, no prose.

--- KICKOFF ---
{{KICKOFF_CONTENT}}
"""


# ── plan-JSON extraction (D-011/016/052) ──

def _objects(s):
    i = 0
    while True:
        start = s.find("{", i)
        if start < 0:
            return
        depth = 0
        in_str = False
        esc = False
        for j in range(start, len(s)):
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield s[start:j + 1]
                    i = j + 1
                    break
        else:
            return


def _contains_placeholder(v):
    if isinstance(v, str):
        stripped = v.strip()
        return stripped.startswith("<") and stripped.endswith(">")
    if isinstance(v, list):
        return any(_contains_placeholder(x) for x in v)
    if isinstance(v, dict):
        return any(_contains_placeholder(x) for x in v.values())
    return False


def _is_plan(obj):
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("verifier_contract"), dict):
        return False
    if not obj.get("verifier_contract", {}).get("checks"):
        return False
    if _contains_placeholder(obj):
        return False
    return any(k in obj for k in ("objective", "decomposition", "artifact_contract"))


def extract_plan_json(raw: str) -> str:
    first = None
    for chunk in _objects(raw):
        if first is None:
            first = chunk
        try:
            parsed = json.loads(chunk)
        except Exception:
            continue
        if _is_plan(parsed):
            return json.dumps(parsed, indent=2)
    return first if first is not None else raw


def validate_plan(plan_json: str) -> str:
    """Return the HAS_VERIFIER verdict string (verbatim logic)."""
    try:
        p = json.loads(plan_json)
        vc = p.get("verifier_contract", {})
        if not vc.get("checks", []):
            return "missing_verifier_contract"
        if _contains_placeholder(p):
            return "placeholder_plan"
        ac = p.get("artifact_contract", {})
        if not isinstance(ac, dict):
            return "bad_artifact_contract"
        bad = []
        for i, step in enumerate(p.get("decomposition", []) or []):
            nt = (step.get("node_type") or "").strip()
            if not nt:
                bad.append(f'step[{i}] {step.get("id", "?")}: empty node_type')
            elif nt not in _NODE_TYPES:
                bad.append(f'step[{i}] {step.get("id", "?")}: node_type={nt!r} not in {sorted(_NODE_TYPES)}')
        if bad:
            sys.stderr.write("bad_node_types:" + "|".join(bad) + "\n")
            return "bad_node_types"
        return "ok"
    except Exception as e:
        sys.stderr.write(f"parse_error:{e}\n")
        return "parse_error"


def _detect_truncation(raw: str) -> bool:
    depth = 0
    in_str = False
    esc = False
    for c in (raw or "").rstrip()[-200:]:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    return depth > 0


def _build_repair_prompt(root, kickoff, workflow, profile_path, raw_invalid, verdict, truncated) -> str:
    original = _build_prompt(root, kickoff, workflow, profile_path)
    truncation_note = "yes" if truncated else "no"
    return f"""{original}

--- INVALID PLANNER OUTPUT ---
{raw_invalid or ""}
--- /INVALID PLANNER OUTPUT ---

The previous planner output was rejected with verdict: {verdict}
Trailing-window truncation suspected: {truncation_note}

Return a corrected planner plan using this required JSON schema:
  objective: string
  decomposition: array of steps, each with node_type set to one of planner, researcher, implementer, reviewer, verifier, reflector, publisher, rollback
  verifier_contract.checks: non-empty array
  artifact_contract: object

No prose, no markdown fences, return ONLY valid JSON.
"""


def recipe_fallback_plan(recipe, workflow_path, root, kickoff) -> str | None:
    if not recipe or not workflow_path or not os.path.isfile(workflow_path):
        return None
    import yaml
    workflow = yaml.safe_load(Path(workflow_path).read_text(encoding="utf-8")) or {}
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    contract_path = Path(root) / "recipes" / recipe / "artifact_contract.yaml"
    contract = {}
    if contract_path.exists():
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    outputs = contract.get("outputs") or workflow.get("outputs") or []
    success_verifiers = contract.get("success_verifiers") or workflow.get("success_verifiers") or []
    plan = {
        "objective": f"Execute recipe {recipe} for {kickoff}",
        "assumptions": [
            "Recipe workflow.yaml is the source of truth for dispatch order.",
            "Planner LLM output was invalid JSON, so mini-ork generated this deterministic recipe plan.",
        ],
        "decomposition": [
            {"id": n.get("name"),
             "description": n.get("description") or f"{n.get('type', 'unknown')} node {n.get('name')}",
             "node_type": n.get("type"),
             "depends_on": [e.get("from") for e in edges if e.get("to") == n.get("name") and e.get("from")]}
            for n in nodes if isinstance(n, dict) and n.get("name")],
        "dependencies": [{"from": e.get("from"), "to": e.get("to")}
                         for e in edges if isinstance(e, dict) and e.get("from") and e.get("to")],
        "risk_notes": [
            "Fallback plan does not include model-authored verifier shell commands.",
            "Execution still uses the recipe workflow nodes and recipe verifier_ref scripts.",
        ],
        "artifact_contract": {"outputs": outputs, "success_verifiers": success_verifiers},
        "verifier_contract": {"checks": [
            {"id": "recipe-workflow-dispatch",
             "description": f"Dispatch every node declared in recipes/{recipe}/workflow.yaml."},
            {"id": "recipe-artifacts",
             "description": "Recipe artifact contract is satisfied by execute/verify."}]},
    }
    return json.dumps(plan, indent=2)


def overlay_plan(plan_json, task_class, profile_path, root) -> str:
    try:
        p = json.loads(plan_json)
    except Exception:
        return plan_json
    p.setdefault("task_class", task_class)
    recipe = ""
    if profile_path and os.path.isfile(profile_path):
        try:
            recipe = (json.load(open(profile_path)).get("recipe") or "").strip()
        except Exception:
            recipe = ""
    contract_yaml = os.path.join(root, "recipes", recipe, "artifact_contract.yaml") if recipe else ""
    if contract_yaml and os.path.isfile(contract_yaml):
        try:
            import yaml
            recipe_contract = yaml.safe_load(open(contract_yaml)) or {}
            recipe_verifiers = recipe_contract.get("success_verifiers") or []
            if recipe_verifiers:
                ac = p.get("artifact_contract")
                if not isinstance(ac, dict):
                    ac = {}
                prose = ac.get("success_verifiers") or []
                if prose and prose != recipe_verifiers:
                    ac.setdefault("acceptance_criteria", prose)
                ac["success_verifiers"] = recipe_verifiers
                if recipe_contract.get("outputs"):
                    ac.setdefault("outputs", recipe_contract["outputs"])
                p["artifact_contract"] = ac
        except Exception:
            pass
    return json.dumps(p, indent=2)


def _charge_cost(db, run_id):
    if not (db and os.path.isfile(db) and run_id):
        return
    import sqlite3
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("UPDATE task_runs SET cost_usd=COALESCE(cost_usd,0)+0.05, updated_at=? WHERE id=?",
                    (int(time.time()), run_id))
        con.commit(); con.close()
    except Exception:
        pass


def _mark_failed(db, run_id, verdict):
    if not (db and os.path.isfile(db) and run_id):
        return
    import sqlite3
    try:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        now = int(time.time())
        con.execute("UPDATE task_runs SET status='failed', verdict=COALESCE(verdict, ?), "
                    "updated_at=?, ended_at=COALESCE(ended_at, ?) WHERE id=?",
                    (verdict, now, now, run_id))
        con.commit(); con.close()
    except Exception:
        pass


def _preserve_raw(home, run_id, verdict, raw, sanitized):
    if not (home and run_id):
        return
    d = os.path.join(home, "runs", run_id)
    os.makedirs(d, exist_ok=True)
    try:
        open(os.path.join(d, f"plan-failure-{verdict}.raw.txt"), "w").write(raw or "")
        open(os.path.join(d, f"plan-failure-{verdict}.sanitized.txt"), "w").write(sanitized or "")
    except OSError:
        pass
    sys.stderr.write(f"[D-015 forensics preserved at {d}/plan-failure-{verdict}.*]\n")


def _db_write(db, run_id, task_class, out_file, plan_hash):
    if not (db and os.path.isfile(db)):
        return
    import sqlite3
    if not run_id:
        sys.stderr.write("[info] run_id not set; skipping run row update\n")
        return
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        now = int(time.time())
        con.execute("UPDATE task_runs SET plan_path=?, plan_hash=?, status='planned', updated_at=? WHERE id=?",
                    (out_file, plan_hash, now, run_id))
        if con.execute("SELECT changes()").fetchone()[0] == 0:
            con.execute("INSERT OR IGNORE INTO task_runs (id, task_class, plan_path, plan_hash, "
                        "kickoff_path, status, created_at, updated_at) VALUES (?,?,?,?,?, 'planned', ?, ?)",
                        (run_id, task_class, out_file, plan_hash, "", now, now))
        con.commit(); con.close()
    except sqlite3.OperationalError as e:
        sys.stderr.write(f"[warn] DB write skipped: {e}\n")


def _read_profile_meta(profile_path):
    try:
        profile = json.load(open(profile_path, encoding="utf-8"))
    except Exception:
        profile = {}
    status = str(profile.get("profile_status") or "")
    try:
        confidence = float(profile.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    return status, confidence, profile.get("human_questions") or []


def main(argv=None, *, root=None, dispatch=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))
    os.environ["MINI_ORK_ROOT"] = root

    kickoff = ""
    task_class = os.environ.get("MINI_ORK_TASK_CLASS", "")
    out_file = ""
    dry_run = os.environ.get("MINI_ORK_DRY_RUN", "0") == "1"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_USAGE); return 0
        elif a == "--dry-run":
            dry_run = True; i += 1
        elif a == "--task-class":
            task_class = argv[i + 1]; i += 2
        elif a == "--out":
            out_file = argv[i + 1]; i += 2
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            if not kickoff:
                kickoff = a; i += 1
            else:
                sys.stderr.write(f"Unexpected argument: {a}\n"); return 2
    if not kickoff:
        sys.stdout.write(_USAGE); return 2
    if not os.path.isfile(kickoff):
        sys.stderr.write(f"kickoff not found: {kickoff}\n"); return 2

    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    os.environ["MINI_ORK_HOME"] = home; os.environ["MINI_ORK_DB"] = db
    task_class = task_class or "generic"
    workflow = os.environ.get("MINI_ORK_WORKFLOW", "")
    run_id = os.environ.get("MINI_ORK_RUN_ID") or f"run-{int(time.time())}-{os.getpid()}"
    os.environ["MINI_ORK_RUN_ID"] = run_id
    if not out_file:
        run_dir = os.path.join(home, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        out_file = os.path.join(run_dir, "plan.json")

    profile_path = os.environ.get("MINI_ORK_PROFILE_PATH", "")
    profile_status, confidence, human_questions = "", 1.0, []
    if profile_path and os.path.isfile(profile_path):
        profile_status, confidence, human_questions = _read_profile_meta(profile_path)

    # ── dry-run ──
    if dry_run:
        sys.stderr.write(f"[dry-run] would invoke LLM planner with task_class={task_class}\n")
        sys.stderr.write(f"[dry-run] would write plan to: {out_file}\n")
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        # bash writes the literal heredoc verbatim (inline objects) when no profile;
        # only the profile branch re-dumps via json.dump(indent=2). Match byte-for-byte.
        open(out_file, "w", encoding="utf-8").write(_DRY_RUN_PLACEHOLDER)
        if profile_path and os.path.isfile(profile_path):
            plan = json.load(open(out_file, encoding="utf-8"))
            plan["run_profile_path"] = profile_path
            try:
                profile = json.load(open(profile_path, encoding="utf-8"))
                plan["run_profile"] = {"profile_status": profile.get("profile_status", ""),
                                       "confidence": profile.get("confidence"),
                                       "human_questions": profile.get("human_questions", [])}
            except Exception:
                pass
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(plan, f, indent=2); f.write("\n")
        print(f"plan_path={out_file}")
        print(f"task_class={task_class}")
        return 0

    # ── profile gate ──
    profile_gate = os.environ.get("MINI_ORK_PROFILE_GATE", "1") == "1"
    floor = float(os.environ.get("MINI_ORK_PLAN_CONFIDENCE_FLOOR", "0.7"))
    if profile_gate and (profile_status == "needs_answers" or confidence < floor):
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        plan = {"plan_status": "needs_answers", "blocked_by": "run_profile",
                "profile_status": profile_status, "confidence": confidence,
                "human_questions": human_questions, "objective": "blocked: profile incomplete",
                "assumptions": [], "decomposition": [], "dependencies": [],
                "risk_notes": ["run_profile is incomplete; planner dispatch skipped"],
                "run_profile_path": profile_path,
                "artifact_contract": {"outputs": [], "success_verifiers": []},
                "verifier_contract": {"checks": [{"id": "profile-needs-answers",
                    "description": "Planner dispatch is blocked until run_profile is ready."}]}}
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2); f.write("\n")
        print(f"plan_path={out_file}")
        print(f"task_class={task_class}")
        print('{"plan_status":"needs_answers","blocked_by":"run_profile"}')
        return 0

    # ── get raw plan: MO_GIVEN_PLAN | force-recipe-fallback | LLM dispatch ──
    recipe = os.environ.get("MINI_ORK_RECIPE", "")
    given = os.environ.get("MO_GIVEN_PLAN", "")
    if os.environ.get("MO_FORCE_RECIPE_FALLBACK_PLAN", "0") == "1" and task_class == "doc_to_features_loop":
        fb = recipe_fallback_plan(recipe or "generic",
                                  workflow or os.path.join(root, "recipes", recipe or "generic", "workflow.yaml"),
                                  root, kickoff)
        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        open(out_file, "w").write((fb or "") + "\n" if fb else "")
        print(f"plan_path={out_file}")
        print(f"task_class={task_class}")
        return 0
    if given:
        if not os.access(given, os.R_OK):
            sys.stderr.write(f"MO_GIVEN_PLAN is set but not readable: {given}\n"); return 1
        raw = open(given).read()
        sys.stderr.write(f"plan: using given plan from MO_GIVEN_PLAN={given} (planner LLM skipped)\n")
    else:
        prompt = _build_prompt(root, kickoff, workflow, profile_path)
        if dispatch is None:
            sys.stderr.write("LLM dispatch unavailable (no seam provided)\n"); return 1
        rc, raw = dispatch(task_class, "planner", prompt)
        if rc != 0:
            sys.stderr.write("LLM dispatch failed for planner node\n")
            _charge_cost(db, run_id)
            return 1

    plan_json = extract_plan_json(raw)
    _charge_cost(db, run_id)
    verdict = validate_plan(plan_json)

    if not given and verdict in _RECOVERABLE_VERDICTS:
        first_verdict = verdict
        _preserve_raw(home, run_id, first_verdict, raw, plan_json)
        if os.environ.get("MO_PLAN_DETERMINISTIC_FALLBACK", "0") == "1":
            fb = recipe_fallback_plan(recipe, workflow, root, kickoff)
            if fb:
                plan_json, verdict = fb, "ok"
                sys.stderr.write("PLAN WARNING: planner output was invalid; using deterministic recipe fallback plan because MO_PLAN_DETERMINISTIC_FALLBACK=1.\n")
        else:
            try:
                max_repairs = int(os.environ.get("MO_PLAN_MAX_REPAIRS", "2"))
            except ValueError:
                max_repairs = 2
            for attempt in range(1, max(0, max_repairs) + 1):
                sys.stderr.write(f"PLAN REPAIR: retrying planner output repair ({attempt}/{max_repairs}) after {verdict}.\n")
                truncated = _detect_truncation(raw)
                repair_prompt = _build_repair_prompt(root, kickoff, workflow, profile_path,
                                                     raw, verdict, truncated)
                rc, repaired = dispatch(task_class, "planner", repair_prompt)
                _charge_cost(db, run_id)
                if rc != 0:
                    sys.stderr.write("LLM dispatch failed for planner repair\n")
                    _mark_failed(db, run_id, first_verdict)
                    return 1
                raw = repaired
                plan_json = extract_plan_json(raw)
                verdict = validate_plan(plan_json)
                _preserve_raw(home, run_id, first_verdict, raw, plan_json)
                if verdict == "ok":
                    break
                if verdict == "placeholder_plan":
                    break

    if verdict == "missing_verifier_contract":
        sys.stderr.write("PLAN REJECTED: verifier_contract.checks is missing or empty.\n")
        _mark_failed(db, run_id, verdict); return 1
    if verdict == "placeholder_plan":
        sys.stderr.write("PLAN REJECTED: planner emitted a template plan with placeholder values.\n")
        _preserve_raw(home, run_id, verdict, raw, plan_json); _mark_failed(db, run_id, verdict); return 1
    if verdict == "bad_artifact_contract":
        sys.stderr.write("PLAN REJECTED: artifact_contract must be an object.\n")
        _mark_failed(db, run_id, verdict); return 1
    if verdict == "bad_node_types":
        sys.stderr.write("PLAN REJECTED: one or more decomposition[].node_type values are empty or invalid (D-008b).\n")
        _mark_failed(db, run_id, verdict); return 1
    if verdict == "parse_error":
        sys.stderr.write("PLAN REJECTED: planner emitted non-JSON output.\n")
        _mark_failed(db, run_id, verdict); return 1

    plan_json = overlay_plan(plan_json, task_class, profile_path, root)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    open(out_file, "w").write(plan_json + "\n")
    print(f"plan_path={out_file}")
    print(f"task_class={task_class}")

    plan_hash = hashlib.sha256((plan_json + "\n").encode()).hexdigest()[:16]
    _db_write(db, run_id, task_class, out_file, plan_hash)
    return 0


def _build_prompt(root, kickoff, workflow, profile_path) -> str:
    recipe = os.environ.get("MINI_ORK_RECIPE", "")
    candidates = []
    if recipe:
        candidates.append(os.path.join(root, "recipes", recipe, "prompts", "planner.md"))
    if workflow:
        candidates.append(os.path.join(os.path.dirname(workflow), "prompts", "planner.md"))
    candidates.append(os.path.join(root, "prompts", "planner.md"))
    tpl = _BUILTIN_PROMPT
    for c in candidates:
        if os.path.isfile(c):
            tpl = open(c).read(); break
    prompt = tpl.replace("{{KICKOFF_CONTENT}}", open(kickoff).read())
    if profile_path and os.path.isfile(profile_path):
        prompt += "\n\n--- RUN PROFILE ---\n" + open(profile_path).read() + "\n--- /RUN PROFILE ---\n"
    return prompt


def _default_llm_dispatch(root):
    """Default planner LLM seam for the CLI entry — shells to llm_dispatch, mirroring
    bash bin/mini-ork-plan:656 (RESULT=$(llm_dispatch --node-type planner …)). Without
    this, a python-runtime `mini-ork run` hard-fails at plan ('LLM dispatch unavailable')
    because __main__ passed no dispatch. The seam stays injectable for tests (which pass
    dispatch= explicitly or use MO_GIVEN_PLAN); only the CLI boundary wires this default."""
    lib = os.path.join(root, "lib", "llm-dispatch.sh")

    def d(task_class, node_type, prompt):
        r = subprocess.run(
            ["bash", "-c", f'source "{lib}"; llm_dispatch --task-class "$1" '
             '--node-type "$2" --prompt-text "$3" 2>&1', "_", task_class, node_type, prompt],
            capture_output=True, text=True)
        return r.returncode, r.stdout
    return d


if __name__ == "__main__":
    _root = os.environ.get("MINI_ORK_ROOT") or \
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main(dispatch=_default_llm_dispatch(_root)))
