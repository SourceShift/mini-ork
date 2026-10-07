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
:mod:`mini_ork.ide_pages.run` (``Run``, ``Node``) only. File-changed-file
resolution is delegated to ``board_cmd._project_file`` via a lazy import
inside the helper to keep the CLI → pages import direction intact.
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
    # Fix #4 + #5: load diffs once per build; pass ``run`` so the git
    # fallback can resolve ``run.workspace`` (base_sha / branch / path).
    diff_entries, diff_text, diff_source = _load_diffs(run)
    title, items = _result_items(run, node)
    show_diff = _show_diff_for(run, node)
    files, files_note = _files_and_note(run, show_diff, diff_entries, diff_source)
    diff, cap_note = _diff_text(files, diff_entries, diff_text) if show_diff else ("", "")
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


def _is_review_node(node: Node) -> bool:
    """Fix #3: lens / review coalition rule mirrored from ``run.py:614``.

    Treat a node as a review node when its type is in the review set OR
    its id contains ``lens`` or ``review`` (the recipe's lens convention).
    """
    ntype = str(node.type or "")
    if ntype in ("reviewer", "eval", "judge", "lens", "synthesizer"):
        return True
    nid = str(node.id or "")
    return "lens" in nid or "review" in nid


def _result_items(run: Run, node: Node) -> tuple[str, list[dict[str, Any]]]:
    """Project the node's result artefacts onto ``(title, items)``."""
    ntype = str(node.type or "")
    if ntype in ("planner", "decomposer"):
        return _planner_items(run.run_dir)
    if _is_review_node(node):
        return _review_items(run, node)
    if ntype in ("verifier", "test", "typecheck", "static_check"):
        return _verifier_items(run.run_dir, node)
    if ntype == "rollback":
        return _rollback_items(run.run_dir)
    return _other_node_items(run.run_dir, node)


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


def _review_items(run: Run, node: Node) -> tuple[str, list[dict[str, Any]]]:
    """Reviewer / lens / synthesizer / eval / judge: verdict first, then findings.

    Fix #3: lens / review resolution reuses ``Node._report_paths`` (lazy import,
    strips ``_lens`` → ``lens-code_impact.md``). The ``review-<id>.json``
    fallback remains for legacy runs.
    """
    items: list[dict[str, Any]] = []
    verdict_text, verdict_color = "", "sub"
    run_dir = run.run_dir

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
            items.extend(_finding_item(n, run) for n in notes if n)

    # 2. Markdown fallback (only when the review JSON had no notes). Fix
    # #3: prefer ``Node._report_paths(run_dir, node.id)`` (lazy import),
    # which strips ``_lens`` and walks ``lens-<stripped>.md`` →
    # ``lens-<id>.md`` → ``<stripped>.md`` → ``<id>.md``.
    if not items:
        try:
            from mini_ork.ide_pages.node import _report_paths

            report_candidates = _report_paths(run_dir, node.id)
        except Exception:  # noqa: BLE001 — a missing helper must not break the view
            report_candidates = []
        # Drop ``review-<id>.json`` / ``verifier_<id>.json`` (those are JSON
        # paths the helper lists but the markdown fallback consumes only
        # ``*.md`` files).
        for md_path in report_candidates:
            if md_path.suffix != ".md":
                continue
            if md_path.is_file():
                items = _items_from_markdown(md_path)
                break
        if not items:
            for md_path in (run_dir / f"lens-{node.id}.md", run_dir / "synthesis.md",
                            run_dir / f"{node.id}.md"):
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
    """Fix #1 + #2: read verifier artefacts by stem (from ``node.prompt``), then node id.

    Stem = ``Path(node.prompt).stem`` when ``node.prompt`` ends in ``.py``;
    otherwise fall back to ``node.id``. Read, in order:

    * ``verifier_<stem>.json`` — its ``checks`` array (recipe verifier shape).
    * ``verifier-<stem>.checks.tsv`` — recipe verifier tab-separated rows.
    * ``verifier_<stem>.json`` again, treated as the executor shape
      (``pass`` / ``post_rc`` / ``error_summary`` / ``evidence_path``)
      when no ``checks[]`` was found and no TSV resolved (Fix #1).
    * ``evidence/<stem>*.log`` — first matching evidence log for ``path``.

    Each check item: ``t`` = check name, ``sub`` = ``rc <n> · <log name>``,
    ``path`` = absolute log path when it exists, colour by pass/fail.
    """
    items: list[dict[str, Any]] = []
    title = f"Verifier · {node.id}"

    stem = _verifier_stem(node)
    candidates = [stem, node.id]

    # 1. JSON with ``checks`` array (recipe verifier shape).
    for candidate in candidates:
        vjson = _read_json(run_dir / f"verifier_{candidate}.json")
        if isinstance(vjson, dict):
            checks = vjson.get("checks") or []
            if isinstance(checks, list) and checks:
                for chk in checks:
                    if not isinstance(chk, dict):
                        continue
                    items.append(_check_item(chk, run_dir, _fallback_log(run_dir, candidate, vjson)))
                if items:
                    break

    # 2. TSV fallback (recipe verifier when JSON was not written).
    if not items:
        for candidate in candidates:
            tsv_path = run_dir / f"verifier-{candidate}.checks.tsv"
            if tsv_path.is_file():
                items = _items_from_checks_tsv_with_log(tsv_path, run_dir)
                if items:
                    break

    # 3. Executor-shape JSON fallback (Fix #1): the executor may have
    #    written a flat verdict JSON (``pass`` / ``post_rc`` /
    #    ``error_summary`` / ``evidence_path``) without a ``checks[]``
    #    array and without a TSV. Render one red/green verdict row with
    #    the log tail. Never fall back to the silent "No verifier
    #    artefacts found" dot when the keys are recognisable.
    if not items:
        for candidate in candidates:
            vjson = _read_json(run_dir / f"verifier_{candidate}.json")
            if not isinstance(vjson, dict):
                continue
            row = _verdict_row_from_executor_shape(vjson, run_dir)
            if row is not None:
                items.append(row)
                break

    if not items:
        items.append(S.dot("No verifier artefacts found"))
    return (title, items)


