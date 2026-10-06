"""Changes & worktrees — run worktrees waiting for review, and the pre-push review.

Tabs: ``worktrees`` (mini-ork workspaces with Merge / Discard, then the
project's other git worktrees), ``review`` (the latest stored pre-push review:
``pre_push_reviews`` + ``pre_push_review_issues``).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("worktrees", "Worktrees"), ("review", "Pre-push review")]
_OTHER_WORKTREE_LIMIT = 25
# The heuristic lenses ``mini_ork.review.lenses`` runs, as stored in
# ``pre_push_review_issues.lens`` (``heuristic.<name>``), with display names.
_HEURISTIC_LENSES = [("bash_syntax", "bash syntax"), ("migration_safety", "migration safety"),
                     ("todo_marker", "added TODOs"), ("diff_size", "diff size"),
                     ("test_pairing", "test pairing"), ("secret_leak", "secret patterns")]
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_COLOUR = {"critical": "red", "high": "red", "medium": "yellow", "low": "sub", "info": "sub"}
_VERDICT_COLOUR = {"approve": "green", "warn": "yellow", "block": "red", "aborted": "red",
                   "pending": "sub"}


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _git_worktrees(project: Path) -> list[dict[str, Any]]:
    """``git worktree list --porcelain`` → ``[{"path", "branch", "detached", "prunable"}]``."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in _git(project, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": "", "detached": False, "prunable": False}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
    return out


def _other_worktrees(project: Path, claimed_paths: list[Path]) -> list[dict[str, Any]]:
    """The project's linked git worktrees that are not mini-ork workspaces."""
    claimed = {str(p.resolve()) for p in claimed_paths}
    main = str(project.resolve())
    out = []
    for wt in _git_worktrees(project):
        try:
            resolved = str(Path(wt["path"]).resolve())
        except OSError:
            resolved = wt["path"]
        if resolved != main and resolved not in claimed:
            out.append(wt)
    # Named branches first, scratch (detached) next, stale (prunable) last.
    out.sort(key=lambda w: (w["prunable"], w["detached"], w["path"]))
    return out


def _titles(home: Path) -> dict[str, str]:
    from mini_ork.acp.history import list_runs

    rows, _ = list_runs(home, limit=200)
    return {str(r.get("run_id")): str(r.get("title") or "") for r in rows}


def _open_workspaces(home: Path) -> list[tuple[Any, dict[str, Any]]]:
    from mini_ork import workspaces

    out = []
    for ws in sorted(workspaces.list_open(home), key=lambda w: w.run_id, reverse=True):
        out.append((ws, workspaces.status(ws)))
    return out


