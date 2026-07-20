"""Python port of lib/memory.sh — shared-memory primitive for mini-ork v2+v3.

Wraps state.db reads/writes for the 14 memory tables defined in
db/migrations/0006/0007/0008/0009. Strangler-fig parity port:
- env-var contract preserved (MINI_ORK_DB, MINI_ORK_ROOT, MO_CYCLE_ID,
  MINI_ORK_RUN_DIR, MINI_ORK_RUN_ID, MINI_ORK_KICKOFF_PATH, REPO_ROOT)
- SQL strings ported verbatim from the bash heredocs so the two sides
  insert against identical schemas/ORDER BYs/conflict clauses
- JSON columns pass through as opaque strings (design rule §2 in
  lib/memory.sh:1-19) — no json.dumps/json.loads layer on top
- reflection at call time = git HEAD + timestamp + per-citation
  fingerprint + per-citation blame SHA (driven by the same
  _mo_capture_reflection helper the bash shell-outs)
- D3 writers (memory_write_task, memory_write_failure) wrap their
  inserts in try/except that matches the bash `|| return 0` swallow
  contract — they log errors to trace-write-errors.log but never raise

Public surface mirrors the bash function names:

    mo_mem_put_arch_spec(...)   → put_arch_spec(...)
    mo_mem_list_arch_specs(...) → list_arch_specs(...)
    mo_mem_get_arch_spec(...)   → get_arch_spec(...)            (returns dict or None)
    mo_mem_get_node_annotation(...) → get_node_annotation(...) (returns dict or None)
    mo_mem_put_node_annotation(...) → put_node_annotation(...)
    mo_mem_record_inspector_run(...) → record_inspector_run(...)
    mo_mem_put_module_plan(...)   → put_module_plan(...)
    mo_mem_list_module_plans(...) → list_module_plans(...)      (TSV string)
    mo_mem_put_atom_pr(...)       → put_atom_pr(...)
    mo_mem_list_atom_prs(...)     → list_atom_prs(...)          (TSV string)
    mo_mem_put_adr(...)           → put_adr(...)
    mo_mem_list_adrs(...)         → list_adrs(...)              (TSV string)
    mo_mem_smoke()                → smoke(...)                  (returns (msg, rc))
    memory_write_task(...)        → write_task(...)
    memory_write_failure(...)     → write_failure(...)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid


# ─── Env resolution (mirrors `${VAR:-default}` semantics) ─────────────


def _db_path() -> str:
    return (
        os.environ.get("MINI_ORK_DB")
        or os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/state.db"
    )


def _cycle_id() -> str:
    return os.environ.get("MO_CYCLE_ID") or f"cycle-{time.strftime('%Y%m%d-%H%M%S')}"


# ─── Internal helpers (port of _mo_now, _mo_repo_root, _mo_git_head,
#      _mo_capture_reflection) ──────────────────────────────────────────


def _now() -> int:
    return int(time.time())


def _repo_root() -> str:
    env = os.environ.get("REPO_ROOT")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.getcwd()


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", _repo_root(), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def _content_hash(path: str) -> str:
    abs_p = os.path.join(_repo_root(), path)
    try:
        with open(abs_p, "rb") as f:
            return "sha256:" + hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _fingerprint(path: str, line: int) -> str:
    abs_p = os.path.join(_repo_root(), path)
    try:
        with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if 0 < line <= len(lines):
            return lines[line - 1].strip()[:80]
    except OSError:
        pass
    return ""


def _blame_sha(path: str, line: int) -> str:
    repo_root = _repo_root()
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "blame", "-L", f"{line},{line}",
             "--porcelain", "--", path],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.split()[0][:16]
    except Exception:
        pass
    return ""


def _capture_reflection(cited_json: str = "[]") -> str:
    """Port of _mo_capture_reflection. Echoes reflected_substrate JSON."""
    try:
        cited = json.loads(cited_json or "[]")
        if not isinstance(cited, list):
            cited = []
    except json.JSONDecodeError:
        cited = []

    head_sha = _git_head()
    cited_files = []
    seen = set()
    for citation in cited:
        if not isinstance(citation, str) or ":" not in citation:
            continue
        path, _, line_str = citation.rpartition(":")
        try:
            line = int(line_str)
        except ValueError:
            continue
        if (path, line) in seen:
            continue
        seen.add((path, line))
        cited_files.append({
            "path": path,
            "content_hash_xxh3": _content_hash(path),
            "line_range_start": line,
            "line_range_end": line,
            "blame_sha_at_lines": _blame_sha(path, line),
            "fingerprint_text": _fingerprint(path, line),
        })
    return json.dumps({"cited_files": cited_files, "git_head_at_write": head_sha})


# ─── ARCH-SPECs ────────────────────────────────────────────────────────


def put_arch_spec(arch_id: str, feature: str, title: str,
                  precondition: str, postcondition: str, verifier: str,
                  frame_json: str = "[]", evidence_json: str = "[]",
                  info_gain: str = "0.0") -> None:
    db = _db_path()
    now = _now()
    head = _git_head()
    reflection = _capture_reflection(evidence_json)
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO arch_specs (
        arch_id, feature, cycle_id, title, precondition, postcondition,
        frame_json, info_gain, verifier, evidence_for_pre, status,
        via_gate, reflection_at, reflection_sha, reflected_substrate,
        reflection_status, reflection_last_check, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(arch_id) DO UPDATE SET
        title=excluded.title,
        precondition=excluded.precondition,
        postcondition=excluded.postcondition,
        frame_json=excluded.frame_json,
        info_gain=excluded.info_gain,
        verifier=excluded.verifier,
        evidence_for_pre=excluded.evidence_for_pre,
        reflection_at=excluded.reflection_at,
        reflection_sha=excluded.reflection_sha,
        reflected_substrate=excluded.reflected_substrate,
        reflection_status='fresh',
        reflection_last_check=excluded.reflection_last_check,
        updated_at=excluded.updated_at
    """, (
        arch_id, feature, _cycle_id(), title, precondition, postcondition,
        frame_json, float(info_gain), verifier, evidence_json, "proposed",
        "architectural_decision_gate", int(now), head, reflection,
        "fresh", int(now), int(now), int(now),
    ))
    con.commit()
    con.close()


