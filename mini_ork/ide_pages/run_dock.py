"""The run dock — the right-hand Run panel's data, in one column.

The IDE's run page gets a compact single-column dock panel beside the session
(Orca's right sidebar): **Changes**, **Checks**, **Agents**, **Cost**,
**Learned**. ``mini-ork board page run --arg run=<id> --arg view=dock --tab <t>``
builds it; ``run.build`` branches to :func:`build` at its top when the IDE asks
for ``view=dock`` at spec level 2.

This is a *view of the run page*, not a new page key: ``DOCK_TABS`` is its own
tab bar (so the ``agents`` key never collides with the v2 ``V2_TABS`` alias
logic — the dock is selected before any of that runs).

Everything is reused, never re-derived: the diff loader / file list / commits
live in ``node_changes.py``; the check-row and headline shapes in ``spec.py``
and ``node.py``; the fail-soft wrappers in ``run_story.py``; the run helpers
(``_cost`` / ``_providers`` / ``_node_output`` / ``_review_actions``) in
``run.py``; the outcome in ``outcome.py``. This module only composes them.

Read-only: the panel never writes under the run dir (``_load_diffs`` →
``cached_or_computed`` never writes; ``_commits`` runs read-only ``git log``)
and never mutates the ``run``. Fail-soft per section: every section is built
through ``S.guarded``, so one broken artefact costs one section, never the
panel.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mini_ork import cost_ledger
from mini_ork.ide_pages import outcome as O
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages import run as R
from mini_ork.ide_pages import run_story
from mini_ork.ide_pages.node import _learned_record
from mini_ork.ide_pages.node_changes import (
    _check_item,
    _commits,
    _diff_text,
    _fallback_log,
    _is_review_node,
    _items_from_checks_tsv_with_log,
    _read_json,
    _verdict_row_from_executor_shape,
    _verifier_stem,
)

# The dock's own tab bar: ``changes`` is the default, an unknown tab → ``changes``.
DOCK_TABS: list[tuple[str, str]] = [
    ("changes", "Changes"),
    ("checks", "Checks"),
    ("agents", "Agents"),
    ("cost", "Cost"),
    ("learned", "Learned"),
]
_DOCK_KEYS = frozenset(k for k, _ in DOCK_TABS)

# Verifier node types: the story's set, so the two views cannot drift.
_VERIFIER_TYPES = run_story._VERIFIER_TYPES

# Caps that keep each tab under the board's budget.
_DIFF_LINE_CAP = 600       # unified diff lines shown in the Changes tab
_CHECK_LOG_LINES = 8       # log tail on a failing check row


# ── entry point ──────────────────────────────────────────────────────────────

def build(run: R.Run, tab: str | None, errors: dict[str, str] | None = None
          ) -> list[dict[str, Any]]:
    """The dock's sections for ``tab`` (unknown → ``changes``).

    ``errors`` is threaded into every ``S.guarded`` so a broken section surfaces
    in the page's ``errors`` map; callers that do not care may omit it.
    """
    if errors is None:
        errors = {}
    t = tab if tab in _DOCK_KEYS else "changes"
    return {
        "changes": _changes_sections,
        "checks": _checks_sections,
        "agents": _agents_sections,
        "cost": _cost_sections,
        "learned": _learned_sections,
    }[t](run, errors)


# ── changes ──────────────────────────────────────────────────────────────────

def _changes_sections(run: R.Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    # Load the run-cumulative diffs once, fail-soft, and pass them to both
    # sections so nothing re-reads the cache or shells to git twice.
    entries, diff_text, source = run_story._safe_diffs(run)
    files, files_note = run_story._safe_files(run, entries, source)
    return (
        S.guarded(errors, "Changes", lambda: _changes_summary(run, files))
        + S.guarded(errors, "Changed files",
                    lambda: _changes_files(run, files, files_note, entries, diff_text))
    )


def _changes_summary(run: R.Run, files: list[dict[str, Any]]) -> dict[str, Any]:
    """``+a −r across n files``, plus the branch / commits-ahead line."""
    added = sum(int(f.get("added") or 0) for f in files)
    removed = sum(int(f.get("removed") or 0) for f in files)
    n = len(files)
    text = f"+{added} −{removed} across {n} file{'s' if n != 1 else ''}"
    detail = ""
    actions: list[dict[str, Any]] = []
    ws = run.workspace
    if ws is not None:
        try:
            from mini_ork import workspaces

            st = workspaces.status(ws)
        except Exception:  # noqa: BLE001 — a broken worktree must not blank the panel
            st = {}
        if isinstance(st, dict) and st.get("exists"):
            base = str(getattr(ws, "base_branch", "") or "base")
            ahead = int(st.get("commits_ahead") or 0)
            detail = (f"on {ws.branch} · {ahead} commit{'s' if ahead != 1 else ''} "
                      f"ahead of {base}")
        # The decision a finished run with a worktree needs (reused verbatim).
        if not R._running(run):
            actions = R._review_actions(run)
    return S.triage(text, detail=detail, actions=actions, menu=[], full=False)


def _changes_files(run: R.Run, files: list[dict[str, Any]], files_note: str,
                   diff_entries: list[dict[str, Any]], diff_text: str) -> dict[str, Any]:
    """The changed-files card: per-file ``±``, the capped diff, the commits."""
    if not files:
        return S.lst("Changed files", [S.dot("No changes recorded for this run")], full=False)
    entries = [
        S.file_entry(str(f.get("path") or ""), abs=str(f.get("abs") or ""),
                     added=int(f.get("added") or 0), removed=int(f.get("removed") or 0))
        for f in files
    ]
    text, cap_note = _diff_text(files, diff_entries, diff_text)
    text, line_note = run_story._cap_lines(text, _DIFF_LINE_CAP)
    commits, _commits_note = _commits(run)
    note = " ".join(p for p in (files_note, cap_note, line_note) if p)
    return S.files("Changed files", entries, diff=text, diff_note=note, commits=commits,
                   full=False)


# ── checks ───────────────────────────────────────────────────────────────────

def _checks_sections(run: R.Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    sections = S.guarded(errors, "Checks", lambda: _checks_triage(run))
    for node in run.nodes:
        if str(node.type or "") in _VERIFIER_TYPES:
            sections += S.guarded(errors, f"Checks · {node.id}",
                                  lambda n=node: _verifier_section(run, n))
    # Computed inside the guard: a malformed run-verdict.json (e.g. deeply
    # nested → RecursionError) must cost this one section, never the page.
    sections += S.guarded(errors, "Levels", lambda: _levels_section(run))
    for node in run.nodes:
        if _is_review_node(node):
            sections += S.guarded(errors, f"Review · {node.id}",
                                  lambda n=node: _review_section(run, n))
    return sections


def _levels_section(run: R.Run) -> dict[str, Any] | list[dict[str, Any]]:
    rows = _levels_rows(run)
    return S.checks("Levels", rows, full=False) if rows else []


def _checks_triage(run: R.Run) -> dict[str, Any]:
    """The one true outcome — ``outcome.resolve``'s text/tone/icon/counts, no menu."""
    out = O.resolve(run)
    return S.triage(out["text"], tone=out["tone"], icon=out["icon"], detail=out["detail"],
                    counts=out["counts"], actions=out["actions"], menu=[], full=False)


