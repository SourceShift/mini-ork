"""The run dock — the right-hand Run panel: one glance column, not a second page.

The IDE keeps a compact single-column panel beside the session. It is a
*glance*, not a duplicate of the run page: the run page owns every fact in
full (Outcome, Orchestrator, Agents, Checks, Learnings, Artifacts), and this
panel answers the one question a reader asks without opening that tab — *is
this run healthy, what did it cost, what did it change, and where do I go to
see more?* Each fact here is a summary plus a deep link into the tab that
holds the full view; nothing is shown twice at full length.

What it keeps, and why:

- the outcome triage (the one status statement),
- the verifier checks **that failed**, with their log tails (the run page's
  Checks tab has the full list; a failure is the one thing worth reading
  without a click),
- spend against the run's own budget (the run page shows cost by stage, not
  the cap),
- ``+a −r across n files`` plus the worktree branch — the diff itself lives on
  the run page's Artifacts tab.

``mini-ork board page run --arg run=<id> --arg view=dock`` builds it;
``run.build`` branches to :func:`build` when the IDE asks for ``view=dock`` at
spec level 2. :data:`DOCK_TABS` is empty on purpose: the panel has no tab bar
(one column), and the run page's own tab bar is the only tab surface.

Everything is reused, never re-derived: the check-row and headline shapes in
``spec.py`` and ``node.py``; the fail-soft wrappers in ``run_story.py``; the
run helpers (``_cost`` / ``_run_profile`` / ``_review_actions``) in ``run.py``;
the outcome in ``outcome.py``. This module only composes them.

Read-only: the panel never writes under the run dir and never mutates the
``run``. Fail-soft per section: every section is built through ``S.guarded``,
so one broken artefact costs one section, never the panel.
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
from mini_ork.ide_pages.node_changes import (
    _check_item,
    _fallback_log,
    _is_review_node,
    _items_from_checks_tsv_with_log,
    _read_json,
    _verdict_row_from_executor_shape,
    _verifier_stem,
)

# No tab bar: the dock is one glance column, and the run page's tab bar is the
# only tab surface. ``run.build`` still validates the IDE's ``--tab`` against
# this list; an empty list sends any tab to the single view.
DOCK_TABS: list[tuple[str, str]] = []

# Verifier node types: the story's set, so the two views cannot drift.
_VERIFIER_TYPES = run_story._VERIFIER_TYPES

_CHECK_LOG_LINES = 8       # log tail on a failing check row


# ── entry point ──────────────────────────────────────────────────────────────

def build(run: R.Run, tab: str | None = None, errors: dict[str, str] | None = None
          ) -> list[dict[str, Any]]:
    """The dock's glance column.

    ``tab`` is accepted for the ``run.build`` call site and ignored — the panel
    has one view. ``errors`` is threaded into every ``S.guarded`` so a broken
    section surfaces in the page's ``errors`` map; callers that do not care may
    omit it.
    """
    if errors is None:
        errors = {}
    return (
        S.guarded(errors, "Outcome", lambda: _outcome_glance(run))
        + S.guarded(errors, "Checks", lambda: _checks_glance(run))
        + S.guarded(errors, "Spend", lambda: _spend(run))
        + S.guarded(errors, "Changes", lambda: _changes_glance(run))
        + S.guarded(errors, "More", lambda: _more_links(run))
    )


# ── glance sections ──────────────────────────────────────────────────────────

def _outcome_glance(run: R.Run) -> dict[str, Any]:
    """The one true outcome — ``outcome.resolve``'s text/tone/icon/counts, no menu."""
    out = O.resolve(run)
    return S.triage(out["text"], tone=out["tone"], icon=out["icon"], detail=out["detail"],
                    counts=out["counts"], actions=out["actions"], menu=[], full=False)


def _open_btn(label: str, tab: str, run: R.Run) -> dict[str, Any]:
    return S.btn(label, S.page_link("run", tab, run=run.id), "ghost")


def _checks_glance(run: R.Run) -> dict[str, Any] | list[dict[str, Any]]:
    """Pass/fail counts, plus the failing rows with their log tails.

    The summary counts every verifier row and is computed by the same helper
    ``S.checks`` would use, so the counts here and the run page's Checks tab
    cannot disagree; only the failing rows are listed, because the passing ones
    are the page's job.
    """
    rows: list[dict[str, Any]] = []
    for node in run.nodes:
        if str(node.type or "") in _VERIFIER_TYPES:
            rows.extend(_verifier_rows(run.run_dir, node))
    if not rows:
        return S.lst("Checks", [S.dot("No verifier checks recorded for this run")],
                     actions=[_open_btn("Open Checks", "checks", run)], full=False)
    counts = S._checks_summary(rows)
    failing = [row for row in rows if str(row.get("state") or "") == "fail"]
    return S.checks("Checks", failing, summary=counts,
                    actions=[_open_btn("Open Checks", "checks", run)], full=False)


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
    return S.kv("Spend", items, actions=[_open_btn("Open Agents", "agents", run)],
                full=False)


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


def _changes_glance(run: R.Run) -> dict[str, Any]:
    """``+a −r across n files`` and the worktree branch — the diff is on the page."""
    entries, _diff_text, source = run_story._safe_diffs(run)
    files, _files_note = run_story._safe_files(run, entries, source)
    added = sum(int(f.get("added") or 0) for f in files)
    removed = sum(int(f.get("removed") or 0) for f in files)
    n = len(files)
    items: list[tuple[Any, ...]] = [
        ("diff", f"+{added} −{removed} across {n} file{'s' if n != 1 else ''}"),
    ]
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
            items.append(("branch", f"on {ws.branch} · {ahead} commit"
                                    f"{'s' if ahead != 1 else ''} ahead of {base}"))
    actions = [_open_btn("Open Changes", "artifacts", run)]
    # The decision a finished run with a worktree needs (reused verbatim).
    if ws is not None and not R._running(run):
        actions = R._review_actions(run) + actions
    return S.kv("Changes", items, actions=actions, full=False)


def _more_links(run: R.Run) -> dict[str, Any]:
    """Doors to the tabs the glance does not summarise."""
    items = [
        S.dot("Orchestrator", "activity · steering",
              [S.btn("Open", S.page_link("run", "orchestrator", run=run.id), "ghost")]),
        S.dot("Learnings", "injected per node",
              [S.btn("Open", S.page_link("run", "learnings", run=run.id), "ghost")]),
    ]
    return S.lst("More", items, full=False)


# ── checks (shared with the run page's Checks tab) ───────────────────────────

def checks_sections(run: R.Run, errors: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every verifier's checks, the run levels and the reviewers' findings.

    The run page's **Checks** tab renders this; the dock renders only the
    failing rows. No triage: the page's Outcome tab already leads with it.
    """
    if errors is None:
        errors = {}
    sections: list[dict[str, Any]] = []
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
