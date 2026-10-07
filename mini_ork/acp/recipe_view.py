"""Per-recipe rows + recipe-card markdown for the ACP recipe surface (Zed S3a).

``/recipes`` answers ``"what can I run and how good is it?"``; ``/recipe <id>``
answers ``"what does this one do and how has it done?"``. ``recipe_rows`` /
``recipe_card`` are the read-model projections; ``render_recipes`` /
``render_recipe_card`` are the markdown formatters.

Pure functions, no async, no ACP types, no module-level state — same shape as
``mini_ork.acp.fleet`` (sister module for S2) and ``mini_ork.acp.task_state``.
``recipe_rows`` returns plain dicts so the caller can shape, filter, and
order without coupling to a dataclass, and the MCP ``describe_recipe`` tool
can serialise the same projection.
"""
from __future__ import annotations

import time

from pathlib import Path
from typing import Any

from mini_ork.acp import task_state as _task_state

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # PyYAML is a soft dep; degrade like the catalog does
    yaml = None  # type: ignore[assignment]

# Cap on recent-runs rows in the card. Picked to match the kickoff's
# "last 5 runs" line; anything older stays in the aggregate numbers.
RECENT_RUNS_LIMIT = 5

# Source-filter labels the user can type on ``/recipes``.
SOURCE_LABELS = ("all", "project", "engine")


# ── YAML + first-line helpers (no raise) ──────────────────────────────────