def list_arch_specs(feature: str, status: str = "accepted") -> str:
    db = _db_path()
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT arch_id, title, info_gain, reflection_status, status "
        "FROM arch_specs WHERE feature=? AND status=? "
        "ORDER BY info_gain DESC, arch_id ASC",
        (feature, status),
    ).fetchall()
    con.close()
    return "".join("\t".join(_row_val(v) for v in r) + "\n" for r in rows)


def get_arch_spec(arch_id: str) -> dict | None:
    db = _db_path()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM arch_specs WHERE arch_id=?", (arch_id,)).fetchone()
    con.close()
    return _row_dict(row) if row else None


# ─── NodeAnnotations ───────────────────────────────────────────────────


def get_node_annotation(node_id: str, content_hash: str) -> dict | None:
    db = _db_path()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM node_annotations WHERE node_id=? AND content_hash=?",
        (node_id, content_hash),
    ).fetchone()
    con.close()
    return _row_dict(row) if row else None


def put_node_annotation(node_id: str, file_path: str, symbol: str,
                        content_hash: str, task: str,
                        pre_state: str, post_state: str,
                        mutating: str = "0", side_effects: str = "[]") -> None:
    db = _db_path()
    now = _now()
    head = _git_head()
    reflection = _capture_reflection(f'["{file_path}:1"]')
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO node_annotations (
        node_id, file_path, symbol_name, content_hash, task,
        pre_state_json, post_state_json, mutating, side_effects_json,
        annotated_at, annotated_by_cycle,
        via_gate, reflection_at, reflection_sha, reflected_substrate,
        reflection_status, reflection_last_check
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(node_id) DO UPDATE SET
        content_hash=excluded.content_hash,
        task=excluded.task,
        pre_state_json=excluded.pre_state_json,
        post_state_json=excluded.post_state_json,
        mutating=excluded.mutating,
        side_effects_json=excluded.side_effects_json,
        annotated_at=excluded.annotated_at,
        reflection_at=excluded.reflection_at,
        reflection_sha=excluded.reflection_sha,
        reflected_substrate=excluded.reflected_substrate,
        reflection_status='fresh',
        reflection_last_check=excluded.reflection_last_check
    """, (
        node_id, file_path, symbol, content_hash,
        task, pre_state, post_state, int(mutating), side_effects,
        int(now), _cycle_id(),
        "verifier_run_gate", int(now), head, reflection,
        "fresh", int(now),
    ))
    con.commit()
    con.close()


# ─── Inspector runs (dual-inspector audit) ─────────────────────────────


def record_inspector_run(site: str, prompt_hash: str,
                         opus_v: str, codex_v: str,
                         opus_rc: str, codex_rc: str,
                         agreement: str, actions_diff: str = "null",
                         final_v: str = "",
                         dur_opus: str = "0", dur_codex: str = "0",
                         fallback: str = "") -> None:
    db = _db_path()
    now = _now()
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO inspector_runs (
        site, cycle_id, prompt_hash, opus_verdict_json, codex_verdict_json,
        opus_rc, codex_rc, agreement, actions_diff_json, final_verdict_json,
        fallback_reason, duration_ms_opus, duration_ms_codex, ran_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        site, _cycle_id(), prompt_hash,
        opus_v, codex_v,
        int(opus_rc), int(codex_rc), int(agreement),
        actions_diff if actions_diff != "null" else None,
        final_v,
        fallback if fallback else None,
        int(dur_opus), int(dur_codex), int(now),
    ))
    con.commit()
    con.close()


# ─── MODULE-PLAN candidates ────────────────────────────────────────────


def put_module_plan(module_id: str, candidate_id: str, arch_id: str,
                    label: str, files_touched: str = "0",
                    new_files_json: str = "[]",
                    cohesion: str = "0.0", coupling: str = "0.0",
                    files_score: str = "0.0", volatility: str = "0.0",
                    frame_json: str = "[]", is_recommended: str = "0") -> None:
    db = _db_path()
    now = _now()
    head = _git_head()
    reflection = _capture_reflection(new_files_json)
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO module_plans (
        module_id, candidate_id, arch_id, cycle_id, label, files_touched,
        new_files_json, cohesion_score, coupling_score, files_touched_score,
        volatility_score, frame_json, is_recommended, status,
        via_gate, reflection_at, reflection_sha, reflected_substrate,
        reflection_status, reflection_last_check, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(module_id, candidate_id) DO UPDATE SET
        cohesion_score=excluded.cohesion_score,
        coupling_score=excluded.coupling_score,
        is_recommended=excluded.is_recommended,
        reflection_at=excluded.reflection_at,
        reflection_sha=excluded.reflection_sha,
        reflected_substrate=excluded.reflected_substrate,
        reflection_status='fresh'
    """, (
        module_id, candidate_id, arch_id, _cycle_id(), label, int(files_touched),
        new_files_json, float(cohesion), float(coupling), float(files_score),
        float(volatility), frame_json, int(is_recommended), "proposed",
        "architectural_decision_gate", int(now), head, reflection,
        "fresh", int(now), int(now),
    ))
    con.commit()
    con.close()


