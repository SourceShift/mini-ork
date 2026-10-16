"""``mini-ork board`` — the project's live state as JSON, for IDE panels.

The mini-ork IDE (a Zed build with mini-ork panels) polls ``board --json`` and
calls the action subcommands from its buttons. Everything the panels show comes
from here, so the IDE side stays a thin view and mini-ork stays the source of
truth.

  board [--home H] [--json]           runs, learnings, automations, recipes,
                                      workspaces, scheduler — one document
  board run <run_id> [--home H]       one run's card (markdown) + its open workspace
  board merge|discard <run_id>        act on a run's workspace
  board race-keep <winner> [--discard RUN_ID ...] [--dry-run] [--force]
                                      keep one race contestant: merge the winner,
                                      discard the runners-up (same kickoff only)
  board stop <run_id>                 soft-stop a running run
  board kill <run_id>                 SIGTERM then SIGKILL after 2 s
  board resume <run_id>               resume a cost-paused run
  board gate approve|reject <id> [--note TEXT]
                                      resolve a mo_inbox_gates row
  board page <key> [--tab T] [--arg k=v ...]
                                      one IDE page as data (mini_ork.ide_pages)
  board checkpoints <run_id>          per-node git checkpoints for a run
  board restore <run_id> <node>       restore a run's worktree to a checkpoint
  board start --kickoff <path> [--recipe R] [--workspace worktree|in-place]
              [--set K=V ...] [--run-id ID] [--init] [--dry-run]
                                      launch a run from a kickoff file, detached

Every section is computed independently: a section that fails comes back
empty with its error in ``errors`` instead of failing the whole board.
Exit codes: 0 ok, 1 failure, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

_RUN_LIMIT = 50
_RUNS_VERB_DEFAULT_LIMIT = 50
_RUNS_VERB_MAX_LIMIT = 200
_GRADIENT_LIMIT = 30
_RECORD_LIMIT = 20
_PATTERN_LIMIT = 10


def _section(errors: dict[str, str], name: str, fn: Callable[[], Any], empty: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one broken source must not blank the board
        errors[name] = f"{type(exc).__name__}: {exc}"
        return empty


def _runs(home: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from mini_ork.acp.fleet import fleet_rows

    fleet, counts = fleet_rows(home, state="all", limit=_RUN_LIMIT)
    open_ws = _open_workspace_ids(home)
    return [_fleet_row_to_dict(r, open_ws) for r in fleet], dict(counts)


def _fleet_row_to_dict(row: Any, open_ws: set[str]) -> dict[str, Any]:
    """Map a ``FleetRow`` to the IDE row dict — shared by ``_runs`` and
    :func:`_rows_for` so both stay byte-compatible (the recspec requires
    ``board runs`` rows to match ``board --json``)."""
    return {
        "id": row.run_id,
        "title": row.title,
        "recipe": row.recipe,
        "state": row.state,
        "mark": row.mark,
        "step": row.step,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "cost_usd": round(float(row.cost_usd or 0.0), 4),
        "added": row.added,
        "removed": row.removed,
        "has_workspace": row.run_id in open_ws,
    }


def _open_workspace_ids(home: Path) -> set[str]:
    from mini_ork import workspaces

    return {w.run_id for w in workspaces.list_open(home)}


def _rows_for(home: Path, run_ids: list[str]) -> list[dict[str, Any]]:
    """Build the IDE row dicts for ``run_ids`` in input order.

    Real rows for every id — no stub fallback. ``runs_by_ids`` is one
    ``SELECT ... WHERE id IN (...)`` for the candidate rows; ``rows_from_candidates``
    computes the precise ``state``/``step``/``added``/``removed`` from the run
    dir + a batched events lookup. Shape mirrors :func:`_runs` so ``board runs``
    rows are byte-compatible with ``board --json`` rows.
    """
    if not run_ids:
        return []
    from mini_ork.acp.fleet import rows_from_candidates
    from mini_ork.acp.history import runs_by_ids

    candidates = runs_by_ids(home, run_ids)
    fleet = rows_from_candidates(home, candidates)
    by_id = {r.run_id: r for r in fleet}
    open_ws = _open_workspace_ids(home)

    out: list[dict[str, Any]] = []
    for rid in run_ids:
        r = by_id.get(rid)
        if r is None:
            continue
        out.append(_fleet_row_to_dict(r, open_ws))
    return out


def _learnings(home: Path) -> list[dict[str, Any]]:
    from mini_ork.web.db import db_for

    db = db_for(home)
    out: list[dict[str, Any]] = []
    if db.has_table("gradient_records"):
        for g in db.rows(
            "SELECT gradient_id, target, signal, suggested_change, confidence, created_at, task_class "
            "FROM gradient_records ORDER BY created_at DESC LIMIT ?",
            (_GRADIENT_LIMIT,),
        ):
            out.append({
                "kind": "gradient",
                "id": g.get("gradient_id"),
                "target": g.get("target") or "",
                "text": g.get("signal") or "",
                "suggestion": g.get("suggested_change") or "",
                "confidence": float(g.get("confidence") or 0.0),
                "created_at": g.get("created_at"),
                "task_class": g.get("task_class") or "",
            })
    if db.has_table("learning_record"):
        for r in db.rows(
            "SELECT id, run_id, category, title, patch_summary, outcome, severity, confidence, created_at "
            "FROM learning_record ORDER BY updated_at DESC LIMIT ?",
            (_RECORD_LIMIT,),
        ):
            out.append({
                "kind": "record",
                "id": str(r.get("id")),
                "target": r.get("category") or "",
                "text": r.get("title") or "",
                "suggestion": r.get("patch_summary") or "",
                "confidence": float(r.get("confidence") or 0.0),
                "created_at": r.get("created_at"),
                "task_class": "",
                "outcome": r.get("outcome") or "",
                "severity": r.get("severity") or "",
                "run_id": r.get("run_id") or "",
            })
    try:
        from mini_ork.web.routes.learning import emergent_patterns

        for p in emergent_patterns(db, _PATTERN_LIMIT):
            out.append({
                "kind": "pattern",
                "id": str(p.get("id") or p.get("cluster_label") or ""),
                "target": p.get("cluster_label") or "",
                "text": p.get("suggested_meta_adr") or p.get("cluster_label") or "",
                "suggestion": "",
                "confidence": float(p.get("strength_score") or 0.0),
                "created_at": p.get("created_at"),
                "task_class": "",
            })
    except Exception:  # noqa: BLE001 — patterns are optional
        pass
    return out


def _automations(home: Path) -> list[dict[str, Any]]:
    from mini_ork.acp.automation_view import automation_rows

    keys = ("id", "name", "recipe", "schedule", "when", "next", "last_run", "last_run_id",
            "last_error", "mark", "enabled", "workspace")
    return [{k: a.get(k) for k in keys} for a in automation_rows(home)]


def _scheduler(home: Path) -> dict[str, Any]:
    from mini_ork import automations

    s = automations.scheduler_status(home)
    return {"installed": bool(s.get("installed")), "last_tick": s.get("last_tick"),
            "platform": s.get("platform")}


def _recipes(home: Path) -> list[dict[str, Any]]:
    from mini_ork.acp.recipe_view import recipe_rows

    keys = ("id", "source", "description", "steps", "grade_letter", "grade_score",
            "runs", "success_pct", "avg_cost_usd")
    return [{k: r.get(k) for k in keys} for r in recipe_rows(home)]


def _workspaces(home: Path) -> list[dict[str, Any]]:
    from mini_ork import workspaces

    out = []
    for ws in workspaces.list_open(home):
        st = workspaces.status(ws)
        out.append({
            "run_id": ws.run_id, "branch": ws.branch, "base": ws.base_branch,
            "path": str(ws.path), "added": st.get("added", 0), "removed": st.get("removed", 0),
            "files": [f.get("path") for f in st.get("files") or []],
            "commits_ahead": st.get("commits_ahead", 0),
        })
    return out


_SHELL_KEYS: tuple[str, ...] = ("version", "project", "home", "generated_at", "header",
                        "runs", "total", "counts", "errors")


def _shell_subset(payload: dict[str, Any]) -> dict[str, Any]:
    """The IDE shell's view — only what the always-on panels render.

    The full payload carries learnings, automations, scheduler, recipes and
    workspaces; the shell never reads them. Projecting to ``_SHELL_KEYS`` keeps
    ``board --json --shell`` cheap and lets the IDE poll at 5 s without a
    Python process always running. ``errors`` is preserved so a header or runs
    failure still surfaces in the shell.
    """
    return {k: payload.get(k) for k in _SHELL_KEYS}


def _board_shell_payload(home: Path) -> dict[str, Any]:
    """The IDE shell's document — ``_SHELL_KEYS`` only, no full-board I/O.

    Bypasses ``board()`` so the shell skips ``_learnings``/``_automations``/
    ``_scheduler``/``_recipes``/``_workspaces`` and stays under the 1.5 s warm
    budget. ``header`` stays in (kickoff keeps ContextNest + cost), so the
    0.5 s ``CN_TIMEOUT_SEC`` ceiling dominates — the win comes from dropping
    the five full-board sections, not from the header.
    """
    errors: dict[str, str] = {}
    runs, counts = _section(errors, "runs", lambda: _runs(home), ([], {}))
    from mini_ork.ide_pages.header import header

    return {
        "version": 1,
        "project": home.absolute().parent.name,
        "home": str(home.absolute()),
        "generated_at": int(time.time()),
        "header": _section(errors, "header", lambda: header(home, counts), {}),
        "runs": runs,
        # ``runs`` is one page (``_RUN_LIMIT``); ``total`` is the census. Only
        # both together answer "is there another page", which is what the IDE
        # shell's Load more control asks — see ``board()`` below.
        "total": _count_runs(home),
        "counts": counts,
        "errors": errors,
    }


def board(home: Path) -> dict[str, Any]:
    """The whole board for ``home`` — see the module docstring."""
    errors: dict[str, str] = {}
    runs, counts = _section(errors, "runs", lambda: _runs(home), ([], {}))
    from mini_ork.ide_pages.header import header

    return {
        "version": 1,
        "header": _section(errors, "header", lambda: header(home, counts), {}),
        "project": home.absolute().parent.name,
        "home": str(home.absolute()),
        "generated_at": int(time.time()),
        "runs": runs,
        # The census beside the page: the IDE shell draws a "Load more" at the
        # end of ``runs`` and has no other way to learn whether one is left.
        # Same value ``board runs`` pages on (``_count_runs``), so the control
        # and the CLI agree about where the runs end.
        "total": _count_runs(home),
        "counts": counts,
        "learnings": _section(errors, "learnings", lambda: _learnings(home), []),
        "automations": _section(errors, "automations", lambda: _automations(home), []),
        "scheduler": _section(errors, "scheduler", lambda: _scheduler(home), {}),
        "recipes": _section(errors, "recipes", lambda: _recipes(home), []),
        "workspaces": _section(errors, "workspaces", lambda: _workspaces(home), []),
        "errors": errors,
    }


def run_card(home: Path, run_id: str) -> dict[str, Any]:
    from mini_ork import workspaces
    from mini_ork.acp.fleet import render_card, run_card as _card

    card = _card(home, run_id)
    if card is None:
        return {"ok": False, "error": f"no run {run_id}"}
    ws = workspaces.load(home, run_id)
    workspace = None
    if ws is not None:
        st = workspaces.status(ws)
        workspace = {"branch": ws.branch, "base": ws.base_branch, "path": str(ws.path),
                     "added": st.get("added", 0), "removed": st.get("removed", 0),
                     "files": st.get("files") or []}
    markdown = render_card(card, now=int(time.time()), serve_url=None)
    run_dir = home / "runs" / run_id
    return {"ok": True, "run_id": run_id, "markdown": markdown, "workspace": workspace,
            "card": _card_fields(card, home.absolute().parent),
            "kickoff": _kickoff(home, run_id),
            "artifacts": _artifacts(run_dir),
            "diff": _unified_diff(run_dir)}


_KICKOFF_LIMIT = 20_000
_DIFF_LIMIT = 300_000
_ARTIFACT_LIMIT = 400


def _kickoff(home: Path, run_id: str) -> dict[str, Any]:
    from mini_ork.acp.history import _read_kickoff
    from mini_ork.web.db import db_for

    path = None
    try:
        rows = db_for(home).rows("SELECT kickoff_path FROM task_runs WHERE id = ?", (run_id,))
        path = (rows[0].get("kickoff_path") if rows else None) or None
    except Exception:  # noqa: BLE001
        path = None
    text = _read_kickoff(home, run_id, path)
    if not path:
        inbox = home / "runs-inbox" / f"{run_id}.md"
        path = str(inbox) if inbox.is_file() else None
    return {"path": path, "text": text[:_KICKOFF_LIMIT], "truncated": len(text) > _KICKOFF_LIMIT}


def _artifact_group(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1].lower()
    if rel.startswith("evidence/"):
        return "Evidence"
    if name.endswith((".diff", ".patch")) or name == "acp-diffs.json":
        return "Diffs"
    if "kickoff" in name or name in {"plan.json", "run_profile.json", "profile-answers.json",
                                     "context-pack.json"}:
        return "Kickoff & plan"
    if name == "verdict.json" or name.startswith(("verifier_", "review", "implementer-summary")):
        return "Results"
    if name.startswith(("agent-", "lens-", "impl-")) or rel.startswith("sessions/"):
        return "Agent output"
    if name.endswith(".log"):
        return "Logs"
    return "Other"


_GROUP_ORDER = ["Kickoff & plan", "Results", "Diffs", "Agent output", "Logs", "Evidence", "Other"]

# Subtrees a run uses as workspace, not output: verifier/implementer sandboxes
# (a full repo copy each — thousands of files), the pre-implementer fixture
# file copy, and transform scratch. Without pruning these swamp the listing
# and, worse, crowd real artifacts out under _ARTIFACT_LIMIT because a lexical
# walk hits ``verifier-*-work`` before ``workspace``.
_ARTIFACT_EXCLUDE_RE = re.compile(r"^(?:[^/]+-work|pre-impl-fixture/files|workspace/scratch)(?:/|$)")


def _artifact_excluded(rel: str) -> bool:
    """True for scratch paths no artifact listing should show."""
    if _ARTIFACT_EXCLUDE_RE.match(rel):
        return True
    # A ``.git`` anywhere (sandbox repo copies carry one) is never run output.
    return ".git" in rel.split("/")


def _artifacts(run_dir: Path) -> list[dict[str, Any]]:
    """What the run produced, grouped and ordered for the run tab.

    Scratch subtrees are pruned during the walk (not filtered after), and
    ``_ARTIFACT_LIMIT`` applies after grouping so an over-limit run keeps
    its Kickoff & plan / Results rows instead of whichever files a lexical
    walk happened to reach first.
    """
    if not run_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(run_dir):
        base = Path(dirpath)
        rel_dir = base.relative_to(run_dir).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if not _artifact_excluded(d if rel_dir == "." else f"{rel_dir}/{d}")
        ]
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if _artifact_excluded(rel):
                continue
            path = base / name
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({"path": rel, "abs": str(path), "size": stat.st_size,
                        "modified": int(stat.st_mtime), "group": _artifact_group(rel)})
    out.sort(key=lambda a: (_GROUP_ORDER.index(a["group"]), a["path"]))
    return out[:_ARTIFACT_LIMIT]


def _unified_diff(run_dir: Path) -> str:
    """The run's change as a unified diff (its recorded diff when there is one)."""
    import difflib

    from mini_ork.acp import diffs

    try:
        entries, _cached = diffs.cached_or_computed(run_dir)
    except Exception:  # noqa: BLE001
        return ""
    chunks: list[str] = []
    for entry in entries or []:
        path = str(entry.get("path") or "")
        old = (entry.get("old_text") or "").splitlines(keepends=True)
        new = (entry.get("new_text") or "").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}"))
    return "".join(chunks)[:_DIFF_LIMIT]


