"""The run flow map — a run as Ticket → Design → Build → Integrate → Review →
optional Merge, for the IDE's Graph view.

:func:`build_flow` returns the plain-JSON model the IDE (Rust) draws: the
ticket, the stage chips, and the per-stage detail — design nodes with their
revise loops, build packages with Implementer lanes and critics, the integrate
check tiers, the review hub with its rubric / level / model / axis spokes, the
merge checklist (only when code merged), the rollback record, and the node-event
timeline. Nothing here starts a model or writes to the run dir.

Read-only and **fail-soft**: every section reads the run's own artefacts through
the existing tolerant readers (``run._yaml`` / ``run._tail``,
``node_changes._read_json`` / ``_verifier_items``, ``outcome._review`` /
``_run_verdict``) and degrades to an empty or null part on any error — one
broken artefact must never raise out of :func:`build_flow`, and a broken section
must cost only that section.

The mapping keys off the workflow node **type** (planner, researcher,
implementer, verifier, reviewer, eval, …), never off the recipe name.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import outcome as O
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.node_changes import (
    _read_json,
    _verifier_items,
    _verifier_stem,
)
from mini_ork.ide_pages.run import Node, Run, _epoch, _tail, _yaml
from mini_ork.ide_pages.run_story import _ordered

# The six stages, in draw order. ``merge`` is present only when code merged.
_STAGE_KEYS = ("intake", "design", "build", "integrate", "review", "merge")
_STAGE_LABELS = {"intake": "Intake", "design": "Design", "build": "Build",
                 "integrate": "Integrate", "review": "Review", "merge": "Merge"}

_VERIFIER_TYPES = frozenset({"verifier", "test", "typecheck", "static_check"})
_PLANNER_TYPES = frozenset({"planner", "decomposer"})
_EVAL_TYPES = frozenset({"eval", "judge"})

# The three edge colours the IDE draws.
_LEGEND = [{"key": "in_progress", "label": "in progress"},
           {"key": "sent_back", "label": "sent back"},
           {"key": "approved", "label": "approved"}]

# Stage / step state ranking, worst first (``sent_back`` and ``failed`` tie).
_RANK = {"sent_back": 0, "failed": 0, "in_progress": 1, "pending": 2, "skipped": 2,
         "approved": 3}

# The publisher's commit line (``publisher.py:166``): ``[publish] committed N
# file(s): <sha>``. The sha is what the merge bar shows.
_PUBLISH_RE = re.compile(r"\[publish\]\s+committed\s+\d+\s+file\(s\):\s*([0-9a-fA-F]{7,40})")
# A revise round's section header names its sender: ``## <node> (<type>)``.
_REVISE_SECTION_RE = re.compile(r"^##\s+(\S+)\s+\((\w+)\)", re.MULTILINE)

_APPROVE_VERDICTS = frozenset({"pass", "approve", "approved"})
_NA_VALUES = frozenset({"", "n/a", "na", "none", "unknown"})


# ── small read-only helpers ─────────────────────────────────────────────────

def _guard(fn, default):
    """Call ``fn``; on any exception return ``default`` (fail-soft section)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — one broken section must not blank the flow
        return default


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _humanise(node_id: Any) -> str:
    """``code_impact_lens`` → ``Code impact lens`` (first letter up, rest kept)."""
    text = str(node_id or "").replace("_", " ").replace("-", " ").strip()
    return (text[:1].upper() + text[1:]) if text else str(node_id or "")


def _worst(states: list[str]) -> str:
    """The worst of a stage's step states; an empty list is ``pending``."""
    present = [s for s in states if s]
    if not present:
        return "pending"
    return min(present, key=lambda s: _RANK.get(s, 2))


def _verifier_pass(vjson: Any) -> bool | None:
    """A verifier JSON's verdict: ``pass`` when stated, else all ``checks[]`` pass."""
    if not isinstance(vjson, dict):
        return None
    if str(vjson.get("status") or "") == "unverified" and vjson.get("suite_green") is True:
        # An abstention over a green suite (e.g. a build-only command the replay
        # cannot judge) passed what it could measure: not a failure.
        return True
    if "pass" in vjson:
        return bool(vjson.get("pass"))
    checks = vjson.get("checks")
    if isinstance(checks, list):
        rows = [c for c in checks if isinstance(c, dict)]
        if rows:
            return all(bool(c.get("pass")) for c in rows)
    return None