# Fix #1: executor-shape JSON → one verdict row. Recognise when the
# JSON has the flat verdict keys (no ``checks[]`` array).
_RECOGNISED_EXECUTOR_KEYS = ("pass", "post_rc", "error_summary", "evidence_path")


def _verdict_row_from_executor_shape(vjson: dict[str, Any], run_dir: Path) -> dict[str, Any] | None:
    """One executor-shape verifier JSON → one spec row + log tail.

    ``verifier_vnode.json = {"pass": false, "post_rc": 2,
    "error_summary": "pytest failed"}`` renders ``t="pytest failed"``,
    ``sub="rc 2"`` (with the log filename), ``mc="red"``, ``path`` =
    absolute log path, ``log_tail`` = the file's tail text. Returns
    ``None`` when the JSON has no recognised executor-shape keys.
    """
    if not any(k in vjson for k in _RECOGNISED_EXECUTOR_KEYS):
        return None
    text = str(vjson.get("error_summary") or vjson.get("verifier") or "verifier")
    passed = bool(vjson.get("pass"))
    rc = vjson.get("post_rc", "")
    evidence_path = str(vjson.get("evidence_path") or "")
    sub_bits: list[str] = []
    if rc != "" and rc is not None:
        sub_bits.append(f"rc {rc}")
    log_path = Path(evidence_path) if evidence_path else None
    log_basename = ""
    abs_log_path = ""
    if log_path is not None and log_path.is_file():
        abs_log_path = str(log_path)
        log_basename = log_path.name
    elif evidence_path:
        # Try to resolve via evidence/<stem>*.log when evidence_path's
        # basename is generic (e.g. ``verifier-static-check.log``).
        generic = Path(evidence_path).name
        stem = generic.removesuffix(".log") or generic
        if (run_dir / "evidence").is_dir():
            matches = sorted((run_dir / "evidence").glob(f"{stem}*.log"))
            if matches:
                abs_log_path = str(matches[0])
                log_basename = Path(abs_log_path).name
    if log_basename:
        sub_bits.append(log_basename)
    sub = " · ".join(sub_bits)
    mark, mc = ("✓", "green") if passed else ("✗", "red")
    acts: list[dict[str, Any]] = []
    if abs_log_path:
        acts.append(S.btn("Open log", S.open_path(abs_log_path), "ghost"))
    out = S.item(text, sub, m=mark, mc=mc, acts=acts)
    out["path"] = abs_log_path
    out["passed"] = passed
    out["log_tail"] = _log_tail(abs_log_path) if abs_log_path else ""
    return out


