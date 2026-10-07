"""``board node <run> <node> --view changes`` — what the node did.

The IDE's node inspector shows this on top of the standard node detail
(kv/acts). Three blocks, all read-only, designed to come back in < 1.5 s on
a real run:

* ``result`` — per-node-type items drawn from the node's result artifacts
  (``review-<id>.json`` / ``verifier_<id>.json`` / ``plan.json`` /
  ``lens-<id>.md`` / ``rolled-back.json`` / per-node files-by-name).
* ``files`` + ``diff`` — the run's cumulative code changes as of this
  node. Empty for nodes that ran before any code-changing node, with
  ``diff_note`` saying so. Capped at 300 KB with ``diff_note`` saying
  when the cap fires.
* ``commits`` — ``git log --numstat <base_sha>..<branch>`` on the
  workspace when it still exists; the publisher-recorded commit from
  ``publish.json`` / ``verdict.json`` / ``run-verdict.json`` otherwise;
  empty with ``commits_note`` when neither is available.

Reuses (not re-derives):

* :func:`mini_ork.acp.diffs.cached_or_computed` for the per-file cache
  (``acp-diffs.json``).
* :class:`mini_ork.ide_pages.run.Run` / ``Workspace`` for the workspace
  record (``base_sha`` / ``branch`` / ``path``).

Layering note: this page module imports its sibling
:mod:`mini_ork.ide_pages.run` (``Run``, ``Node``) only. ``_project_file``
is duplicated from :mod:`mini_ork.cli.board_cmd` to avoid inverting the
CLI → pages import direction that the kickoff's "files in scope" forbids.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run import Node, Run

# ── caps (kickoff §1, §2 — fresh constants, do NOT reuse board_cmd._DIFF_LIMIT)

DIFF_TEXT_CAP = 300_000
USER_FULL_CAP = 200_000
MD_FILE_CAP = 200_000

# Verdict colour reuse from ``mini_ork.ide_pages.changes._VERDICT_COLOUR`` so
# the IDE draws the same verdict pill everywhere; extended with the verdict
# values the verifier/reviewer writers emit.
_VERDICT_COLOUR = {
    "approve": "green",
    "warn": "yellow",
    "block": "red",
    "aborted": "red",
    "pending": "sub",
    "needs_revision": "yellow",
    "pass": "green",
    "fail": "red",
    "ok": "green",
}

# Node types that change code (kickoff §1 "implementer /worker/writer/drafter").
# Verifier/review/publisher nodes read the run's diff but did not create it.
_CODE_CHANGING_TYPES = frozenset({"implementer", "worker", "writer", "drafter"})

# Per-node-type source artefacts for the ``files the node wrote`` fallback
# (the "other nodes" branch of the kickoff §1 result table).
_NODE_FILE_REGEX = re.compile(
    r"^(?:agent-|impl-|verifier_|review-|lens-)(?P<id>[A-Za-z0-9_.-]+)"
    r"(?:\.live)?\.(?:jsonl|log|json|md)$"
)


def build_changes_view(run: Run, node: Node) -> dict[str, Any]:
    """One DAG node's ``changes`` view payload.

    Shape::

        {"result": {"title": str, "items": [spec item]},
         "files": [{path, added, removed, abs}],
         "diff": <unified diff text, capped>,
         "diff_note": str,
         "commits": [{sha, subject, when, author, files}],
         "commits_note": str}
    """
    run_dir = run.run_dir
    title, items = _result_items(run_dir, node)
    show_diff = _show_diff_for(run, node)
    files, files_note = _files_and_note(run_dir, run, show_diff)
    diff, cap_note = _diff_text(run_dir, files) if show_diff else ("", "")
    diff_note = " ".join(n for n in (files_note, cap_note) if n)
    commits, commits_note = _commits(run)
    return {
        "result": {"title": title, "items": items},
        "files": files,
        "diff": diff,
        "diff_note": diff_note,
        "commits": commits,
        "commits_note": commits_note,
    }


# ── per-node-type result items ─────────────────────────────────────────────


def _result_items(run_dir: Path, node: Node) -> tuple[str, list[dict[str, Any]]]:
    """Project the node's result artefacts onto ``(title, items)``."""
    ntype = str(node.type or "")
    if ntype in ("planner", "decomposer"):
        return _planner_items(run_dir)
    if ntype in ("reviewer", "eval", "judge", "lens", "synthesizer"):
        return _review_items(run_dir, node)
    if ntype in ("verifier", "test", "typecheck", "static_check"):
        return _verifier_items(run_dir, node)
    if ntype == "rollback":
        return _rollback_items(run_dir)
    return _other_node_items(run_dir, node)


