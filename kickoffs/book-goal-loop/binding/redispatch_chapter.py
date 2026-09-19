#!/usr/bin/env python3
"""redispatch-cmd for the goal-loop: re-dispatch chapter <n> of BOOK_UUID.

Contract (recipes/goal-loop/lib/transforms.py::_run_apply): invoked as
``python3 redispatch_chapter.py <chapter_number>`` — the MO_GOAL_REDISPATCH_CMD
argv prefix with the failing unit id appended. Exit 0 == the run was re-dispatched
(or the call was a no-op because the run is already progressing); any non-zero ==
re-dispatch failed and the wave should surface it.

Why this is NOT a raw ``UPDATE book_chapter_lifecycle SET status='pending'``
------------------------------------------------------------------------------
A book's chapters share ONE ``book_generation_run``. When that run terminates its
FSM lands at ``compose_job_fsm_state.current_state = 'failed'``. The dispatch claim
query (server/services/bookGeneration/eventDrivenDispatch.ts) only claims chapters
for a run whose FSM ``current_state = 'generating'`` AND that carries a live
``hatchet_dispatch_token``. A lifecycle-row reset touches neither, so the chapter
would sit ``pending`` forever. The single sanctioned way to move a failed run back
to ``generating`` with a fresh token is ``bookGenerationJobService.forceResumeJob``
(lifecycle.ts:3249): it flips the FSM, zeroes chapter attempt budgets (needed here —
ch attempts hit the dead-letter cap), and re-enqueues to Hatchet with a rotated
token via ``enqueueBookGeneration``. We drive it through the existing operator CLI
``server/scripts/forceResumeChaptersMinimax.ts`` (same service the /force-resume
route uses; re-resolves the chapter provider FRESH).

Per-wave idempotency
--------------------
``forceResumeJob`` resumes the WHOLE job, so only the FIRST failing chapter in a
wave must trigger it. ``setRunStatus`` (lifecycle.ts:418) writes the FSM
synchronously, so the moment the first resume lands the run leaves ``'failed'`` and
every later chapter in the same wave observes a non-failed FSM and no-ops here.

book_uuid vs run.book_id
------------------------
The goal unit space is keyed by the generated-book uuid (BOOK_UUID), but a run's
``book_id`` is a different identity. They are bridged by ``book_identity_aliases``
(``alias_kind = 'generated_book_uuid'``), mirroring
bookIdentityResolver.resolveGeneratedBookUuidByJobId — so we join that alias to go
book_uuid -> run -> job_id, never guessing from book_id/document_uuid.

Why we reconcile the chapter model to the live runner before resuming
---------------------------------------------------------------------
A chapter's model literal lives in TWO independent channels that drift apart: the
worker's env ``CHAPTER_PRIMARY_MODEL`` (chapterExecutionRuntime binds it into the
exact-model readyz contract at load) and the run's PERSISTED
``provenance.chapter_dispatch.model`` (frozen at creation). A RESUMED job dispatches
from PROVENANCE, ignoring the worker env — so if server/.env was bumped (observed:
'glm-5.3-flash') while the microVM runner still serves 'glm-5.3', the resume
re-fails the exact-model preflight within seconds ('readiness response violated the
exact runtime contract'). This binding now expands the loop's action space beyond
'edit researcher code' to include reconciling that drift: it asks the runner's
/readyz which model it ACTUALLY serves (the single source of truth — the model
travels with the gateway env, not a redeploy) and aligns BOTH channels to it —
``jsonb_set`` on the persisted provenance (what the resumed dispatch reads) AND a
``CHAPTER_PRIMARY_MODEL`` pin in the resume subprocess env (forceResumeChaptersMinimax
re-resolves the provider FRESH from its env and writes it back to provenance). Both
target the same served model, so they can't conflict; best-effort — an unreachable
runner leaves both untouched (an unreachable runner can't generate anyway).

Env
    BOOK_UUID               (required) the generated-book uuid (goal unit space)
    MO_RESEARCHER_DIR       researcher checkout with node_modules + server/.env
                            (default: the primary checkout; the worktree may lack
                            node_modules/server/.env — the resume's DB + Hatchet
                            effects are identical regardless of checkout)
    MO_GOAL_TARGET_CWD      the worktree the worker runs from; its server/.env is
                            the preferred source of the runner URL/token (falls back
                            to MO_RESEARCHER_DIR — both point at the same runner)
    MO_GOAL_REDISPATCH_DRY  =1 -> resolve + decide + print the plan, do NOT resume
    PG* libpq vars          DB connection (no secret lives in this file)

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

_DEFAULT_RESEARCHER_DIR = "/Volumes/docker-ssd/Migration/Development/researcher"
# A run in any of these FSM states is already progressing (or done): re-dispatch
# is a no-op. We resume ONLY out of a terminal-failed state.
_FAILED_STATES = frozenset({"failed"})
# Chapter microVM runner probe: read these from server/.env to ask the runner's
# /readyz which model it serves, then align both model channels to match.
_RUNNER_KEYS = ("CHAPTER_MICROVM_RUNNER_URL", "CHAPTER_MICROVM_RUNNER_TOKEN")
_RUNNER_PROBE_TIMEOUT = 6
# A served model id must match this before it is trusted in SQL / an env pin — this
# is both the injection guard (no quotes/semicolons reach the UPDATE) and a sanity
# filter on the runner's advertised model (e.g. 'glm-5.3', 'MiniMax-M3').
_MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")


def _q(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "100.74.239.22"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
    )


def _resolve_job(book: str) -> tuple[str | None, str | None, str | None]:
    """book_uuid -> (job_id, run_uuid, fsm_state) via the generated_book_uuid alias."""
    sql = (
        "SELECT bgr.job_id, bgr.id::text, coalesce(f.current_state,'(none)') "
        "FROM book_generation_runs bgr "
        "JOIN book_identity_aliases alias "
        "  ON alias.book_id = bgr.book_id AND alias.alias_kind = 'generated_book_uuid' "
        "LEFT JOIN compose_job_fsm_state f ON f.job_id = bgr.id "
        f"WHERE alias.alias_value = '{book}' "
        "ORDER BY bgr.updated_at DESC LIMIT 1;"
    )
    proc = _q(sql)
    if proc.returncode != 0:
        print(f"db-error: {proc.stderr.strip()[:120]}", file=sys.stderr)
        return None, None, None
    rows = proc.stdout.strip().splitlines()
    if not rows:
        return None, None, None
    cols = (rows[0].split("|") + ["", "", ""])[:3]
    return cols[0] or None, cols[1] or None, cols[2] or None


def _read_dotenv(path: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Minimal dotenv reader for a fixed key set (values may be quoted / have an
    inline '='). Empty or absent values are skipped. Mirrors the reader in
    restart_worker.py — these bindings are deliberately self-contained."""
    out: dict[str, str] = {}
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, val = line.partition("=")
                key = key.strip()
                if key not in keys:
                    continue
                val = val.strip().strip('"').strip("'").strip()
                if val:
                    out[key] = val
    except FileNotFoundError:
        pass
    return out