def _node_passed(run: Run, node: Node) -> bool:
    """Positive evidence a node passed — never ``Node.state == "done"`` alone.

    A verifier's ``verifier_<stem>.json`` verdict wins; then the node's own
    ``review-<id>.json`` verdict; a node with neither is "passed" only when it
    finished (``done``): an implementer / planner / lens has no verdict file.
    """
    for candidate in (_verifier_stem(node), node.id):
        verdict = _verifier_pass(_read_json(run.run_dir / f"verifier_{candidate}.json"))
        if verdict is not None:
            return verdict
    review = _read_json(run.run_dir / f"review-{node.id}.json")
    if isinstance(review, dict) and review.get("verdict"):
        return str(review["verdict"]).strip().lower() in _APPROVE_VERDICTS
    return str(node.state or "").strip().lower() == "done"


def _step_state(node: Node, passed: bool, sent_back_ids: set[str],
                verdict: str = "") -> str:
    """Map a node's lifecycle + verdict to the flow's step vocabulary."""
    state = str(node.state or "pending").strip().lower()
    if state == "running":
        return "in_progress"
    if state == "done":
        # "approved" needs the "and passed" half — a done node whose verifier
        # verdict is negative was sent back, not approved.
        return "approved" if passed else "sent_back"
    if state == "failed":
        if node.id in sent_back_ids:
            return "sent_back"
        # A reviewer that asked for a revision SENT the work back; it did not
        # crash. Reviewers are never retries targets, so read their verdict.
        if str(node.type or "") in ("reviewer", "eval", "judge"):
            if str(verdict or "").strip().lower() in ("needs_revision", "revise", "request_changes", "fail", "failed"):
                return "sent_back"
        return "failed"
    if state == "skipped":
        return "skipped"
    return "pending"


def _review_verdict(run: Run, node: Node) -> str:
    """The node's recorded review verdict (``review-<id>.json``), or ``""``."""
    review = _read_json(run.run_dir / f"review-{node.id}.json")
    return str(review.get("verdict") or "") if isinstance(review, dict) else ""


def _step(run: Run, node: Node, passed: bool, sent_back_ids: set[str], *,
          label: str | None = None, sub: str = "", score: str = "",
          do: dict[str, Any] | None = None) -> dict[str, Any]:
    """One node as a flow ``step``."""
    return {
        "id": node.id,
        "label": label if label is not None else _humanise(node.id),
        "sub": sub,
        "lane": node.family or node.role_lane,
        "type": node.type,
        "state": _step_state(node, passed, sent_back_ids, _review_verdict(run, node)),
        "score": score,
        "do": do if do is not None else S.page_link("run", "graph", run=run.id, node=node.id),
    }


# ── workflow + edges ────────────────────────────────────────────────────────

