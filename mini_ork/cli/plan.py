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
import functools
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mini_ork.context import apply_env_overrides, context_env
from mini_ork.dispatch.predicates import looks_like_json_text

# ── extracted pure helpers (mini_ork.planning) — re-exported for parity ──
# Plan-JSON shape/extraction/validation and the deterministic recipe fallback +
# contract overlay now live in mini_ork.planning; the names below are re-exported
# so existing `from mini_ork.cli import plan` consumers see an identical surface.
from mini_ork.planning.plan_schema import (  # noqa: F401
    _contains_placeholder,
    _detect_truncation,
    _is_plan,
    _is_stub_string,
    _NODE_TYPES,
    _objects,
    _PLACEHOLDER_HINT,
    extract_plan_json,
    validate_plan,
)
from mini_ork.planning.recipe_plan import (  # noqa: F401
    overlay_plan,
    recipe_dir,
    recipe_fallback_plan,
)

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


def _plan_file_name(dry_run: bool) -> str:
    """The live run is the only author of its own ``plan.json``.

    A rehearsal (``MINI_ORK_DRY_RUN=1``, e.g. a nested lifecycle that inherited the
    run id) writes ``_DRY_RUN_PLACEHOLDER``. Under the live name it overwrote the
    real plan the implementer was about to be handed, so every node downstream was
    planned against ``objective: '<dry-run: not generated>'`` with an empty
    decomposition. Rehearsals get their own filename, mirroring
    ``mini_ork.cli.main._execute_log_name``.

    An explicit ``--out`` is not routed through here: the caller named that file,
    so the rehearsal/live distinction is theirs to draw.
    """
    return "plan.dryrun.json" if dry_run else "plan.json"


def _is_plan_artifact_name(name: str) -> bool:
    """``plan.json`` is the generic plan runtime's name. A recipe whose planner is
    an LLM node names the artefact after itself — epic-runner's planner prompt
    writes ``epic-runner-plan.json`` — and that is a plan too."""
    return name == "plan.json" or name.endswith("-plan.json")


@functools.lru_cache(maxsize=64)
def _declared_plan_name(recipe_dir: str) -> str:
    """The plan artefact a recipe declares in ``artifact_contract.yaml``.

    The contract is the recipe's own statement of what a run produces; for
    epic-runner its ``required_artifacts`` is the only place
    ``epic-runner-plan.json`` is written down *before* the run happens. Cached
    because the IDE resolves a plan once per rendered run row and the recipe
    directory is a pure function of the path.
    """
    try:
        import yaml

        with open(os.path.join(recipe_dir, "artifact_contract.yaml"), encoding="utf-8") as handle:
            contract = yaml.safe_load(handle) or {}
    except (OSError, ValueError, ImportError):
        return ""
    if not isinstance(contract, dict):
        return ""
    for key in ("required_artifacts", "source_artifact", "outputs"):
        raw = contract.get(key)
        for entry in raw if isinstance(raw, list) else [raw]:
            base = os.path.basename(os.path.expandvars(str(entry or "")))
            if base and _is_plan_artifact_name(base):
                return base
    return ""


def _plan_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def plan_artifact(run_dir, recorded: str = "", *, recipe_dir=None) -> Path | None:
    """The run's plan file, whatever the recipe chose to call it.

    ``task_runs.plan_path`` is written by ``mini-ork plan`` — the generic plan
    runtime — so a recipe that plans inside its DAG (epic-runner: a planner LLM
    node writes ``epic-runner-plan.json``) leaves it empty and every reader keyed
    on a literal ``plan.json`` reports "no plan" for a run whose plan is sitting
    in the directory.

    Run-dir-local candidates come first and ``recorded`` last: a ``state.db``
    carried over from another checkout keeps absolute ``plan_path`` values
    pointing at *that* checkout's home, so trusting the DB first would serve
    another tree's plan.

    1. ``<run_dir>/plan.json`` — the generic plan runtime's name.
    2. the recipe's declared plan artefact (``artifact_contract.yaml``).
    3. the newest ``*-plan.json`` in the run dir.
    4. ``recorded``, when that file still exists.
    """
    if run_dir is None:
        return None
    run_dir = Path(run_dir)
    canonical = run_dir / "plan.json"
    if canonical.is_file():
        return canonical
    if recipe_dir:
        declared = _declared_plan_name(str(recipe_dir))
        if declared:
            candidate = run_dir / declared
            if candidate.is_file():
                return candidate
    found = [path for path in run_dir.glob("*-plan.json") if path.is_file()]
    if found:
        return max(found, key=_plan_mtime)
    if recorded:
        candidate = Path(recorded)
        if candidate.is_file():
            return candidate
    return None