def _runner_model(env_dirs: list[str]) -> tuple[str | None, str]:
    """Best-effort: ask the Chapter microVM runner's /readyz which model it serves
    (the single source of truth — see the module docstring). Reads the runner
    URL/token from the first server/.env found among env_dirs (the worktree the
    worker runs from, then the resume checkout — both point at the SAME runner).
    Returns (model, why) on a ready runner with an allow-listed model, else
    (None, why). Never raises — a probe miss degrades to leaving both channels
    untouched (an unreachable runner can't generate anyway)."""
    cfg: dict[str, str] = {}
    src: str | None = None
    for d in env_dirs:
        if not d:
            continue
        cfg = _read_dotenv(os.path.join(d, "server", ".env"), _RUNNER_KEYS)
        if cfg.get("CHAPTER_MICROVM_RUNNER_URL") and cfg.get("CHAPTER_MICROVM_RUNNER_TOKEN"):
            src = d
            break
    url = cfg.get("CHAPTER_MICROVM_RUNNER_URL", "").rstrip("/")
    token = cfg.get("CHAPTER_MICROVM_RUNNER_TOKEN", "")
    if not url or not token:
        return None, "runner url/token absent from server/.env; chapter model left as-is"
    req = urllib.request.Request(f"{url}/readyz", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_RUNNER_PROBE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # unreachable / timeout / non-JSON — stay best-effort
        return None, f"runner /readyz probe failed ({exc!r}); chapter model left as-is"
    model = body.get("model") if isinstance(body, dict) else None
    status = body.get("status") if isinstance(body, dict) else None
    if status != "ready" or not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        return None, f"runner not ready / bad model (status={status!r}, model={model!r}); left as-is"
    return model, f"live runner serves {model!r} (from {src}/server/.env)"


def _reconcile_provenance_model(run_uuid: str, model: str) -> tuple[bool, str]:
    """Align the run's PERSISTED provenance.chapter_dispatch.model to the live runner
    model, so a RESUMED job (which dispatches from provenance, not the worker env)
    can't re-fail the exact-model preflight. Idempotent + non-fabricating: only
    rewrites when a chapter_dispatch.model already exists AND differs (the resume's
    fresh re-resolve creates the block if it is absent). Returns (changed, why);
    never raises — a DB miss degrades to leaving provenance as-is (the env pin is the
    second channel)."""
    if not run_uuid or not _UUID_RE.fullmatch(run_uuid):
        return False, f"run uuid invalid ({run_uuid!r}); skipped provenance reconcile"
    if not _MODEL_RE.fullmatch(model or ""):
        return False, f"model unsafe ({model!r}); skipped provenance reconcile"
    sql = (
        "UPDATE book_generation_runs "
        "SET provenance = jsonb_set(provenance::jsonb, '{chapter_dispatch,model}', "
        f"to_jsonb('{model}'::text), false) "
        f"WHERE id = '{run_uuid}' "
        "AND provenance -> 'chapter_dispatch' ? 'model' "
        "AND provenance #>> '{chapter_dispatch,model}' IS DISTINCT FROM "
        f"'{model}' "
        "RETURNING provenance #>> '{chapter_dispatch,model}';"
    )
    proc = _q(sql)
    if proc.returncode != 0:
        return False, f"provenance reconcile db-error: {proc.stderr.strip()[:120]}"
    if proc.stdout.strip():  # a row was updated -> the value flipped
        return True, f"provenance.chapter_dispatch.model -> {model!r}"
    return False, f"provenance already aligned to {model!r} (or no chapter_dispatch.model to pin)"


def _force_resume(job_id: str, researcher_dir: str, model: str | None = None) -> tuple[bool, str]:
    """Drive the sanctioned operator CLI; return (accepted, human_reason). When
    `model` is set, pin CHAPTER_PRIMARY_MODEL into the subprocess env so the resume's
    FRESH provider re-resolve writes the runner-aligned model back to provenance."""
    tsx = os.path.join(researcher_dir, "node_modules", ".bin", "tsx")
    script = os.path.join("server", "scripts", "forceResumeChaptersMinimax.ts")
    if not os.path.isfile(tsx):
        return False, f"tsx not found at {tsx} (set MO_RESEARCHER_DIR to a built checkout)"
    if not os.path.isfile(os.path.join(researcher_dir, script)):
        return False, f"{script} missing under {researcher_dir}"
    env = None
    if model:
        env = dict(os.environ)
        env["CHAPTER_PRIMARY_MODEL"] = model  # fresh re-resolve writes this to provenance
    proc = subprocess.run(
        [tsx, script, job_id],
        cwd=researcher_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    # forceResumeChaptersMinimax.ts always exits 0 and prints one JSON object per
    # jobId on stdout ({jobId, success, failureCode, ...}); logger noise may
    # interleave, so scan for the object carrying `success`.
    result: dict | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "success" in obj and obj.get("jobId") == job_id:
            result = obj
            break
    if result is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return False, f"no force-resume receipt parsed (rc={proc.returncode}); tail: {tail}"
    if result.get("success") is True:
        reset = result.get("resetChapters") or []
        pending = result.get("pendingChapters") or []
        return True, f"resumed reset={len(reset)} pending={len(pending)}"
    code = result.get("failureCode")
    if code == "cooldown":
        # Another redispatch already resumed this job moments ago -> idempotent OK.
        return True, "already resumed (cooldown)"
    if code == "completed":
        return True, "job already completed"
    return False, f"force-resume refused: {code}: {str(result.get('message'))[:160]}"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: redispatch_chapter.py <chapter_number>", file=sys.stderr)
        return 2
    chapter = argv[0].strip()
    if not re.fullmatch(r"\d+", chapter):
        print(f"bad chapter id: {chapter!r}", file=sys.stderr)
        return 2
    book = os.environ.get("BOOK_UUID", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        print(f"BOOK_UUID unset/invalid: {book!r}", file=sys.stderr)
        return 2

    job_id, run_uuid, fsm = _resolve_job(book)
    if not job_id:
        print(f"ch{chapter} redispatch: no run/job for book {book}")
        return 3

    if fsm not in _FAILED_STATES:
        print(f"ch{chapter} redispatch noop: run {job_id} fsm={fsm} (already progressing)")
        return 0

    # Reconcile the chapter model to the live runner BEFORE resuming — a resumed job
    # dispatches from provenance, so runner drift would re-fail the exact-model
    # preflight in seconds. Best-effort: an unreachable runner leaves both channels
    # untouched (probe happens after the fsm==failed gate, only when we WILL resume).
    worktree = os.environ.get("MO_GOAL_TARGET_CWD", "").strip()
    researcher_dir = os.environ.get("MO_RESEARCHER_DIR", "").strip() or _DEFAULT_RESEARCHER_DIR
    model, why_model = _runner_model([worktree, researcher_dir])
    print(f"ch{chapter} redispatch runner-model: {why_model}")

    dry = os.environ.get("MO_GOAL_REDISPATCH_DRY", "").strip() == "1"
    if dry:
        plan = f"would force-resume {job_id} (run {run_uuid}, fsm={fsm})"
        if model:
            plan += (
                f"; would reconcile provenance.chapter_dispatch.model->{model} "
                f"+ pin CHAPTER_PRIMARY_MODEL={model} into the resume"
            )
        print(f"ch{chapter} redispatch DRY: {plan}")
        return 0

    if model:
        _changed, why_prov = _reconcile_provenance_model(run_uuid or "", model)
        print(f"ch{chapter} redispatch provenance: {why_prov}")

    accepted, reason = _force_resume(job_id, researcher_dir, model)
    tag = "ok" if accepted else "FAILED"
    print(f"ch{chapter} redispatch {tag}: {job_id} fsm={fsm} -> {reason}")
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