def _verifier_rows(run_dir: Path, node: R.Node) -> list[dict[str, Any]]:
    """A verifier node's ``checks[]`` (or ``*.checks.tsv``) as :func:`S.check_row`.

    Builds ``check_row``s directly — the ``_verifier_items`` shape (``t``/``sub``)
    is NOT a check row, so feeding it to ``S.checks`` would count every row as
    ``na``. Logs resolve through ``_check_item`` / ``_fallback_log``.
    """
    stem = _verifier_stem(node)
    candidates = [stem, node.id]
    # 1. JSON with a ``checks`` array (recipe verifier shape).
    for cand in candidates:
        vjson = _read_json(run_dir / f"verifier_{cand}.json")
        if isinstance(vjson, dict):
            checks = vjson.get("checks")
            if isinstance(checks, list) and checks:
                fallback = _verifier_log_path(run_dir, cand, vjson)
                rows = [_row_from_item(_check_item(chk, run_dir, fallback), fallback)
                        for chk in checks if isinstance(chk, dict)]
                if rows:
                    return rows
    # 2. TSV fallback.
    for cand in candidates:
        tsv = run_dir / f"verifier-{cand}.checks.tsv"
        if tsv.is_file():
            fallback = _verifier_log_path(run_dir, cand, _read_json(run_dir / f"verifier_{cand}.json") or {})
            rows = [_row_from_item(i, fallback)
                    for i in _items_from_checks_tsv_with_log(tsv, run_dir)]
            if rows:
                return rows
    # 3. Executor-shape JSON (flat ``pass`` / ``error_summary`` / ``post_rc``).
    for cand in candidates:
        vjson = _read_json(run_dir / f"verifier_{cand}.json")
        if isinstance(vjson, dict):
            item = _verdict_row_from_executor_shape(vjson, run_dir)
            if item is not None:
                return [_row_from_item(item)]
    return []