def _declared_plan_artifact(root, recipe, run_dir, workflow_path="") -> str:
    """Where a recipe's declared plan artifact *will* be, or "".

    ``plan_artifact`` above answers "where is this run's plan"; this answers
    "where will it be". The difference is the whole point: the dispatch boundary
    must install its shape gate *before* the planner lane runs, when the file
    does not exist yet, so it cannot ask a resolver that requires the file to be
    present. It asks the recipe's declared name instead — and asks
    ``_declared_plan_name``, the same function ``plan_artifact`` consults, so a
    recipe that renames its plan moves both readers at once.
    """
    if not recipe:
        return ""
    try:
        rdir = recipe_dir(recipe, root, workflow_path or None)
    except Exception:
        return ""
    if rdir is None:
        return ""
    name = _declared_plan_name(str(rdir))
    return os.path.join(run_dir, name) if name else ""


def _read_declared_plan(path: str) -> str:
    """A declared plan artifact's text, or "" when it is absent or not JSON.

    Judged by ``looks_like_json_text`` — the predicate that gated the dispatch.
    Reusing it is the point: if the boundary accepted this file as the planner's
    output, the adoption here must agree, or a lane could pass the gate and be
    rejected by the very stage that follows it.
    """
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    return text if looks_like_json_text(text) else ""


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


# ``task_runs.verdict`` is constrained to the reviewer enum or NULL. A
# plan-stage reason (``parse_error``, ``planner_dispatch_rc_4``, …) is not a
# member, and writing it raised — which, because the status and the verdict are
# set in ONE statement, threw away the ``status='failed'`` write too. Every
# ``_mark_failed`` call in this module was therefore a silent no-op, and a run
# whose plan was rejected was left exactly as it was found.
_VERDICT_ENUM = ("APPROVE", "REQUEST_CHANGES", "ESCALATE", "CRASH")


def _mark_failed(db, run_id, verdict):
    """Mark the run failed, recording ``verdict`` where the schema can hold it.

    The status is written unconditionally. The verdict column is written only
    when the value is one the CHECK constraint accepts; any other value is
    appended to ``notes`` instead (the ``'; '``-separated slot the retry
    machinery already reads for ``killed-by-user``), so the reason survives
    without fighting the schema.
    """
    if not (db and os.path.isfile(db) and run_id):
        return
    import sqlite3
    now = int(time.time())
    legal = verdict if verdict in _VERDICT_ENUM else None
    reason = "" if legal else (verdict or "")
    try:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("UPDATE task_runs SET status='failed', verdict=COALESCE(verdict, ?), "
                    "updated_at=?, ended_at=COALESCE(ended_at, ?) WHERE id=?",
                    (legal, now, now, run_id))
        if reason:
            con.execute("UPDATE task_runs SET notes = CASE WHEN COALESCE(notes, '') = '' "
                        "THEN ? ELSE notes || '; ' || ? END WHERE id=?",
                        (reason, reason, run_id))
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
        payload = trace_store.enrich_stage_trace(
            {"trace_id": trace_id, "task_class": task_class, "status": status, **extra},
            node_type="planner",
            verdict=str(extra.get("reviewer_verdict") or ""))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            trace_store.trace_write(payload, db=db)
    except Exception:
        pass