def list_module_plans(arch_id: str) -> str:
    db = _db_path()
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT module_id, candidate_id, label, cohesion_score, coupling_score, "
        "is_recommended FROM module_plans WHERE arch_id=? "
        "ORDER BY is_recommended DESC, cohesion_score DESC",
        (arch_id,),
    ).fetchall()
    con.close()
    return "".join("\t".join(_row_val(v) for v in r) + "\n" for r in rows)


# ─── ATOM-PRs ──────────────────────────────────────────────────────────


def put_atom_pr(pr_id: str, module_id: str, candidate_id: str = "",
                title: str = "", kind: str = "rename",
                frame_json: str = "[]", depends_on: str = "[]",
                test_gate: str = "true",
                functoriality_check: str = "true") -> None:
    db = _db_path()
    now = _now()
    head = _git_head()
    reflection = _capture_reflection(frame_json)
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO atom_prs (
        pr_id, module_id, candidate_id, cycle_id, title, kind,
        frame_json, depends_on_json, test_gate, functoriality_check, status,
        via_gate, reflection_at, reflection_sha, reflected_substrate,
        reflection_status, reflection_last_check, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(pr_id) DO UPDATE SET
        title=excluded.title,
        depends_on_json=excluded.depends_on_json,
        test_gate=excluded.test_gate,
        reflection_at=excluded.reflection_at,
        reflection_sha=excluded.reflection_sha,
        reflected_substrate=excluded.reflected_substrate,
        reflection_status='fresh'
    """, (
        pr_id, module_id, candidate_id if candidate_id else None, _cycle_id(),
        title, kind, frame_json, depends_on, test_gate, functoriality_check, "pending",
        "artifact_committed_gate", int(now), head, reflection,
        "fresh", int(now), int(now),
    ))
    con.commit()
    con.close()


def list_atom_prs(module_id: str, status: str = "pending") -> str:
    db = _db_path()
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT pr_id, kind, title, status FROM atom_prs "
        "WHERE module_id=? AND status=? ORDER BY pr_id",
        (module_id, status),
    ).fetchall()
    con.close()
    return "".join("\t".join(_row_val(v) for v in r) + "\n" for r in rows)


# ─── ADRs ──────────────────────────────────────────────────────────────


def put_adr(adr_id: str, arch_id: str, title: str,
            precondition: str, postcondition: str, verifier: str,
            body: str, supersedes: str = "") -> None:
    db = _db_path()
    now = _now()
    head = _git_head()
    reflection = _capture_reflection("[]")
    con = sqlite3.connect(db)
    con.execute("""
      INSERT INTO adrs (
        adr_id, arch_id, title, status, supersedes, precondition, postcondition,
        verifier, body_md,
        via_gate, reflection_at, reflection_sha, reflected_substrate,
        reflection_status, reflection_last_check, written_at, written_by_cycle
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(adr_id) DO UPDATE SET
        title=excluded.title,
        body_md=excluded.body_md,
        reflection_at=excluded.reflection_at,
        reflection_sha=excluded.reflection_sha,
        reflection_status='fresh'
    """, (
        adr_id, arch_id if arch_id else None, title, "accepted",
        supersedes if supersedes else None,
        precondition, postcondition, verifier, body,
        "architectural_decision_gate", int(now), head, reflection,
        "fresh", int(now), int(now), _cycle_id(),
    ))
    con.commit()
    con.close()


def list_adrs(status: str = "accepted") -> str:
    db = _db_path()
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT adr_id, title, supersedes FROM adrs WHERE status=? ORDER BY adr_id",
        (status,),
    ).fetchall()
    con.close()
    return "".join("\t".join(_row_val(v) for v in r) + "\n" for r in rows)


# ─── Smoke ─────────────────────────────────────────────────────────────


def smoke(db: str | None = None) -> tuple[str, int]:
    db = db if db is not None else _db_path()
    con = sqlite3.connect(db)
    cnt = con.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ("
        "'arch_specs','module_plans','atom_prs','adrs','node_annotations',"
        "'communities','validations','fixes','cascade_invalidations',"
        "'reflection_log','decision_basins','decision_basin_membership',"
        "'emergent_patterns','inspector_runs'"
        "))"
    ).fetchone()[0]
    con.close()
    if cnt == 14:
        return ("OK — 14 memory tables present", 0)
    return (f"FAIL — expected 14 tables, found {cnt}", 1)


# ─── D3 task-class writers ─────────────────────────────────────────────
#
# Best-effort: matches bash `|| return 0` swallow contract. Errors land
# in ${MINI_ORK_RUN_DIR}/trace-write-errors.log (or /tmp/trace-write-
# errors.log when MINI_ORK_RUN_DIR is unset). NEVER raises.


_OUTCOME_WHITELIST = {"success", "failure", "partial"}
_CATEGORY_WHITELIST = {"verifier_fail", "timeout", "cost_overrun", "dispatch_error"}


def _resolve_runs_id(uid: str = "") -> int:
    rid_str = uid or os.environ.get("MINI_ORK_RUN_ID", "")
    if not rid_str:
        return 0
    try:
        db = _db_path()
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT id FROM runs WHERE run_dir LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{rid_str}%",),
        ).fetchone()
        con.close()
        if row is None:
            return 0
        return int(row[0] or 0)
    except Exception:
        return 0


def _trace_err_path() -> str:
    return os.environ.get("MINI_ORK_RUN_DIR", "/tmp") + "/trace-write-errors.log"


def write_task(task_class: str, outcome: str = "success",
               duration_ms: str = "0", cost_usd: str = "0",
               artifacts_json: str = "[]") -> None:
    """Memory writer for the task_memory table. Best-effort: never raises.

    Mirrors the bash idem-potent sentinel contract: if MINI_ORK_RUN_DIR is
    set and the run already has .task_memory_written, the call is a no-op.
    """
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir:
        sentinel = f"{run_dir}/.task_memory_written"
        if os.path.isfile(sentinel):
            return
    if outcome not in _OUTCOME_WHITELIST:
        outcome = "partial"
    err_path = _trace_err_path()

    kickoff_hash = ""
    kickoff_path = os.environ.get("MINI_ORK_KICKOFF_PATH", "")
    if kickoff_path and os.path.isfile(kickoff_path):
        try:
            with open(kickoff_path, "rb") as f:
                kickoff_hash = hashlib.sha256(f.read()).hexdigest()
        except Exception:
            kickoff_hash = ""

    rid = _resolve_runs_id()

    try:
        artifacts = json.loads(artifacts_json) if artifacts_json.strip() else []
        if not isinstance(artifacts, list):
            artifacts = []
    except json.JSONDecodeError:
        artifacts = []

    try:
        db = _db_path()
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO task_memory
                (run_id, task_class, kickoff_hash, outcome,
                 artifacts_produced, duration_ms, cost_usd)
            VALUES (?,?,?,?,?,?,?)
            """,
            (int(rid or 0), task_class, kickoff_hash or "", outcome,
             json.dumps(artifacts), int(float(duration_ms or 0)),
             float(cost_usd or 0)),
        )
        con.commit()
        con.close()
    except Exception as e:
        try:
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"[memory.write_task] {task_class}: {e}\n")
        except Exception:
            pass
        return

    if run_dir:
        try:
            os.makedirs(run_dir, exist_ok=True)
            open(f"{run_dir}/.task_memory_written", "w").close()
        except Exception:
            pass