def _project_file(path: str, project: Path) -> tuple[str, str | None]:
    """``(display path, existing absolute path or None)`` for a changed file.

    A delivered run's worktree is gone, so its recorded path is mapped onto the
    project: the longest tail of the path that exists in the project wins.

    A RELATIVE path is tried as ``project / path`` first (start 0) so a
    run-recorded path such as ``mini_ork/ide_pages/x.py`` resolves against the
    project root even when the process cwd is somewhere else. When a relative
    path resolves nowhere it is returned unchanged for display — never just its
    basename (``p.name`` would drop the directory). An ABSOLUTE path keeps
    today's tail mapping, and an absolute path that resolves nowhere keeps the
    basename display.
    """
    p = Path(path)
    if p.is_absolute():
        if p.is_file():
            try:
                return str(p.relative_to(project)), str(p)
            except ValueError:
                pass
        parts = p.parts
        for start in range(1, len(parts)):
            candidate = project.joinpath(*parts[start:])
            if candidate.is_file():
                return str(Path(*parts[start:])), str(candidate)
        return p.name, None
    parts = p.parts
    for start in range(0, len(parts)):
        rel = Path(*parts[start:])
        candidate = project / rel
        if candidate.is_file():
            return str(rel), str(candidate)
    return path, None


def _card_fields(card: dict[str, Any], project: Path) -> dict[str, Any]:
    files = []
    for f in card.get("files") or []:
        display, absolute = _project_file(str(f.get("path") or ""), project)
        files.append({"path": display, "abs": absolute,
                      "added": int(f.get("added") or 0), "removed": int(f.get("removed") or 0)})
    verdict = card.get("verdict") or {}
    return {
        "title": card.get("title") or "",
        "recipe": card.get("recipe") or "",
        "status": card.get("status") or "",
        "detail": card.get("detail") or "",
        "steps": [
            {"name": s.get("node_id") or "", "type": s.get("node_type") or "",
             "lane": s.get("lane") or "", "seconds": s.get("duration"),
             "state": s.get("state") or ""}
            for s in card.get("steps") or []
        ],
        "cost_by_stage": {k: round(float(v or 0.0), 4)
                          for k, v in (card.get("cost_by_stage") or {}).items() if v},
        "cost_total": round(float(card.get("cost_total") or 0.0), 4),
        "files": files,
        "verdict": verdict.get("verdict") if isinstance(verdict, dict) else None,
    }


