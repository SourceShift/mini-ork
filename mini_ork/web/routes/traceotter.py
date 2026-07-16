"""TraceOtter — the distillation lane, exposed.

TraceOtter has no HTTP server; it is a CLI that writes artifacts into
`.mini-ork/traceotter/`. Per the architecture, TraceOtter sits BEHIND mini-ork, so
mini-ork is the right place to serve it rather than standing up a fourth daemon.

The funnel this exposes is the whole point of the distillation lane, and it is the
number that matters:

    episodes  →  shouldImitate  →  SFT examples  →  a trained student

An episode count alone says nothing. What says something is how many of those episodes
the distiller judged worth IMITATING — because that, not the raw trace volume, is the
training set. A loop that logs 10,000 episodes and imitates none has learned nothing.

Everything is read lazily from disk and guarded: a workspace that has never run
TraceOtter returns `available: false`, never a 500 and never a misleading zero.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from ..deps import get_home

router = APIRouter(prefix="/api/v1/traceotter", tags=["traceotter"])


def _dir(home: Path) -> Path:
    return home / "traceotter"


def _load_json(p: Path) -> Any | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


@router.get("/summary")
def summary(home: Path = Depends(get_home)) -> dict[str, Any]:
    """The distillation funnel + what the episodes were judged on.

    `available: false` means TraceOtter has never run in this workspace. That is a
    DISTINCT state from "ran and found nothing" — reporting 0 episodes for a lane that
    was never invoked would be a vacuous zero, the same lie as a green test that never
    executed.
    """
    d = _dir(home)
    report = _load_json(d / "report.json")
    if report is None:
        return {"available": False, "reason": "no .mini-ork/traceotter/report.json — TraceOtter has not run here"}

    episodes_path = d / "episodes.jsonl"
    total = 0
    imitate = 0
    process: list[float] = []
    cost: list[float] = []
    failure_modes: Counter[str] = Counter()
    skill_candidates: Counter[str] = Counter()

    if episodes_path.exists():
        with episodes_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ep = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                labels = ep.get("labels") or {}
                if labels.get("shouldImitate"):
                    imitate += 1
                for f in labels.get("failureModes") or []:
                    failure_modes[str(f)] += 1
                for s in labels.get("usefulSkillCandidates") or []:
                    skill_candidates[str(s)] += 1
                if isinstance(labels.get("processScore"), (int, float)):
                    process.append(float(labels["processScore"]))
                if isinstance(labels.get("costEfficiencyScore"), (int, float)):
                    cost.append(float(labels["costEfficiencyScore"]))

    lf = report.get("llamafactory") or {}
    try:
        sft_examples = int(lf.get("examples", 0))
    except (TypeError, ValueError):
        sft_examples = 0

    return {
        "available": True,
        # ── the funnel ──
        "episodes": total or int(report.get("episodes", 0) or 0),
        "should_imitate": imitate,
        "sft_examples": sft_examples,
        "skills": int(report.get("skills", 0) or 0),
        # ── what the distiller judged ──
        "avg_process_score": round(sum(process) / len(process), 3) if process else None,
        "avg_cost_efficiency": round(sum(cost) / len(cost), 3) if cost else None,
        "failure_modes": [{"mode": m, "count": c} for m, c in failure_modes.most_common(8)],
        "skill_candidates": [{"skill": s, "count": c} for s, c in skill_candidates.most_common(8)],
        # ── where the training config landed ──
        "llamafactory_config": lf.get("config"),
    }


@router.get("/skills")
def skills(home: Path = Depends(get_home)) -> list[dict[str, Any]]:
    """Procedures TraceOtter mined out of the trajectories, with a confidence.

    A skill is a *reusable procedure the system taught itself* — the closest thing in
    the stack to compounding know-how, as opposed to a one-off successful run.
    """
    data = _load_json(_dir(home) / "skills.json")
    return data if isinstance(data, list) else []


@router.get("/episodes")
def episodes(
    home: Path = Depends(get_home),
    limit: int = 50,
    imitate_only: bool = False,
) -> list[dict[str, Any]]:
    """Recent episodes. `imitate_only=true` returns exactly the training set.

    Heavy fields (full transcripts, artifacts) are dropped — the panel renders labels,
    and streaming megabytes into a sidebar helps nobody.
    """
    path = _dir(home) / "episodes.jsonl"
    if not path.exists():
        return []

    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ep = json.loads(line)
            except json.JSONDecodeError:
                continue
            labels = ep.get("labels") or {}
            if imitate_only and not labels.get("shouldImitate"):
                continue
            out.append(
                {
                    "episode_id": ep.get("episodeId"),
                    "outcome": ep.get("outcome"),
                    "cwd": ep.get("cwd"),
                    "ended_at": ep.get("endedAt"),
                    "should_imitate": bool(labels.get("shouldImitate")),
                    "process_score": labels.get("processScore"),
                    "cost_efficiency_score": labels.get("costEfficiencyScore"),
                    "failure_modes": labels.get("failureModes") or [],
                }
            )

    # Newest last in the file → return newest first.
    return list(reversed(out))[:limit]