def write_failure(stage: str, category: str = "dispatch_error",
                  error_message: str = "") -> None:
    """Memory writer for the failure_memory table. Best-effort: never raises."""
    if category not in _CATEGORY_WHITELIST:
        category = "dispatch_error"
    err_path = _trace_err_path()

    rid = _resolve_runs_id()
    fid = ""
    try:
        fid = str(uuid.uuid4())
    except Exception:
        return

    try:
        db = _db_path()
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO failure_memory
                (failure_id, run_id, workflow_stage, failure_category,
                 error_message, stack_trace)
            VALUES (?,?,?,?,?,?)
            """,
            (fid, int(rid or 0), stage, category,
             (error_message or "")[:8000], ""),
        )
        con.commit()
        con.close()
    except Exception as e:
        try:
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"[memory.write_failure] {stage}: {e}\n")
        except Exception:
            pass


# ─── Bash-function-name aliases (parity with lib/memory.sh) ──────────
#
# These let a caller use either `from mini_ork.memory.store import
# mo_mem_put_arch_spec` or `from mini_ork.memory.store import
# put_arch_spec` interchangeably. Port-as-bash-parity keeps the original
# surface live.


mo_mem_put_arch_spec = put_arch_spec
mo_mem_list_arch_specs = list_arch_specs
mo_mem_get_arch_spec = get_arch_spec
mo_mem_get_node_annotation = get_node_annotation
mo_mem_put_node_annotation = put_node_annotation
mo_mem_record_inspector_run = record_inspector_run
mo_mem_put_module_plan = put_module_plan
mo_mem_list_module_plans = list_module_plans
mo_mem_put_atom_pr = put_atom_pr
mo_mem_list_atom_prs = list_atom_prs
mo_mem_put_adr = put_adr
mo_mem_list_adrs = list_adrs
mo_mem_smoke = smoke


# ─── Small helpers used by parity tests + ts/list_* output shaping ───


def _row_val(v):
    """Stable repr for sqlite3 field values that mirrors ts/list_* stdout."""
    if v is None:
        return ""
    return str(v)


def _row_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}
