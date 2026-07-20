"""Python Planner node runtime.

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

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_NODE_TYPES = {"planner", "researcher", "implementer", "reviewer", "verifier",
               "reflector", "publisher", "rollback"}
_RECOVERABLE_VERDICTS = {"parse_error", "missing_verifier_contract",
                         "bad_artifact_contract", "bad_node_types"}

# Golden dry-run placeholder retained from the retired Bash runtime.
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


# A genuine unfilled stub carries a stub word (the dry-run placeholder reads
# "<dry-run: not generated>"). A bare angle-bracketed identifier does NOT.
_PLACEHOLDER_HINT = re.compile(
    r"\b(dry[- ]?run|not[- ]generated|todo|tbd|fixme|fill[- ]?(me|in)?|xxx|placeholder)\b",
    re.I,
)


def _is_stub_string(v) -> bool:
    """True only for an UNFILLED template value — not for code that uses angle brackets."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not (s.startswith("<") and s.endswith(">")):
        return False
    inner = s[1:-1].strip()
    if not inner:
        return True
    # "<dry-run: not generated>", "<TODO>", "<fill me>" -> a real stub
    # "<ContentNodeCreationModal>", "<div>", "<Foo />"   -> real code
    return bool(_PLACEHOLDER_HINT.search(inner))


def _contains_placeholder(v):
    """True when the PLAN ITSELF was never generated (i.e. it is the dry-run stub).

    Scope matters. The old check recursed into EVERY nested string and flagged anything
    shaped like `<...>`. That rejected two entirely legitimate things:

      * JSX/HTML tags a plan naturally names — "<ContentNodeCreationModal>" — which made
        mini-ork unable to plan work on ANY React/JSX codebase; and
      * the planner's own step annotations — "<shell-only>", "<analysis-only — no edit>"
        — meaning "this step edits no file".

    A *placeholder plan* means the planner produced nothing: the dry-run stub, whose
    objective is "<dry-run: not generated>" and whose decomposition is empty. Judge the
    plan by its objective (and emptiness), not by every string inside it.
    """
    if isinstance(v, dict):
        if _is_stub_string(v.get("objective")):
            return True
        # the dry-run stub is an empty shell: no objective, nothing to do
        if not str(v.get("objective") or "").strip() and not (v.get("decomposition") or []):
            return True
        return False
    return _is_stub_string(v)


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


def _build_repair_prompt(root, kickoff, workflow, profile_path, raw_invalid, verdict, truncated,
                         original_prompt=None) -> str:
    original = original_prompt or _build_prompt(root, kickoff, workflow, profile_path)
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
        confidence = float(str(profile.get("confidence")))
    except (TypeError, ValueError):
        confidence = 0.0
    return status, confidence, profile.get("human_questions") or []


def _trace_plan(trace_id, task_class, status, db, **extra):
    """Best-effort planner lifecycle trace; never pollute the CLI streams."""
    if not trace_id:
        return
    try:
        from mini_ork import trace_store
        payload = {"trace_id": trace_id, "task_class": task_class, "status": status, **extra}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            trace_store.trace_write(payload, db=db)
    except Exception:
        pass


def _normalize_profile(profile_path):
    if not profile_path:
        return ""
    try:
        from mini_ork.ported.profile_gate import normalize_zero_questions
        return normalize_zero_questions(profile_path)
    except Exception:
        return ""