def _verifier_log_path(run_dir: Path, stem: str, vjson: dict[str, Any]) -> str:
    """The verifier's own log: its ``evidence_path``, else ``verifier_<stem>.log``.

    ``_fallback_log`` covers ``evidence/<stem>*.log``; the dock additionally
    honours the run-root ``verifier_<id>.log`` (what ``run._node_output`` reads).
    """
    top = str(vjson.get("evidence_path") or "")
    if top and Path(top).is_file():
        return top
    for name in (f"verifier_{stem}.log", f"verifier-{stem}.log"):
        path = run_dir / name
        if path.is_file():
            return str(path)
    return _fallback_log(run_dir, stem, vjson)


def _row_from_item(item: dict[str, Any], fallback_path: str = "") -> dict[str, Any]:
    passed = bool(item.get("passed"))
    return S.check_row(
        str(item.get("t") or "check"),
        "pass" if passed else "fail",
        detail=str(item.get("sub") or ""),
        log=[] if passed else run_story._log_tail(item.get("path") or fallback_path,
                                                  _CHECK_LOG_LINES),
    )


def _verifier_section(run: R.Run, node: R.Node) -> list[dict[str, Any]]:
    rows = _verifier_rows(run.run_dir, node)
    return [S.checks(node.id, rows, full=False)] if rows else []


_LEVEL_STATE = {"PROVEN": "pass", "REFUTED": "fail", "UNVERIFIED": "pending"}


def _levels_rows(run: R.Run) -> list[dict[str, Any]]:
    """``run-verdict.json``'s ``levels`` as check rows (PROVEN→pass, …)."""
    rv = O._run_verdict(run)
    levels = rv.get("levels") if isinstance(rv, dict) else None
    if not isinstance(levels, dict):
        return []
    rows = []
    for name in O.LEVELS:
        if name not in levels:
            continue
        value = str(levels.get(name) or "")
        rows.append(S.check_row(name, _LEVEL_STATE.get(value, "na")))
    return rows


def _review_section(run: R.Run, node: R.Node) -> list[dict[str, Any]]:
    """A reviewer / lens node with a review JSON → a findings section."""
    review = _read_json(run.run_dir / f"review-{node.id}.json")
    if not isinstance(review, dict):
        return []
    items: list[dict[str, Any]] = []
    reasons: list[str] = []
    run_story._collect_review_items(run, node.id, review.get("findings"), items, reasons,
                                    strings_as_reasons=False)
    run_story._collect_review_items(run, node.id, review.get("notes"), items, reasons,
                                    strings_as_reasons=True)
    run_story._collect_review_items(run, node.id, review.get("reasons"), items, reasons,
                                    strings_as_reasons=True)
    verdict = review.get("verdict")
    if not (items or reasons or verdict):
        return []
    return [S.findings(f"Review · {node.id}", items,
                       verdict=run_story._verdict(verdict), reasons=reasons, full=False)]


# ── agents ───────────────────────────────────────────────────────────────────

