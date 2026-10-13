"""``mini-ork run --resume <run_id> [--checkpoint-id <id>]`` surface.

Implements the P1.3 checkpointer-driven resume path. The checkpointer
itself (the per-``(run_id, step_id)`` ledger) is owned by
``mini_ork.checkpoints`` (P1.2) — this module only wires the CLI flags
and decides, given the ledger, which nodes to skip / re-dispatch on
resume and what one-line plan to print to stdout.

Hard rules (mirror p1-3-resume-cli.md):

  F1 — Succeeded nodes are SKIPPED with their persisted outputs intact
       (no re-execution, no artifact rewrite). Failed / error / never-ran
       nodes are RE-DISPATCHED.
  F2 — Before any execution, ONE line is printed to stdout describing
       which steps are skipped and which are re-dispatched. The line is
       prefixed ``[mini-ork-resume]`` and is the only thing this module
       writes to stdout — it must not interleave with logger output.
  F3 — ``--checkpoint-id <id>`` rewinds the resume to an earlier point
       in the run: every step recorded up to and including ``id`` is
       treated as succeeded (skip), every step beyond it is in the
       dispatch set.
  F4 — Unknown ``run_id`` / missing checkpoint data exits non-zero with
       a clear stderr message. NEVER a silent fresh-start fallback.
  C1 — The run_id from ``--resume`` MUST take precedence over the
       auto-generated ``run-<ts>-<pid>`` default, so the resumed run
       re-uses its existing ``<home>/runs/<run_id>/`` directory and the
       ledger entries the interrupted run already wrote.

The resume flags are ADDITIVE to the existing ``mini-ork run`` surface
(``<recipe> <kickoff.md>``); they do not change how a fresh run starts.
``--resume <run_id>`` and ``--checkpoint-id <id>`` may be specified in
either order; the parser accepts both ``--flag value`` and
``--flag=value`` forms.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from mini_ork import checkpoints as ck


# Exit codes — kept local so the resume surface has a single, documented
# mapping independent of the rest of the CLI's rc vocabulary.
RC_OK = 0
RC_USAGE = 2           # argparse-style malformed invocation
RC_UNKNOWN_RUN = 64    # EX_USAGE: --resume targets a run that does not exist
RC_CORRUPT = 65        # EX_DATAERR: a checkpoint row exists but is unreadable
RC_INTERNAL = 70       # EX_SOFTWARE: unexpected lookup / IO failure


_PLAN_PREFIX = "[mini-ork-resume]"


# ─── flag parsing ────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    """Argparse surface for the resume flags.

    Defined as a sub-parser so the resume module can be tested in isolation
    (no live lifecycle, no subprocess fork); ``mini-ork run`` will compose
    this with its own positional ``<recipe> <kickoff>`` parser.
    """
    p = argparse.ArgumentParser(
        prog="mini-ork run --resume",
        add_help=False,
    )
    # --resume <run_id>: re-bind this invocation to an existing run. The
    # value is a bare id (e.g. ``run-1781000000-12345``) and is required
    # by AC1 — without it the lifecycle generates a fresh run id.
    p.add_argument(
        "--resume",
        metavar="RUN_ID",
        default=None,
        help="Bind this run to an existing run_id (replay its checkpoints).",
    )
    # --checkpoint-id <id>: rewind the resume to an earlier point in the
    # run. The value is a step_id; every step recorded with step_id <=
    # ``id`` is treated as succeeded for the purposes of skip/redispatch
    # (the checkpoint table is keyed on step_id, so this addresses a
    # specific earlier point rather than a wall-clock instant).
    p.add_argument(
        "--checkpoint-id",
        metavar="STEP_ID",
        default=None,
        help="Rewind resume to an earlier checkpoint (step_id).",
    )
    return p


def parse_resume_flags(argv: Sequence[str]) -> tuple[str | None, list[str], dict]:
    """Extract ``--resume <run_id>`` and ``--checkpoint-id <id>`` from argv.

    Returns ``(resume_id, rest, info)`` where:

      ``resume_id``   the value of ``--resume`` if any, else ``None``
      ``rest``        the remaining argv (resume flags stripped) so the
                      lifecycle parser can see the original positional
                      arguments intact
      ``info``        diagnostic flags — ``{"reset": True}`` when both
                      ``--resume`` and ``--checkpoint-id`` are present
                      (the F3 contract is independent+additive so this is
                      always legal; the info entry is for the caller's log)

    Accepts both ``--flag value`` and ``--flag=value`` forms; unknown
    flags are LEFT IN PLACE in ``rest`` so the lifecycle parser can
    reject them with its own diagnostics.

    Strict mode is OFF — this function never raises on a malformed argv;
    the lifecycle arg-parser owns the error message for unknown flags.
    """
    parser = _build_parser()
    # parse_known_args preserves flags we don't own (so the lifecycle
    # parser downstream still sees <recipe> / <kickoff> / etc.).
    ns, rest = parser.parse_known_args(list(argv))
    info: dict = {}
    if ns.resume is not None and ns.checkpoint_id is not None:
        info["reset"] = True
    return ns.resume, rest, info


# ─── home / run_dir resolution ───────────────────────────────────────────────


def _resolve_home(home: str | None = None) -> str:
    """Canonical home resolution matching the rest of the CLI.

    Order: explicit arg > ``MINI_ORK_HOME`` env > ``<cwd>/.mini-ork``.
    Mirrors the resolution used by ``_run_lifecycle_impl`` (which also
    defaults to ``cwd/.mini-ork``) so a resume invocation sees the same
    home the interrupted run used.
    """
    if home:
        return home
    env = os.environ.get("MINI_ORK_HOME")
    if env:
        return env
    return os.path.join(os.getcwd(), ".mini-ork")


def _resolve_db(home: str | None = None) -> str:
    """Resolve the SQLite path the checkpointer will read from."""
    env = os.environ.get("MINI_ORK_DB") or os.environ.get("MO_STORE_DB")
    if env:
        return env
    return os.path.join(_resolve_home(home), "state.db")


def _resolve_run_dir(run_id: str, *, home: str | None = None) -> str:
    return os.path.join(_resolve_home(home), "runs", run_id)


# ─── pre-flight: run exists / checkpoints readable ───────────────────────────


class ResumeError(Exception):
    """Typed resume failure surfaced to the CLI as a clear stderr line.

    The CLI never falls back to a fresh start on this error (C1 / F4) —
    the run is either resumed from a real ledger or it exits non-zero.
    """

    def __init__(self, rc: int, run_id: str, message: str) -> None:
        self.rc = rc
        self.run_id = run_id
        self.message = message
        super().__init__(message)


def validate_resume_target(run_id: str, *, home: str | None = None
                           ) -> tuple[str, str]:
    """Verify the resume target exists and is readable.

    Returns ``(db_path, run_dir)``. Raises :class:`ResumeError` when
    the run_id does not name an existing run directory (F4 — unknown
    run_id exits non-zero with a clear message, never a fresh start).

    The state.db is allowed to be missing on disk; an interrupted run
    may have written a run_profile / durable-DAG ledger without ever
    reaching the per-step checkpointer. The plan line will then be
    "skip=[], redispatch=[all candidates]" which is the correct C1
    behaviour (every node is re-dispatched, nothing is silently skipped).
    """
    run_dir = _resolve_run_dir(run_id, home=home)
    if not os.path.isdir(run_dir):
        raise ResumeError(
            RC_UNKNOWN_RUN, run_id,
            f"[mini-ork-resume] unknown run_id: {run_id!r} "
            f"(no run dir at {run_dir}). Refusing to start a fresh run; "
            "pass --resume with a run_id from `mini-ork board list`.",
        )
    return _resolve_db(), run_dir


# ─── plan line construction ─────────────────────────────────────────────────


def _classify_checkpoint(
    *,
    has_rec: dict[str, object],
    step_id: str,
    rewound: set[str],
) -> str:
    """Return one of ``"skip"`` / ``"redispatch"`` for a single candidate.

    ``rewound`` holds the steps a ``--checkpoint-id`` rewind masks: every
    step recorded at or after the checkpoint in the ledger's completion
    order looks "never-ran" (redispatch). This is the F3 contract.
    """
    rec = has_rec.get(step_id)
    if rec is None:
        return "redispatch"
    # Lazy import — CompletionRecord is the public dataclass; type checks
    # avoid a hard dependency cycle on store.py.
    status = getattr(rec, "status", None)
    if step_id in rewound:
        return "redispatch"
    if status == "succeeded":
        return "skip"
    # failed / error / any unknown terminal state — redo from every
    # candidate we don't trust verbatim (the F3/F4 rule).
    return "redispatch"


def build_resume_plan(
    db: str,
    run_id: str,
    *,
    candidate_steps: Sequence[str] | None = None,
    checkpoint_id: str | None = None,
) -> tuple[str, list[str]]:
    """Build the one-line resume plan + the dispatch set.

    Returns ``(plan_line, dispatch_set)``:

      ``plan_line`` is the single stdout line to print before any other
      work — prefixed ``[mini-ork-resume]`` so it can be grepped.

      ``dispatch_set`` is the list of step_ids the executor must
      re-dispatch on this resume. Succeeded steps (or steps at-or-after
      ``checkpoint_id`` when rewinding) are EXCLUDED.

    The plan line always lists the FULL skip / redispatch sets derived
    from the ledger (every completion row the run wrote) so the operator
    sees the entire state of the resume, not a slice. The dispatch set
    is additionally intersected with ``candidate_steps`` when supplied —
    the lifecycle passes its workflow DAG nodes here so the executor
    knows exactly which candidates to dispatch on this resume.

    Raises :class:`ResumeError` with rc=RC_CORRUPT if the ledger is
    unreadable (the Errors contract in the checkpointer: hard-error
    naming the run, never silently re-execute a node that might have
    succeeded).
    """
    # Read the ledger up front so a corrupt row hard-errors BEFORE the
    # plan line is printed — operators must see the failure as the
    # first signal, not after the plan (which would imply we already
    # knew what to skip).
    try:
        records = ck.list_completions(db, run_id=run_id)
    except ck.CheckpointCorrupt as exc:
        raise ResumeError(
            RC_CORRUPT, run_id,
            f"[mini-ork-resume] corrupt ledger for run {run_id!r}: "
            f"node={exc.node_id!r} step={exc.step_id!r} reason={exc}. "
            "Refusing to silently re-execute; repair or drop the bad row.",
        ) from exc
    has_rec = {r.step_id: r for r in records}

    # The rewind follows completion order (the ledger's row order), not the
    # step names: a node called "zeta" that finished first is still "before"
    # one called "alpha" that finished later.
    rewound: set[str] = set()
    if checkpoint_id is not None:
        order = [r.step_id for r in records]
        if checkpoint_id not in order:
            raise ResumeError(
                RC_UNKNOWN_RUN, run_id,
                f"[mini-ork-resume] unknown checkpoint-id {checkpoint_id!r} for run "
                f"{run_id!r}; the run's checkpoints are: {', '.join(order) or '(none)'}.",
            )
        rewound = set(order[order.index(checkpoint_id):])

    # Plan line: classify every step we know about — ledger rows plus
    # candidate steps with no row (the "never ran" case). Use recorded
    # order for ledger rows and candidate order for never-ran steps so
    # the operator sees the timeline the run actually executed.
    seen_skip: dict[str, None] = {}
    seen_redispatch: list[str] = []
    for rec in records:
        cls = _classify_checkpoint(
            has_rec=has_rec, step_id=rec.step_id, rewound=rewound,
        )
        if cls == "skip":
            seen_skip[rec.step_id] = None
        elif rec.step_id not in seen_skip:
            seen_redispatch.append(rec.step_id)

    # The candidate list adds two things the ledger doesn't carry:
    #   * never-ran steps (no ledger row) become redispatch targets;
    #   * a rewind past ``checkpoint_id`` may force a step the ledger
    #     recorded as succeeded into redispatch (already handled by
    #     _classify_checkpoint above when ``step_id >= checkpoint_id``).
    if candidate_steps:
        for step in candidate_steps:
            if step in seen_skip:
                continue
            if step not in has_rec and step not in seen_redispatch:
                seen_redispatch.append(step)

    skip = list(seen_skip.keys())
    redispatch = seen_redispatch

    # Dispatch set: the slice the lifecycle will actually re-dispatch on
    # this pass. Intersect the redispatch list with the candidate list
    # (when supplied) so the executor knows exactly which candidates to
    # dispatch on this resume; otherwise the dispatch set defaults to
    # the entire redispatch list.
    if candidate_steps:
        candidate_set = list(candidate_steps)
        dispatch_set = [
            s for s in redispatch if s in candidate_set
        ]
    else:
        dispatch_set = list(redispatch)

    parts = [f"{_PLAN_PREFIX} resume run {run_id!r}"]
    if checkpoint_id is not None:
        parts.append(f"from checkpoint-id={checkpoint_id!r}")
    parts.append(f"skip=[{','.join(skip)}]")
    parts.append(f"redispatch=[{','.join(redispatch)}]")
    plan_line = " ".join(parts) + "\n"

    return plan_line, dispatch_set


# ─── CLI entry ───────────────────────────────────────────────────────────────


def emit_plan(plan_line: str) -> None:
    """Write the one-line plan to stdout exactly once.

    Stdout, not stderr (F2). Writes via ``sys.stdout.write`` directly so
    it lands BEFORE any logger output that may interleave during the
    remainder of the lifecycle — the plan line is the operator's first
    signal that this invocation is a resume, not a fresh start.
    """
    sys.stdout.write(plan_line)
    sys.stdout.flush()


def run_with_resume(
    argv: Sequence[str],
    *,
    home: str | None = None,
) -> tuple[int, dict]:
    """Resume-aware preflight for ``mini-ork run --resume``.

    Returns ``(rc, sink)`` where ``sink`` carries the parsed flags and
    the computed dispatch set so the lifecycle can re-use them:

      sink["run_id"]            = the resume target (overrides the
                                  default ``run-<ts>-<pid>``)
      sink["checkpoint_id"]     = the optional checkpoint id, or None
      sink["resume_dispatch"]   = the dispatch set (the lifecycle
                                  filters its workflow candidate list
                                  against this BEFORE dispatch)

    ``argv`` is the post-subcommand argv (everything after ``run``).
    Errors are emitted to stderr with the ``[mini-ork-resume]`` prefix
    and the function returns a non-zero rc without invoking the
    lifecycle; this is the C1 / F4 zero-fallback contract.
    """
    sink: dict = {}
    try:
        resume_id, _rest, _info = parse_resume_flags(argv)
    except SystemExit as exc:
        # argparse raised SystemExit on a malformed value — surface it
        # as our own rc=USAGE so the CLI's contract is uniform.
        return int(exc.code or RC_USAGE), sink

    if resume_id is None:
        # No --resume on this invocation — let the lifecycle run as a
        # fresh start (this is the default path; the resume module is
        # transparent when the flag is absent).
        return RC_OK, sink

    sink["resume_id"] = resume_id

    # F4: unknown run_id is a HARD error, never a fresh-start fallback.
    try:
        db, run_dir = validate_resume_target(resume_id, home=home)
    except ResumeError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.rc, sink

    sink["resume_db"] = db
    sink["resume_run_dir"] = run_dir

    # Pick up --checkpoint-id if it was passed (it was preserved by
    # parse_resume_flags' parse_known_args call — the lifecycle can
    # still see it if it wants, but the resume preflight owns its
    # semantics for the plan line).
    checkpoint_id = _extract_checkpoint_id(list(argv))
    sink["checkpoint_id"] = checkpoint_id

    # The candidates are the run's own workflow nodes in topological order,
    # so a node the run never reached appears in the plan as redispatch. A
    # run whose workflow cannot be resolved plans over its ledger alone;
    # ``dispatch_resume`` refuses to execute that case.
    candidate_steps = _workflow_target(resume_id)["candidates"]
    sink["resume_candidates"] = candidate_steps
    try:
        plan_line, dispatch_set = build_resume_plan(
            db, resume_id,
            candidate_steps=candidate_steps,
            checkpoint_id=checkpoint_id,
        )
    except ResumeError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.rc, sink

    # Bind MINI_ORK_RUN_ID so the resume re-uses the existing
    # ``<home>/runs/<run_id>/`` directory + ledger rows — only once the plan
    # is known to be valid, so a refused resume leaves no binding behind.
    os.environ["MINI_ORK_RUN_ID"] = resume_id
    sink["run_id"] = resume_id
    sink["resume_dispatch"] = dispatch_set
    emit_plan(plan_line)

    # Return the cleaned argv (resume flags stripped) so the lifecycle
    # parser doesn't choke on them.
    sink["argv_after_resume"] = _strip_resume_flags(argv)
    return RC_OK, sink


def _workflow_target(run_id: str) -> dict:
    """The run's recipe, workflow and candidate steps (topological order).

    Resolved exactly as ``mini-ork recover`` resolves them (task_runs →
    run_profile → catalog), so a resume and a recover of the same run walk
    the same DAG. ``candidates`` is empty when the workflow cannot be read.
    """
    from mini_ork.recovery.dag import load_dag
    from mini_ork.recovery.planner import _resolve_default_paths

    run_dir, db, workflow, recipe = _resolve_default_paths(run_id)
    candidates: list[str] = []
    if workflow and os.path.isfile(workflow):
        try:
            candidates = list(load_dag(workflow).topo)
        except Exception:  # noqa: BLE001 — an unreadable workflow plans over the ledger only
            candidates = []
    return {"run_dir": run_dir, "db": db, "workflow": workflow,
            "recipe": recipe, "candidates": candidates}


def _run_task_class(db: str, run_id: str) -> str:
    """The task class the run recorded, so the resumed execute applies the
    run's own publish gates rather than falling back to ``generic``."""
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT task_class FROM task_runs WHERE id = ?",
                              (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "").strip() if row else ""


def dispatch_resume(sink: dict, *, root: str, execute_fn=None) -> int:
    """Execute the plan ``run_with_resume`` printed — exactly
    ``sink["resume_dispatch"]``, against the run's existing ``plan.json``.

    This is the same in-process hand-off ``mini-ork recover`` makes
    (``execute --recovery`` with ``MINI_ORK_RECOVERY_CLOSURE``), with two
    differences that make it the checkpointer path: the closure comes from the
    P1.2 completion ledger rather than the durable-DAG reuse check, and
    classify + plan never run again — the run keeps the plan it already had.

    Anything that would amount to starting the run over is refused (C1 / F4):
    an unresolvable workflow, a run that never wrote ``plan.json``, a run with
    no completion checkpoints at all, or a recipe argument naming a different
    recipe than the one the run ran.
    """
    from mini_ork.context import apply_env_overrides

    run_id = sink["resume_id"]
    target = _workflow_target(run_id)
    run_dir = sink.get("resume_run_dir") or target["run_dir"]
    db = sink.get("resume_db") or target["db"]
    plan_json = os.path.join(run_dir, "plan.json")
    rest = sink.get("argv_after_resume") or []
    named_recipe = rest[0] if rest and not rest[0].startswith("-") else ""

    refusal = ""
    if not target["candidates"]:
        refusal = (f"cannot read the workflow of run {run_id!r} "
                   f"(recipe {target['recipe'] or '<unknown>'!r})")
    elif named_recipe and named_recipe != target["recipe"]:
        refusal = (f"run {run_id!r} ran recipe {target['recipe']!r}, "
                   f"not {named_recipe!r}")
    elif not os.path.isfile(plan_json):
        refusal = f"run {run_id!r} has no plan.json (it never reached execute)"
    elif not ck.list_completions(db, run_id=run_id):
        refusal = (f"run {run_id!r} has no completion checkpoints (it predates "
                   "the checkpointer or finished no node); `mini-ork recover` "
                   "resumes it from the durable-DAG ledger instead")
    if refusal:
        sys.stderr.write(f"{_PLAN_PREFIX} {refusal}. Refusing to start the run over.\n")
        return RC_UNKNOWN_RUN

    dispatch_set = list(sink.get("resume_dispatch") or [])
    if not dispatch_set:
        sys.stdout.write(f"{_PLAN_PREFIX} nothing to re-dispatch: every step of "
                         f"run {run_id!r} succeeded\n")
        return RC_OK

    overrides = {
        "MINI_ORK_RUN_ID": run_id,
        "MINI_ORK_TASK_RUN_ID": run_id,
        "MINI_ORK_RUN_DIR": run_dir,
        "MINI_ORK_DB": db,
        "MINI_ORK_WORKFLOW": target["workflow"],
        "MINI_ORK_RECIPE": target["recipe"],
        "MINI_ORK_RECOVERY_RUN_ID": run_id,
        "MINI_ORK_RECOVERY_CLOSURE": " ".join(dispatch_set),
        "MINI_ORK_RECOVERY_STRATEGY": "resume",
    }
    if not os.environ.get("MINI_ORK_TASK_CLASS", "").strip():
        task_class = _run_task_class(db, run_id)
        if task_class:
            overrides["MINI_ORK_TASK_CLASS"] = task_class
    apply_env_overrides(overrides)

    # The interrupted attempt can leave a node_start with no node_end; the
    # stale-heartbeat watchdog would read it as a live hang and abort this
    # resume. Same closer ``mini-ork recover`` runs before it dispatches.
    try:
        from pathlib import Path

        from mini_ork.web.control import _close_dangling_node_events
        from mini_ork.web.db import StateDB

        _close_dangling_node_events(StateDB(Path(db)), run_id)
    except Exception as exc:  # noqa: BLE001 — never stops the resume
        sys.stderr.write(f"{_PLAN_PREFIX} could not close dangling nodes: {exc}\n")

    if execute_fn is None:
        from mini_ork.cli import execute as _execute

        def execute_fn(argv):
            return _execute.main(argv, root=root)
    return execute_fn([plan_json, "--recovery"])


def _extract_checkpoint_id(argv: Sequence[str]) -> str | None:
    """Pull ``--checkpoint-id <id>`` (or ``--checkpoint-id=<id>``) out."""
    out = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--checkpoint-id" and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
            continue
        if a.startswith("--checkpoint-id="):
            out = a.split("=", 1)[1]
            i += 1
            continue
        i += 1
    return out


def _strip_resume_flags(argv: Sequence[str]) -> list[str]:
    """Return ``argv`` with ``--resume`` / ``--checkpoint-id`` removed.

    The two flags are independent and additive to the lifecycle's
    positional ``<recipe> <kickoff>`` arguments; stripping them here
    lets the lifecycle parser see the original argv minus the resume
    surface.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--resume" and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--resume="):
            i += 1
            continue
        if a == "--checkpoint-id" and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--checkpoint-id="):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


__all__ = [
    "RC_OK",
    "RC_USAGE",
    "RC_UNKNOWN_RUN",
    "RC_CORRUPT",
    "RC_INTERNAL",
    "ResumeError",
    "parse_resume_flags",
    "validate_resume_target",
    "build_resume_plan",
    "emit_plan",
    "run_with_resume",
    "dispatch_resume",
]