def _worktrees(home: Path, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    project = home.absolute().parent
    opened = _open_workspaces(home)
    titles = _titles(home) if opened else {}
    items: list[dict[str, Any]] = []
    for ws, st in opened:
        title = titles.get(ws.run_id) or ws.run_id
        files = st.get("files") or []
        added, removed = int(st.get("added") or 0), int(st.get("removed") or 0)
        change = f"+{added} −{removed}" if (added or removed) else "no changes yet"
        sub = f"{title} · {change} · {len(files)} file{'s' if len(files) != 1 else ''}"
        if st.get("uncommitted"):
            sub += f" · {len(st['uncommitted'])} uncommitted"
        if ws.adopted:
            sub += " · this is a Zed worktree (kept after merge)"
        acts = [S.btn(f"Merge into {ws.base_branch}" if ws.base_branch else "Merge",
                      S.cli("board", "merge", ws.run_id), "primary"),
                S.btn("Discard", S.cli("board", "discard", ws.run_id,
                                       confirm=f"Discard {ws.branch}? Its changes are deleted."), "danger"),
                S.btn("Open run", S.open_run(ws.run_id, title), "ghost")]
        items.append(S.item(ws.branch or ws.run_id, sub, m="⑂", mc="blue" if (added or removed) else "sub",
                            is_mono=True, acts=acts))

    others = _other_worktrees(project, [ws.path for ws, _ in opened])
    for wt in others[:_OTHER_WORKTREE_LIMIT]:
        label = wt["branch"] or ("detached HEAD" if wt["detached"] else Path(wt["path"]).name)
        sub = wt["path"] + (" · prunable (directory gone)" if wt["prunable"] else "") + " · not a mini-ork run"
        items.append(S.item(label, sub, m="⑂", mc="dim", tc="muted", is_mono=True,
                            acts=[S.btn("Reveal", S.reveal(wt["path"]), "ghost")] if not wt["prunable"] else []))
    if len(others) > _OTHER_WORKTREE_LIMIT:
        items.append(S.dot(f"{len(others) - _OTHER_WORKTREE_LIMIT} more git worktrees",
                           "`git worktree list` shows them all"))
    if not items:
        items = [S.dot("No open worktrees",
                       "Runs started with “New worktree per task” get one; finished changes land as agent edits.")]
    ctx["open"] = len(opened) + len(others)

    setup_hook = home / "worktree-setup.sh"
    mode = os.environ.get("MO_WORKSPACE_MODE") or "worktree"
    mode_label = "new worktree per task" if mode == "worktree" else "in place (this checkout)"
    setup = [
        S.ok(f"{setup_hook.name} is set up", "New worktrees run it (copy .env, install dependencies)",
             [S.btn("Open", S.open_path(str(setup_hook)), "ghost")])
        if setup_hook.is_file() else
        S.warn(f".mini-ork/{setup_hook.name} is missing",
               "New worktrees will not copy .env or install dependencies"),
        S.ok(f"Workspace mode: {mode_label}",
             "MO_WORKSPACE_MODE" + ("" if os.environ.get("MO_WORKSPACE_MODE") else " (default)")
             + " · In place edits your checkout directly"),
    ]
    return [S.lst("Worktrees", items, full=True,
                  note="Also from the thread: /workspaces, /merge [run], /discard [run]."),
            S.lst("Setup", setup, full=True)]


def _review(home: Path, now: int) -> list[dict[str, Any]]:
    from mini_ork.web.deps import db_for

    db = db_for(home)
    if not db.has_table("pre_push_reviews"):
        return [S.lst("Pre-push review", [S.dot("No reviews recorded",
                                                "This project's state.db has no pre_push_reviews table.")],
                      full=True)]
    rows = db.rows("SELECT * FROM pre_push_reviews ORDER BY id DESC LIMIT 1")
    if not rows:
        return [S.lst("Pre-push review", [S.dot(
            "No review yet",
            "The pre-push hook reviews every push; run one by hand with "
            "`mini-ork review run <sha> <branch>`.")], full=True)]
    r = rows[0]
    rid = int(r["id"])
    issues = db.rows(
        "SELECT lens, severity, file_path, line_no, title, status FROM pre_push_review_issues "
        "WHERE review_id = ?", (rid,))
    open_issues = [i for i in issues if (i.get("status") or "open") == "open"]
    verdict = str(r.get("verdict") or "pending")
    llm_lenses = sorted({str(i["lens"])[4:] for i in issues if str(i.get("lens") or "").startswith("llm.")})
    lens_count = len(_HEURISTIC_LENSES) + (1 if llm_lenses or r.get("reviewer_mode") != "heuristic" else 0)
    sha = str(r.get("source_sha") or "")[:8]
    summary = S.kv(
        f"{r.get('target_branch') or '?'}…{sha} · review #{rid}",
        [("Verdict", verdict, _VERDICT_COLOUR.get(verdict, "text")),
         ("Open issues", len(open_issues)),
         ("Diff", f"+{r.get('lines_added') or 0} −{r.get('lines_removed') or 0}",
          None, f"{r.get('files_changed') or 0} files"),
         ("Lenses", f"{len(_HEURISTIC_LENSES)}" + (" + LLM panel" if lens_count > len(_HEURISTIC_LENSES) else ""),
          None, f"{r.get('reviewer_mode')} · {S.age(r.get('reviewed_at'), now)} ago")],
        full=True, note=str(r.get("rationale") or ""))

    lens_items = []
    for key, label in _HEURISTIC_LENSES:
        found = [i for i in open_issues if i.get("lens") == f"heuristic.{key}"]
        if not found:
            lens_items.append(S.ok(label))
            continue
        worst = min(found, key=lambda i: _SEVERITY_ORDER.get(str(i.get("severity")), 9))
        sub = f"{len(found)} issue{'s' if len(found) != 1 else ''} · {worst.get('title') or ''}"
        if str(worst.get("severity")) in ("critical", "high"):
            lens_items.append(S.bad(label, sub))
        else:
            lens_items.append(S.warn(label, sub))
    if llm_lenses:
        llm_open = [i for i in open_issues if str(i.get("lens") or "").startswith("llm.")]
        blockers = [i for i in llm_open if i.get("severity") in ("critical", "high")]
        label = "LLM panel · " + " + ".join(llm_lenses)
        sub = f"{len(llm_open)} comment{'s' if len(llm_open) != 1 else ''}, " + (
            f"{len(blockers)} high or critical" if blockers else "no blockers")
        lens_items.append(S.bad(label, sub) if blockers else S.ok(label, sub))

    ordered = sorted(open_issues, key=lambda i: (_SEVERITY_ORDER.get(str(i.get("severity")), 9),
                                                 str(i.get("file_path") or "")))
    issue_rows = []
    for i in ordered:
        where = str(i.get("file_path") or "")
        if where and i.get("line_no"):
            where += f":{i['line_no']}"
        sev = str(i.get("severity") or "medium")
        issue_rows.append([S.cell(sev, _SEVERITY_COLOUR.get(sev, "sub")),
                           S.cell(f"{i.get('title') or ''}" + (f" · {where}" if where else ""), "body")])
    if not issue_rows:
        issue_rows = [[S.muted("—"), S.muted("No open issues")]]
    return [summary, S.lst("Lenses", lens_items),
            S.table("Issues", [S.col(70), S.col(fr=1, min=200)], ["severity", "issue"], issue_rows)]


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in {k for k, _ in TABS} else "worktrees"
    errors: dict[str, str] = {}
    ctx: dict[str, Any] = {}
    if tab == "review":
        sections = S.guarded(errors, "Pre-push review", lambda: _review(home, int(time.time())))
        try:
            from mini_ork import workspaces

            opened = workspaces.list_open(home)
            others = _other_worktrees(home.absolute().parent, [ws.path for ws in opened])
            ctx["open"] = len(opened) + len(others)
        except Exception:  # noqa: BLE001
            pass
    else:
        sections = S.guarded(errors, "Worktrees", lambda: _worktrees(home, ctx))
    chips = [S.chip(f"{ctx['open']} open")] if "open" in ctx else []
    return S.page(
        "changes", "Changes & worktrees",
        "Every run works in its own git worktree. Finished changes land as agent edits; "
        "conflicts wait here.",
        chips_=chips, actions=[], tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
