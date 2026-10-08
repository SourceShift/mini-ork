"""The run story — one readable row per pipeline node, in DAG order.

The Story tab's spine. :func:`story_section` returns one ``S.story`` section
whose steps are the run's nodes flattened from ``run.cols``; each step says
what the node did (``node._overview_headline``) and expands to its evidence:
per-file diff cards, check results, review findings. It follows Orca's agent
transcript — tool rows, per-file diff cards with ``+N −N``.

Everything here is reused, never re-derived: the headline logic lives in
``node.py``, the per-kind items / diffs / commits in ``node_changes.py``, the
section shapes in ``spec.py``. This module only composes them.

Read-only: it never writes under the run dir (the IDE's fs-watch reloads on
writes) and never mutates the ``run``. Fail-soft per step: an exception in one
step's body becomes a red "could not read" line and the step still renders, so
one broken artefact can never blank the story. The run-level loads
(``_load_diffs`` / ``_files_and_note``) are guarded the same way, and the tab
that embeds this section wraps it in ``S.guarded`` — no single artefact can
blank the page.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.node import (
    _VERDICT_COLOUR_FOR_OVERVIEW,
    _call_for_node,
    _node_tokens,
    _overview_headline,
    _report_paths,
)
from mini_ork.ide_pages.node_changes import (
    _CODE_CHANGING_TYPES,
    _commits,
    _diff_text,
    _files_and_note,
    _is_review_node,
    _load_diffs,
    _read_json,
    _resolve_finding_path,
    _result_items,
    _snippet_line,
    _verifier_items,
)
from mini_ork.ide_pages.run import Node, Run, _wall

# The five state words the IDE draws for a story step.
_STATES = ("done", "running", "failed", "skipped", "pending")
_VERIFIER_TYPES = frozenset({"verifier", "test", "typecheck", "static_check"})
_PLANNER_TYPES = frozenset({"planner", "decomposer"})

# Caps that keep one story under the board's 1.5 s budget.
_DIFF_LINE_CAP = 400       # unified diff lines shown under the first implementer
_CHECK_LOG_LINES = 8       # log tail on a failing check row
_LENS_MD_LINES = 30        # report lines for a lens with no review JSON
_PLAN_STEPS = 6            # plan steps echoed under the planner step


# ── step order + state ──────────────────────────────────────────────────────

def _ordered(run: Run) -> list[Node]:
    """The run's nodes flattened from ``run.cols`` (DAG order), each once.

    Nodes the workflow did not place in a column (defensive) keep their
    ``run.nodes`` order at the end so the story never drops one silently.
    """
    by_id = {n.id: n for n in run.nodes}
    out: list[Node] = []
    seen: set[str] = set()
    for col in run.cols or []:
        for nid in col:
            node = by_id.get(nid)
            if node is None or node.id in seen:
                continue
            seen.add(node.id)
            out.append(node)
    for node in run.nodes:
        if node.id not in seen:
            seen.add(node.id)
            out.append(node)
    return out


def _state(node: Node) -> str:
    """The node's story state: its own state, with ``finish_reason=skipped``
    overriding to ``skipped`` (a node the workflow chose not to run)."""
    if str(node.finish or "").strip().lower() == "skipped":
        return "skipped"
    state = str(node.state or "").strip().lower()
    return state if state in _STATES else "pending"


def _model(run: Run, node: Node) -> str:
    """The provider model that served the node, when an attributed call says so."""
    try:
        for call in run.calls or []:
            if isinstance(call, dict) and _call_for_node(call, node):
                model = str(call.get("model_id") or "")
                if model:
                    return model
    except Exception:  # noqa: BLE001 — a step's meta must not blank the story
        return ""
    return ""


def _meta(run: Run, node: Node) -> list[str]:
    """``[<calls> calls, <tokens> tokens]`` — each part only when known."""
    try:
        out: list[str] = []
        if node.calls:
            out.append(f"{node.calls} calls")
        tokens = _node_tokens(node, run)
        if tokens is not None:
            out.append(f"{tokens:,} tokens")
        return out
    except Exception:  # noqa: BLE001 — a step's meta must not blank the story
        return []


def _headline(run: Run, node: Node, changes: dict[str, Any] | None) -> tuple[str, str]:
    """``_overview_headline`` as a ``(t, c)`` tuple (fail-soft to a red line)."""
    try:
        h = _overview_headline(run, node, run.run_dir, changes)
    except Exception as exc:  # noqa: BLE001 — a broken headline must not blank the step
        return (f"could not read: {type(exc).__name__}: {exc}", "red")
    if not isinstance(h, dict):
        return (str(h), "sub")
    return (str(h.get("t") or ""), str(h.get("c") or "sub"))


# ── body blocks, by node kind ────────────────────────────────────────────────

def _files_block(run: Run, files: list[dict[str, Any]], files_note: str,
                 diff_entries: list[dict[str, Any]], diff_text: str) -> dict[str, Any]:
    """The implementer's per-file diff card block (computed once per run)."""
    entries = [
        S.file_entry(str(f.get("path") or ""), abs=str(f.get("abs") or ""),
                     added=int(f.get("added") or 0), removed=int(f.get("removed") or 0))
        for f in files
    ]
    text, cap_note = _diff_text(files, diff_entries, diff_text)
    text, line_note = _cap_lines(text, _DIFF_LINE_CAP)
    commits, _commits_note = _commits(run)
    note = " ".join(n for n in (files_note, cap_note, line_note) if n)
    return S.block_files(entries, diff=text, diff_note=note, commits=commits)