def _title_for(home: Path, run_id: str) -> str:
    try:
        from mini_ork.acp.history import list_runs

        rows, _ = list_runs(home, limit=200)
        return next((r.get("title") or "" for r in rows if r.get("run_id") == run_id), "")
    except Exception:  # noqa: BLE001
        return ""


def act(home: Path, verb: str, run_id: str) -> dict[str, Any]:
    from mini_ork import workspaces

    if verb == "stop":
        from mini_ork.web.control import stop_run
        from mini_ork.web.db import db_for

        return stop_run(home, db_for(home), run_id)
    ws = workspaces.load(home, run_id)
    if ws is None:
        return {"ok": False, "error": f"run {run_id} has no open workspace"}
    # Lazy ``getpass`` import — the rest of this module never reads it, and
    # the kickoff's edit scope is ``act()`` only. ``getuser()`` can fail in
    # minimal containers, so fall back to ``unknown`` rather than 500ing the
    # IDE's button click.
    import getpass
    try:
        ide_user = f"ide:{getpass.getuser()}"
    except (OSError, KeyError):
        ide_user = "ide:unknown"
    if verb == "merge":
        title = _title_for(home, run_id)
        message = f"{title} (mini-ork {run_id})" if title else f"mini-ork run {run_id}"
        return workspaces.merge(ws, message=message, approver=ide_user)
    return workspaces.discard(ws, approver=ide_user)


def _act_kill(home: Path, run_id: str) -> dict[str, Any]:
    """SIGTERM, then SIGKILL after 2 s — wired through the live control plane."""
    from mini_ork.web.control import kill_run
    from mini_ork.web.db import db_for

    return kill_run(home, db_for(home), run_id)


def _act_resume(home: Path, run_id: str) -> dict[str, Any]:
    """Lift the cost-pause sentinel on a run; ``approver="ide"`` records who."""
    from mini_ork.web.control import resume_cost_run

    return resume_cost_run(home, run_id, approver="ide")


def _act_dismiss(home: Path, run_id: str, *, undo: bool = False) -> dict[str, Any]:
    """Hide a run from the IDE inbox, or put it back (``--undo``).

    The marker is a per-run file, alongside ``landed.json`` and ``repair.json``
    that ``ide_pages.inbox`` already reads, so a dismissal travels with the run
    directory instead of living in one index a concurrent writer could lose.
    Nothing is deleted: the run keeps its events, artifacts and worktree, and
    still appears on the runs page. An unwritable home is reported, never
    silently ignored — a button that leaves the row on screen with no
    explanation is worse than a refusal.
    """
    if not run_id:
        return {"ok": False, "error": "dismiss needs a run id"}
    run_dir = Path(home) / "runs" / run_id
    if not run_dir.is_dir():
        return {"ok": False, "run_id": run_id, "error": f"no run {run_id}"}
    marker = run_dir / "dismissed.json"
    try:
        if undo:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(
                json.dumps({"at": int(time.time()), "by": "ide"}, indent=2) + "\n",
                encoding="utf-8",
            )
    except OSError as exc:
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "run_id": run_id, "mode": "restored" if undo else "dismissed"}


# The board retry verb spawns a ``mini-ork recover`` subprocess under the same
# detached shape as ``acp.commands._spawn``. We re-bind the import here so the
# board tests can monkeypatch ``_retry_spawn`` and never actually launch.
_retry_spawn = None  # populated lazily on first use, see ``_act_retry``


def _merge_lanes(argv: list[str], lanes: list[str] | None) -> list[str]:
    """Apply operator ``--lane <alias>=<lane>`` flags to a spawned argv.

    Each operator lane REPLACES any ``--lane`` flag already carrying the same
    alias (the hint's suggested lane for a ``kind == "lane"`` hint), and is
    appended otherwise. Matches both the ``--lane <alias>=<lane>`` two-token
    form and the ``--lane=<alias>=<lane>`` single-token form; the alias is the
    substring before the first ``=``.
    """
    if not lanes:
        return argv
    for lane in lanes:
        alias = lane.split("=", 1)[0].strip()
        replaced = False
        out: list[str] = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--lane" and i + 1 < len(argv):
                existing = argv[i + 1].split("=", 1)[0].strip()
                if existing == alias:
                    out.extend(["--lane", lane])
                    i += 2
                    replaced = True
                    continue
            elif tok.startswith("--lane="):
                existing = tok[len("--lane="):].split("=", 1)[0].strip()
                if existing == alias:
                    out.append(f"--lane={lane}")
                    i += 1
                    replaced = True
                    continue
            out.append(tok)
            i += 1
        if not replaced:
            out.extend(["--lane", lane])
        argv = out
    return argv