def _apply_profile_answers(profile_path, answer_payload) -> bool:
    if not profile_path:
        return False
    items = answer_payload.get("answers") if isinstance(answer_payload, dict) else None
    if isinstance(items, dict):
        answers = {str(k): str(v) for k, v in items.items() if str(v).strip()}
    else:
        answers = {
            str(item.get("question")): str(item.get("answer"))
            for item in (items or [])
            if isinstance(item, dict) and item.get("question") and item.get("answer")
        }
    if not answers:
        return False
    try:
        with open(profile_path, encoding="utf-8") as fh:
            profile = json.load(fh)
    except Exception:
        profile = {}
    profile.setdefault("answers", {}).update(answers)
    profile["profile_answers_auto_answered"] = bool(answer_payload.get("auto_answered"))
    profile["profile_status"] = "ready"
    try:
        current_confidence = float(profile.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        current_confidence = 0.0
    profile["confidence"] = max(current_confidence, 0.9)
    profile["human_questions"] = []
    with open(profile_path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2)
        fh.write("\n")
    return True


def _auto_answer_profile(kickoff, questions, profile_path, dispatch_fn) -> str:
    if not profile_path or dispatch_fn is None:
        return ""
    from mini_ork.ported.profile_answerer import answer_profile_questions
    answers_path = os.path.join(os.path.dirname(profile_path), "profile-answers.json")

    def answer_dispatch(prompt):
        rc, raw = dispatch_fn("profile_answerer", "profile_answerer", prompt)
        if rc != 0 or not raw.strip():
            raise RuntimeError("profile answerer dispatch failed")
        return raw

    payload = answer_profile_questions(
        kickoff,
        json.dumps(questions),
        answers_path,
        dispatch=answer_dispatch,
    )
    return answers_path if _apply_profile_answers(profile_path, payload) else ""


def _can_prompt_profile() -> bool:
    if os.environ.get("MINI_ORK_NONINTERACTIVE", "0") == "1":
        return False
    try:
        with open("/dev/tty", encoding="utf-8"):
            return True
    except OSError:
        return False


def _prompt_profile_questions(questions, profile_path) -> str:
    answers = {}
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty_r, \
                open("/dev/tty", "w", encoding="utf-8") as tty_w:
            for index, question in enumerate(questions, 1):
                if isinstance(question, str):
                    text = question
                elif isinstance(question, dict):
                    text = question.get("text") or question.get("question") or str(question)
                else:
                    text = str(question)
                tty_w.write(f"\n  Q{index}: {text}\n  > ")
                tty_w.flush()
                answer = tty_r.readline().strip()
                if answer:
                    answers[text] = answer
                else:
                    tty_w.write("  [skipped]\n")
                    tty_w.flush()
    except (OSError, KeyboardInterrupt):
        return ""
    if not answers:
        return ""
    answers_path = os.path.join(os.path.dirname(profile_path), "profile-answers.json")
    with open(answers_path, "w", encoding="utf-8") as fh:
        json.dump(answers, fh, indent=2)
        fh.write("\n")
    payload = {"answers": answers, "auto_answered": False}
    return answers_path if _apply_profile_answers(profile_path, payload) else ""


def _brief_query(path) -> str:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except Exception:
            return raw[:512].strip()
        parts = [str(data.get(key)).strip() for key in
                 ("title", "objective", "description", "task_class")
                 if isinstance(data.get(key), str) and data.get(key).strip()]
        return " ".join(parts)[:600] if parts else raw[:512].strip()
    except Exception:
        return ""


def _contextnest_atoms_md(brief_path, limit=6) -> str:
    try:
        from mini_ork import cn_client
        from mini_ork.ported.context_role_packs import extract_query
        if not cn_client.available():
            return ""
        capsule = cn_client.capsule(extract_query(brief_path), "14d")
        try:
            min_chars = int(os.environ.get("CN_CAPSULE_MIN_CHARS", "100"))
        except ValueError:
            min_chars = 100
        if len(capsule) > min_chars and any(line.startswith("## ") for line in capsule.splitlines()):
            return ("--- ContextNest capsule (kind-ordered substrate digest) ---\n"
                    + capsule + "\n--- /ContextNest capsule ---\n")
        query = _brief_query(brief_path)
        return cn_client.render_atoms_md(cn_client.retrieve(query, limit), limit) if query else ""
    except Exception:
        return ""


def _contextnest_recent_sessions_md(brief_path, max_files=4) -> str:
    try:
        from mini_ork import cn_client
        if not cn_client.available():
            return ""
        data = json.loads(Path(brief_path).read_text(encoding="utf-8"))
        candidates = []
        for key in ("files", "paths", "relevant_files", "targets"):
            for item in data.get(key) or []:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    path = item.get("path") or item.get("file") or item.get("name")
                    if isinstance(path, str):
                        candidates.append(path)
        rendered = []
        for path in candidates[:max_files]:
            try:
                payload = json.loads(cn_client.sessions_by_file(path))
            except Exception:
                continue
            sessions = payload.get("sessions") or payload.get("hits") or []
            if not sessions:
                continue
            lines = [f"- File `{path}` recently touched in:"]
            for session in sessions[:3]:
                sid = session.get("session_id") or session.get("id", "")
                ts = (session.get("last_seen") or session.get("ts") or "")[:10]
                title = (session.get("title") or session.get("intent") or "").strip()[:80]
                lines.append(f"  - {sid[:8]} ({ts}) {title}")
            rendered.append("\n".join(lines))
        if not rendered:
            return ""
        return ("--- ContextNest: recent sessions for relevant files ---\n"
                + "\n".join(rendered)
                + "\n--- /ContextNest: recent sessions ---\n")
    except Exception:
        return ""


def _inject_context(prompt, kickoff, task_class, db, out_file, dry_run) -> str:
    if os.environ.get("MO_INJECT_LEARNINGS", "1") != "1":
        return prompt
    blocks = []
    try:
        from mini_ork import context_assembler
        for producer in (context_assembler.failure_modes_md, context_assembler.prior_runs_md):
            try:
                block = producer(task_class, 5, db=db)
            except Exception:
                block = ""
            if block:
                blocks.append(block)

        role_pack = ""
        if os.environ.get("MO_USE_ROLE_PACKS", "1") == "1":
            try:
                from mini_ork.ported.context_role_packs import role_pack_md
                role_pack = role_pack_md("planner", kickoff, "")
            except Exception:
                role_pack = ""
        generic = "" if role_pack else _contextnest_atoms_md(kickoff, 6)
        if role_pack or generic:
            blocks.append(role_pack or generic)
        recent = _contextnest_recent_sessions_md(kickoff, 4)
        if recent:
            blocks.append(recent)
        try:
            from mini_ork.ported.active_state_index import render_active_state_block
            active = render_active_state_block(task_class, 30, db_path=db)
        except Exception:
            active = ""
        if active:
            blocks.append(active)

        if not dry_run:
            brief_path = ""
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                                 prefix="mini-ork-brief-", suffix=".json") as fh:
                    json.dump({"task_class": task_class,
                               "kickoff": Path(kickoff).read_text(encoding="utf-8")[:20000]}, fh)
                    brief_path = fh.name
                pack = context_assembler.context_assemble(brief_path, "planner", db=db)
                pack_path = os.path.join(os.path.dirname(out_file), "context-pack.json")
                os.makedirs(os.path.dirname(pack_path), exist_ok=True)
                with open(pack_path, "w", encoding="utf-8") as fh:
                    json.dump(pack, fh, indent=2)
                    fh.write("\n")
            except Exception:
                try:
                    os.remove(os.path.join(os.path.dirname(out_file), "context-pack.json"))
                except OSError:
                    pass
            finally:
                if brief_path:
                    try:
                        os.remove(brief_path)
                    except OSError:
                        pass
    except Exception:
        return prompt
    for block in blocks:
        prompt += "\n\n" + block.rstrip("\n") + "\n"
    return prompt


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
    run_dir = os.path.dirname(out_file)
    trace_id = "" if dry_run else f"tr-plan-{int(time.time())}-{os.getpid()}"
    _trace_plan(trace_id, task_class, "running", db)
    dispatch_fn = dispatch

    profile_path = os.environ.get("MINI_ORK_PROFILE_PATH", "")
    profile_status, confidence, human_questions = "", 1.0, []
    if profile_path and os.path.isfile(profile_path):
        profile_status, confidence, human_questions = _read_profile_meta(profile_path)
        if profile_status == "needs_answers" and not human_questions:
            if _normalize_profile(profile_path) == "ready":
                profile_status, confidence, human_questions = _read_profile_meta(profile_path)
                sys.stderr.write("  [ok] profile flagged needs_answers with 0 questions — "
                                 "nothing to answer; treating as ready\n")

    prompt = _build_prompt(root, kickoff, workflow, profile_path)
    prompt = _inject_context(prompt, kickoff, task_class, db, out_file, dry_run)

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

    # ── profile question handling + gate ──
    profile_gate = os.environ.get("MINI_ORK_PROFILE_GATE", "1") == "1"
    try:
        floor = float(os.environ.get("MINI_ORK_PLAN_CONFIDENCE_FLOOR", "0.7"))
    except ValueError:
        floor = 0.7
    can_prompt = _can_prompt_profile()
    if (profile_status == "needs_answers" and not can_prompt
            and os.environ.get("MO_AUTO_ANSWER_PROFILE", "1") == "1"):
        try:
            answers_path = _auto_answer_profile(
                kickoff, human_questions, profile_path, dispatch_fn
            )
        except Exception:
            answers_path = ""
        if answers_path:
            profile_status, confidence, human_questions = _read_profile_meta(profile_path)
            sys.stderr.write(f"  [ok] profile questions auto-answered ({answers_path}) — "
                             "continuing planner dispatch\n")
        else:
            sys.stderr.write("  [skip] profile auto-answer failed — falling through to gate-block\n")
    if profile_status == "needs_answers" and can_prompt:
        sys.stderr.write(
            "\n──────────────────────────────────────────────────────────────────────\n"
            "  mini-ork planner needs your input before dispatching agents.\n"
            f"  Profile confidence: {confidence} (floor: {floor})\n"
            f"  Run: {run_id}\n"
            "──────────────────────────────────────────────────────────────────────\n"
        )
        answers_path = _prompt_profile_questions(human_questions, profile_path)
        if answers_path:
            profile_status, confidence, human_questions = _read_profile_meta(profile_path)
            sys.stderr.write(f"  [ok] answers captured ({answers_path}) — "
                             "continuing planner dispatch\n\n")
        else:
            sys.stderr.write("  [skip] no answers provided — falling through to gate-block\n")
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
        _trace_plan(trace_id, task_class, "blocked", db,
                    reviewer_verdict="run_profile_needs_answers")
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
        _trace_plan(trace_id, task_class, "success", db,
                    final_artifact_ref=out_file, reviewer_verdict="recipe_fallback")
        return 0
    if given:
        if not os.access(given, os.R_OK):
            sys.stderr.write(f"MO_GIVEN_PLAN is set but not readable: {given}\n")
            _trace_plan(trace_id, task_class, "failure", db,
                        reason="given_plan_unreadable")
            return 1
        raw = open(given).read()
        sys.stderr.write(f"plan: using given plan from MO_GIVEN_PLAN={given} (planner LLM skipped)\n")
    else:
        if dispatch_fn is None:
            sys.stderr.write("LLM dispatch unavailable (no seam provided)\n")
            _trace_plan(trace_id, task_class, "failure", db)
            return 1
        rc, raw = dispatch_fn(task_class, "planner", prompt)
        if rc != 0:
            sys.stderr.write("LLM dispatch failed for planner node\n")
            _charge_cost(db, run_id)
            _trace_plan(trace_id, task_class, "failure", db)
            return 1

    plan_json = extract_plan_json(raw)
    if not given:
        _charge_cost(db, run_id)
    verdict = validate_plan(plan_json)

    if not given and verdict in _RECOVERABLE_VERDICTS:
        assert dispatch_fn is not None
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
                                                     raw, verdict, truncated,
                                                     original_prompt=prompt)
                rc, repaired = dispatch_fn(task_class, "planner", repair_prompt)
                _charge_cost(db, run_id)
                if rc != 0:
                    sys.stderr.write("LLM dispatch failed for planner repair\n")
                    _mark_failed(db, run_id, first_verdict)
                    _trace_plan(trace_id, task_class, "failure", db,
                                reviewer_verdict=first_verdict)
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
        _mark_failed(db, run_id, verdict)
        _trace_plan(trace_id, task_class, "failure", db, reviewer_verdict=verdict)
        return 1
    if verdict == "placeholder_plan":
        sys.stderr.write("PLAN REJECTED: planner emitted a template plan with placeholder values.\n")
        _preserve_raw(home, run_id, verdict, raw, plan_json); _mark_failed(db, run_id, verdict)
        _trace_plan(trace_id, task_class, "failure", db, reviewer_verdict=verdict)
        return 1
    if verdict == "bad_artifact_contract":
        sys.stderr.write("PLAN REJECTED: artifact_contract must be an object.\n")
        _mark_failed(db, run_id, verdict)
        _trace_plan(trace_id, task_class, "failure", db, reviewer_verdict=verdict)
        return 1
    if verdict == "bad_node_types":
        sys.stderr.write("PLAN REJECTED: one or more decomposition[].node_type values are empty or invalid (D-008b).\n")
        _mark_failed(db, run_id, verdict)
        _trace_plan(trace_id, task_class, "failure", db, reviewer_verdict=verdict)
        return 1
    if verdict == "parse_error":
        sys.stderr.write("PLAN REJECTED: planner emitted non-JSON output.\n")
        _mark_failed(db, run_id, verdict)
        _trace_plan(trace_id, task_class, "failure", db, reviewer_verdict=verdict)
        return 1

    plan_json = overlay_plan(plan_json, task_class, profile_path, root)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    open(out_file, "w").write(plan_json + "\n")
    print(f"plan_path={out_file}")
    print(f"task_class={task_class}")

    plan_hash = hashlib.sha256((plan_json + "\n").encode()).hexdigest()[:16]
    _db_write(db, run_id, task_class, out_file, plan_hash)
    _trace_plan(trace_id, task_class, "success", db, final_artifact_ref=out_file)
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
    """Return the native dispatch seam with the former merged-stream contract.

    The dispatcher emits the provider reply on stdout and diagnostics on stderr.
    Redirecting both to one buffer preserves shell ``2>&1`` ordering and prevents
    dispatcher output from leaking into the planner's public stdout.
    """
    from mini_ork.ported import llm_dispatch as native_dispatch

    def d(task_class, node_type, prompt):
        combined = io.StringIO()
        try:
            with contextlib.redirect_stdout(combined), contextlib.redirect_stderr(combined):
                rc = native_dispatch.llm_dispatch(
                    ["--task-class", task_class, "--node-type", node_type,
                     "--prompt-text", prompt],
                    root=root,
                )
        except Exception as exc:
            combined.write(f"llm_dispatch: {exc}\n")
            rc = 1
        return rc, combined.getvalue()
    return d


if __name__ == "__main__":
    _root = os.environ.get("MINI_ORK_ROOT") or \
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raise SystemExit(main(dispatch=_default_llm_dispatch(_root)))
