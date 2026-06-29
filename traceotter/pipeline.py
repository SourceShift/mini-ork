"""TraceOtter pipeline orchestration."""

from __future__ import annotations

from pathlib import Path

from .adapters import discover_files, parse_file
from .exporters import export_llamafactory
from .io import write_json, write_jsonl
from .models import Episode
from .skills import consolidate_skills


def ingest(roots: list[Path], out_dir: Path, limit_files: int | None = None, max_events_per_file: int | None = None) -> dict[str, object]:
    files = discover_files(roots)
    if limit_files:
        files = files[:limit_files]
    episodes: list[Episode] = []
    skipped = 0
    for path in files:
        episode = parse_file(path, max_events=max_events_per_file)
        if episode is None:
            skipped += 1
            continue
        episodes.append(episode)

    out_dir.mkdir(parents=True, exist_ok=True)
    episode_path = out_dir / "episodes.jsonl"
    write_jsonl(episode_path, (episode.to_dict() for episode in episodes))
    write_json(
        out_dir / "manifest.json",
        {
            "roots": [str(root) for root in roots],
            "filesDiscovered": len(files),
            "episodesWritten": len(episodes),
            "skipped": skipped,
            "episodePath": str(episode_path),
        },
    )
    return {"episodes": episodes, "episode_path": episode_path, "files": len(files), "skipped": skipped}


def run_pipeline(roots: list[Path], out_dir: Path, limit_files: int | None = None, max_events_per_file: int | None = None) -> dict[str, object]:
    result = ingest(roots, out_dir, limit_files=limit_files, max_events_per_file=max_events_per_file)
    episodes = result["episodes"]
    assert isinstance(episodes, list)
    skills = consolidate_skills(episodes)
    skill_path = out_dir / "skills.json"
    write_json(skill_path, [skill.to_dict() for skill in skills])
    lf = export_llamafactory(episodes, skills, out_dir / "llamafactory")
    report = {
        "episodes": len(episodes),
        "skills": len(skills),
        "episodePath": str(result["episode_path"]),
        "skillPath": str(skill_path),
        "llamafactory": lf,
    }
    write_json(out_dir / "report.json", report)
    return report