def _act_retry(
    home: Path, run_id: str, *,
    ack_change: bool = False, force: bool = False, dry_run: bool = False,
    lanes: list[str] | None = None,
) -> dict[str, Any]:
    """``board retry <run_id> [--ack-change] [--force] [--dry-run]
    [--lane <alias>=<lane>]…``.

    Always returns ``{"ok": …, "hint": <hint or null>, …}`` — never raises.
    The hint itself is computed by ``mini_ork.recovery.retry_hint``; this
    verb wires the gating rules from the kickoff (``retry-hint.md`` §2):

      * ``--dry-run``         → hint only, never spawns.
      * no hint              → ``ok: false``, "nothing to retry".
      * not retryable & !``--force`` → ``ok: false``, error = summary.
      * needs_change & !``--ack-change`` → ``ok: false``, error = ack prompt —
        except a ``kind`` in ``retry_hint.NO_CHANGE_KINDS`` (an ``interrupted``
        run: resuming IS the fix) or a ``lane`` hint, both of which need no
        ``--ack-change``.
      * otherwise            → detached spawn via the canonical
                               ``acp.commands._spawn`` shape
                               (``sys.executable + bin/mini-ork + …``,
                                ``MINI_ORK_ROOT`` set, ``MINI_ORK_VENV_ACTIVE``
                                popped, ``cwd`` = engine root),
                               log at ``<run_dir>/recover-<ts>.log``.

    Operator ``--lane`` flags (a list of ``alias=lane``) are threaded onto the
    spawned command: each replaces any ``--lane`` flag already carrying the
    same alias (the hint's suggested lane) and is appended otherwise. ``--lane``
    alone never implies ``--ack-change``/``--force``. A ``--lane`` on a hint
    whose command verb is not ``recover`` (the resume-cost hint runs
    ``mini-ork resume``, which ignores ``--lane``) is REFUSED — never silently
    dropped.
    """
    from mini_ork.recovery import retry_hint
    from mini_ork.web.control import _mini_ork_root

    try:
        hint = retry_hint.load_or_compute(home, run_id, write=True)
    except Exception as exc:  # noqa: BLE001 — a hint crash must not blank the verb
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}

    if hint is None:
        return {"ok": False, "run_id": run_id, "error": "nothing to retry"}

    if dry_run:
        return {"ok": True, "run_id": run_id, "hint": hint, "dry_run": True}

    needs_change = hint.get("needs_change") if isinstance(hint.get("needs_change"), dict) else None
    retryable = bool(hint.get("retryable"))

    if not retryable and not force:
        summary = (needs_change or {}).get("summary") or "run is not retryable"
        return {"ok": False, "run_id": run_id, "hint": hint, "error": summary}

    if needs_change is not None and not ack_change and not force \
            and str(hint.get("strategy") or "") != "resume-cost" \
            and needs_change.get("kind") != "lane" \
            and needs_change.get("kind") not in retry_hint.NO_CHANGE_KINDS:
        return {"ok": False, "run_id": run_id, "hint": hint,
                "error": f"needs a change first: {needs_change.get('summary') or '?'}"}

    command = str(hint.get("command") or "")
    if not command:
        # ``--force`` on a not-retryable hint (e.g. reviewer reject) still
        # needs a command so the operator gets a recovery to inspect. We
        # do NOT do this without ``--force`` — a bare retry verb should
        # refuse to spawn anything when the hint has no command.
        if force:
            command = f"mini-ork recover {run_id}"
        else:
            return {"ok": False, "run_id": run_id, "hint": hint,
                    "error": "hint has no command"}

    # The hint's command is a string of ``mini-ork …`` tokens. The canonical
    # spawn shape replaces the leading literal ``mini-ork`` with the absolute
    # ``bin/mini-ork`` and prefixes ``sys.executable`` (mirrors
    # ``acp.commands.handle_recover`` at L681-694). Operator-typed flags
    # (``--ack-change``, ``--force``) are appended AFTER the split so a
    # ``--force`` alone never implies ``--ack-change`` and the hint's
    # command string never embeds them.
    tokens = command.split()
    # ``--lane`` is a ``recover`` flag: the resume-cost hint's command is
    # ``mini-ork resume <run>``, and ``mini_ork/cli/resume.py`` ignores extra
    # args — so appending ``--lane`` there would silently drop the switch and
    # re-run the dead lane. Refuse instead of reporting a switch that never
    # happened (the verb is the token right after the ``mini-ork`` prefix).
    if lanes:
        verb = tokens[1] if len(tokens) > 1 else ""
        if verb != "recover":
            return {"ok": False, "run_id": run_id, "hint": hint,
                    "error": f"--lane requires a recover hint (this hint runs "
                             f"'mini-ork {verb}', which takes no --lane)"}
    root = _mini_ork_root()
    if tokens and tokens[0] == "mini-ork":
        tokens[0] = str(root / "bin" / "mini-ork")
        argv: list[str] = [sys.executable, *tokens]
    else:
        argv = tokens
    # Operator ``--lane`` flags replace the hint's same-alias ``--lane`` and
    # append otherwise; ``--lane`` alone never implies ``--ack-change``/``--force``.
    argv = _merge_lanes(argv, lanes)
    # ``--force`` does NOT add ``--ack-change`` — the two are independent
    # operator intents. ``--ack-change`` is only added when the operator
    # typed it, regardless of ``--force``.
    if ack_change and "--ack-change" not in argv:
        argv.append("--ack-change")
    if force and "--force" not in argv:
        argv.append("--force")

    global _retry_spawn
    if _retry_spawn is None:
        from mini_ork.acp.commands import _spawn as _retry_spawn  # type: ignore[assignment]
    run_dir = home / "runs" / run_id
    log_path = run_dir / f"recover-{int(time.time())}.log"
    env = dict(os.environ)
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(root)
    env.pop("MINI_ORK_VENV_ACTIVE", None)
    try:
        proc = _retry_spawn(argv, cwd=str(root), env=env, stdout_path=log_path)
    except Exception as exc:  # noqa: BLE001 — spawn failure: surface, do not crash
        return {"ok": False, "run_id": run_id, "hint": hint,
                "error": f"spawn failed: {type(exc).__name__}: {exc}"}

    # ``board retry <run> --ack-change`` resolves the run's pending
    # ``retry_precondition`` gate with the audit note "retried via board
    # retry" so the resolve→spawn path is visible in ``mo_inbox_gates``
    # alongside the gate→retry handoff path. Fail-soft — a missing row
    # or DB never blanks the spawn.
    if ack_change:
        try:
            from mini_ork.gates import oversight_inbox
            from mini_ork.recovery import retry_notify
            _db = str(home / "state.db")
            for row in oversight_inbox.pending(db_path=_db):
                if (str(row.get("gate_id") or "") == retry_notify.GATE_ID
                        and str(row.get("feature") or "") == run_id):
                    oversight_inbox.resolve(
                        int(row["inbox_id"]), "approved",
                        review_note="retried via board retry",
                        db_path=_db,
                    )
                    break
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True, "run_id": run_id, "hint": hint, "pid": proc.pid,
            "log": str(log_path), "command": command}