def _cap_lines(text: str, limit: int) -> tuple[str, str]:
    """Trim ``text`` to ``limit`` lines; ``(text, note)`` with a note on the cap."""
    if not text:
        return "", ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text, ""
    return "\n".join(lines[:limit]) + "\n", f"Diff capped at {limit} lines."


def _safe_diffs(run: Run) -> tuple[list[dict[str, Any]], str, str]:
    """``_load_diffs`` fail-soft: an unreadable cache yields the empty-state."""
    try:
        return _load_diffs(run)
    except Exception:  # noqa: BLE001 — a broken diff cache must not blank the story
        return [], "", ""


def _int_counts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy diff entries with ``added`` / ``removed`` coerced to ints.

    ``_files_and_note`` does a bare ``int(...)`` on each count, so a single
    non-numeric value (e.g. ``"n/a"``) in ``acp-diffs.json`` would raise out of
    it and blank the story. Coercing to ``0`` keeps the file row and the run.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        for key in ("added", "removed"):
            try:
                entry[key] = int(entry.get(key) or 0)
            except (TypeError, ValueError):
                entry[key] = 0
        out.append(entry)
    return out


def _safe_files(run: Run, diff_entries: list[dict[str, Any]], diff_source: str
                ) -> tuple[list[dict[str, Any]], str]:
    """``_files_and_note`` fail-soft: one bad entry degrades to the empty-state."""
    try:
        return _files_and_note(run, True, _int_counts(diff_entries), diff_source)
    except Exception:  # noqa: BLE001 — one bad entry must not blank the story
        return [], "No code changes recorded for this run."


def _finding(run: Run, raw: dict[str, Any], source: str) -> dict[str, Any]:
    """One review finding dict → ``S.finding`` (mirrors ``_finding_item``'s fields)."""
    issue = str(raw.get("title") or raw.get("text") or raw.get("note")
                or raw.get("issue") or raw.get("summary") or "")
    file_path = str(raw.get("file") or raw.get("path") or "")
    return S.finding(
        issue,
        severity=str(raw.get("severity") or raw.get("level") or ""),
        file=file_path,
        line=raw.get("line"),
        abs=_resolve_finding_path(file_path, run),
        snippet=_snippet_line(raw.get("snippet")),
        source=source,
    )


def _verdict(v: Any) -> tuple[str, str] | None:
    text = str(v or "").strip()
    if not text:
        return None
    return (text, _VERDICT_COLOUR_FOR_OVERVIEW.get(text, "sub"))


