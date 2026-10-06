"""``mini-ork board`` — the project's live state as JSON, for IDE panels.

The mini-ork IDE (a Zed build with mini-ork panels) polls ``board --json`` and
calls the action subcommands from its buttons. Everything the panels show comes
from here, so the IDE side stays a thin view and mini-ork stays the source of
truth.

  board [--home H] [--json]           runs, learnings, automations, recipes,
                                      workspaces, scheduler — one document
  board run <run_id> [--home H]       one run's card (markdown) + its open workspace
  board merge|discard <run_id>        act on a run's workspace
  board stop <run_id>                 soft-stop a running run

Every section is computed independently: a section that fails comes back
empty with its error in ``errors`` instead of failing the whole board.
Exit codes: 0 ok, 1 failure, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

_RUN_LIMIT = 50
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
    from mini_ork import workspaces
    from mini_ork.acp.fleet import fleet_rows

    rows, counts = fleet_rows(home, state="all", limit=_RUN_LIMIT)
    open_ws = {w.run_id for w in workspaces.list_open(home)}
    return [
        {
            "id": r.run_id,
            "title": r.title,
            "recipe": r.recipe,
            "state": r.state,
            "mark": r.mark,
            "step": r.step,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "cost_usd": round(float(r.cost_usd or 0.0), 4),
            "added": r.added,
            "removed": r.removed,
            "has_workspace": r.run_id in open_ws,
        }
        for r in rows
    ], dict(counts)


def _learnings(home: Path) -> list[dict[str, Any]]:
    from mini_ork.web.deps import db_for

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


def board(home: Path) -> dict[str, Any]:
    """The whole board for ``home`` — see the module docstring."""
    errors: dict[str, str] = {}
    runs, counts = _section(errors, "runs", lambda: _runs(home), ([], {}))
    return {
        "version": 1,
        "project": home.absolute().parent.name,
        "home": str(home.absolute()),
        "generated_at": int(time.time()),
        "runs": runs,
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
    from mini_ork.web.deps import db_for

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


def _artifacts(run_dir: Path) -> list[dict[str, Any]]:
    """Every file the run wrote, grouped and ordered for the run tab."""
    if not run_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if len(out) >= _ARTIFACT_LIMIT:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({"path": rel, "abs": str(path), "size": stat.st_size,
                    "modified": int(stat.st_mtime), "group": _artifact_group(rel)})
    out.sort(key=lambda a: (_GROUP_ORDER.index(a["group"]), a["path"]))
    return out


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
    """
    p = Path(path)
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
        from mini_ork.web.deps import db_for

        return stop_run(home, db_for(home), run_id)
    ws = workspaces.load(home, run_id)
    if ws is None:
        return {"ok": False, "error": f"run {run_id} has no open workspace"}
    if verb == "merge":
        title = _title_for(home, run_id)
        message = f"{title} (mini-ork {run_id})" if title else f"mini-ork run {run_id}"
        return workspaces.merge(ws, message=message)
    return workspaces.discard(ws)


def _default_home() -> Path:
    return Path(os.environ.get("MINI_ORK_HOME", "").strip() or (Path.cwd() / ".mini-ork"))


def main(rest: list[str], root: str) -> int:
    del root
    parser = argparse.ArgumentParser(prog="mini-ork board", add_help=False)
    parser.add_argument("verb", nargs="?", default="show",
                        choices=["show", "run", "merge", "discard", "stop"])
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--home", default=None)
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(rest)
    except SystemExit:
        sys.stderr.write("usage: mini-ork board [run|merge|discard|stop <run_id>] [--home H] [--json]\n")
        return 2
    home = (Path(args.home) if args.home else _default_home()).expanduser().absolute()
    if args.verb != "show" and not args.run_id:
        sys.stderr.write(f"mini-ork board {args.verb}: a run id is required\n")
        return 2
    if args.verb == "show":
        payload = board(home)
    elif args.verb == "run":
        payload = run_card(home, args.run_id)
    else:
        payload = act(home, args.verb, args.run_id)
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    return 0 if payload.get("ok", True) is not False else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))