def _log_tail(path: str, n: int = 50) -> str:
    """Last ``n`` lines of a verifier log file (empty on missing)."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _verifier_stem(node: Node) -> str:
    """Stem from ``node.prompt`` (``verifiers/<x>.py`` → ``<x>``), else node id.

    The recipe writes ``node.prompt = "<recipe_dir.name>/<prompt_ref|verifier_ref>"``;
    for verifier nodes the relevant tail is the verifier_ref basename without
    ``.py``. When ``node.prompt`` doesn't end in ``.py`` (e.g. a legacy
    prompt_ref), fall back to ``node.id`` so the legacy fixture still
    resolves.
    """
    prompt = str(node.prompt or "")
    if prompt.endswith(".py"):
        tail = prompt.rsplit("/", 1)[-1]
        return Path(tail).stem
    return node.id


def _fallback_log(run_dir: Path, stem: str, vjson: dict[str, Any]) -> str:
    """The verifier's own log when its checks name none.

    Real ``checks[]`` entries carry only ``name/expected/actual/pass``. Use
    ``evidence/<stem>.log``, then the newest ``evidence/<stem>*.log``, then
    the JSON's top-level ``evidence_path``; ``""`` when none exists.
    """
    evidence = run_dir / "evidence"
    exact = evidence / f"{stem}.log"
    if exact.is_file():
        return str(exact)
    if evidence.is_dir():
        matches = sorted(evidence.glob(f"{stem}*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return str(matches[0])
    top = str(vjson.get("evidence_path") or "")
    return top if top and Path(top).is_file() else ""


def _check_item(chk: dict[str, Any], run_dir: Path, fallback_log: str = "") -> dict[str, Any]:
    """One recipe-verifier check → spec item with ``t`` / ``sub`` / ``path``.

    A check that names no log of its own points at ``fallback_log`` (the
    verifier's log), so real rows still carry a log name and path.
    """
    name = str(chk.get("name") or chk.get("cid") or chk.get("id") or "")
    rc = chk.get("rc", "")
    log_name = str(chk.get("log") or chk.get("log_name") or "")
    if not log_name and fallback_log:
        log_name = fallback_log
    passed = bool(chk.get("pass"))
    sub_bits: list[str] = []
    if rc != "" and rc is not None:
        sub_bits.append(f"rc {rc}")
    if log_name:
        sub_bits.append(Path(log_name).name)
    sub = " · ".join(sub_bits)
    abs_log_path = ""
    if log_name:
        candidate = Path(log_name) if Path(log_name).is_absolute() else run_dir / log_name
        if candidate.is_file():
            abs_log_path = str(candidate)
        else:
            # Try evidence/<stem>*.log pattern when the check records a
            # generic log name (e.g. "verifier-static-check.log").
            stem = Path(log_name).stem
            matches = sorted((run_dir / "evidence").glob(f"{stem}*.log")) if (run_dir / "evidence").is_dir() else []
            if matches:
                abs_log_path = str(matches[0])
    acts: list[dict[str, Any]] = []
    if abs_log_path:
        acts.append(S.btn("Open log", S.open_path(abs_log_path), "ghost"))
    mark, mc = ("✓", "green") if passed else ("✗", "red")
    out = S.item(name, sub, m=mark, mc=mc, acts=acts)
    out["path"] = abs_log_path
    out["passed"] = passed
    return out


def _items_from_checks_tsv_with_log(tsv_path: Path, run_dir: Path) -> list[dict[str, Any]]:
    """Recipe verifier TSV → items with log-path resolution.

    Fix #3: each row's ``sub`` is ``rc <n> · <log name>`` when either is
    known; ``path`` is the absolute log path. The TSV has no ``rc`` /
    ``log`` columns, so ``rc`` is looked up by ``cid`` from
    ``verifier_<stem>.json``'s ``checks[]`` (when present). When no
    ``rc`` is found, ``sub`` is log-name-only.
    """
    items: list[dict[str, Any]] = []
    try:
        text = tsv_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [S.bad("could not read checks.tsv")]
    stem = tsv_path.name.removeprefix("verifier-").removesuffix(".checks.tsv")
    # Look up rc / log_name from the JSON's matching ``checks[]`` by cid.
    rc_by_cid: dict[str, Any] = {}
    log_by_cid: dict[str, str] = {}
    vjson = _read_json(run_dir / f"verifier_{stem}.json")
    if isinstance(vjson, dict):
        chk_list = vjson.get("checks") or []
        if isinstance(chk_list, list):
            for chk in chk_list:
                if not isinstance(chk, dict):
                    continue
                cid = str(chk.get("name") or chk.get("cid") or chk.get("id") or "")
                if not cid:
                    continue
                rc_by_cid[cid] = chk.get("rc", "")
                ln = str(chk.get("log") or chk.get("log_name") or "")
                if ln:
                    log_by_cid[cid] = ln
    abs_log = ""
    log_basename = ""
    if (run_dir / "evidence").is_dir():
        matches = sorted((run_dir / "evidence").glob(f"{stem}*.log"))
        if matches:
            abs_log = str(matches[0])
            log_basename = Path(abs_log).name
    acts: list[dict[str, Any]] = []
    if abs_log:
        acts.append(S.btn("Open log", S.open_path(abs_log), "ghost"))
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        cid, desc, passed = parts[0], parts[1], parts[2].strip().lower() == "true"
        sub_bits: list[str] = []
        rc = rc_by_cid.get(cid, "")
        if rc != "" and rc is not None:
            sub_bits.append(f"rc {rc}")
        log_name = log_by_cid.get(cid, "") or log_basename
        if log_name:
            sub_bits.append(log_name)
        sub = " · ".join(sub_bits)
        mark, mc = ("✓", "green") if passed else ("✗", "red")
        out = S.item(f"{cid} · {desc}", sub, m=mark, mc=mc, acts=list(acts))
        out["path"] = abs_log
        out["passed"] = passed
        items.append(out)
    return items


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


def _finding_item(note: Any, run: Run | None = None) -> dict[str, Any]:
    """One review finding → spec item with severity mark + ``file:line`` + open.

    Fix #6: resolve relative ``file_path`` against the project root, then
    the run's workspace path when it has one. Set ``item["path"]`` to the
    absolute path when the file exists, and keep the Open act pointing at
    it. Never resolve against the process cwd.
    """
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
    abs_path = _resolve_finding_path(file_path, run)
    acts: list[dict[str, Any]] = []
    if abs_path:
        acts.append(S.btn("Open", S.open_path(abs_path), "ghost"))
    out = S.item(text, sub, m=m, mc=mc, acts=acts)
    if abs_path:
        out["path"] = abs_path
    return out


def _resolve_finding_path(file_path: str, run: Run | None) -> str:
    """Project-rooted resolution (Fix #6).

    A reviewer captures paths against the worktree at run time; the IDE
    view runs in a different cwd (process cwd, often the operator's
    shell) and must not consult ``Path(file_path).is_file()`` against
    cwd. Resolution order:

    1. ``Path(file_path)`` absolute and existing → return as-is.
    2. Try ``run.home.absolute().parent / file_path`` (project root).
    3. Try ``run.workspace.path / file_path`` (workspace record).
    4. Otherwise ``""`` (no open action; finding stays in the list).
    """
    if not file_path:
        return ""
    p = Path(file_path)
    if p.is_absolute() and p.is_file():
        return str(p)
    if run is None:
        return ""
    project = run.home.absolute().parent
    candidate = project / file_path
    if candidate.is_file():
        return str(candidate)
    ws = run.workspace
    if ws is not None:
        ws_path = getattr(ws, "path", None)
        if ws_path:
            candidate = Path(ws_path) / file_path
            if candidate.is_file():
                return str(candidate)
    return ""


def _verdict_item(verdict: str, color: str) -> dict[str, Any]:
    mark = {"green": "✓", "yellow": "!", "red": "✗", "sub": "•"}.get(color, "•")
    return S.item(verdict, m=mark, mc=color)


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
    """Fix #1: workflow-order index check.

    A node shows the diff only if a code-changing node sits at or before it
    in ``run.nodes``. The previous start-time heuristic returned ``True``
    defensively when either side was missing, hiding the bug where a
    planner with ``start=None`` was treated as running before every other
    node. With the index check the planner (always index 0 in the DAG) is
    ``False`` and the implementer (later index) is ``True`` even when
    start times are missing.
    """
    nodes_list = list(run.nodes)
    try:
        node_idx = next(i for i, n in enumerate(nodes_list) if n.id == node.id)
    except StopIteration:
        return False
    for n in nodes_list[: node_idx + 1]:
        if str(n.type or "") in _CODE_CHANGING_TYPES:
            return True
    return False


def _files_and_note(run: Run, show_diff: bool,
                    diff_entries: list[dict[str, Any]],
                    diff_source: str) -> tuple[list[dict[str, Any]], str]:
    """Run-cumulative files list with abs paths + ``diff_note`` for empty state.

    Fix #5: ``diff_entries`` and ``diff_source`` come from
    ``build_changes_view``'s single ``_load_diffs`` call, not a fresh one
    here.
    """
    if not show_diff:
        return [], "No code changed by this point."
    if not diff_entries:
        return [], "No code changes recorded for this run."
    project = run.home.absolute().parent
    files: list[dict[str, Any]] = []
    for d in diff_entries:
        path = str(d.get("path") or "")
        added = int(d.get("added") or 0)
        removed = int(d.get("removed") or 0)
        old_text = str(d.get("old_text") or "")
        new_text = str(d.get("new_text") or "")
        if diff_source == "acp" and (old_text or new_text) and not (added or removed):
            added, removed = _line_diff_counts(old_text, new_text)
        display, absolute = _project_file_lazy(path, project)
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


# Fix #4 + #5 — patch-file fallback returns the patch text and per-file
# +/- counts from the patch's hunks (excluding ``+++``/``---`` headers).
# Prefer ``review-diff.patch``, falling back to ``framework-edit.diff``
# when the reviewer patch was capped at 50 KB or ends mid-hunk. When
# neither a patch nor acp diffs exist but the run has a workspace
# branch, fall back to ``git -C <ws_path> diff <base>..<branch>``
# (timeout 5 s). The chosen patch text is returned in the 3-tuple so
# ``_diff_text`` can render it directly without re-derivation.
def _load_diffs(run: Run) -> tuple[list[dict[str, Any]], str, str]:
    """Single source-of-truth loader for the changes view (3-tuple).

    * ``("acp", entries, "")`` — entries from
      ``cached_or_computed(acp-diffs.json)`` (single-arg); empty patch
      text because the source IS the per-file text.
    * ``("review-diff.patch" | "framework-edit.diff", entries, text)``
      — entries parsed from the chosen patch; ``text`` is the patch
      text itself (Fix #5: never concatenated, never re-derived).
    * ``("git", entries, "")`` — entries via ``git diff`` on the
      workspace branch (5 s timeout).
    * ``("", [], "")`` — empty; downstream renders the empty-state.

    The single-arg ``cached_or_computed(run.run_dir)`` call preserves
    the test spy in
    ``tests/unit/test_ide_pages_node_changes.py::test_changes_view_cached_or_computed_called_once``
    — it expects one arg.
    """
    try:
        from mini_ork.acp import diffs as _diffs

        entries, _ = _diffs.cached_or_computed(run.run_dir)
    except Exception:  # noqa: BLE001 — a broken diff cache must not blank the view
        entries = []
    if entries:
        return entries, "", "acp"
    text, name = _select_patch_text(run.run_dir)
    if text:
        return _parse_patch_into_entries(text), text, name
    git_entries, git_text = _git_diff_fallback(run)
    if git_entries:
        return git_entries, git_text, "git"
    return [], "", ""


def _patch_is_truncated(text: str) -> bool:
    """``True`` when ``review-diff.patch`` ends mid-hunk.

    The file on disk is the full git diff (only the reviewer's prompt copy is
    capped), so size alone never means truncated.
    """
    if text.endswith("\n"):
        return False
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped.splitlines()[-1][:1] in ("+", "-", " ")


def _select_patch_text(run_dir: Path) -> tuple[str, str]:
    """Pick ONE patch file as the diff source (never concatenate).

    Fix #5: prefer ``review-diff.patch``. Fall back to
    ``framework-edit.diff`` when ``review-diff.patch`` ends mid-hunk and
    ``framework-edit.diff`` is longer. Returns
    ``("", "")`` when neither file exists or both fail to read.
    """
    review_text = _read_text(run_dir / "review-diff.patch")
    framework_text = _read_text(run_dir / "framework-edit.diff")
    if not review_text and not framework_text:
        return "", ""
    if (review_text and framework_text and _patch_is_truncated(review_text)
            and len(framework_text) > len(review_text)):
        return framework_text, "framework-edit.diff"
    if review_text:
        return review_text, "review-diff.patch"
    return framework_text, "framework-edit.diff"


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _git_diff_fallback(run: Run) -> tuple[list[dict[str, Any]], str]:
    """``git -C <ws_path> diff <base>..<branch>`` with a 5 s timeout.

    Fix #4: ``base_sha`` / ``branch`` / ``path`` come from ``run.workspace``,
    which itself was loaded from ``<home>/worktrees/<run_id>.json`` by
    ``mini_ork.workspaces.load``. We NEVER enumerate sibling run records
    here. When ``base_sha`` is missing, derive
    ``merge-base origin/main <branch>`` instead (5 s timeout). Returns
    ``([], "")`` on missing branch, subprocess failure, or timeout;
    otherwise ``(entries, raw_text)`` so ``_diff_text`` can render the
    patch verbatim.
    """
    ws = run.workspace
    if ws is None:
        return [], ""
    ws_path = getattr(ws, "path", None)
    branch = str(getattr(ws, "branch", "") or "")
    if ws_path is None or not branch:
        return [], ""
    ws_path_str = str(ws_path)
    base = str(getattr(ws, "base_sha", "") or "")
    if not base:
        # No recorded base → ``merge-base origin/main <branch>``.
        try:
            proc = subprocess.run(
                ["git", "-C", ws_path_str, "merge-base", "origin/main", branch],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return [], ""
        if proc.returncode != 0 or not proc.stdout.strip():
            return [], ""
        base = proc.stdout.strip().splitlines()[0]
    try:
        proc = subprocess.run(
            ["git", "-C", ws_path_str, "diff", f"{base}..{branch}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], ""
    if proc.returncode != 0 or not proc.stdout:
        return [], ""
    return _parse_patch_into_entries(proc.stdout), proc.stdout


def _parse_patch_into_entries(text: str) -> list[dict[str, Any]]:
    """Unified-diff → ``[{path, added, removed, old_text, new_text}]``.

    ``old_text`` / ``new_text`` are empty because the patch format is
    not invertible without git. ``added`` / ``removed`` are the ``+`` /
    ``-`` line counts in each file's hunks (excluding the ``+++`` /
    ``---`` headers that the unified-diff format uses to label files).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            if len(parts) != 2:
                current = None
                continue
            path = parts[1].strip()
            if path in seen:
                current = None
                continue
            seen.add(path)
            current = {"path": path, "old_text": "", "new_text": "",
                       "added": 0, "removed": 0}
            out.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            current["added"] += 1
        elif line.startswith("-"):
            current["removed"] += 1
    return out


def _diff_text(files: list[dict[str, Any]],
               diff_entries: list[dict[str, Any]],
               source_text: str) -> tuple[str, str]:
    """Unified-diff text for the run + a cap note.

    Fix #5: ``source_text`` is the single loader's chosen patch text —
    render it directly (capped), never re-derive from ``old_text`` /
    ``new_text`` and never concatenate ``review-diff.patch`` +
    ``framework-edit.diff``. For ``"acp"`` / ``"git"`` sources
    ``source_text`` is empty and we fall back to a ``unified_diff`` from
    per-file old/new text (when available).
    """
    if not files:
        return "", ""
    if source_text:
        text = source_text
    else:
        chunks: list[str] = []
        for d in diff_entries:
            old = str(d.get("old_text") or "").splitlines(keepends=True)
            new = str(d.get("new_text") or "").splitlines(keepends=True)
            if old or new:
                path = str(d.get("path") or "")
                chunks.extend(difflib.unified_diff(
                    old, new, fromfile=f"a/{path}", tofile=f"b/{path}"))
        text = "".join(chunks)
    if len(text) > DIFF_TEXT_CAP:
        text = text[:DIFF_TEXT_CAP]
        return text, f"Diff capped at {DIFF_TEXT_CAP // 1000} KB."
    return text, ""


def _project_file_lazy(path: str, project: Path) -> tuple[str, str | None]:
    """Fix #7: lazy import of ``board_cmd._project_file``.

    The import is inside the function so this module does not need a
    top-level ``from mini_ork.cli.board_cmd import ...`` (mirrors the
    pattern at ``run.py:732``).
    """
    from mini_ork.cli.board_cmd import _project_file

    display, absolute = _project_file(path, project)
    if absolute is None and Path(path).is_absolute() and Path(path).is_file():
        # A live worktree's file that the project does not have yet (a new
        # file): open it where it is, shown relative to its repo root.
        absolute = path
        for parent in Path(path).parents:
            if (parent / ".git").exists():
                display = str(Path(path).relative_to(parent))
                break
    return display, absolute


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


def _read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from ``path`` — verifier files may carry log lines before it.

    Fix #2: tolerant read. ``verifier_static-check.json`` starts with a
    ``DeprecationWarning`` line, so a raw ``json.loads`` fails. Find the
    first line that starts with ``{`` and parse from there to end-of-file; return ``None``
    when the slice is not valid JSON or the parsed value is not a dict.
    Mirrors :func:`mini_ork.ide_pages.run._json_obj`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\{", text, re.MULTILINE)
    if match is None:
        return None
    start = match.start()
    try:
        data = json.loads(text[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"