def _act_gate(home: Path, action: str, inbox_id: str, note: str | None) -> dict[str, Any]:
    """Resolve a ``mo_inbox_gates`` row. ``False`` ⇒ already decided.

    When the resolved row is a ``retry_precondition`` gate, the approve
    branch hands off to :func:`_act_retry` (with ``--ack-change``) so the
    run is retried under the same detached spawn shape ``recover`` uses.
    The reject branch writes ``{"abandoned": true}`` into the run dir's
    ``retry-gate.json`` so the IDE / polling layers can read the abandon
    status without a DB query.
    """
    from mini_ork.gates import oversight_inbox
    from mini_ork.recovery import retry_notify

    db_path = home / "state.db"
    if not db_path.is_file():
        return {"ok": False, "error": "no state.db"}
    try:
        iid = int(inbox_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid inbox id"}
    status = "approved" if action == "approve" else "rejected"
    ok = oversight_inbox.resolve(iid, status, review_note=note or "", db_path=db_path)
    if not ok:
        return {"ok": False, "error": "not pending"}

    # Resolve the gate's feature to a run_id so we can route the handoff.
    # Fetch on BOTH branches (approve + reject) — the reject branch needs
    # gate_id + feature to write the abandoned marker, and a missing row
    # is itself a meaningful signal we should report.
    row = oversight_inbox.get(iid, db_path=db_path)
    if not isinstance(row, dict):
        return {"ok": True, "inbox_id": iid, "status": status}
    gate_id = str(row.get("gate_id") or "")
    feature = str(row.get("feature") or "")
    # The retry gate writes ``feature=<run_id>`` (retry_notify._enqueue_retry_gate).
    # Run dirs are at ``<home>/runs/<run_id>``; check that the dir exists
    # so a custom MINI_ORK_RUN_ID (e.g. "retry-notify-20261007123316") still
    # resolves to a run instead of being silently dropped.
    run_id = feature if feature and (home / "runs" / feature).is_dir() else ""

    if gate_id == retry_notify.GATE_ID and status == "approved" and run_id:
        # Approve: dispatch ``board retry <run> --ack-change`` (detached),
        # returning the same pid/log payload so callers can tail the log.
        return _act_retry(home, run_id, ack_change=True)
    if gate_id == retry_notify.GATE_ID and status == "rejected" and run_id:
        # Reject: mark the gate pointer ``{"abandoned": true}`` so polling
        # readers see the abandon without needing the row.
        gate_pointer = home / "runs" / run_id / retry_notify.GATE_POINTER_FILENAME
        try:
            gate_pointer.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if gate_pointer.is_file():
                try:
                    existing = json.loads(gate_pointer.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = {}
            if isinstance(existing, dict):
                existing["abandoned"] = True
            else:
                existing = {"abandoned": True}
            gate_pointer.write_text(
                json.dumps(existing, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return {"ok": True, "inbox_id": iid, "status": status, "abandoned": True}
    return {"ok": True, "inbox_id": iid, "status": status}


def _act_node(home: Path, run_id: str, node_id: str | None,
              view: str | None, offset: int) -> dict[str, Any]:
    """One DAG node's detail panel — ``board node <run_id> <node_id>``.

    Delegates to ``mini_ork.ide_pages.node.build_node`` so the verb and the
    page module never duplicate the reader/dispatch logic. The unknown-run /
    unknown-node cases return ``ok: false`` and ``exit 1`` via the main()
    rule at L519; path-traversal guards live in ``node.build_node``.
    """
    from mini_ork.ide_pages.node import build_node

    return build_node(home, run_id, node_id or "", view=view, offset=offset or 0)


def _act_steer(home: Path, run_id: str, text: str | None, role: str | None,
               severity: str | None) -> dict[str, Any]:
    """``board steer <run_id> --text ... --role ... --severity ...`` — inject an
    operator_steering row through ``web.control.steer_run``.

    Validation (role, severity, confidence) lives in ``steer_run``; we forward
    ``source="ide"`` so a future audit can tell IDE-origin rows from CLI /
    dashboard ones. ``steer_run`` returns ``ok: false`` and a typed error on
    a bad role/severity — ``main()`` then exits 1.
    """
    from mini_ork.web.control import steer_run
    from mini_ork.web.deps import db_for

    return steer_run(db_for(home), run_id, text or "",
                     role_target=role or "any",
                     severity=severity or "info",
                     source="ide")


def _act_answer(home: Path, run_id: str, texts: list[str],
                fors: list[str]) -> dict[str, Any]:
    """``board answer <run_id> --text T [--for Q]`` — answer a run parked on
    ``needs_answers`` from the CLI / IDE instead of the HTTP control plane.

    ``save_answers`` keys the answers map by the question *text* (that is what
    the planner writes into the kickoff on re-dispatch, see
    ``plan._prompt_profile_questions``), so a single ``--text`` is only
    unambiguous when exactly one question is outstanding. With several, we
    refuse and list them rather than guess — an answer attached to the wrong
    question silently misleads the planner. ``--for`` names the question (its
    text, or its 1-based ``q<N>``), one per ``--text``.
    """
    from mini_ork.cli.plan import _question_text
    from mini_ork.web.control import get_profile, save_answers

    if not (home / "runs" / run_id).is_dir():
        return {"ok": False, "run_id": run_id, "stage": "usage",
                "error": f"run {run_id} not found"}

    profile = get_profile(home, run_id)
    questions = [_question_text(q) for q in (profile.get("questions") or [])]
    answered = set((profile.get("answers") or {}).keys())
    pending = [(f"q{index}", text)
               for index, text in enumerate(questions, 1) if text not in answered]
    if not pending:
        return {
            "ok": False, "run_id": run_id, "questions": [],
            "error": f"run {run_id} has no pending questions "
                     f"(profile_status={profile.get('profile_status') or 'unknown'})",
        }

    ids = [{"id": qid, "text": text} for qid, text in pending]
    by_id = dict(pending)
    id_of = {text: qid for qid, text in pending}
    listing = ", ".join(f"{qid} ({text[:70]})" for qid, text in pending)

    answers: dict[str, str] = {}
    if fors:
        if len(fors) != len(texts):
            return {"ok": False, "run_id": run_id, "questions": ids,
                    "error": f"{len(fors)} --for with {len(texts)} --text; "
                             "pass one of each per question"}
        for target, text in zip(fors, texts):
            # ``--for`` names the question either way; what the answers map has
            # to key on is its *text* — save_answers folds this map straight
            # into the profile the planner re-reads, so an id here would answer
            # a question the planner never asked.
            question = by_id.get(target)
            if question is None and target in id_of:
                question = target
            if question is None:
                return {"ok": False, "run_id": run_id, "questions": ids,
                        "error": f"no pending question {target!r}; pending: {listing}"}
            if question in answers:
                return {"ok": False, "run_id": run_id, "questions": ids,
                        "error": f"question {target!r} answered twice"}
            answers[question] = text
    elif len(texts) > 1:
        return {"ok": False, "run_id": run_id, "questions": ids,
                "error": f"{len(texts)} --text without --for; pass one --text, "
                         "or one --for per question"}
    elif len(pending) > 1:
        return {"ok": False, "run_id": run_id, "questions": ids,
                "error": f"run {run_id} has {len(pending)} pending questions and a "
                         f"single --text cannot name one; pass --for with each "
                         f"--text. Pending: {listing}"}
    else:
        answers = {pending[0][1]: texts[0]}

    result = save_answers(home, run_id, answers)
    result.setdefault("run_id", run_id)
    result["answered"] = [{"id": id_of.get(text, ""), "text": text}
                          for text in answers]
    return result


def _kickoff_paths(home: Path, run_ids: list[str]) -> dict[str, str | None]:
    """``task_runs.kickoff_path`` for each id — the authoritative kickoff path.

    :func:`mini_ork.acp.history.kickoff_key` falls back to
    ``<home>/runs-inbox/<run_id>.md`` when handed ``None``, which covers what
    ``control.launch_run`` stages. Reading the column first keeps the identity
    check working for a run whose kickoff was written somewhere else.
    """
    if not run_ids:
        return {}
    from mini_ork.web.db import db_for

    placeholders = ", ".join("?" for _ in run_ids)
    try:
        rows = db_for(home).rows(
            f"SELECT id, kickoff_path FROM task_runs WHERE id IN ({placeholders})",
            tuple(run_ids),
        )
    except Exception:  # noqa: BLE001 — no db just means the inbox fallback is used
        return {}
    return {str(row["id"]): row.get("kickoff_path") for row in rows}


def _act_race_keep(
    home: Path,
    winner_run_id: str,
    discards: list[str],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Keep one contestant of a race: merge the winner, discard the runners-up.

    One call because the IDE action vocabulary cannot chain ``merge && discard
    && discard``, and the half-decided race — winner merged, runners-up still
    holding worktrees — is the state this verb exists to prevent.

    Two safety rules, both fail-closed:

    * **Every discard must share the winner's kickoff.** The page supplies the
      sibling ids it displayed and a page is not a witness: an id from another
      race would throw away a different task's work. An unreadable kickoff is
      ``""``, and ``""`` never matches — not even another ``""``, because "could
      not prove the same" must not read as "proved the same".
    * **Nothing is discarded unless the merge landed.** Dropping the runners-up
      when the winner did not make it leaves the race with nothing at all, so a
      refused merge — including the review gate's ``needs_approval`` — stops the
      call before the first discard.

    ``--dry-run`` returns the plan (winner, the runs to discard, the worktrees
    that would be freed) and touches nothing, so a caller can build a confirm
    from real numbers instead of estimating them. ``--force`` is what supplies
    the operator's approval to the merge; without it the review policy decides
    and its refusal is propagated verbatim, ``needs_approval`` intact.
    """
    from mini_ork import workspaces
    from mini_ork.acp.history import kickoff_key

    winner = workspaces.load(home, winner_run_id)
    if winner is None:
        return {"ok": False, "step": "winner", "run_id": winner_run_id,
                "error": f"run {winner_run_id} has no open workspace"}

    targets: list[str] = []
    for run_id in discards:
        if run_id == winner_run_id:
            return {"ok": False, "step": "plan", "run_id": run_id, "error":
                    f"--discard names the winner ({run_id}) — keep it or discard "
                    f"it, not both"}
        if run_id not in targets:
            targets.append(run_id)

    paths = _kickoff_paths(home, [winner_run_id, *targets])
    winner_key = kickoff_key(home, winner_run_id, paths.get(winner_run_id))
    if not winner_key:
        return {"ok": False, "step": "plan", "run_id": winner_run_id, "error":
                f"the kickoff of {winner_run_id} cannot be read, so no discard can "
                f"be checked against it — refusing rather than guessing"}
    planned: list[dict[str, Any]] = []
    for run_id in targets:
        ws = workspaces.load(home, run_id)
        if ws is None:
            return {"ok": False, "step": "plan", "run_id": run_id,
                    "error": f"run {run_id} has no open workspace"}
        key = kickoff_key(home, run_id, paths.get(run_id))
        if key != winner_key:
            return {"ok": False, "step": "plan", "run_id": run_id, "error":
                    f"run {run_id} is not a contestant of the same race as "
                    f"{winner_run_id} (kickoff {key or 'unreadable'} vs "
                    f"{winner_key}) — refusing to discard it"}
        # An adopted worktree belongs to Zed: discarding the run resets it but
        # frees no directory, and a confirm that promised otherwise would lie.
        planned.append({"run_id": run_id, "worktree": str(ws.path),
                        "frees_worktree": not ws.adopted})

    plan: dict[str, Any] = {
        "winner": winner_run_id,
        "merge_worktree": str(winner.path),
        "discard": planned,
        "frees": [entry["worktree"] for entry in planned if entry["frees_worktree"]],
    }
    if dry_run:
        return {"ok": True, "dry_run": True, **plan}

    import getpass
    try:
        approver = f"ide:{getpass.getuser()}"
    except (OSError, KeyError):
        approver = "ide:unknown"

    title = _title_for(home, winner_run_id)
    message = f"{title} (mini-ork {winner_run_id})" if title else f"mini-ork run {winner_run_id}"
    merged = workspaces.merge(winner, message=message,
                              approver=approver if force else None)
    if merged.get("ok") is not True:
        out: dict[str, Any] = {"ok": False, "step": "merge", "run_id": winner_run_id,
                               "merge": merged, **plan}
        if merged.get("needs_approval"):
            out["needs_approval"] = True
        return out

    discarded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for entry in planned:
        # Re-loaded rather than reused: the merge above committed and deleted the
        # winner's record, and a discard resolves its own record path.
        ws = workspaces.load(home, entry["run_id"])
        if ws is None:
            failed.append({"run_id": entry["run_id"], "error": "workspace disappeared"})
            continue
        result = workspaces.discard(ws, approver=approver)
        if result.get("ok") is True:
            discarded.append({"run_id": entry["run_id"], "worktree": entry["worktree"]})
        else:
            failed.append({"run_id": entry["run_id"],
                           "error": str(result.get("error") or "discard failed")})

    out = {"ok": not failed, "merged": True, "merge": merged,
           "discarded": discarded, "failed": failed, **plan}
    if failed:
        out["step"] = "discard"
    return out


def _clamp_runs_verb_args(offset: int, limit: int) -> tuple[int, int]:
    """Sanitise the verb's offset/limit — ``argparse`` only checks types."""
    if offset < 0:
        raise ValueError(f"offset must be >= 0 (got {offset})")
    if limit < 1:
        raise ValueError(f"limit must be >= 1 (got {limit})")
    return int(offset), min(int(limit), _RUNS_VERB_MAX_LIMIT)


def _runs_verb(home: Path, query: str | None, offset: int, limit: int) -> dict[str, Any]:
    """``mini-ork board runs [--query Q] [--offset N] [--limit M]``.

    No query: pages across every run (newest first); ``total`` is the full
    ``task_runs`` count, so paging past the 200-candidate ``CANDIDATE_LIMIT``
    window still surfaces real records — the ``list_runs`` rows are passed
    straight to :func:`mini_ork.acp.fleet.rows_from_candidates` (no
    ``runs_by_ids`` re-read). With a query: FTS5 over kickoff + main
    artifacts, with a LIKE fallback when sqlite was built without FTS5.
    Errors are surfaced in the payload's ``errors`` field rather than
    failing the whole verb. When reindex runs with a partial budget the
    payload also carries ``"indexing": True``.
    """
    errors: dict[str, str] = {}
    offset, limit = _clamp_runs_verb_args(offset, limit)
    query = (query or "").strip()

    if query:
        from mini_ork.ide_pages import search as _search

        # Reindex first so cold calls find matches; then search once.
        # Both calls thread ``errors`` so the FTS5-degraded note + the
        # partial-index note (``errors["index"]``) reach the verb payload.
        _section(errors, "reindex", lambda: _search.reindex(home, errors=errors), 0)
        run_ids, total = _section(
            errors, "search",
            lambda: _search.search(home, query, limit, offset, errors=errors),
            ([], 0),
        )
        run_ids = list(run_ids)
        rows = _rows_for(home, run_ids)
    else:
        from mini_ork.acp.fleet import rows_from_candidates
        from mini_ork.acp.history import list_runs

        try:
            candidates, _ = list_runs(home, limit=limit, offset=offset)
            total = _count_runs(home)
        except Exception as exc:  # noqa: BLE001
            candidates = []
            total = 0
            errors["list_runs"] = f"{type(exc).__name__}: {exc}"
        # No-query path: pass ``list_runs`` rows straight to
        # ``rows_from_candidates`` instead of collapsing to ids and
        # re-reading via ``runs_by_ids`` (kickoff r3 fix #5).
        fleet = rows_from_candidates(home, candidates)
        open_ws = _open_workspace_ids(home)
        rows = [_fleet_row_to_dict(r, open_ws) for r in fleet]

    has_more = (offset + len(rows)) < int(total or 0)
    return {
        "ok": True,
        "runs": rows,
        "total": int(total or 0),
        "offset": offset,
        "has_more": has_more,
        "errors": errors,
        "indexing": "index" in errors,
    }


def _count_runs(home: Path) -> int:
    """Total ``task_runs`` rows — ``0`` on a missing DB / missing table."""
    if not (Path(home) / "state.db").is_file():
        return 0
    try:
        from mini_ork.web.db import db_for

        db = db_for(home)
        if not db.has_table("task_runs"):
            return 0
        row = db.row("SELECT COUNT(*) AS n FROM task_runs") or {}
        return int(row.get("n") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _default_home() -> Path:
    return Path(os.environ.get("MINI_ORK_HOME", "").strip() or (Path.cwd() / ".mini-ork"))


def _resolve_worktree_for_run(home: Path, run_id: str) -> str | None:
    """The worktree path for a checkpoint/restore verb.

    Resolves via ``workspaces.load`` first (the canonical pointer), and
    falls back to ``load_run_roots(<home>/runs/<run_id>).target`` for
    in-place runs (the worktree was opened with ``--owns`` rather than
    provisioned as a ``mini-ork worktree``). Returns None when neither
    yields a directory — both ``checkpoints`` and ``restore`` then
    return ``ok: False`` with the reason ``"no worktree for run"``."""
    try:
        from mini_ork import workspaces
        ws = workspaces.load(home, run_id)
        if ws is not None and Path(ws.path).is_dir():
            return str(ws.path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from mini_ork.runtime.run_roots import load_run_roots
        run_dir = home / "runs" / run_id
        if run_dir.is_dir():
            roots = load_run_roots(str(run_dir))
            if roots and getattr(roots, "target", None):
                return roots.target
    except Exception:  # noqa: BLE001
        pass
    return None


def _act_checkpoints(home: Path, run_id: str) -> dict[str, Any]:
    """``board checkpoints <run_id>`` — list the run's per-node git refs."""
    if not run_id:
        return {"ok": False, "run_id": run_id, "error": "a run id is required"}
    worktree = _resolve_worktree_for_run(home, run_id)
    if not worktree:
        return {"ok": False, "run_id": run_id, "error": "no worktree for run"}
    try:
        from mini_ork.vcs import node_checkpoint
        items = node_checkpoint.list_checkpoints(worktree, run_id)
    except Exception as exc:  # noqa: BLE001 — never let vcs errors blank the verb
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}",
                "worktree": worktree}
    return {"ok": True, "run_id": run_id, "worktree": worktree, "checkpoints": items}


def _act_restore(home: Path, run_id: str, node_id: str, *,
                 force: bool = False) -> dict[str, Any]:
    """``board restore <run_id> <node> [--force]`` — put the worktree back
    to how it was at the named node. Refusals come back as
    ``ok: False`` with a ``reason``; the calling ``main()`` echoes the
    reason to stderr so the operator can see why nothing happened."""
    if not run_id:
        return {"ok": False, "error": "a run id is required"}
    if not node_id:
        return {"ok": False, "error": "a node id is required"}
    worktree = _resolve_worktree_for_run(home, run_id)
    if not worktree:
        return {"ok": False, "error": "no worktree for run",
                "run_id": run_id}
    try:
        from mini_ork.vcs import node_checkpoint
        result = node_checkpoint.restore(
            worktree, run_id, node_id, home=home, force=force,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "run_id": run_id, "node": node_id, "worktree": worktree}
    result = dict(result)
    result.setdefault("run_id", run_id)
    result.setdefault("node", node_id)
    result.setdefault("worktree", worktree)
    return result


def _home_initialised(home: Path) -> bool:
    """Whether ``home`` is a mini-ork home rather than a typo.

    The predicate ``MiniOrkClient._is_initialized`` applies: the state database
    and the task-class config both exist. ``launch_run`` mkdirs the tree it is
    handed, so without this check a mistyped ``--home`` starts a run against a
    home that did not exist a second earlier.
    """
    return (home / "state.db").is_file() and (home / "config" / "task_classes").is_dir()


def _start_env(args) -> tuple[dict[str, str], str, str]:
    """``(extra_env, workspace, workspace_source)`` for ``board start``.

    ``--set`` and ``--workspace`` are ``mini-ork run``'s own flags, mirrored so
    a detached launch takes the arguments a foreground run would. ``--workspace``
    becomes ``MO_WORKSPACE_MODE`` rather than a second mechanism: the child's own
    resolver already reads that variable, and ``launch_run`` passes the pairs
    through ``gate_env``, so premium gating is unchanged.

    ``workspace`` is the mode the child will really run in, not just the flag:
    when the flag is absent the child inherits the variable, so a caller that
    reported only ``extra_env`` would name the source and not the value. That is
    what the dry run has to show.

    Raises ``ValueError`` on a malformed ``--set`` — a pair with no ``=`` would
    otherwise reach the child as a bare environment name.
    """
    extra_env: dict[str, str] = {}
    for pair in args.set_env or []:
        key, sep, value = str(pair).partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set expects K=V, got {pair!r}")
        extra_env[key.strip()] = value
    if args.workspace:
        extra_env["MO_WORKSPACE_MODE"] = args.workspace
        return extra_env, args.workspace, "flag"
    inherited = os.environ.get("MO_WORKSPACE_MODE") or ""
    if inherited:
        return extra_env, inherited, "MO_WORKSPACE_MODE"
    return extra_env, "", "default"


def _act_start(home: Path, args) -> dict[str, Any]:
    """Start a run from a kickoff file, detached.

    ``web.control.launch_run`` is the launcher every HTTP route and the ACP agent
    already use; this is its CLI face, so the IDE can start a run instead of only
    opening a chat. The launch is detached by construction — ``launch_run``
    returns once the child is spawned and never waits for the run.

    Refusals carry ``stage: "usage"`` so ``main`` can exit 2 for a mistyped
    invocation and 1 for a launch the launcher itself rejected.
    """
    import secrets

    def refuse(error: str) -> dict[str, Any]:
        return {"ok": False, "stage": "usage", "error": error}

    kickoff = Path(str(args.kickoff)).expanduser().absolute()
    if not kickoff.is_file():
        return refuse(f"kickoff not found: {kickoff}")
    try:
        text = kickoff.read_text(encoding="utf-8")
    except OSError as exc:
        return refuse(f"could not read kickoff: {exc}")

    if not _home_initialised(home) and not args.init:
        return refuse(
            f"{home} is not an initialised mini-ork home; refusing to start a run "
            "against it. launch_run would create the tree it is given — pass "
            "--init if creating a home here is genuinely what you want."
        )

    recipe = (args.recipe or "").strip()
    recipe_source = "--recipe"
    if not recipe:
        from mini_ork import kickoff_lint

        recipe = kickoff_lint.front_matter_recipe(text) or ""
        recipe_source = "front matter"
    if not recipe:
        return refuse(
            f"no recipe for {kickoff}: pass --recipe, or declare a `recipe:` line "
            "in the kickoff's front matter"
        )

    try:
        extra_env, workspace, workspace_source = _start_env(args)
    except ValueError as exc:
        return refuse(str(exc))

    run_id = (args.start_run_id or "").strip() or f"run-{int(time.time())}-{secrets.token_hex(3)}"
    if args.dry_run:
        return {
            "ok": True, "dry_run": True, "spawned": False,
            "home": str(home), "run_id": run_id,
            "recipe": recipe, "recipe_source": recipe_source,
            "workspace": workspace,
            "workspace_source": workspace_source,
            "kickoff": str(kickoff),
            "kickoff_path": str(home / "runs-inbox" / f"{run_id}.md"),
            "extra_env": sorted(extra_env),
        }

    from mini_ork.web.control import launch_run

    result: dict[str, Any] = dict(launch_run(home, recipe, text, run_id=run_id,
                                             extra_env=extra_env))
    result.setdefault("recipe_source", recipe_source)
    result.setdefault("kickoff", str(kickoff))
    if not result.get("ok", True):
        result["stage"] = "launch"
    return result


def main(rest: list[str], root: str) -> int:
    del root
    parser = build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit:
        sys.stderr.write("usage: mini-ork board [run|merge|discard|stop|kill|resume <run_id>] "
                         "[gate approve|reject <inbox_id> [--note TEXT]] "
                         "[node <run_id> <node_id> [--view V] [--offset N]] "
                         "[steer <run_id> --role R --severity S --text TEXT] "
                         "[answer <run_id> --text T [--for Q ...]] "
                         "[race-keep <winner> [--discard RUN_ID ...] [--dry-run] [--force]] "
                         "[retry <run_id> [--ack-change] [--force] [--dry-run]] "
                         "[checkpoints <run_id>] "
                         "[restore <run_id> <node> [--force]] "
                         "[page <key> [--tab T] [--arg k=v]] [--home H] [--json]\n")
        return 2
    home = (Path(args.home) if args.home else _default_home()).expanduser().absolute()
    error = validate(args)
    if error:
        sys.stderr.write(error + "\n")
        return 2
    try:
        payload = build_payload(home, args)
    except ValueError as exc:
        sys.stderr.write(f"mini-ork board runs: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    if payload.get("ok", True) is False:
        # The kickoff limits stderr writes to the two new verbs (restore
        # carries ``reason``; checkpoints uses ``error``). Other verbs
        # keep the pre-kickoff behaviour of exiting 1 silently — touching
        # their stderr output would change behaviour the existing test
        # suite (and other tooling) already expects.
        if args.verb in ("restore", "checkpoints", "start", "answer", "race-keep"):
            reason = payload.get("reason") or payload.get("error")
            if reason:
                sys.stderr.write(f"{reason}\n")
        # A mistyped `board start` is a usage error (2); a launch the launcher
        # itself rejected is a failure (1). `_act_start` marks which it was.
        if args.verb == "start" and payload.get("stage") == "usage":
            return 2
        return 1
    return 0


def validate(args) -> str | None:
    """The verb-level usage checks, shared by `main` and the IDE HTTP route.

    Returns the message to print, or None when the parsed argv is well formed.
    Factored out alongside `build_payload` so a request over loopback cannot be
    held to a weaker contract than the same read over a subprocess.
    """
    # The third positional is meaningful for `gate approve|reject <inbox_id>`
    # (the inbox id) and `node <run_id> <node_id>` (the node id). Any other
    # verb that gets one is a usage error, not a silent extra. ``restore``
    # also takes a third positional — the node id to restore to.
    if args.target and args.verb not in ("gate", "node", "restore"):
        return f"mini-ork board {args.verb}: unexpected extra argument {args.target!r}"
    if args.verb == "gate":
        if not args.run_id or args.run_id not in ("approve", "reject"):
            return "mini-ork board gate: action must be 'approve' or 'reject'"
        if not args.target:
            return "mini-ork board gate: an inbox id is required"
    elif args.verb == "node":
        if not args.run_id:
            return "mini-ork board node: a run id is required"
        if not args.target:
            return "mini-ork board node: a node id is required"
    elif args.verb == "steer":
        if not args.run_id:
            return "mini-ork board steer: a run id is required"
        if not args.text:
            return "mini-ork board steer: --text is required"
    elif args.verb == "answer":
        if not args.run_id:
            return "mini-ork board answer: a run id is required"
        if not args.text:
            return "mini-ork board answer: --text is required"
    elif args.verb == "restore":
        if not args.run_id:
            return "mini-ork board restore: a run id is required"
        if not args.target:
            return "mini-ork board restore: a node id is required"
    elif args.verb == "start":
        if not args.kickoff:
            return "mini-ork board start: --kickoff <path> is required"
    elif args.verb == "race-keep":
        if not args.run_id:
            return "mini-ork board race-keep: a winner run id is required"
    elif args.verb != "show" and args.verb != "runs" and not args.run_id:
        what = "a page key" if args.verb == "page" else "a run id"
        return f"mini-ork board {args.verb}: {what} is required"
    return None


def build_payload(home: Path, args) -> dict[str, Any]:
    """The JSON payload for a parsed `board` argv.

    The single source of truth for what a `board` read answers, shared by the
    CLI (`main`) and the IDE's HTTP route (`web/routes/ide.py`), so a read over
    loopback and one over a fresh subprocess cannot drift. Raises ValueError
    for an argv whose verb-level arguments are out of range (`runs`'s
    offset/limit); the verb-target checks are `validate`'s.
    """
    # The feature flags in force must be the ones THIS home declared: the IDE
    # reads every project with `--home` while the process's own home is the
    # app's, so the defaults loaded at startup belong to the wrong project.
    from mini_ork.features.store import apply_defaults

    apply_defaults(home)
    # Reads first fail the runs whose dispatcher provably died, so no view
    # below lists a dead run as in flight (orchestration/run_reaper.py).
    reap_errors: dict[str, str] = {}
    if args.verb in ("show", "page"):
        from mini_ork.orchestration.run_reaper import reap

        _section(reap_errors, "reaper", lambda: reap(home), [])
    if args.verb == "page":
        from mini_ork.ide_pages import build_page

        page_args = dict(a.split("=", 1) for a in args.arg if "=" in a)
        payload = build_page(home, args.run_id, args.tab, page_args)
    elif args.verb == "show":
        if args.shell:
            # Skip ``board()`` entirely so ``_learnings``/``_automations``/
            # ``_scheduler``/``_recipes``/``_workspaces`` never run for the
            # shell poll. ``_shell_subset`` stays as the shared key projection
            # for any caller that already built a full payload.
            payload = _board_shell_payload(home)
        else:
            payload = board(home)
    elif args.verb == "run":
        payload = run_card(home, args.run_id)
    elif args.verb == "kill":
        payload = _act_kill(home, args.run_id)
    elif args.verb == "resume":
        payload = _act_resume(home, args.run_id)
    elif args.verb == "gate":
        payload = _act_gate(home, args.run_id, args.target, args.note)
    elif args.verb == "node":
        payload = _act_node(home, args.run_id, args.target, args.view, args.offset)
    elif args.verb == "steer":
        payload = _act_steer(home, args.run_id, " ".join(args.text or []),
                             args.role, args.severity)
    elif args.verb == "answer":
        payload = _act_answer(home, args.run_id, args.text or [], args.answer_for)
    elif args.verb == "race-keep":
        payload = _act_race_keep(home, args.run_id, args.race_discard,
                                 dry_run=args.dry_run, force=args.force)
    elif args.verb == "retry":
        payload = _act_retry(home, args.run_id,
                             ack_change=args.ack_change, force=args.force,
                             dry_run=args.dry_run, lanes=args.lane)
    elif args.verb == "runs":
        payload = _runs_verb(home, args.query, args.offset, args.limit)
    elif args.verb == "dismiss":
        payload = _act_dismiss(home, args.run_id, undo=args.undo)
    elif args.verb == "checkpoints":
        payload = _act_checkpoints(home, args.run_id)
    elif args.verb == "restore":
        payload = _act_restore(home, args.run_id, args.target, force=args.force)
    elif args.verb == "start":
        payload = _act_start(home, args)
    else:
        payload = act(home, args.verb, args.run_id)
    if reap_errors:
        payload.setdefault("errors", {}).update(reap_errors)
    errors = payload.get("errors")
    if isinstance(errors, dict) and isinstance(payload.get("sections"), list):
        # The attention strip (FR-040) is prepended to every tab of the
        # orchestrator page. It rides here, not inside ``orch.build``, because
        # the reaper's error is merged into the map just above — and an error
        # the strip cannot see is one it cannot report. It is also why the strip
        # is built before the banner below: reading run profiles discovers parse
        # failures that belong in that banner too.
        if args.verb == "page" and payload.get("key") == "orch":
            from mini_ork.ide_pages import orch

            strip = orch.attention(home, str(payload.get("tab") or ""), errors)
            if strip is not None:
                payload["sections"].insert(0, strip)
        # A page records a broken source in ``errors`` and the IDE draws only the
        # sections — so an error in that map but not on the page is one nobody
        # sees. The reaper's error is merged just above, after the page was
        # built, which is why this rides here rather than inside a page module:
        # it is the last point that holds the whole map.
        from mini_ork.ide_pages.spec import errors_callout

        banner = errors_callout(errors)
        if banner is not None:
            payload["sections"].append(banner)
    return payload


def payload_for_verb(home: Path, argv: list[str]) -> dict[str, Any]:
    """Parse a `board` argv (as `main` would, minus `--home`) and return its
    payload. The HTTP route uses this so its reads run the identical code.
    """
    args = build_parser().parse_args(argv)
    error = validate(args)
    if error:
        raise ValueError(error)
    return build_payload(home, args)


def build_parser() -> argparse.ArgumentParser:
    """The ``board`` subcommand's argparse — factored out so the IDE action test
    can route ``board``-verb CLI lists through the same parser that ``main``
    uses (no hand-copied parser drifting out of sync)."""
    parser = argparse.ArgumentParser(prog="mini-ork board", add_help=False)
    parser.add_argument("verb", nargs="?", default="show",
                        choices=["show", "run", "merge", "discard", "dismiss", "stop", "kill",
                                 "resume", "retry", "gate", "page", "node", "steer", "answer",
                                 "runs", "checkpoints", "restore", "start", "race-keep"])
    parser.add_argument("run_id", nargs="?")
    # `gate approve|reject <inbox_id>` puts the action in run_id and the id here;
    # `node <run_id> <node_id>` uses the same slot for the node id (the L464
    # guard allows it through).
    parser.add_argument("target", nargs="?")
    parser.add_argument("--home", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--shell", action="store_true",
                        help="project the payload to the IDE shell's keys only "
                             "(version, project, home, generated_at, header, runs, counts, errors)")
    parser.add_argument("--tab", default=None)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--note", default=None)
    parser.add_argument("--view", default=None,
                        choices=["stream", "output", "prompt", "telemetry", "learning",
                                 "changes", "overview", "artifacts", "harness"],
                        help="node detail view (default: overview)")
    parser.add_argument("--offset", type=int, default=0,
                        help="stream-view offset — return only entries from this line on")
    parser.add_argument("--role", default="any",
                        help="steer target role (default: any)")
    parser.add_argument("--severity", default="info",
                        help="steer severity: info|warn|critical (default: info)")
    parser.add_argument("--text", action="append", default=None,
                        help="steer message text; answer: the answer text "
                             "(repeat, paired with --for, to answer several questions)")
    parser.add_argument("--for", dest="answer_for", action="append", default=[],
                        help="answer: the question to answer (its text, or q<N>). "
                             "Repeat alongside a matching --text to answer several.")
    parser.add_argument("--ack-change", action="store_true",
                        help="retry: acknowledge the hint's needs_change block "
                             "(otherwise the verb refuses to spawn).")
    parser.add_argument("--force", action="store_true",
                        help="retry: bypass the retryable=false gate (e.g. for "
                             "case-3 'code' revisions). race-keep: supply the "
                             "operator's merge approval, so a review policy of "
                             "before_merge=required is satisfied rather than refused.")
    parser.add_argument("--dry-run", action="store_true",
                        help="retry: return the hint only, never spawn. race-keep: "
                             "return the plan (winner, discards, freed worktrees) "
                             "and perform no merge and no discard.")
    parser.add_argument("--discard", dest="race_discard", action="append", default=[],
                        help="race-keep: a contestant to discard alongside the winner "
                             "(repeatable). Every id must belong to the same race as "
                             "the winner.")
    parser.add_argument("--undo", action="store_true",
                        help="dismiss: clear the run's dismissal marker, so the "
                             "inbox shows it again. A dismissal hides a row, it "
                             "never deletes a run.")
    parser.add_argument("--lane", action="append", default=[],
                        help="retry: pin a lane for the spawned recover "
                             "(repeatable, <alias>=<lane>). Does not imply "
                             "--ack-change/--force.")
    parser.add_argument("--kickoff", default=None,
                        help="start: the kickoff markdown to launch")
    parser.add_argument("--recipe", default=None,
                        help="start: recipe name (default: the kickoff's front matter)")
    parser.add_argument("--set", dest="set_env", action="append", default=[],
                        help="start: K=V passed to the launched run's environment "
                             "(repeatable; mirrors `mini-ork run --set`)")
    parser.add_argument("--workspace", default=None, choices=["worktree", "in-place"],
                        help="start: workspace mode (mirrors `mini-ork run --workspace`)")
    # A distinct dest, not `run_id`: the positional above already claims that
    # name, and argparse writes the positional's default over an optional that
    # shares it — so `board start --run-id x` parsed to `run_id is None` and
    # silently used a generated id. `_act_start` reads `start_run_id`.
    parser.add_argument("--run-id", dest="start_run_id", default=None,
                        help="start: the run id to use (default: generated)")
    parser.add_argument("--init", action="store_true",
                        help="start: allow launching against a --home that is not yet "
                             "an initialised mini-ork home")
    parser.add_argument("--query", default=None,
                        help="full-text query for board runs (whitespace ANDed)")
    parser.add_argument("--limit", type=int, default=_RUNS_VERB_DEFAULT_LIMIT,
                        help=f"page size for runs board (default {_RUNS_VERB_DEFAULT_LIMIT}, "
                             f"max {_RUNS_VERB_MAX_LIMIT})")
    return parser


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))