def _review_body(run: Run, node: Node) -> list[dict[str, Any]]:
    """Reviewer / judge / lens: findings block from the review JSON, else the
    report's first lines as markdown.

    ``findings``, ``notes`` and ``reasons`` are the list-valued review fields.
    A dict entry (``{file, line, issue, severity}``) — the shape the runtime
    asks reviewers for — becomes a finding item; a string becomes a reason.
    This is why a ``{verdict, notes: [{file, line, issue}]}`` review renders
    finding rows, not a Python ``repr`` in the reasons list.
    """
    review = _read_json(run.run_dir / f"review-{node.id}.json")
    items: list[dict[str, Any]] = []
    reasons: list[str] = []
    verdict: Any = None
    if isinstance(review, dict):
        verdict = review.get("verdict")
        # ``findings`` carries findings; ``notes`` / ``reasons`` are strings
        # unless an entry is itself a dict (then it is a finding too).
        _collect_review_items(run, node.id, review.get("findings"), items, reasons,
                              strings_as_reasons=False)
        _collect_review_items(run, node.id, review.get("notes"), items, reasons,
                              strings_as_reasons=True)
        _collect_review_items(run, node.id, review.get("reasons"), items, reasons,
                              strings_as_reasons=True)
    if items or reasons or verdict:
        return [S.block_findings(items, verdict=_verdict(verdict), reasons=reasons)]
    return _lens_md_body(run.run_dir, node)


def _collect_review_items(run: Run, source: str, raw: Any, items: list[dict[str, Any]],
                          reasons: list[str], *, strings_as_reasons: bool) -> None:
    """Fold one review field into ``items`` / ``reasons`` (dicts → findings)."""
    entries = raw if isinstance(raw, list) else [raw]
    for entry in entries:
        if not entry:
            continue
        if isinstance(entry, dict):
            items.append(_finding(run, entry, source))
        elif strings_as_reasons:
            reasons.append(str(entry))
        else:
            items.append(S.finding(str(entry), source=source))


def _lens_md_body(run_dir: Path, node: Node) -> list[dict[str, Any]]:
    """A lens with no review JSON: the first ~30 lines of its report markdown."""
    for path in _report_paths(run_dir, node.id):
        if path.suffix != ".md" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()[:_LENS_MD_LINES]
        if lines:
            return [S.block_md("\n".join(lines))]
    return [S.block_lines([("No review artefacts found", "sub")])]