def _normalize_profile(profile_path):
    if not profile_path:
        return ""
    try:
        from mini_ork.gates.profile_gate import normalize_zero_questions
        return normalize_zero_questions(profile_path)
    except Exception:
        return ""


def _kickoff_declares_verification_command(kickoff_path) -> bool:
    """True when the kickoff file states how success is proven (C8).

    Unreadable / missing kickoff → False, i.e. keep today's behaviour: the
    profile gate interrogates rather than silently passing an unknown kickoff.
    """
    try:
        with open(kickoff_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    try:
        from mini_ork.gates.profile_gate import declares_verification_command
        return declares_verification_command(text)
    except Exception:
        return False


def _normalize_kickoff_complete(profile_path) -> str:
    if not profile_path:
        return ""
    try:
        from mini_ork.gates.profile_gate import normalize_kickoff_complete
        return normalize_kickoff_complete(profile_path)
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
    from mini_ork.steering.profile_answerer import answer_profile_questions
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
    """True only when a human is actually attached to answer the prompt.

    SE-3 incident, Layer 1 (level a): a parent that spawns mini-ork headless —
    the Node BE, a scheduler, CI — while a controlling terminal is still
    inherited made the old ``open('/dev/tty')`` probe return True, so the run
    blocked forever on a question no human would ever answer. Opening
    ``/dev/tty`` only proves the *session* owns a controlling terminal; it does
    NOT prove a human is watching *this process's* stdio (stdin may be a pipe or
    /dev/null). The positive signal for "someone can type an answer" is stdin
    being an interactive tty, so we gate on that FIRST. Ordering: an explicit
    opt-out still wins; then stdin must be a real terminal; only then do we
    confirm ``/dev/tty`` is actually reachable. This inverts the fragile
    opt-out convention — forgetting ``MINI_ORK_NONINTERACTIVE`` now degrades to
    the safe auto-answer path (plan.py gate below), never a deadlock.
    """
    if os.environ.get("MINI_ORK_NONINTERACTIVE", "0") == "1":
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except (OSError, ValueError):
        # A closed/detached stdin (e.g. daemonized) has no isatty → not a human.
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


def _question_text(q) -> str:
    """Normalise a profile question to its promptable text.

    ``gen_profile`` writes plain strings, but the profile reader passes through
    whatever the profile file holds; dicts and non-strings degrade to their text
    the same way ``_prompt_profile_questions`` does, so the ASK file's
    ``question`` key and the answers map resume builds stay keyed identically.
    """
    if isinstance(q, str):
        return q
    if isinstance(q, dict):
        return q.get("text") or q.get("question") or str(q)
    return str(q)


def _emit_asks_blocked_event(db, run_id, ask_ids, created_at):
    """Best-effort ``run_events`` row for a run blocked on profile questions.

    ``event_id`` embeds the run id (``evt-asks_blocked-<run_id>-<ts>``) because
    ``run_events.event_id`` is UNIQUE — two runs blocking in the same second
    would otherwise collide. ``event_type`` is unconstrained TEXT; ``finish_reason``
    is left NULL so no CHECK enum is widened.
    """
    if not (db and os.path.isfile(db) and run_id):
        return
    import sqlite3
    try:
        con = sqlite3.connect(db, timeout=5.0)
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute(
            "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"evt-asks_blocked-{run_id}-{created_at}", run_id, "asks_blocked",
             json.dumps({"ask_ids": ask_ids, "blocked_by": "run_profile"}), created_at))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def _write_ask_files(run_dir: str, run_id: str, questions, db) -> list[str]:
    """Persist one ``asks/ask-N.json`` per human question and print the resume
    hint. Returns the ask ids written, in 1-based question order."""
    asks_dir = os.path.join(run_dir, "asks")
    os.makedirs(asks_dir, exist_ok=True)
    created_at = int(time.time())
    ask_ids: list[str] = []
    for index, q in enumerate(questions, 1):
        ask_id = f"ask-{index}"
        ask_ids.append(ask_id)
        ask = {
            "schema_version": "ask@1",
            "ask_id": ask_id,
            "run_id": run_id,
            "question": _question_text(q),
            "blocking_refs": ["run_profile"],
            "options": [],
            "default": None,
            "created_at": created_at,
            "answer": None,
            "answered_at": None,
        }
        with open(os.path.join(asks_dir, f"{ask_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(ask, fh, indent=2)
            fh.write("\n")
    print(f"[asks] {len(questions)} question(s) written to {asks_dir} — "
          f"answer with: mini-ork resume {run_id} --answer ask-1=<text>")
    _emit_asks_blocked_event(db, run_id, ask_ids, created_at)
    return ask_ids


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
        from mini_ork.steering.context_role_packs import extract_query
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


def _context_v2_planner(kickoff, task_class, db, out_file, dry_run) -> tuple[str, dict, dict]:
    """``(block, pack, ledger_extra)`` for the planner. ``block`` is non-empty only
    in the ``v2`` arm (``MO_CONTEXT_V2=on``, not held out); ``shadow`` builds and
    records the pack without injecting it. Fail-soft."""
    try:
        from mini_ork import context_v2
        ctx_mode = context_v2.mode()
        if dry_run or ctx_mode == "off":
            return "", {}, {"context_v2": {"mode": ctx_mode, "arm": "off", "injected": False}}
        run_dir = os.path.dirname(out_file)
        run_id = os.environ.get("MINI_ORK_RUN_ID", "") or os.path.basename(run_dir)
        pack = context_v2.pack_for_run(run_dir, kickoff_path=kickoff, task_class=task_class,
                                       run_id=run_id, db=db)
        arm = context_v2.arm(run_id)
        block = context_v2.render(pack, "planner") if (pack and arm == "v2") else ""
        return block, pack, {"context_v2": {
            "mode": ctx_mode, "arm": arm, "injected": bool(block),
            "item_ids": context_v2.rendered_ids(block, pack) if block else []}}
    except Exception:
        return "", {}, {}


def _inject_context(prompt, kickoff, task_class, db, out_file, dry_run) -> str:
    if os.environ.get("MO_INJECT_LEARNINGS", "1") != "1":
        return prompt
    # (kind, text) in prompt order. The three task_class-keyed v1 blocks are the
    # ones context v2 replaces in its v2 arm; everything else is kept as is.
    v1_task_blocks: list[tuple[str, str]] = []
    other_blocks: list[tuple[str, str]] = []
    # Blocks withheld from the prompt, with the reason. Written to the planner
    # injection ledger (change 3) so an operator can see what was left out and
    # why, instead of it silently vanishing.
    skipped: dict[str, str] = {}
    try:
        from mini_ork import context_assembler
        # Build the v2 block FIRST. The v1 task_class producers below run the
        # UCB pattern path, which records a retrieval for every row it returns;
        # computing them only to drop their output when the v2 block rendered
        # credited UCB for context the planner never saw (LF-R3). The v1
        # producers are called only when the v2 block is empty — the arm is not
        # ``v2``, or the pack rendered nothing.
        v2_block, _v2_pack, ledger_extra = _context_v2_planner(
            kickoff, task_class, db, out_file, dry_run)

        # Raw gradients are EVIDENCE, not verified guidance (user rule
        # 2026-10-07): the graph-context block rides the same
        # MO_INJECT_UNVERIFIED opt-in as the node learned blocks. Default off.
        # The ledger records the withholding on every arm — including ``v2``,
        # where the block is absent because the v1 task blocks are replaced.
        graph_opt_in = os.environ.get("MO_INJECT_UNVERIFIED", "") == "1"
        if not graph_opt_in:
            skipped["graph_context"] = "unverified"
        if not v2_block:
            for kind, producer in (("failure_modes", context_assembler.failure_modes_md),
                                   ("prior_runs", context_assembler.prior_runs_md)):
                try:
                    block = producer(task_class, 5, db=db)
                except Exception:
                    block = ""
                if block:
                    v1_task_blocks.append((kind, block))

            graph_block = ""
            if graph_opt_in:
                try:
                    graph_block = context_assembler.graph_context_md(task_class, 5, db=db)
                except Exception:
                    graph_block = ""
            if graph_block:
                v1_task_blocks.append(("graph_context", graph_block))

        # Other sessions' and other projects' material (the role pack, the
        # ContextNest attention inbox / recent sessions, the global
        # active-state index) is not this run's context: a planner can act on
        # it. Off by default — and when off the producers are not called at
        # all (no ContextNest HTTP, no DB scan). Opt back in per run.
        if os.environ.get("MO_PLANNER_SHARED_CONTEXT", "") == "1":
            role_pack = ""
            if os.environ.get("MO_USE_ROLE_PACKS", "1") == "1":
                try:
                    from mini_ork.steering.context_role_packs import role_pack_md
                    role_pack = role_pack_md("planner", kickoff, "")
                except Exception:
                    role_pack = ""
            generic = "" if role_pack else _contextnest_atoms_md(kickoff, 6)
            if role_pack or generic:
                other_blocks.append(("role_pack" if role_pack else "contextnest_atoms",
                                     role_pack or generic))
            recent = _contextnest_recent_sessions_md(kickoff, 4)
            if recent:
                other_blocks.append(("contextnest_recent", recent))
            try:
                from mini_ork.orchestration.active_state_index import render_active_state_block
                active = render_active_state_block(task_class, 30, db_path=db)
            except Exception:
                active = ""
            if active:
                other_blocks.append(("active_state", active))
        else:
            skipped["role_pack"] = "shared_context_off"
            skipped["contextnest_recent"] = "shared_context_off"
            skipped["active_state"] = "shared_context_off"

        # Record what was withheld and why, alongside the v2 arm metadata.
        ledger_extra = {**ledger_extra, "skipped_blocks": dict(skipped)}
        tagged = ([("context_v2", v2_block)] if v2_block else v1_task_blocks) + other_blocks
        blocks = [text for _, text in tagged]

        if not dry_run:
            # The planner's exact injected text, in the same ledger shape the
            # execute handlers write for LLM nodes (learned/<node>.md + .json).
            try:
                from mini_ork import context_v2
                sources = [{"kind": kind} for kind, _ in tagged]
                if v2_block:
                    sources += [{"kind": "context_v2", "id": i}
                                for i in ledger_extra.get("context_v2", {}).get("item_ids", [])]
                context_v2.write_injection_record(
                    os.path.dirname(out_file), "planner",
                    text="\n\n".join(b.rstrip("\n") for b in blocks),
                    sources=sources, extra=ledger_extra)
            except Exception:
                pass

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

    home = context_env("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    db = context_env("MINI_ORK_DB") or os.path.join(home, "state.db")
    # Plan runs as a subprocess stage of the lifecycle — it does not own the
    # run context, so it writes the process env only (historical semantics).
    # Only the lifecycle (run owner) publishes into the contextvar layer.
    apply_env_overrides({"MINI_ORK_HOME": home, "MINI_ORK_DB": db})
    task_class = task_class or "generic"
    workflow = context_env("MINI_ORK_WORKFLOW", "")
    run_id = context_env("MINI_ORK_RUN_ID") or f"run-{int(time.time())}-{os.getpid()}"
    apply_env_overrides({"MINI_ORK_RUN_ID": run_id})
    if not out_file:
        run_dir = os.path.join(home, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        out_file = os.path.join(run_dir, _plan_file_name(dry_run))
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
        # A kickoff that declares its own verification command is self-sufficient:
        # the standard run_profile questions (success criteria / scope / how you
        # prove it) are all answerable from the kickoff itself. Interrogating it
        # spends one LLM call per node — every goal-loop child re-plans — and when
        # that call fails the gate BLOCKS a kickoff that never needed an answer.
        # Ready the profile instead; the questions are deferred, not dropped.
        # MO_PROFILE_REQUIRE_ANSWERS=1 restores the LLM interrogation.
        if (profile_status == "needs_answers"
                and os.environ.get("MO_PROFILE_REQUIRE_ANSWERS", "0") != "1"
                and _kickoff_declares_verification_command(kickoff)):
            if _normalize_kickoff_complete(profile_path) == "ready":
                profile_status, confidence, human_questions = _read_profile_meta(profile_path)
                sys.stderr.write("  [ok] kickoff declares a verification command — profile "
                                 "questions deferred, no LLM interrogation\n")

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
        plan_text = json.dumps(plan, indent=2) + "\n"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(plan_text)
        print(f"plan_path={out_file}")
        print(f"task_class={task_class}")
        print('{"plan_status":"needs_answers","blocked_by":"run_profile"}')
        _trace_plan(trace_id, task_class, "blocked", db,
                    reviewer_verdict="run_profile_needs_answers")
        if human_questions:
            # A headless run blocked on real questions is resumable, not failed:
            # write one ASK file per question, mark the row planned (non-terminal),
            # and return 0 like any blocked plan: execute's gate sees the open
            # ASKs and exits 6 (resumable), so `mini-ork plan` keeps its contract.
            _write_ask_files(run_dir, run_id, human_questions, db)
            _db_write(db, run_id, task_class, out_file,
                      hashlib.sha256(plan_text.encode()).hexdigest()[:16])
        return 0

    # ── get raw plan: MO_GIVEN_PLAN | force-recipe-fallback | LLM dispatch ──
    recipe = os.environ.get("MINI_ORK_RECIPE", "")
    given = os.environ.get("MO_GIVEN_PLAN", "")
    # A recipe that declares its plan as a run-dir artifact hands the harness a
    # FILE, not a reply (epic-runner's planner prompt says "write the plan file
    # and nothing else"). Publish the path so the dispatch boundary gates the
    # plan where it actually lives — and clear it otherwise, so a nested planner
    # cannot inherit an outer run's artifact.
    declared_plan = _declared_plan_artifact(root, recipe, run_dir, workflow)
    if declared_plan:
        apply_env_overrides({"MO_PLAN_ARTIFACT": declared_plan})
    else:
        os.environ.pop("MO_PLAN_ARTIFACT", None)
    # Static-recipe-plan skip. A forced recipe whose workflow.yaml is the fixed
    # dispatch DAG gives the planner LLM nothing to decide — recipe_fallback_plan
    # renders the identical plan (nodes + edges + artifact contract) from
    # workflow.yaml with zero LLM. Two opt-ins, kept distinct so callers don't
    # collide: MO_FORCE_RECIPE_FALLBACK_PLAN keeps its historical
    # doc_to_features_loop scope (and its always-write semantics); the general
    # MO_STATIC_RECIPE_PLAN honours ANY forced recipe but zero-fallbacks to the
    # LLM path if the workflow can't be rendered (fb is empty) rather than
    # emitting a blank plan.
    _force_doc_features = (
        os.environ.get("MO_FORCE_RECIPE_FALLBACK_PLAN", "0") == "1"
        and task_class == "doc_to_features_loop"
    )
    _static_recipe = os.environ.get("MO_STATIC_RECIPE_PLAN", "0") == "1"
    if _force_doc_features or _static_recipe:
        fb = recipe_fallback_plan(recipe or "generic",
                                  workflow or os.path.join(root, "recipes", recipe or "generic", "workflow.yaml"),
                                  root, kickoff)
        if fb or _force_doc_features:
            os.makedirs(os.path.dirname(out_file), exist_ok=True)
            open(out_file, "w").write((fb or "") + "\n" if fb else "")
            print(f"plan_path={out_file}")
            print(f"task_class={task_class}")
            _trace_plan(trace_id, task_class, "success", db,
                        final_artifact_ref=out_file, reviewer_verdict="recipe_fallback")
            return 0
    # "" on every path but the one below that adopts a recipe-declared artifact:
    # MO_GIVEN_PLAN is a third source of a plan, and it is not an adoption.
    adopted = ""
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
        # The declared artifact can already be on disk before the dispatch: a
        # revived run re-enters the planner with its plan sitting in the run
        # dir, and re-planning it would cost a full-length call to reproduce a
        # file that is right there.
        adopted = _read_declared_plan(declared_plan)
        if adopted:
            sys.stderr.write(f"plan: using the recipe's declared plan artifact "
                             f"{declared_plan} (planner LLM skipped)\n")
        else:
            rc, raw = dispatch_fn(task_class, "planner", prompt)
            # Charge before judging: a rejected dispatch still spent tokens — the
            # abandoned epic-runner planner call in run-1791788217-61320 produced
            # 17,757 output tokens before the shape gate refused the lane.
            _charge_cost(db, run_id)
            if rc != 0:
                # rc=65 is a shape reject: the dispatch boundary judged the
                # lane's *reply*. A planner whose contract is "write the plan
                # file and nothing else" answers in prose by design, so before
                # failing the run, look where its plan actually belongs.
                adopted = _read_declared_plan(declared_plan)
                if not adopted:
                    sys.stderr.write(f"LLM dispatch failed for planner node (rc={rc}): {_dispatch_error_detail(raw)}\n")
                    # Record WHY the run died, not only that it did. Without this
                    # the run keeps whatever status it had, so the board shows a
                    # red run with nothing red inside it — no node ever ran, and
                    # the only explanation (the planner's LLM call) lives in
                    # ``llm_calls``, which no surface attributes to the run.
                    _mark_failed(db, run_id, f"planner_dispatch_rc_{rc}")
                    _trace_plan(trace_id, task_class, "failure", db, reason=f"planner_dispatch_rc_{rc}")
                    return 1
                sys.stderr.write(f"plan: planner dispatch rc={rc} but the recipe's declared plan "
                                 f"artifact is on disk ({declared_plan}) — adopting it\n")

    if adopted:
        raw = adopted
    plan_json = extract_plan_json(raw)
    # An adopted file IS the recipe's own plan contract, checked by the recipe's
    # own success verifier downstream. This module's schema describes a
    # DIFFERENT planner (a JSON reply carrying verifier_contract/decomposition),
    # so validating an adopted artifact here rejects the only plan the recipe
    # has — and the repair loop spends two more full dispatches learning that
    # (run-1791788217-61320: verdict "missing_verifier_contract" for a plan the
    # run's own verifier declares complete).
    verdict = "ok" if adopted else validate_plan(plan_json)

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
                    sys.stderr.write(f"LLM dispatch failed for planner repair (rc={rc}): {_dispatch_error_detail(raw)}\n")
                    _mark_failed(db, run_id, first_verdict)
                    _trace_plan(trace_id, task_class, "failure", db,
                                reviewer_verdict=first_verdict, reason=f"planner_repair_rc_{rc}")
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


def _dispatch_error_detail(raw):
    """One-line, capped tail of the dispatcher's combined output.

    The dispatch callable returns ``(rc, combined_stdout_stderr)``; on failure
    the buffer holds the provider CLI's own error prints (e.g. claude CLI
    usage errors, rc=2). Surface the last non-empty line so run logs show the
    real cause instead of a generic 'LLM dispatch failed'.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return "no dispatcher output"
    return lines[-1][:200]


def _default_llm_dispatch(root):
    """Return the native dispatch seam with the former merged-stream contract.

    The dispatcher emits the provider reply on stdout and diagnostics on stderr.
    Redirecting both to one buffer preserves shell ``2>&1`` ordering and prevents
    dispatcher output from leaking into the planner's public stdout.
    """
    from mini_ork.dispatch import llm_dispatch as native_dispatch

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