def _planner_items(run_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    plan_path = run_dir / "plan.json"
    if not plan_path.is_file():
        return ("Planner plan", [S.dot("No plan.json recorded")])
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ("Planner plan", [S.bad("plan.json failed to parse")])
    if not isinstance(plan, dict):
        return ("Planner plan", [S.dot("plan.json is not an object")])
    items: list[dict[str, Any]] = []
    objective = str(plan.get("objective") or "").strip()
    if objective:
        items.append(S.item(objective, "objective"))
    steps = plan.get("steps") or plan.get("decomposition") or []
    if isinstance(steps, list):
        for s in steps:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or s.get("name") or "")
            stype = str(s.get("type") or s.get("node_type") or "")
            lane = str(s.get("lane") or s.get("model_lane") or "")
            sub = " · ".join(p for p in (stype, lane) if p)
            items.append(S.item(sid, sub, m="☰", mc="blue"))
    if not items:
        items.append(S.dot("plan.json has no steps"))
    return ("Planner plan", items)


def _review_items(run_dir: Path, node: Node) -> tuple[str, list[dict[str, Any]]]:
    """Reviewer / lens / synthesizer / eval / judge: verdict first, then findings."""
    items: list[dict[str, Any]] = []
    verdict_text, verdict_color = "", "sub"

    # 1. Whole-run verdict (verdict.json), then per-node review JSON.
    verdict = _read_json(run_dir / "verdict.json")
    if isinstance(verdict, dict):
        v = str(verdict.get("verdict") or "")
        if v:
            verdict_text, verdict_color = v, _VERDICT_COLOUR.get(v, "sub")

    review_path = run_dir / f"review-{node.id}.json"
    review = _read_json(review_path)
    if isinstance(review, dict):
        rv = str(review.get("verdict") or "")
        if rv:
            verdict_text, verdict_color = rv, _VERDICT_COLOUR.get(rv, "sub")
        notes = review.get("notes") or review.get("findings") or []
        if isinstance(notes, list):
            items.extend(_finding_item(n) for n in notes if n)

    # 2. Markdown fallback (only when the review JSON had no notes). The
    # first existing markdown in the kickoff-named priority order wins.
    if not items:
        for md_path in (run_dir / f"lens-{node.id}.md", run_dir / "synthesis.md", run_dir / f"{node.id}.md"):
            if md_path.is_file():
                items = _items_from_markdown(md_path)
                break

    if not verdict_text and not items:
        items.append(S.dot("No review artefacts found"))
    title = f"Reviewer · {node.id}"
    if verdict_text:
        title = f"Reviewer · {verdict_text}"
        items = [_verdict_item(verdict_text, verdict_color)] + items
    return (title, items)


