"""Recipes, specs & epics — the catalog, one recipe in detail, specs and the epic queue.

The catalog and track record come from :func:`mini_ork.acp.recipe_view.recipe_rows`
(the same projection ``/recipes`` shows); a recipe's flow and contract are read
from its YAML; specs from ``spec-index.json`` files; epics from ``state.db``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("recipes", "Recipes"), ("specs", "Specs"), ("epics", "Epics & roadmap")]

_EPIC_LIMIT = 40
_DEP_LIMIT = 14
_SPEC_SEARCH = ("spec-index.json", "specs/spec-index.json", "docs/specs/spec-index.json",
                ".mini-ork/specs/spec-index.json")


def _yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506 — a safe loader
    except Exception:  # noqa: BLE001 — a broken recipe file costs its section only
        return {}
    return data if isinstance(data, dict) else {}


def _db(home: Path):
    from mini_ork.web.db import db_for

    if not (home / "state.db").is_file():
        return None
    return db_for(home)


# ── recipes ────────────────────────────────────────────────────────────────

def _grade_colour(letter: str) -> str:
    return {"A": "green", "B": "text", "C": "yellow", "D": "red", "F": "red"}.get(letter[:1], "sub")


def _recipe_sections(home: Path, args: dict[str, str], errors: dict[str, str],
                     rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from mini_ork import recipes_catalog

    entries = {e.id: e for e in recipes_catalog.list_recipes(home)}
    sel_id = args.get("recipe") if args.get("recipe") in {r["id"] for r in rows} else (
        rows[0]["id"] if rows else "")
    args["recipe"] = sel_id
    sel = next((r for r in rows if r["id"] == sel_id), None)
    entry = entries.get(sel_id)
    workflow = _yaml(entry.path / "workflow.yaml") if entry else {}
    contract = _yaml(entry.path / "artifact_contract.yaml") if entry else {}

    def catalog() -> dict[str, Any]:
        out = []
        for r in rows:
            source = "project" if str(r["source"]).startswith("project") else "engine"
            ok_pct = f"{r['success_pct']:.0f}%" if r.get("runs") else "—"
            avg = S.money(r["avg_cost_usd"]) if r.get("runs") and r.get("avg_cost_usd") else "—"
            out.append({"cells": [S.mono(r["id"]), S.cell(source, "cyan" if source == "project" else "sub"),
                                  S.cell(r.get("grade_letter") or "—", _grade_colour(str(r.get("grade_letter") or ""))),
                                  S.mono(r.get("runs") or 0), S.mono(ok_pct), S.mono(avg)],
                        "do": S.set_args(recipe=r["id"]), "sel": r["id"] == sel_id})
        if not out:
            out = [{"cells": [S.muted("No recipes found"), "", "", "", "", ""]}]
        return S.table("Catalog", [S.col(fr=1, min=120), S.col(56), S.col(40), S.col(40), S.col(44), S.col(56)],
                       ["recipe", "source", "grade", "runs", "ok", "avg"], out, col=1)

    def detail() -> dict[str, Any]:
        if sel is None:
            return S.kv("No recipe selected", [], col=2)
        is_project = str(sel["source"]).startswith("project")
        return S.kv(sel_id, [
            ("Grade", sel.get("grade_letter") or "—", _grade_colour(str(sel.get("grade_letter") or "")),
             f"recipe-eval {sel.get('grade_score')}/100" if sel.get("grade_score") else ""),
            ("Runs", str(sel.get("runs") or 0)),
            ("Success", f"{sel['success_pct']:.0f}%" if sel.get("runs") else "—",
             "green" if sel.get("runs") and sel["success_pct"] >= 80 else "text", "published / finished"),
            ("Avg cost", S.money(sel["avg_cost_usd"]) if sel.get("runs") and sel.get("avg_cost_usd") else "—"),
        ], col=2, note=str(sel.get("description") or ""), actions=[
            S.btn("Run with this", S.thread(f"/run {sel_id} "), "primary"),
            S.btn("Edit" if is_project else "Copy into project", S.thread(f"/recipe edit {sel_id} ")),
        ])

    def flow() -> dict[str, Any]:
        nodes = [n for n in workflow.get("nodes") or [] if isinstance(n, dict)]
        items = [(str(n.get("name")), str(n.get("model_lane") or n.get("type") or ""),
                  "cyan" if n.get("type") == "verifier" else "text") for n in nodes]
        return S.flow("Flow", items or [("no nodes in workflow.yaml", "", "sub")], col=2)

    def contract_list() -> dict[str, Any]:
        items = [S.dot(f"Produces: {contract.get('expected_artifact') or '—'}")]
        verifiers = [str(v) for v in contract.get("success_verifiers") or []]
        items += [S.ok(f"verifier {v.rsplit('/', 1)[-1]}") for v in verifiers]
        if not verifiers:
            items.append(S.warn("No success_verifiers", "a run of this recipe can only read vacuous, never a pass"))
        lanes: dict[str, str] = {}
        try:
            from mini_ork.web.recipes import load_lanes

            lanes = load_lanes(home)
        except Exception:  # noqa: BLE001 — lane families are a nicety
            pass
        for n in workflow.get("nodes") or []:
            if isinstance(n, dict) and n.get("model_lane"):
                lane = str(n["model_lane"])
                fam = lanes.get(lane, "")
                items.append(S.dot(f"{n.get('name')} → {lane}", fam))
        return S.lst("Contract", items, col=2)

    def checks() -> dict[str, Any]:
        items = []
        if entry is not None:
            from mini_ork.cli.recipe_eval import eval_recipe

            result = eval_recipe(entry.path.parent.parent, sel_id)
            findings = [f for f in result.get("findings") or [] if isinstance(f, dict)]
            errors_ = [f for f in findings if f.get("sev") == "error"]
            if not findings:
                items.append(S.ok("recipe-eval: static evaluation clean", f"score {result.get('score')}/100"))
            else:
                items.append((S.bad if errors_ else S.warn)(
                    f"recipe-eval: {len(findings)} finding{'s' if len(findings) != 1 else ''}",
                    f"score {result.get('score')}/100"))
                items += [S.dot(f.get("msg") or "", f.get("fix") or "", mc="red" if f.get("sev") == "error"
                                else "yellow") for f in findings[:6]]
            example = entry.path / "example-kickoff.md"
            examples = sorted((entry.path / "examples").glob("*/kickoff.md")) if (entry.path / "examples").is_dir() else []
            kickoff = example if example.is_file() else (examples[0] if examples else None)
            if kickoff is not None:
                items.append(S.dot("Test it now", "runs once on its example kickoff",
                                   [S.btn("Open kickoff", S.open_path(str(kickoff)), "ghost"),
                                    S.btn("Test", S.thread(f"/run {sel_id} {kickoff}"))]))
            else:
                items.append(S.dot("Test it now", "no example kickoff yet"))
        return S.lst("Checks", items or [S.dot("Select a recipe")], col=2)

    return (S.guarded(errors, "Catalog", catalog) + S.guarded(errors, "Detail", detail)
            + S.guarded(errors, "Flow", flow) + S.guarded(errors, "Contract", contract_list)
            + S.guarded(errors, "Checks", checks))


# ── specs ──────────────────────────────────────────────────────────────────

def _spec_indexes(project: Path) -> list[Path]:
    found = [project / rel for rel in _SPEC_SEARCH if (project / rel).is_file()]
    return found


def _specs_sections(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    def specs() -> dict[str, Any]:
        project = home.absolute().parent
        indexes = _spec_indexes(project)
        rows = []
        for idx in indexes:
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for spec_id, s in sorted((data.get("specs") or {}).items()):
                status = str(s.get("status") or "draft")
                deps = len(s.get("depends_on") or [])
                src = str(s.get("source_path") or "")
                rows.append({"cells": [S.mono(spec_id), s.get("title") or "",
                                       S.cell(status, {"done": "green", "ready": "green", "blocked": "yellow"}
                                              .get(status, "sub")),
                                       S.mono(deps)],
                             "do": S.open_path(str(Path(data.get("root") or idx.parent) / src)) if src else None})
        note = ("Ingested deterministically into spec cards; spec-driven-delivery runs against them."
                if rows else "No spec-index.json in this project yet. mini-ork specs ingest <dir> builds one.")
        if not rows:
            rows = [[S.muted("No specs ingested"), "", "", ""]]
        return S.table(f"specs/ · {len(indexes)} index{'es' if len(indexes) != 1 else ''}" if indexes else "specs/",
                       [S.col(fr=1, min=140), S.col(fr=1, min=160), S.col(70), S.col(50)],
                       ["spec", "title", "status", "deps"], rows, full=True, note=note,
                       actions=[S.btn("Ingest directory", S.thread("/kickoff ingest the specs directory with "
                                                                   "mini-ork specs ingest ")),
                                S.btn("Lint all", S.thread("Run mini-ork specs lint on the specs directory"))])

    return S.guarded(errors, "specs/", specs)


# ── epics ──────────────────────────────────────────────────────────────────

_EPIC_COLOUR = {"done": "green", "in progress": "blue", "in review": "yellow", "blocked": "sub",
                "escalated": "red", "not started": "sub"}


def _epics_sections(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    epics: list[dict[str, Any]] = []

    def table() -> dict[str, Any]:
        db = _db(home)
        if db is None or not db.has_table("epics"):
            return S.table("Epics", [S.col(fr=1)], ["epic"], [[S.muted("No epics yet")]], full=True)
        # Older databases lack the scheduler columns (priority, recipe, attempts).
        cols = {r["name"] for r in db.rows("PRAGMA table_info(epics)")}
        optional = [c for c in ("priority", "recipe", "attempts", "max_attempts") if c in cols]
        epics.extend(db.rows(
            "SELECT id, title, status, lane, worker_default, salvage_attempts"
            + "".join(f", {c}" for c in optional)
            + " FROM epics WHERE archived_at IS NULL "
              "ORDER BY CASE status WHEN 'in progress' THEN 0 WHEN 'escalated' THEN 1 WHEN 'in review' THEN 2 "
              "WHEN 'not started' THEN 3 WHEN 'blocked' THEN 4 ELSE 5 END"
            + (", priority DESC" if "priority" in cols else "")
            + f", updated_at DESC LIMIT {_EPIC_LIMIT}"))
        rows = []
        for e in epics:
            recipe = e.get("recipe") or e.get("lane") or e.get("worker_default") or "—"
            if "attempts" in e:
                attempts = f"{e.get('attempts') or 0}/{e.get('max_attempts') or '—'}"
            else:
                attempts = str(e.get("salvage_attempts") or 0)
            rows.append([e.get("title") or e["id"],
                         S.cell(e.get("status"), _EPIC_COLOUR.get(str(e.get("status")), "sub")),
                         S.muted(recipe), S.mono(e.get("priority") or 0), S.mono(attempts)])
        if not rows:
            rows = [[S.muted("No epics yet — ingest a roadmap"), "", "", "", ""]]
        total = len(db.rows("SELECT 1 FROM epics WHERE archived_at IS NULL"))
        return S.table("Epics", [S.col(fr=1, min=180), S.col(80), S.col(110), S.col(40), S.col(60)],
                       ["epic", "status", "recipe", "prio", "attempts"], rows, full=True,
                       note=f"{total} active epics" + (f", showing {len(rows)}" if total > len(rows) else "")
                            + ". The scheduler dispatches ready ones in priority order.",
                       actions=[S.btn("Ingest roadmap", S.thread("/kickoff ingest this roadmap with mini-ork epics ingest ")),
                                S.btn("Split into kickoffs", S.thread("Split the roadmap into kickoffs with "
                                                                      "mini-ork epics split "))])

    def deps() -> dict[str, Any]:
        db = _db(home)
        lines: list[tuple[str, str]] = []
        if db is not None and db.has_table("epic_dependencies"):
            for d in db.rows("SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies "
                             "WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT ?", (_DEP_LIMIT,)):
                arrow = "──▶" if d.get("kind") == "hard" else "··▶"
                lines.append((f"{d['from_epic_id']} {arrow} {d['to_epic_id']}",
                              "text" if d.get("kind") == "hard" else "muted"))
        return S.code("Dependencies", lines or [("No unresolved dependencies.", "dim")],
                      note="Unresolved edges: ──▶ hard, ··▶ soft.")

    def attention() -> dict[str, Any]:
        db = _db(home)
        items = []
        for e in epics:
            if e.get("status") == "escalated":
                items.append(S.bad(f"{e['id']} escalated", e.get("title") or "",
                                   [S.btn("Retry", S.thread(f"Retry epic {e['id']} with mini-ork epics retry {e['id']}"),
                                          "primary")]))
        if db is not None and db.has_table("inbox"):
            for i in db.rows("SELECT epic_id, kind, body_md, opened_at FROM inbox WHERE resolved_at IS NULL "
                             "ORDER BY opened_at DESC LIMIT 8"):
                first = next((ln.strip("# ").strip() for ln in str(i.get("body_md") or "").splitlines()
                              if ln.strip()), "")
                items.append(S.warn(f"{i.get('kind')} · {i.get('epic_id')}", first[:160]))
        return S.lst("Needs attention", items or [S.ok("Nothing escalated", "no escalated epics or open epic inbox items")])

    return (S.guarded(errors, "Epics", table) + S.guarded(errors, "Dependencies", deps)
            + S.guarded(errors, "Needs attention", attention))


# ── page ───────────────────────────────────────────────────────────────────

def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    from mini_ork.acp.recipe_view import recipe_rows

    tab = tab if tab in {k for k, _ in TABS} else "recipes"
    errors: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    try:
        rows = recipe_rows(home) if tab == "recipes" else []
        count = len(rows) if tab == "recipes" else _recipe_count(home)
    except Exception as exc:  # noqa: BLE001
        errors["catalog"] = f"{type(exc).__name__}: {exc}"
        count = 0
    if tab == "recipes":
        sections = _recipe_sections(home, args, errors, rows)
    elif tab == "specs":
        sections = _specs_sections(home, errors)
    else:
        sections = _epics_sections(home, errors)
    return S.page("recipes", "Recipes, specs & epics",
                  "Workflow definitions and the larger bodies of work they run against. "
                  "Project recipes override engine recipes of the same name.",
                  chips_=[S.chip(f"{count} recipes")],
                  actions=[S.btn("New recipe", S.thread("/recipe new "), "primary")],
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)


def _recipe_count(home: Path) -> int:
    from mini_ork import recipes_catalog

    return len(recipes_catalog.list_recipes(home))