def _log_tail(path: Any, n: int) -> list[tuple[str, str]]:
    if not path or not Path(str(path)).is_file():
        return []
    try:
        text = Path(str(path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [(line, "muted") for line in text.splitlines()[-n:]]


def _checks_body(run_dir: Path, node: Node) -> list[dict[str, Any]]:
    """Verifier / test / typecheck / static_check: one check row per check.

    A failing row carries up to ``_CHECK_LOG_LINES`` log lines. With no
    check artefacts at all, the verifier's dot items render as plain lines.
    """
    _title, items = _verifier_items(run_dir, node)
    rows: list[dict[str, Any]] = []
    for item in items:
        if "passed" not in item:
            continue
        passed = bool(item.get("passed"))
        rows.append(S.check_row(
            str(item.get("t") or "check"),
            "pass" if passed else "fail",
            detail=str(item.get("sub") or ""),
            log=[] if passed else _log_tail(item.get("path"), _CHECK_LOG_LINES),
        ))
    if rows:
        return [S.block_checks(rows)]
    return [S.block_lines([(str(i.get("t") or ""), str(i.get("mc") or "sub")) for i in items])]


def _planner_body(run_dir: Path) -> list[dict[str, Any]]:
    """Planner: the objective plus up to six plan steps as markdown."""
    plan = _read_json(run_dir / "plan.json")
    if not isinstance(plan, dict):
        return []
    parts: list[str] = []
    objective = str(plan.get("objective") or "").strip()
    if objective:
        parts.append(objective)
    steps = plan.get("steps") or plan.get("decomposition") or []
    if isinstance(steps, list):
        for step in steps[:_PLAN_STEPS]:
            if not isinstance(step, dict):
                continue
            sid = str(step.get("id") or step.get("name") or "")
            desc = str(step.get("description") or step.get("type")
                       or step.get("node_type") or "")
            if sid and desc:
                parts.append(f"- **{sid}** — {desc}")
            elif sid:
                parts.append(f"- **{sid}**")
            elif desc:
                parts.append(f"- {desc}")
    return [S.block_md("\n\n".join(parts))] if parts else []


def _other_body(run: Run, node: Node) -> list[dict[str, Any]]:
    """Publisher / rollback / any other node: ``_result_items`` as plain lines."""
    _title, items = _result_items(run, node)
    lines: list[tuple[str, str]] = []
    for item in items:
        text = str(item.get("t") or "")
        sub = str(item.get("sub") or "")
        if sub:
            text = f"{text} — {sub}" if text else sub
        lines.append((text, str(item.get("mc") or "body")))
    return [S.block_lines(lines)] if lines else []


def _step_body(run: Run, node: Node, ntype: str, give_files: bool,
               files_args: tuple[list[dict[str, Any]], str, list[dict[str, Any]], str]
               ) -> list[dict[str, Any]]:
    """The step's body blocks for its kind; a read error becomes one red line.

    The implementer's files block is built lazily here, inside the ``try`` —
    its sources (``_load_diffs`` / ``_files_and_note``) can raise on a
    malformed ``acp-diffs.json``, and that must cost this one step, never the
    whole story.
    """

    def build() -> list[dict[str, Any]]:
        if ntype in _CODE_CHANGING_TYPES:
            if not give_files:
                return []
            files, files_note, diff_entries, diff_text = files_args
            return [_files_block(run, files, files_note, diff_entries, diff_text)]
        if _is_review_node(node):
            return _review_body(run, node)
        if ntype in _VERIFIER_TYPES:
            return _checks_body(run.run_dir, node)
        if ntype in _PLANNER_TYPES:
            return _planner_body(run.run_dir)
        return _other_body(run, node)

    try:
        return build()
    except Exception as exc:  # noqa: BLE001 — one step's body must not blank the story
        return [S.block_lines([(f"could not read: {type(exc).__name__}: {exc}", "red")])]


def _has_findings(body: list[dict[str, Any]]) -> bool:
    """``True`` only when a findings block carries finding *items*.

    A verdict alone (``approve`` with ``findings: []``) or a bare reason is
    not "findings": the kickoff opens the story on a reviewer that has
    findings, and leaves every other step collapsed.
    """
    return any(isinstance(b, dict) and b.get("kind") == "findings" and b.get("items")
               for b in body)


# ── the section ──────────────────────────────────────────────────────────────

def story_section(run: Run) -> dict[str, Any]:
    """One ``S.story`` section: a step per node, in DAG order, with evidence.

    The run's diffs are loaded once, fail-soft (a malformed diff cache yields
    the empty-state, never an exception), and the files block is built lazily
    under the FIRST implementer step only (the run-cumulative change set is the
    same for every later one). A node that never started and is not part of the
    workflow is skipped.
    """
    diff_entries, diff_text, diff_source = _safe_diffs(run)
    files, files_note = _safe_files(run, diff_entries, diff_source)
    changes = {"files": files}
    files_args = (files, files_note, diff_entries, diff_text)

    steps: list[dict[str, Any]] = []
    seen_implementer = False
    for node in _ordered(run):
        if node.start is None and not node.in_workflow:
            continue
        ntype = str(node.type or "")
        is_impl = ntype in _CODE_CHANGING_TYPES
        give_files = is_impl and not seen_implementer
        if is_impl:
            seen_implementer = True
        state = _state(node)
        body = _step_body(run, node, ntype, give_files, files_args)
        steps.append(S.story_step(
            node.id, node.id, kind=ntype, state=state, lane=node.family,
            model=_model(run, node),
            headline=_headline(run, node, changes if is_impl else None),
            meta=_meta(run, node), dur=_wall(node),
            cost=S.money(node.cost) if node.cost and node.cost > 0 else "",
            open=state in ("failed", "running") or give_files or _has_findings(body),
            do=S.page_link("run", "graph", run=run.id, node=node.id),
            body=body,
        ))
    return S.story("What happened", steps, full=True)