def _agents_sections(run: R.Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    return S.guarded(errors, "Pipeline", lambda: _pipeline(run))


def _pipeline(run: R.Run) -> dict[str, Any]:
    """One agent row per node, in ``run.cols`` order (= ``run.nodes``)."""
    providers = R._providers(run.home)
    rows = []
    for node in run.nodes:
        lane = str(node.family or "")
        entry = providers.get(lane)
        model = str(entry.get("model") or "") if isinstance(entry, dict) else ""
        headline, _colour = run_story._headline(run, node, None)
        rows.append(S.agent_row(
            node.id, run_story._state(node), lane=lane, model=model,
            step=headline, last=_last_line(run, node),
            cost=S.money(node.cost) if node.cost and node.cost > 0 else "",
            dur=R._wall(node),
            do=S.page_link("run", "graph", run=run.id, node=node.id),
        ))
    return S.agents("Pipeline", rows, full=False)


def _last_line(run: R.Run, node: R.Node) -> str:
    """The last non-empty output line, capped at 140 chars."""
    try:
        lines = R._node_output(run, node)
    except Exception:  # noqa: BLE001 — a node with no readable output shows none
        return ""
    # An entry can be a whole multi-line agent result: split before picking.
    for entry in reversed(lines):
        for text in reversed(str(entry).splitlines()):
            text = text.strip()
            if text:
                return text[:140]
    return ""


# ── cost ─────────────────────────────────────────────────────────────────────

def _cost_sections(run: R.Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    return (
        S.guarded(errors, "Spend", lambda: _spend(run))
        + S.guarded(errors, "By step", lambda: _bars_by_step(run))
        + S.guarded(errors, "By lane", lambda: _bars_by_lane(run))
    )


def _total(run: R.Run) -> float:
    """The run total the bars scale to — the card's total, else the node sum."""
    total = R._cost(run)
    return total if total > 0 else sum(float(n.cost or 0.0) for n in run.nodes)


def _spend(run: R.Run) -> dict[str, Any]:
    profile = R._run_profile(run)
    cap = profile.get("budget_cap_usd")
    cap_text = S.money(cap) if cap not in (None, "") else "no cap"
    items: list[tuple[Any, ...]] = [
        ("This run", S.money(R._cost(run))),
        ("Calls", len(run.calls)),
        ("Run budget", cap_text),
    ]
    today = _today(run)
    if today is not None:
        items.append(("Today · 24h", f"${today:.2f} of ${_daily_cap():.0f}"))
    return S.kv("Spend", items, full=False)


def _today(run: R.Run) -> float | None:
    """Today's rolling-24h spend, or ``None`` when the run home has no db."""
    db = run.home / "state.db"
    if not db.is_file():
        return None
    try:
        return round(cost_ledger.spent_last_24h(db), 2)
    except Exception:  # noqa: BLE001 — a broken ledger must not blank the panel
        return None


def _daily_cap() -> float:
    try:
        return float(os.environ.get("MO_DAILY_BUDGET_USD", "50") or 50)
    except ValueError:
        return 50.0


def _bars_by_step(run: R.Run) -> dict[str, Any]:
    total = _total(run)
    items = []
    for node in run.nodes:
        cost = float(node.cost or 0.0)
        if cost <= 0:
            continue
        items.append((node.id, (100.0 * cost / total) if total else 0.0, S.money(cost)))
    if not items:
        return S.lst("By step", [S.dot("No metered LLM calls for this run")], full=False)
    return S.bars("By step", items, full=False)


def _bars_by_lane(run: R.Run) -> dict[str, Any]:
    total = _total(run)
    fams: dict[str, float] = {}
    for node in run.nodes:
        cost = float(node.cost or 0.0)
        if cost <= 0:
            continue
        lane = str(node.family or "shell")
        fams[lane] = fams.get(lane, 0.0) + cost
    items = [(lane, (100.0 * cost / total) if total else 0.0, S.money(cost), f"fam:{lane}")
             for lane, cost in sorted(fams.items(), key=lambda kv: -kv[1])]
    if not items:
        return S.lst("By lane", [S.dot("No metered LLM calls for this run")], full=False)
    return S.bars("By lane", items, full=False)


# ── learned ──────────────────────────────────────────────────────────────────

def _learned_sections(run: R.Run, errors: dict[str, str]) -> list[dict[str, Any]]:
    return S.guarded(errors, "Learned", lambda: _learned(run))


def _learned(run: R.Run) -> dict[str, Any]:
    """One item per node with a ``learned/<node>.json`` injection record."""
    items: list[dict[str, Any]] = []
    for node in run.nodes:
        record = _learned_record(run.run_dir, node.id)
        if record is None:
            continue
        md = run.run_dir / "learned" / f"{node.id}.md"
        acts = [S.btn("Open", S.open_path(str(md)), "ghost")] if md.is_file() else []
        if bool(record.get("injected", True)):
            sources = record.get("sources") if isinstance(record.get("sources"), list) else []
            kinds = sorted({str(s.get("kind")) for s in sources
                            if isinstance(s, dict) and s.get("kind")})
            sub = f"injected · {len(sources)} source{'s' if len(sources) != 1 else ''}"
            if kinds:
                sub += f" ({', '.join(kinds)})"
            items.append(S.ok(node.id, sub, acts))
        else:
            reason = str(record.get("reason") or "not injected")
            items.append(S.dot(node.id, f"not injected · {reason}", acts))
    if not items:
        items = [S.dot("No learned context was recorded for this run")]
    return S.lst("Learned", items, full=False)