def _verifier_items(run_dir: Path, node: Node) -> tuple[str, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    title = f"Verifier · {node.id}"

    # Recipe verifier: TSV (verifier-<x>.checks.tsv). Executor verifier:
    # JSON with pass/post_rc/error_summary/evidence_path. The dash/underscore
    # naming differs by hand, so both readers exist side-by-side.
    tsv_path = run_dir / f"verifier-{node.id}.checks.tsv"
    if tsv_path.is_file():
        items = _items_from_checks_tsv(tsv_path)

    vjson = _read_json(run_dir / f"verifier_{node.id}.json")
    if isinstance(vjson, dict):
        passed = bool(vjson.get("pass"))
        post_rc = vjson.get("post_rc")
        ev_path = str(vjson.get("evidence_path") or "")
        err_summary = str(vjson.get("error_summary") or "")
        sub_bits: list[str] = []
        if post_rc not in (None, ""):
            sub_bits.append(f"rc={post_rc}")
        if err_summary:
            sub_bits.append(err_summary[:160])
        if ev_path:
            sub_bits.append(ev_path)
        sub = " · ".join(sub_bits)
        log_path = _first_existing_log(run_dir, node.id)
        acts = [S.btn("Open log", S.open_path(str(log_path)), "ghost")] if log_path else []
        verdict_row = S.ok("pass", sub, acts) if passed else S.bad("fail", sub, acts)
        # Verdict row first so the reader sees pass/fail at a glance.
        items = [verdict_row] + items

    # Tail of the verifier log for context (always, when a log exists).
    for log_name in (f"verifier_{node.id}.log", f"evidence/{node.id}.log", f"verifier-{node.id}.log"):
        log_path = run_dir / log_name
        if not log_path.is_file():
            continue
        tail = _read_tail(log_path, 12)
        for ln in tail:
            items.append(S.item(ln[:200], log_name, m="›", mc="muted"))
        break

    if not items:
        items.append(S.dot("No verifier artefacts found"))
    return (title, items)


def _rollback_items(run_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    rb = _read_json(run_dir / "rolled-back.json")
    items: list[dict[str, Any]] = []
    if isinstance(rb, dict):
        paths = rb.get("paths") or []
        if isinstance(paths, list):
            for p in paths:
                if not isinstance(p, str) or not p:
                    continue
                pp = Path(p)
                acts = [S.btn("Open", S.open_path(p), "ghost")] if pp.is_file() else []
                items.append(S.item(p, "reverted", m="↶", mc="yellow", acts=acts))
        if not items:
            items.append(S.dot("rolled-back.json has no paths"))
    else:
        items.append(S.dot("No rolled-back.json recorded"))
    return ("Rollback", items)


def _other_node_items(run_dir: Path, node: Node) -> tuple[str, list[dict[str, Any]]]:
    """Files the node wrote in the run dir by name match (kickoff §1 other)."""
    items: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*")):
        if not path.is_file():
            continue
        m = _NODE_FILE_REGEX.match(path.name)
        if not m or m.group("id") != node.id:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        items.append(
            S.item(
                path.name,
                _human(size),
                m="›",
                mc="sub",
                acts=[S.btn("Open", S.open_path(str(path)), "ghost")],
            )
        )
    title = f"{node.type or 'Node'} · {node.id}"
    if not items:
        items.append(S.dot(f"No artefacts for {node.id}"))
    return (title, items)


# ── review helpers ──────────────────────────────────────────────────────────


def _finding_item(note: Any) -> dict[str, Any]:
    """One review finding → spec item with severity mark + ``file:line`` + open."""
    if not isinstance(note, dict):
        return S.item(str(note), "")
    text = str(note.get("title") or note.get("text") or note.get("note") or "")
    file_path = str(note.get("file") or note.get("path") or "")
    line = note.get("line")
    sub = ""
    if file_path and line not in (None, ""):
        sub = f"{file_path}:{line}"
    elif file_path:
        sub = file_path
    elif line not in (None, ""):
        sub = f"line {line}"
    severity = str(note.get("severity") or note.get("level") or "").lower()
    if severity in ("critical", "high"):
        m, mc = "!", "red"
    elif severity in ("medium", "warn", "warning"):
        m, mc = "!", "yellow"
    elif severity in ("low", "info"):
        m, mc = "•", "sub"
    else:
        m, mc = "•", "sub"
    acts: list[dict[str, Any]] = []
    if file_path and Path(file_path).is_file():
        acts.append(S.btn("Open", S.open_path(file_path), "ghost"))
    return S.item(text, sub, m=m, mc=mc, acts=acts)


def _verdict_item(verdict: str, color: str) -> dict[str, Any]:
    mark = {"green": "✓", "yellow": "!", "red": "✗", "sub": "•"}.get(color, "•")
    return S.item(verdict, m=mark, mc=color)


def _items_from_checks_tsv(tsv_path: Path) -> list[dict[str, Any]]:
    """Recipe verifier TSV → items. Each row is ``cid\\tdesc\\tpassed``."""
    items: list[dict[str, Any]] = []
    try:
        text = tsv_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [S.bad("could not read checks.tsv")]
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        cid, desc, passed = parts[0], parts[1], parts[2].strip().lower() == "true"
        items.append(S.ok(f"{cid} · {desc}", "") if passed else S.bad(f"{cid} · {desc}", ""))
    return items


def _items_from_markdown(md_path: Path) -> list[dict[str, Any]]:
    """Top-level ``#`` headings → items (review fallback when no notes JSON)."""
    items: list[dict[str, Any]] = []
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [S.dot(f"{md_path.name} could not be read")]
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                items.append(S.item(heading[:200], md_path.name, m="›", mc="blue"))
                if len(items) >= 25:
                    break
    return items


# ── files + diff text (kickoff §1) ──────────────────────────────────────────


def _show_diff_for(run: Run, node: Node) -> bool:
    """``True`` when the node itself is a code-changing node, or when an
    earlier-starting code-changing node already ran.

    The DAG-column layout collapses every node into one column when the
    workflow declares no ``depends_on`` edges (the test fixture does this),
    so a DAG-position check would treat every node as "stage 0". A
    start-time check distinguishes "the planner ran before the
    implementer" correctly in that layout.
    """
    self_start = node.start
    for n in run.nodes:
        if n.type not in _CODE_CHANGING_TYPES:
            continue
        if n.id == node.id:
            return True  # this node is itself a code-changing node
        if self_start is not None and n.start is not None:
            if n.start <= self_start:
                return True
        else:
            # No start info on one side → assume the code-changing node ran
            # first (defensive default: prefer showing the diff over missing
            # it on a row with partial lifecycle data).
            return True
    return False


def _files_and_note(run_dir: Path, run: Run, show_diff: bool) -> tuple[list[dict[str, Any]], str]:
    """Run-cumulative files list with abs paths + ``diff_note`` for empty state."""
    if not show_diff:
        return [], "No code changed by this point."
    diffs = _load_diffs(run_dir)
    if not diffs:
        return [], "No code changes recorded for this run."
    project = run.home.absolute().parent
    files: list[dict[str, Any]] = []
    for d in diffs:
        path = str(d.get("path") or "")
        old_text = str(d.get("old_text") or "")
        new_text = str(d.get("new_text") or "")
        added, removed = _line_diff_counts(old_text, new_text)
        display, absolute = _resolve_project_file(path, project)
        files.append(
            {
                "path": display,
                "added": added,
                "removed": removed,
                "abs": absolute,
            }
        )
    return files, ""


def _line_diff_counts(old_text: str, new_text: str) -> tuple[int, int]:
    """+ / − line counts from two strings (multiset subtraction)."""
    old_counter: dict[str, int] = {}
    for ln in old_text.splitlines():
        old_counter[ln] = old_counter.get(ln, 0) + 1
    added = 0
    for ln in new_text.splitlines():
        if ln in old_counter and old_counter[ln] > 0:
            old_counter[ln] -= 1
        else:
            added += 1
    removed = sum(old_counter.values())
    return added, removed


def _load_diffs(run_dir: Path) -> list[dict[str, Any]]:
    """Prefer ``acp-diffs.json``; fall back to ``framework-edit.diff`` /
    ``review-diff.patch`` (unified diffs parsed for path only), else empty."""
    try:
        from mini_ork.acp import diffs as _diffs

        entries, _ = _diffs.cached_or_computed(run_dir)
    except Exception:  # noqa: BLE001 — a broken diff cache must not blank the view
        entries = []
    if entries:
        return entries
    for rel in ("framework-edit.diff", "review-diff.patch"):
        path = run_dir / rel
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            return _parse_unified_diff_into_entries(text)
    return []


def _parse_unified_diff_into_entries(text: str) -> list[dict[str, Any]]:
    """Rough unified-diff → ``[{path, old_text, new_text}]`` for the fallback path.

    Only the file path is recovered; old/new text is empty because the patch
    format is not invertible without git. Good enough for the ``files[]`` list
    when the executor left only the patch file.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" b/", 1)
        if len(parts) != 2:
            continue
        path = parts[1].strip()
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "old_text": "", "new_text": ""})
    return out


def _diff_text(run_dir: Path, files: list[dict[str, Any]]) -> tuple[str, str]:
    """Unified-diff text for the run + a cap note (kickoff §1).

        Returns ``(text, note)``. ``note`` is non-empty only when the cap fires,
        e.g. ``"Diff capped at 300 KB."`` — the IDE prints it next to a "show
    diff" toggle so a cap is enough to spot without re-loading the whole file.
    """
    if not files:
        return "", ""
    diffs, _ = _safe_cached(run_dir)
    chunks: list[str] = []
    for d in diffs or []:
        path = str(d.get("path") or "")
        old = str(d.get("old_text") or "").splitlines(keepends=True)
        new = str(d.get("new_text") or "").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}"))
    text = "".join(chunks)
    if len(text) > DIFF_TEXT_CAP:
        text = text[:DIFF_TEXT_CAP]
        return text, f"Diff capped at {DIFF_TEXT_CAP // 1000} KB."
    return text, ""


def _safe_cached(run_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        from mini_ork.acp import diffs as _diffs

        return _diffs.cached_or_computed(run_dir)
    except Exception:  # noqa: BLE001
        return [], False


# ── commits (kickoff §1) ─────────────────────────────────────────────────────


def _commits(run: Run) -> tuple[list[dict[str, Any]], str]:
    """``git log --numstat <base_sha>..<branch>`` on the workspace → commits list."""
    ws = run.workspace
    if ws is None:
        return _fallback_recorded_commit(run)
    ws_path = getattr(ws, "path", None)
    if ws_path is None or not Path(ws_path).is_dir():
        return _fallback_recorded_commit(run)
    base_sha = str(getattr(ws, "base_sha", "") or "")
    branch = str(getattr(ws, "branch", "") or "")
    if not base_sha or not branch:
        return _fallback_recorded_commit(run)
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(ws_path),
                "log",
                "--pretty=format:@@@%H|%aI|%aN|%s",
                "--numstat",
                f"{base_sha}..{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _fallback_recorded_commit(run)
    if proc.returncode != 0:
        return _fallback_recorded_commit(run)
    commits = _parse_commits(proc.stdout)
    if commits:
        return commits, ""
    return _fallback_recorded_commit(run)


def _fallback_recorded_commit(run: Run) -> tuple[list[dict[str, Any]], str]:
    """Publisher-recorded commit from run-local JSON (``publish.json`` / ``verdict.json`` /
    ``run-verdict.json``). Empty with a note when neither source has anything."""
    for rel in ("publish.json", "verdict.json", "run-verdict.json"):
        data = _read_json(run.run_dir / rel)
        if not isinstance(data, dict):
            continue
        sha = str(data.get("commit") or data.get("merge_commit") or data.get("sha") or "")
        if sha:
            return (
                [
                    {
                        "sha": sha,
                        "subject": str(data.get("subject") or ""),
                        "when": str(data.get("when") or ""),
                        "author": str(data.get("author") or ""),
                        "files": [],
                    }
                ],
                "",
            )
    return [], "No commits from this run."


def _parse_commits(output: str) -> list[dict[str, Any]]:
    """Parse ``@@@<sha>|<iso>|<author>|<subject>`` + numstat rows into commits."""
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in output.splitlines():
        if raw.startswith("@@@"):
            if current is not None:
                commits.append(current)
            header = raw[3:].split("|", 3)
            if len(header) != 4:
                current = None
                continue
            current = {
                "sha": header[0],
                "subject": header[3],
                "when": header[1],
                "author": header[2],
                "files": [],
            }
            continue
        if current is None:
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        try:
            added = int(parts[0]) if parts[0] != "-" else 0
            removed = int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
        current["files"].append({"path": parts[2], "added": added, "removed": removed})
    if current is not None:
        commits.append(current)
    return commits


# ── shared helpers ──────────────────────────────────────────────────────────


def _resolve_project_file(path: str, project: Path) -> tuple[str, str | None]:
    """``(display path, existing absolute path or None)`` for a changed file.

    Mirrors :func:`mini_ork.cli.board_cmd._project_file`. A delivered run's
    worktree is gone, so its recorded path is mapped onto the project: the
    longest tail of the path that exists in the project wins.
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


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_tail(path: Path, n: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-n:]


def _first_existing_log(run_dir: Path, node_id: str) -> Path | None:
    for tpl in ("verifier_{node}.log", "evidence/{node}.log", "verifier-{node}.log"):
        path = run_dir / tpl.format(node=node_id)
        if path.is_file():
            return path
    return None


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"