def _safe_yaml(path: Path) -> dict[str, Any]:
    """Best-effort YAML load — returns ``{}`` on any failure.

    Mirrors ``mini_ork.recipes_catalog._safe_yaml`` so a typo in a user's
    recipe does not crash the surface.
    """
    if not path.exists() or yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _as_str_list(value: Any) -> list[str]:
    """Best-effort coercion of ``value`` to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(text)
    return out


# ── grade + track-record ──────────────────────────────────────────────────


def _grade_letter(score: int) -> str:
    """Letter grade for ``score`` (0-100). Mirrors ``recipe_eval._grade``."""
    try:
        from mini_ork.cli.recipe_eval import _grade

        return _grade(score)
    except Exception:
        # The grade is cosmetic; if the eval module is unavailable, return
        # a neutral letter rather than crashing the whole card.
        return "—" if score <= 0 else "?"


def _eval_safe(root: Path, name: str) -> dict[str, Any]:
    """``eval_recipe(root, name)`` shaped for the card, with soft failure."""
    empty: dict[str, Any] = {"score": 0, "findings": []}
    try:
        from mini_ork.cli.recipe_eval import eval_recipe
    except Exception:
        return empty
    try:
        result = eval_recipe(root, name)
    except Exception:
        return empty
    if not isinstance(result, dict):
        return empty
    score = result.get("score")
    findings = result.get("findings")
    return {
        "score": int(score) if isinstance(score, (int, float)) else 0,
        "findings": findings if isinstance(findings, list) else [],
    }


def _track_record(db: Any) -> dict[str, dict[str, Any]]:
    """Per-recipe track record from ``task_runs``.

    Aggregates runs, published, finished (terminal), average cost and average
    duration over finished runs. A recipe missing from the table is returned
    as ``{runs: 0, published: 0, finished: 0, success_pct: 0.0,
    avg_cost_usd: 0.0, avg_duration_s: 0.0}`` so the renderer can show a
    coherent "no runs yet" line instead of crashing.
    """
    empty = {
        "runs": 0,
        "published": 0,
        "finished": 0,
        "success_pct": 0.0,
        "avg_cost_usd": 0.0,
        "avg_duration_s": 0.0,
    }
    if db is None:
        return empty
    # ``db.has_table`` is the repository's seam; the real DB handle here is
    # the ``StateDB`` returned by ``mini_ork.web.deps.db_for(home)``.
    has_table = getattr(db, "has_table", None)
    if not callable(has_table) or not has_table("task_runs"):
        return empty
    # ``TERMINAL_STATUSES`` is the project's stable closed set for "finished"
    # runs (published + rolled_back + failed). Mirrors ``agent.py:99`` and
    # ``server.py:442`` — the same definition lives in three places because
    # the agent module deliberately does not import the FastAPI route graph.
    finished_clause = "status IN ('published','rolled_back','failed')"
    rows = db.rows(
        "SELECT recipe, COUNT(*) AS runs, "
        "SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published, "
        "SUM(CASE WHEN " + finished_clause + " THEN 1 ELSE 0 END) AS finished, "
        "AVG(CASE WHEN " + finished_clause + " THEN cost_usd END) AS avg_cost, "
        "AVG(CASE WHEN " + finished_clause
        + " THEN (updated_at - created_at) END) AS avg_dur "
        "FROM task_runs GROUP BY recipe"
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        recipe = row.get("recipe") or ""
        if not recipe:
            continue
        try:
            runs = int(row.get("runs") or 0)
            published = int(row.get("published") or 0)
            finished = int(row.get("finished") or 0)
        except (TypeError, ValueError):
            continue
        avg_cost = float(row.get("avg_cost") or 0.0)
        avg_dur = float(row.get("avg_dur") or 0.0)
        success_pct = (published / finished * 100.0) if finished > 0 else 0.0
        out[str(recipe)] = {
            "runs": runs,
            "published": published,
            "finished": finished,
            "success_pct": success_pct,
            "avg_cost_usd": avg_cost,
            "avg_duration_s": avg_dur,
        }
    return out


def _recent_runs(db: Any, recipe: str, *, limit: int = RECENT_RUNS_LIMIT) -> list[dict[str, Any]]:
    """The most-recent ``limit`` task_runs rows for ``recipe`` (newest-first)."""
    if db is None or not recipe:
        return []
    has_table = getattr(db, "has_table", None)
    if not callable(has_table) or not has_table("task_runs"):
        return []
    try:
        rows = db.rows(
            "SELECT id, status, cost_usd, created_at, updated_at "
            "FROM task_runs WHERE recipe = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (recipe, int(limit)),
        )
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "id": row.get("id") or "",
                "status": row.get("status") or "",
                "cost_usd": float(row.get("cost_usd") or 0.0),
                "created_at": int(row.get("created_at") or 0),
                "updated_at": int(row.get("updated_at") or 0),
            }
        )
    return out


# ── example-kickoff heading (rendered line in the card) ───────────────────


def _example_kickoff_heading(recipe_dir: Path) -> str:
    """First markdown heading of the recipe's example kickoff, or ``""``.

    Looks at ``<recipe>/example-kickoff.md`` first (some recipes flatten
    the example there), then walks ``<recipe>/examples/*/kickoff.md`` and
    picks the lexicographically first directory. Returns ``""`` when
    neither is present or the file is unreadable.
    """
    candidates: list[Path] = []
    flat = recipe_dir / "example-kickoff.md"
    if flat.is_file():
        candidates.append(flat)
    examples = recipe_dir / "examples"
    if examples.is_dir():
        try:
            for child in examples.iterdir():
                kick = child / "kickoff.md"
                if kick.is_file():
                    candidates.append(kick)
        except OSError:
            pass
    candidates.sort()
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("#"):
                # Strip leading "#" + optional space; ``#`` alone stays.
                return line.lstrip("#").strip()[:120]
    return ""


# ── recipe_card file list ─────────────────────────────────────────────────


def _recipe_files(recipe_dir: Path) -> list[Path]:
    """The files the renderer emits as ``ResourceContentBlock`` chunks.

    Mirrors the kickoff's list: the three YAML contracts, every prompts/*.md,
    every verifiers/*, and the example kickoff. Missing files are silently
    omitted so a half-baked recipe still renders cleanly.
    """
    out: list[Path] = []
    for name in (
        "workflow.yaml",
        "task_class.yaml",
        "artifact_contract.yaml",
    ):
        p = recipe_dir / name
        if p.is_file():
            out.append(p)
    prompts = recipe_dir / "prompts"
    if prompts.is_dir():
        try:
            for p in sorted(prompts.iterdir()):
                if p.is_file() and p.suffix == ".md":
                    out.append(p)
        except OSError:
            pass
    verifiers = recipe_dir / "verifiers"
    if verifiers.is_dir():
        try:
            for p in sorted(verifiers.iterdir()):
                if p.is_file():
                    out.append(p)
        except OSError:
            pass
    # Example kickoff: prefer the flat one, else the lexicographically first
    # ``examples/*/kickoff.md``. The card renderer links every match.
    flat = recipe_dir / "example-kickoff.md"
    if flat.is_file():
        out.append(flat)
    examples = recipe_dir / "examples"
    if examples.is_dir():
        try:
            for child in sorted(examples.iterdir()):
                kick = child / "kickoff.md"
                if kick.is_file():
                    out.append(kick)
        except OSError:
            pass
    return out


# ── recipe_rows ───────────────────────────────────────────────────────────