def _workflow(run: Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(nodes, edges)`` from the recipe's ``workflow.yaml``; ``([], [])`` when none."""
    if run.recipe_dir is None:
        return [], []
    wf = _yaml(run.recipe_dir / "workflow.yaml")
    nodes = [n for n in (wf.get("nodes") or []) if isinstance(n, dict) and n.get("name")]
    edges = [e for e in (wf.get("edges") or []) if isinstance(e, dict)]
    return nodes, edges


def _edges_of(edges: list[dict[str, Any]], kind: str) -> list[tuple[str, str]]:
    return [(str(e.get("from") or ""), str(e.get("to") or ""))
            for e in edges
            if str(e.get("edge_type") or "") == kind and e.get("from") and e.get("to")]


def _sent_back_ids(edges: list[dict[str, Any]]) -> set[str]:
    """Nodes a ``retries`` edge sends back (the revise-loop targets)."""
    return {dst for _src, dst in _edges_of(edges, "retries")}


# ── revise rounds ───────────────────────────────────────────────────────────

def _revise_rounds(run: Run) -> list[dict[str, Any]]:
    """Parse ``revise/round-<n>.md`` → ``[{"round", "sources"}]``, sorted, fail-soft."""
    out: list[dict[str, Any]] = []
    revise_dir = run.run_dir / "revise"
    try:
        files = sorted(revise_dir.glob("round-*.md"))
    except OSError:
        return out
    for path in files:
        m = re.match(r"round-(\d+)\.md$", path.name)
        if m is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources = [mm.group(1) for mm in _REVISE_SECTION_RE.finditer(text)]
        out.append({"round": int(m.group(1)), "sources": sources})
    out.sort(key=lambda r: r["round"])
    return out


def _loops(edges: list[dict[str, Any]], rounds: list[dict[str, Any]], want_ids: set[str],
           passed: dict[str, bool]) -> list[dict[str, Any]]:
    """``retries`` loops whose target is in ``want_ids`` — only when they fired.

    ``rounds`` counts the ``revise/round-*.md`` files naming the source;
    ``approved_round`` is the last round the source appears in when it passed,
    else ``None``.
    """
    out: list[dict[str, Any]] = []
    for src, dst in _edges_of(edges, "retries"):
        if dst not in want_ids:
            continue
        seen = [r["round"] for r in rounds if src in r["sources"]]
        if not seen:
            continue
        approved = bool(passed.get(src))
        out.append({
            "from": src, "to": dst, "rounds": len(seen),
            "approved_round": max(seen) if approved else None,
            "state": "approved" if approved else "sent_back",
        })
    return out


# ── design ──────────────────────────────────────────────────────────────────

def _plan_score(run: Run, ordered: list[Node], edges: list[dict[str, Any]]) -> str:
    """The plan critic's score (``9/10``) when an eval node graded the plan, else ''.

    The score counts only when the eval's workflow edge comes from a planner
    (``depends_on`` / ``verifies``): that eval grades the plan itself. In
    code-fix the eval runs after the reviewer and grades the whole run, so it
    is not the plan's score.
    """
    planners = {n.id for n in ordered if str(n.type or "") in _PLANNER_TYPES}
    graded_by = {dst for src, dst in _edges_of(edges, "depends_on") if src in planners}
    graded_by |= {dst for src, dst in _edges_of(edges, "verifies") if src in planners}
    for node in ordered:
        if str(node.type or "") not in _EVAL_TYPES or node.id not in graded_by:
            continue
        data = (_read_json(run.run_dir / f"review-{node.id}.json")
                or _read_json(run.run_dir / f"eval-{node.id}.json"))
        if not isinstance(data, dict):
            continue
        score = data.get("score")
        if score is None:
            continue
        num = _num(score)
        if num is not None:
            return f"{num:g}/10"
    return ""


def _design(run: Run, ordered: list[Node], has_implementer: bool,
            passed: dict[str, bool], sent_back_ids: set[str],
            edges: list[dict[str, Any]], rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Design: the planner, plus the researchers as readers when a build follows."""
    steps: list[dict[str, Any]] = []
    for node in ordered:
        ntype = str(node.type or "")
        if ntype in _PLANNER_TYPES:
            steps.append(_step(run, node, passed.get(node.id, False), sent_back_ids,
                               label="Plan", sub="writes the plan",
                               score=_plan_score(run, ordered, edges)))
        elif ntype == "researcher" and has_implementer:
            steps.append(_step(run, node, passed.get(node.id, False), sent_back_ids,
                               sub=node.family or node.role_lane))
    ids = {n.id for n in ordered if str(n.type or "") in _PLANNER_TYPES}
    # Researchers are design nodes only when a build follows; with no
    # implementer they are the build lanes, so their revise loops belong to
    # build (attaching them here too would emit one loop in both stages).
    if has_implementer:
        ids |= {n.id for n in ordered if str(n.type or "") == "researcher"}
    return {"nodes": steps, "revise": _loops(edges, rounds, ids, passed)}


# ── build ───────────────────────────────────────────────────────────────────

def _critic_map(edges: list[dict[str, Any]], implementer_ids: set[str]) -> dict[str, str]:
    """``implementer id → critiquing verifier id`` for a fan-out's own verifiers.

    A ``verifies`` edge runs implementer → verifier, so the implementers that
    point at a verifier are its sources. A verifier is a critic for an
    implementer when that implementer is its ONLY source (it ``verifies`` just
    that one implementer); an implementer's critic is kept only when exactly
    one such verifier exists — otherwise the mapping is ambiguous and null.
    """
    sources: dict[str, set[str]] = {}
    for src, dst in _edges_of(edges, "verifies"):
        if src in implementer_ids:
            sources.setdefault(dst, set()).add(src)
    candidates: dict[str, list[str]] = {}
    for verifier, imples in sources.items():
        if len(imples) == 1:
            candidates.setdefault(next(iter(imples)), []).append(verifier)
    return {impl: verifiers[0] for impl, verifiers in candidates.items()
            if len(verifiers) == 1}


def _build(run: Run, ordered: list[Node], has_implementer: bool,
           passed: dict[str, bool], sent_back_ids: set[str],
           edges: list[dict[str, Any]], rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Build: one package per implementer (or the researchers when there is none)."""
    by_id = {n.id: n for n in ordered}
    implementers = [n for n in ordered if str(n.type or "") == "implementer"]
    build_ids = {n.id for n in implementers}

    def lane(node: Node, critic_id: str | None) -> dict[str, Any]:
        critic = None
        if critic_id and critic_id in by_id:
            critic = _step(run, by_id[critic_id], passed.get(critic_id, False), sent_back_ids)
        return {"role": "Implementer" if has_implementer else "Lens",
                "node": _step(run, node, passed.get(node.id, False), sent_back_ids),
                "critic": critic}

    packages: list[dict[str, Any]] = []
    if implementers:
        # A critic is a fan-out notion: only when 2+ implementers can a
        # verifier "verify ONLY that implementer".
        critics = (_critic_map(edges, {n.id for n in implementers})
                   if len(implementers) >= 2 else {})
        if len(implementers) == 1:
            packages = [{"id": "package-1", "label": "",
                         "lanes": [lane(implementers[0], critics.get(implementers[0].id))]}]
        else:
            for i, node in enumerate(implementers, 1):
                packages.append({"id": f"package-{i}", "label": f"PACKAGE {i}",
                                 "lanes": [lane(node, critics.get(node.id))]})
    else:
        researchers = [n for n in ordered if str(n.type or "") == "researcher"]
        build_ids = {n.id for n in researchers}
        packages = [{"id": "package-1", "label": "",
                     "lanes": [lane(n, None) for n in researchers]}]
    return {"packages": packages, "revise": _loops(edges, rounds, build_ids, passed)}


# ── integrate ───────────────────────────────────────────────────────────────

def _tier_label(node: Node) -> str:
    """``static_check_verifier`` → ``static checks``, ``test`` → ``tests``, …"""
    key = _verifier_stem(node).lower().replace("-", "_")
    if "typecheck" in key or key.endswith("types"):
        return "types"
    if "static" in key or "lint" in key:
        return "static checks"
    if key in ("test", "tests") or key.endswith("_test") or key.endswith("test") or "pytest" in key:
        return "tests"
    return key.replace("_", " ").strip() or "checks"


def _integrate(run: Run, ordered: list[Node], passed: dict[str, bool],
               sent_back_ids: set[str]) -> dict[str, Any]:
    """Integrate: one tier per verifier, plus the synthetic roll-up node."""
    verifiers = [n for n in ordered if str(n.type or "") in _VERIFIER_TYPES]
    tiers: list[dict[str, Any]] = []
    states: list[str] = []
    for node in verifiers:
        try:
            _title, items = _verifier_items(run.run_dir, node)
        except Exception:  # noqa: BLE001 — one broken verifier costs its tier
            items = []
        scored = [i for i in items if isinstance(i, dict) and "passed" in i]
        state = _step_state(node, passed.get(node.id, False), sent_back_ids)
        states.append(state)
        n_pass, n_total = sum(1 for i in scored if i.get("passed")), len(scored)
        if passed.get(node.id) and n_pass == 0:
            # The verifier passed (e.g. a green abstention) but its rows read as
            # unverified: show it as passed, not 0/N.
            n_pass = n_total = max(1, n_total)
        tiers.append({"label": _tier_label(node), "state": state,
                      "passed": n_pass, "total": n_total})
    node_step = None
    if verifiers:
        node_step = {"id": "integrate", "label": "Integrate", "sub": "runs every check tier",
                     "lane": "", "type": "integrate", "state": _worst(states),
                     "score": "", "do": None}
    return {"node": node_step, "tiers": tiers}


# ── review ──────────────────────────────────────────────────────────────────

def _rubric_spokes(run: Run) -> list[dict[str, Any]]:
    """One spoke per ``rubric.json`` item: PASS→approved, FAIL→sent_back, SKIP→skipped."""
    rubric = _read_json(run.run_dir / "rubric.json")
    if not isinstance(rubric, dict):
        return []
    items = rubric.get("items")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label") or it.get("name") or "")
        verdict = str(it.get("verdict") or "").strip().upper()
        state = {"PASS": "approved", "FAIL": "sent_back", "SKIP": "skipped"}.get(verdict, "pending")
        out.append({"label": label, "sub": verdict.lower(), "state": state, "kind": "rubric"})
    return out


def _rubric_score(run: Run) -> str:
    rubric = _read_json(run.run_dir / "rubric.json")
    if not isinstance(rubric, dict):
        return ""
    num = _num(rubric.get("score"))
    return f"{num:g}/8" if num is not None else ""


def _level_spokes(run: Run) -> list[dict[str, Any]]:
    """One spoke per verification level (skip ``n/a``): PROVEN / REFUTED / UNVERIFIED."""
    rv = O._run_verdict(run)
    levels = rv.get("levels") if isinstance(rv, dict) else None
    if not isinstance(levels, dict):
        return []
    out: list[dict[str, Any]] = []
    for name in O.LEVELS:
        value = str(levels.get(name) or "").strip()
        if value.lower() in _NA_VALUES:
            continue
        up = value.upper()
        state = "approved" if up == "PROVEN" else ("sent_back" if up == "REFUTED" else "pending")
        out.append({"label": name, "sub": up, "state": state, "kind": "level"})
    return out


def _second_model_spoke(run: Run, ordered: list[Node], passed: dict[str, bool],
                        sent_back_ids: set[str]) -> dict[str, Any] | None:
    """A "Second model" spoke when an eval/judge node or a second reviewer family ran."""
    for node in ordered:
        if str(node.type or "") in _EVAL_TYPES:
            state = _step_state(node, passed.get(node.id, False), sent_back_ids)
            return {"label": "Second model", "sub": node.family or node.role_lane,
                    "state": state, "kind": "model"}
    reviewers = [n for n in ordered if str(n.type or "") == "reviewer"]
    if len(reviewers) > 1:
        first = reviewers[0].family or reviewers[0].role_lane
        for node in reviewers[1:]:
            fam = node.family or node.role_lane
            if fam and fam != first:
                state = _step_state(node, passed.get(node.id, False), sent_back_ids)
                return {"label": "Second model", "sub": fam, "state": state, "kind": "model"}
    return None


def _axis_spokes(run: Run, ordered: list[Node]) -> list[dict[str, Any]]:
    """Eval axes ≥0.7 approved, else sent_back (kind ``axis``)."""
    out: list[dict[str, Any]] = []
    for node in ordered:
        if str(node.type or "") not in _EVAL_TYPES:
            continue
        data = (_read_json(run.run_dir / f"review-{node.id}.json")
                or _read_json(run.run_dir / f"eval-{node.id}.json"))
        axes = data.get("axes") if isinstance(data, dict) else None
        if not isinstance(axes, dict):
            continue
        for key, value in axes.items():
            num = _num(value)
            if num is None:
                continue
            out.append({"label": str(key), "sub": f"{num:g}",
                        "state": "approved" if num >= 0.7 else "sent_back", "kind": "axis"})
    return out


def _fix(rounds: list[dict[str, Any]], reviewers: list[Node], hub: dict[str, Any] | None
         ) -> dict[str, Any]:
    """The "Fix — then review again" spoke: revise rounds naming a reviewer."""
    ids = {n.id for n in reviewers}
    fired = [r for r in rounds if any(s in ids for s in r["sources"])]
    if not fired:
        return {"rounds": 0, "state": "none"}
    approved = bool(hub) and hub.get("state") == "approved"
    return {"rounds": len(fired), "state": "approved" if approved else "sent_back"}


def _review(run: Run, ordered: list[Node], passed: dict[str, bool], sent_back_ids: set[str],
            rounds: list[dict[str, Any]]) -> dict[str, Any]:
    reviewers = [n for n in ordered if str(n.type or "") == "reviewer"]
    hub_node = reviewers[0] if reviewers else None
    review = O._review(run)
    verdict = str((review or {}).get("verdict") or "") if isinstance(review, dict) else ""
    spokes = _rubric_spokes(run) + _level_spokes(run)
    model = _second_model_spoke(run, ordered, passed, sent_back_ids)
    if model is not None:
        spokes.append(model)
    spokes += _axis_spokes(run, ordered)
    hub = None
    if hub_node is not None:
        hub = _step(run, hub_node, passed.get(hub_node.id, False), sent_back_ids,
                    label="Review", sub=verdict)
    return {"hub": hub, "verdict": verdict, "score": _rubric_score(run),
            "spokes": spokes, "fix": _fix(rounds, reviewers, hub)}


# ── merge / rollback / timeline ─────────────────────────────────────────────

def _publish_sha(run: Run) -> str | None:
    """The publisher's committed sha.

    Looks in ``execute.log``, then any ``recover-*.log`` / ``repair-*.log`` in
    the run dir (a revived run publishes from there), then the run's target
    repo: the publisher's commit message carries ``[run <id>]``.
    Returns ``None`` when no commit is found.
    """
    logs = [run.run_dir / "execute.log"]
    for pattern in ("recover-*.log", "repair-*.log"):
        logs += sorted(run.run_dir.glob(pattern))
    for log in logs:
        for line in _tail(log, 2000):
            m = _PUBLISH_RE.search(line)
            if m:
                return m.group(1)
    return _target_repo_commit(run)


def _target_repo_commit(run: Run) -> str | None:
    """``git log --grep '[run <id>]'`` in the run's target repo since it started."""
    import subprocess  # noqa: PLC0415

    profile = _read_json(run.run_dir / "run_profile.json")
    if not isinstance(profile, dict):
        return None
    roots = profile.get("roots") if isinstance(profile.get("roots"), dict) else {}
    repo = str(profile.get("target_repo") or roots.get("target") or "")
    if not repo or not (Path(repo) / ".git").exists():
        return None
    since = run.row.get("created_at") if hasattr(run, "row") else None
    args = ["git", "-C", repo, "log", "--all", "-1", "--format=%h", "--fixed-strings",
            "--grep", f"[run {run.id}]"]
    if since:
        args.insert(5, f"--since=@{int(since) - 60}" if str(since).isdigit() else f"--since={since}")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001 — no repo / slow git → no evidence
        return None
    return out or None


def _workspace_waiting(run: Run) -> bool:
    """``True`` when a worktree still holds real work for the human to merge.

    Mirrors ``task_state`` rule 3: a live worktree with commits ahead of its
    base or uncommitted edits. A kept *empty* worktree (a rolled-back run with
    zero commits) is not a merge waiting to happen.
    """
    ws = getattr(run, "workspace", None)
    if ws is None:
        return False
    try:
        from mini_ork import workspaces

        status = workspaces.status(ws)
    except Exception:  # noqa: BLE001 — no workspace snapshot: treat as absent
        return False
    if not (isinstance(status, dict) and status.get("exists")):
        return False
    if int(status.get("commits_ahead") or 0) > 0:
        return True
    return bool(status.get("uncommitted"))


def _merge(run: Run, hub: dict[str, Any] | None, integrate_state: str) -> dict[str, Any] | None:
    """The merge bar — present only when code merged or a published worktree waits.

    Code merged: the publisher's ``[publish] committed … <sha>`` line. A
    worktree still waits only for a ``published`` run whose branch has real
    work (``task_state`` rule 3) — a rolled-back / failed / running run with a
    kept empty worktree never grows a merge stage.
    """
    sha = _publish_sha(run)
    published = sha is not None
    waiting = str(run.card.get("status") or "").strip().lower() == "published" \
        and _workspace_waiting(run)
    if not (published or waiting):
        return None
    review_passed = bool(hub) and hub.get("state") == "approved"
    checks = [{"label": "Review passed", "ok": review_passed},
              {"label": "Checks green", "ok": integrate_state == "approved"}]
    if sha:
        checks.append({"label": f"Committed {sha[:7]}", "ok": True})
    # Human only when a person must make the merge: a worktree is waiting.
    # An in-place publish (the run committed its own change) has no human step.
    human: dict[str, Any] | None = None
    if waiting:
        human = {"label": "Human", "sub": "makes the final merge", "state": "in_progress"}
    return {"state": "approved" if published else "in_progress",
            "commit": sha or "", "checks": checks, "human": human}


def _rollback(run: Run) -> dict[str, Any] | None:
    rb = _read_json(run.run_dir / "rolled-back.json")
    if not isinstance(rb, dict):
        return None
    paths = rb.get("paths")
    n = len([p for p in paths if isinstance(p, str) and p]) if isinstance(paths, list) else 0
    return {"state": "done", "paths": n}


def _timeline(run: Run) -> list[dict[str, Any]]:
    """``node_start`` / ``node_end`` rows for the run, in order, capped at 400.

    The bare-``Run`` tests have no DB, so the whole read is guarded.
    """
    try:
        from mini_ork.web.db import db_for

        db = db_for(run.home)
        if not db.has_table("run_events"):
            return []
        rows = db.rows(
            "SELECT event_type, payload_json, created_at FROM run_events "
            "WHERE run_id = ? AND event_type IN ('node_start','node_end') "
            "ORDER BY created_at ASC, rowid ASC LIMIT 400", (run.id,))
    except Exception:  # noqa: BLE001 — no DB / no table: an empty timeline
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        event = "start" if str(row.get("event_type") or "") == "node_start" else "end"
        if event == "start":
            state = "in_progress"
        else:
            fin = str((payload or {}).get("finish_reason") or "").lower()
            state = {"done": "approved", "fail": "sent_back", "failed": "sent_back",
                     "skipped": "skipped"}.get(fin, "in_progress")
        out.append({"t": _epoch(row.get("created_at")), "node": str((payload or {}).get("node_id") or ""),
                    "event": event, "state": state})
    return out


# ── stages ──────────────────────────────────────────────────────────────────

def _stages(states: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = 0
    for key in _STAGE_KEYS:
        if key not in states:
            continue
        n += 1
        out.append({"key": key, "label": _STAGE_LABELS[key], "n": n, "state": states[key]})
    return out


# ── entry point ─────────────────────────────────────────────────────────────

def build_flow(run: Run) -> dict[str, Any]:
    """The run's flow map — read-only, fail-soft, under 300 ms on a 9-node run.

    ``run`` is a pre-loaded :class:`mini_ork.ide_pages.run.Run`; nothing here
    re-loads it or writes to disk.
    """
    ordered = _ordered(run)
    edges: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    try:
        _nodes, edges = _workflow(run)
        rounds = _revise_rounds(run)
    except Exception:  # noqa: BLE001 — no workflow / revise dir: empty parts
        edges, rounds = [], []
    passed = {n.id: _guard(lambda n=n: _node_passed(run, n), False) for n in ordered}
    sent_back_ids = _sent_back_ids(edges)
    has_implementer = any(str(n.type or "") == "implementer" for n in ordered)

    design = _guard(lambda: _design(run, ordered, has_implementer, passed, sent_back_ids,
                                    edges, rounds), {"nodes": [], "revise": []})
    build = _guard(lambda: _build(run, ordered, has_implementer, passed, sent_back_ids,
                                  edges, rounds), {"packages": [], "revise": []})
    integrate = _guard(lambda: _integrate(run, ordered, passed, sent_back_ids),
                       {"node": None, "tiers": []})
    review = _guard(lambda: _review(run, ordered, passed, sent_back_ids, rounds),
                    {"hub": None, "verdict": "", "score": "", "spokes": [],
                     "fix": {"rounds": 0, "state": "none"}})
    rollback = _guard(lambda: _rollback(run), None)
    integrate_state = str((integrate.get("node") or {}).get("state") or "pending")
    merge = _guard(lambda: _merge(run, review.get("hub"), integrate_state), None)
    # A revived run that published after an earlier rollback landed its code:
    # the stale rolled-back.json no longer describes the run's end.
    if merge is not None and merge.get("commit"):
        rollback = None

    states = {
        "intake": "done",
        "design": _worst([s.get("state", "") for s in design.get("nodes", [])]),
        "build": _worst([lane.get("node", {}).get("state", "")
                         for pkg in build.get("packages", []) for lane in pkg.get("lanes", [])]),
        "integrate": integrate_state if integrate.get("node") else "pending",
        # The stage follows the review HUB (the reviewer's verdict); advisory
        # spokes (rubric items, levels) keep their own colours on the map.
        "review": (str((review.get("hub") or {}).get("state") or "")
                   or _worst([str(s.get("state") or "") for s in review.get("spokes", [])])),
    }
    if merge is not None:
        states["merge"] = str(merge.get("state") or "pending")

    ticket = {"id": run.id, "title": str(run.card.get("title") or run.id), "state": "done"}
    return {
        "ticket": ticket,
        "stages": _stages(states),
        "design": design,
        "build": build,
        "integrate": integrate,
        "review": review,
        "merge": merge,
        "rollback": rollback,
        "timeline": _guard(lambda: _timeline(run), []),
        "legend": _LEGEND,
    }