def recipe_rows(
    home: Path | None,
    *,
    source: str = "all",
    text: str = "",
) -> list[dict[str, Any]]:
    """One row per catalog entry, filtered + ordered for the ``/recipes`` table.

    Each row carries: ``id``, ``source`` (the catalog label — ``"project"``,
    ``"project (overrides engine)"``, or ``"engine"``), ``steps``
    (``node_count``), ``grade_letter`` + ``grade_score``, ``runs``,
    ``success_pct``, ``avg_cost_usd``, ``description`` (truncated to 70 chars
    by the renderer). The track-record fields default to ``0`` when the
    project's db is unreachable — the surface never blocks on the db.

    Sort: project entries first, then by ``runs`` desc, then ``id`` asc.
    Filter: ``source`` (``all|project|engine``); ``text`` is a
    case-insensitive substring match against id, description, or any
    task_class.yaml ``matches.keywords`` entry.
    """
    from mini_ork import recipes_catalog
    from mini_ork.web.db import db_for

    src = source if source in SOURCE_LABELS else "all"
    needle = text.strip().lower()

    db = None
    try:
        db = db_for(home) if home is not None else None
    except Exception:
        db = None
    track = _track_record(db)

    rows: list[dict[str, Any]] = []
    try:
        entries = recipes_catalog.list_recipes(home)
    except Exception:
        entries = []
    for entry in entries:
        rec = track.get(entry.id) or {}
        # Project entry that shadows an engine recipe: render as
        # ``"project (overrides engine)"`` so the table shows the precedence.
        source_label = entry.source
        if entry.source == "project" and entry.shadows_engine:
            source_label = "project (overrides engine)"
        # Read keywords from task_class.yaml — soft-fail means a missing or
        # broken task_class.yaml leaves keywords empty (no crash).
        tc: dict[str, Any] = _safe_yaml(entry.path / "task_class.yaml")
        matches_raw = tc.get("matches")
        matches: dict[str, Any] = matches_raw if isinstance(matches_raw, dict) else {}
        keywords = _as_str_list(matches.get("keywords"))

        if src == "project" and entry.source != "project":
            continue
        if src == "engine" and entry.source != "engine":
            continue
        if needle:
            hay = " ".join(
                [
                    entry.id,
                    entry.description or "",
                    " ".join(keywords),
                ]
            ).lower()
            if needle not in hay:
                continue

        rows.append(
            {
                "id": entry.id,
                "source": source_label,
                "steps": entry.node_count,
                "grade_letter": "—",
                "grade_score": 0,
                "runs": int(rec.get("runs") or 0),
                "success_pct": float(rec.get("success_pct") or 0.0),
                "avg_cost_usd": float(rec.get("avg_cost_usd") or 0.0),
                "description": entry.description,
                "keywords": keywords,
                "shadows_engine": bool(entry.shadows_engine),
            }
        )

    # Lazy grade pass so we only score the rows we're about to render.
    for row in rows:
        entry_path = next(
            (e.path for e in entries if e.id == row["id"]), None
        )
        if entry_path is None:
            continue
        # ``eval_recipe`` expects the PARENT of ``recipes/<id>/``, so the
        # recipe's parent is the engine root or home.
        root = entry_path.parent.parent
        ev = _eval_safe(root, row["id"])
        row["grade_letter"] = _grade_letter(ev["score"])
        row["grade_score"] = ev["score"]

    rows.sort(
        key=lambda r: (
            0 if r["source"].startswith("project") else 1,
            -int(r["runs"]),
            r["id"],
        )
    )
    return rows


# ── render_recipes ────────────────────────────────────────────────────────


def render_recipes(rows: list[dict[str, Any]], *, source: str) -> str:
    """Markdown for the ``/recipes`` table.

    ``source`` is the active filter (``all|project|engine``); it's echoed in
    the header so a ``/recipes project`` reply is visibly scoped. Shape:
            Project N · Engine M
            | recipe | source | steps | grade | runs | success | avg $ | what it does |
            ...
            Details: `/recipe <name>` · Filter: `/recipes project`, `/recipes <text>`

    Empty result → ``"No recipes match."``
    """
    del source  # filter label already baked into each row's ``source`` cell
    if not rows:
        return "No recipes match."
    project_n = sum(1 for r in rows if r["source"].startswith("project"))
    engine_n = sum(1 for r in rows if r["source"] == "engine")
    lines: list[str] = [
        f"Project {project_n} · Engine {engine_n}",
        "",
        "| recipe | source | steps | grade | runs | success | avg $ | what it does |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        desc = (r.get("description") or "")[:70]
        avg = float(r.get("avg_cost_usd") or 0.0)
        success = float(r.get("success_pct") or 0.0)
        grade_letter = r.get("grade_letter") or "—"
        grade_score = r.get("grade_score") or 0
        if grade_letter == "—":
            grade_cell = "—"
        else:
            grade_cell = f"{grade_letter} — {int(grade_score)}/100"
        lines.append(
            f"| `{r['id']}` | {r['source']} | {r['steps']} | {grade_cell} | "
            f"{r['runs']} | {success:.0f}% | ${avg:.2f} | {desc} |"
        )
    lines.append("")
    lines.append(
        "Details: `/recipe <name>` · Filter: `/recipes project`, `/recipes <text>`"
    )
    return "\n".join(lines)


# ── recipe_card ───────────────────────────────────────────────────────────


def recipe_card(home: Path | None, recipe_id: str) -> dict[str, Any] | None:
    """The full card payload for ``recipe_id`` (or ``None`` when absent).

    Combines catalog data, workflow/task_class YAML, the static eval grade,
    the DB track record, and the example-kickoff heading. ``None`` when the
    recipe id is unknown to the catalog (so the handler can render the
    "No recipe <id>" line). Every read is soft — partial / broken YAML
    degrades to empty dicts, never raises.
    """
    from mini_ork import recipes_catalog
    from mini_ork.web.db import db_for

    entry = recipes_catalog.find_recipe(recipe_id, home)
    if entry is None:
        return None

    recipe_dir = entry.path
    wf: dict[str, Any] = _safe_yaml(recipe_dir / "workflow.yaml")
    tc: dict[str, Any] = _safe_yaml(recipe_dir / "task_class.yaml")
    ac: dict[str, Any] = _safe_yaml(recipe_dir / "artifact_contract.yaml")

    matches_raw = tc.get("matches")
    matches: dict[str, Any] = matches_raw if isinstance(matches_raw, dict) else {}
    keywords = _as_str_list(matches.get("keywords"))

    raw_nodes_raw = wf.get("nodes")
    raw_nodes: list[Any] = raw_nodes_raw if isinstance(raw_nodes_raw, list) else []
    nodes: list[dict[str, Any]] = []
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        nodes.append(
            {
                "id": str(n.get("id") or n.get("name") or ""),
                "type": str(n.get("type") or ""),
                "model_lane": n.get("model_lane") or n.get("lane"),
                "verifier_ref": n.get("verifier_ref"),
                "gates": _as_str_list(n.get("gates")),
                "prompt_ref": n.get("prompt_ref"),
            }
        )

    raw_edges_raw = wf.get("edges")
    raw_edges: list[Any] = raw_edges_raw if isinstance(raw_edges_raw, list) else []
    edges: list[dict[str, Any]] = []
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        edges.append(
            {
                "from": str(e.get("from") or ""),
                "to": str(e.get("to") or ""),
                "edge_type": e.get("edge_type") or "default",
            }
        )

    # ``eval_recipe`` is called with the parent of ``recipes/<id>/``.
    root = recipe_dir.parent.parent
    eval_result = _eval_safe(root, recipe_id)

    db = None
    try:
        db = db_for(home) if home is not None else None
    except Exception:
        db = None
    rec = _track_record(db).get(recipe_id) or {
        "runs": 0,
        "published": 0,
        "finished": 0,
        "success_pct": 0.0,
        "avg_cost_usd": 0.0,
        "avg_duration_s": 0.0,
    }

    return {
        "id": entry.id,
        "source": (
            "project (overrides engine)"
            if entry.source == "project" and entry.shadows_engine
            else entry.source
        ),
        "description": entry.description,
        "task_class": entry.task_class,
        "keywords": keywords,
        "nodes": nodes,
        "edges": edges,
        "artifact_contract": ac if isinstance(ac, dict) else {},
        "grade": {
            "score": eval_result["score"],
            "letter": _grade_letter(eval_result["score"]),
            "findings": eval_result["findings"],
        },
        "track_record": rec,
        "recent_runs": _recent_runs(db, recipe_id),
        "example_kickoff_heading": _example_kickoff_heading(recipe_dir),
        "files": _recipe_files(recipe_dir),
        "path": recipe_dir,
    }


# ── render_recipe_card ────────────────────────────────────────────────────


def render_recipe_card(card: dict[str, Any]) -> tuple[str, list[Path]]:
    """Markdown + file links for one recipe card.

    Returns ``(markdown, [Path, ...])`` — the paths are the files the agent
    emits as ``ResourceContentBlock`` chunks (file links in Zed).
    """
    files: list[Path] = [Path(p) for p in (card.get("files") or []) if isinstance(p, Path)]
    nodes: list[dict[str, Any]] = card.get("nodes") or []
    edges: list[dict[str, Any]] = card.get("edges") or []
    findings: list[dict[str, Any]] = card.get("grade", {}).get("findings") or []
    track: dict[str, Any] = card.get("track_record") or {}
    recent: list[dict[str, Any]] = card.get("recent_runs") or []
    keywords: list[str] = (card.get("keywords") or [])[:8]
    ac: dict[str, Any] = card.get("artifact_contract") or {}

    lines: list[str] = []
    title = f"**{card.get('id', '?')}** — {card.get('source', '?')}"
    lines.append(title)
    lines.append("")
    if card.get("description"):
        lines.append(card["description"])
        lines.append("")

    if keywords:
        lines.append("Chosen for requests like:")
        lines.append(", ".join(f"`{kw}`" for kw in keywords))
        lines.append("")

    if nodes:
        lines.append("| step | type | model (role → lane) | checks |")
        lines.append("| --- | --- | --- | --- |")
        # Per-node lane lookup is deferred to the renderer call to keep this
        # module import-light. ``command.handle_recipe`` loads ``load_lanes``
        # and passes the map in; here we accept it via the env-less seam.
        for n in nodes:
            lane = n.get("model_lane")
            lane_cell = (
                f"`{lane}` → `{_lane_for(lane)}`"
                if lane
                else "—"
            )
            checks = []
            vr = n.get("verifier_ref")
            if vr:
                checks.append(Path(vr).name if isinstance(vr, str) else str(vr))
            for g in n.get("gates") or []:
                checks.append(f"gate:{g}")
            lines.append(
                f"| `{n.get('id', '?')}` | {n.get('type', '?')} | {lane_cell} | {', '.join(checks) or '—'} |"
            )
        lines.append("")

    if edges:
        lines.append("Flow: " + _flow_chain(nodes, edges))
        lines.append("")

    # Artifact contract: render whatever is present (per the kickoff).
    contract_bits: list[str] = []
    expected = ac.get("expected_artifact")
    if expected:
        contract_bits.append(f"Produces: {_ARTIFACT_WORDS.get(str(expected), f'`{expected}`')}")
    verifiers = ac.get("success_verifiers")
    if isinstance(verifiers, list) and verifiers:
        joined = ", ".join(f"`{v}`" for v in verifiers if isinstance(v, str))
        if joined:
            contract_bits.append(f"how success is verified: {joined}")
    fp = ac.get("failure_policy")
    if fp:
        contract_bits.append(f"failure policy: `{fp}`")
    rp = ac.get("rollback_policy")
    if rp:
        contract_bits.append(f"rollback policy: `{rp}`")
    if contract_bits:
        lines.append("\n".join(contract_bits))
        lines.append("")

    grade = card.get("grade") or {}
    score = grade.get("score", 0)
    letter = grade.get("letter", "—")
    if letter == "—":
        lines.append("Grade: —")
    else:
        lines.append(f"Grade: {letter} — {score}/100")
    if findings:
        for f in findings:
            sev = f.get("sev") or "info"
            msg = f.get("msg") or ""
            fix = f.get("fix") or ""
            lines.append(f"- [{sev}] {msg}" + (f" — Fix: {fix}" if fix else ""))
        lines.append("")

    runs = int(track.get("runs") or 0)
    success_pct = float(track.get("success_pct") or 0.0)
    avg_cost = float(track.get("avg_cost_usd") or 0.0)
    avg_dur = float(track.get("avg_duration_s") or 0.0)
    if runs:
        lines.append(
            f"Track record: {runs} runs · {success_pct:.0f}% success · "
            f"avg ${avg_cost:.2f} · avg {int(avg_dur // 60)}m{int(avg_dur % 60):02d}s"
        )
        lines.append("")
    else:
        lines.append("Track record: no runs yet.")
        lines.append("")

    if recent:
        lines.append("Recent runs:")
        lines.append("|  | run | age | cost |")
        lines.append("| --- | --- | --- | --- |")
        now_epoch = int(time.time())
        for r in recent:
            age = _format_age(r.get("created_at"), now_epoch)
            mark = _task_state.run_mark(r.get("status"), None)
            cost = float(r.get("cost_usd") or 0.0)
            lines.append(
                f"| {mark} | `{r.get('id', '?')}` | {age} | ${cost:.2f} |"
            )
        lines.append("")

    example_heading = card.get("example_kickoff_heading") or ""
    if example_heading:
        lines.append(f"Example: {example_heading}")

    lines.append(f"Run it: `/run <task>` with Recipe = {card.get('id', '?')}, or ask the orchestrator.")

    markdown = "\n".join(line for line in lines if line is not None)
    return markdown, files


# ── internal: per-node lane lookup ────────────────────────────────────────


def _lane_for(role: Any) -> str:
    """Look up the lane for ``role`` via the recipe's ``load_lanes``.

    Module-level cache so the renderer doesn't re-read
    ``config/agents.yaml`` for every node. ``None`` / unknown role → ``"—"``
    so the cell stays a coherent "I don't know" rather than ``None``.
    """
    if not isinstance(role, str) or not role:
        return "—"
    cache = _LANE_CACHE.get("cache")
    if cache is None:
        cache = {}
        _LANE_CACHE["cache"] = cache
    if role in cache:
        return cache[role]
    try:
        from mini_ork.web.recipes import load_lanes

        lanes = load_lanes(None) or {}
    except Exception:
        lanes = {}
    out = str(lanes.get(role) or "—")
    cache[role] = out
    return out


_LANE_CACHE: dict[str, dict[str, str]] = {}

# artifact_contract.yaml `expected_artifact` (a schema enum) in plain words.
_ARTIFACT_WORDS = {
    "patch": "a patch — changes to files in the repository",
    "prose": "prose — a written report or document",
    "plan": "a structured plan",
    "image": "an image",
    "data": "a data file",
    "composite": "several kinds of output",
}


def _flow_chain(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """The workflow as one chain: steps grouped by depth (longest path from a
    start), same-depth steps joined with commas — ``planner → editor → check_a,
    check_b → publisher``. Cycles or unknown steps fall back to edge pairs."""
    names = [str(n.get("name") or n.get("id")) for n in nodes if n.get("name") or n.get("id")]
    for e in edges:  # steps that appear only in edges still belong to the flow
        for end in (e.get("from"), e.get("to")):
            if end and str(end) not in names:
                names.append(str(end))
    preds: dict[str, list[str]] = {n: [] for n in names}
    for e in edges:
        f, t = e.get("from"), e.get("to")
        if f in preds and t in preds:
            preds[t].append(f)
    depth: dict[str, int] = {}

    def _depth(n: str, seen: tuple = ()) -> int:
        if n in depth:
            return depth[n]
        if n in seen:
            raise ValueError("cycle")
        depth[n] = 1 + max((_depth(p, seen + (n,)) for p in preds[n]), default=-1)
        return depth[n]

    try:
        for n in names:
            _depth(n)
    except ValueError:
        return " · ".join(f"{e.get('from')} → {e.get('to')}" for e in edges)
    levels: dict[int, list[str]] = {}
    for n in names:  # workflow order within a level
        levels.setdefault(depth[n], []).append(n)
    return " → ".join(", ".join(levels[d]) for d in sorted(levels))


def _format_age(created_at: Any, now_epoch: int) -> str:
    """Compact ``"3h"`` / ``"5d"`` / ``"now"`` age label."""
    try:
        ts = int(created_at)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    # No ``now`` is wired in by default — the renderer call may pass it via
    # the ``recent_runs`` row's own context. When 0, fall back to a coarse
    # "—" rather than guessing.
    if now_epoch <= 0:
        return "—"
    delta = max(0, int(now_epoch) - ts)
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    return f"{delta // 86_400}d"


# ── MCP-friendly card projection ──────────────────────────────────────────


def describe_recipe_payload(
    home: Path | None, recipe_id: str
) -> dict[str, Any] | None:
    """Card payload with paths as strings (MCP ``describe_recipe`` shape).

    Mirrors :func:`recipe_card` but resolves the ``Path``-typed fields to
    strings so the MCP tool can serialise the result. ``None`` when the
    recipe id is unknown.
    """
    card = recipe_card(home, recipe_id)
    if card is None:
        return None
    out = dict(card)
    out["files"] = [str(p) for p in (card.get("files") or [])]
    out["path"] = str(card.get("path") or "")
    return out


__all__ = [
    "recipe_rows",
    "render_recipes",
    "recipe_card",
    "render_recipe_card",
    "describe_recipe_payload",
]