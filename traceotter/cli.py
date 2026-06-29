"""TraceOtter command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .io import read_jsonl, write_json
from .models import Episode, Labels, Outcome, Step
from .pipeline import ingest, run_pipeline
from .skills import consolidate_skills
from .exporters import export_llamafactory


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="traceotter",
        description="Train small coding agents from Codex, Claude, and mini-ork traces.",
    )
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Check local TraceOtter setup")

    p_ingest = sub.add_parser("ingest", help="Normalize trace roots into episodes.jsonl")
    _add_roots(p_ingest)
    p_ingest.add_argument("--out", required=True, type=Path)
    p_ingest.add_argument("--limit-files", type=int)
    p_ingest.add_argument("--max-events-per-file", type=int)

    p_skills = sub.add_parser("consolidate", help="Create MemP-style procedural skills from episodes.jsonl")
    p_skills.add_argument("--episodes", required=True, type=Path)
    p_skills.add_argument("--out", required=True, type=Path)
    p_skills.add_argument("--min-support", type=int, default=1)

    p_export = sub.add_parser("export-llamafactory", help="Export LLaMA-Factory SFT dataset and config")
    p_export.add_argument("--episodes", required=True, type=Path)
    p_export.add_argument("--skills", type=Path)
    p_export.add_argument("--out", required=True, type=Path)
    p_export.add_argument("--dataset-name", default="traceotter_sft")

    p_pipe = sub.add_parser("pipeline", help="Run ingest + skill consolidation + LLaMA-Factory export")
    _add_roots(p_pipe)
    p_pipe.add_argument("--out", required=True, type=Path)
    p_pipe.add_argument("--limit-files", type=int)
    p_pipe.add_argument("--max-events-per-file", type=int)

    args = parser.parse_args()
    if args.cmd == "doctor":
        return _emit(args, {"ok": True, "package": "traceotter", "adapters": ["codex_jsonl", "claude_jsonl", "mini_ork_run"], "trainerExport": "llamafactory"})
    if args.cmd == "ingest":
        result = ingest(_roots_from_args(args), args.out, args.limit_files, args.max_events_per_file)
        return _emit(args, {"ok": True, "episodes": len(result["episodes"]), "files": result["files"], "skipped": result["skipped"], "episodePath": str(result["episode_path"])})
    if args.cmd == "consolidate":
        episodes = _episodes_from_jsonl(args.episodes)
        skills = consolidate_skills(episodes, min_support=args.min_support)
        write_json(args.out, [skill.to_dict() for skill in skills])
        return _emit(args, {"ok": True, "skills": len(skills), "path": str(args.out)})
    if args.cmd == "export-llamafactory":
        episodes = _episodes_from_jsonl(args.episodes)
        skills = []
        if args.skills and args.skills.exists():
            # LLaMA export only needs skill ids/title/sourceEpisodeIds; keep this simple.
            for item in json.loads(args.skills.read_text(encoding="utf-8")):
                from .models import Skill

                skills.append(
                    Skill(
                        skill_id=item["skillId"],
                        title=item["title"],
                        trigger=item.get("trigger", ""),
                        procedure=item.get("procedure", []),
                        verification=item.get("verification", []),
                        source_episode_ids=item.get("sourceEpisodeIds", []),
                        confidence=float(item.get("confidence", 0.2)),
                    )
                )
        result = export_llamafactory(episodes, skills, args.out, args.dataset_name)
        return _emit(args, {"ok": True, **result})
    if args.cmd == "pipeline":
        report = run_pipeline(_roots_from_args(args), args.out, args.limit_files, args.max_events_per_file)
        return _emit(args, {"ok": True, **report})
    raise AssertionError(args.cmd)


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex", action="append", type=Path, default=[], help="Codex session root or JSONL file")
    parser.add_argument("--claude", action="append", type=Path, default=[], help="Claude project root or JSONL file")
    parser.add_argument("--mini-ork", action="append", type=Path, default=[], help="mini-ork run root or artifact file")


def _roots_from_args(args: argparse.Namespace) -> list[Path]:
    return [*args.codex, *args.claude, *args.mini_ork]


def _episodes_from_jsonl(path: Path) -> list[Episode]:
    episodes: list[Episode] = []
    for item in read_jsonl(path):
        steps = [
            Step(
                index=step["index"],
                actor=step["actor"],
                action_kind=step["action_kind"] if "action_kind" in step else step.get("actionKind", "other"),
                summary=step["summary"],
                command=step.get("command"),
                files=step.get("files", []),
                output_digest=step.get("outputDigest"),
                grounded_in_evidence=bool(step.get("groundedInEvidence", False)),
                reversible=bool(step.get("reversible", True)),
                noisy=bool(step.get("noisy", False)),
            )
            for step in item.get("steps", [])
        ]
        outcome_data = item.get("outcome", {})
        label_data = item.get("labels", {})
        episodes.append(
            Episode(
                episode_id=item["episodeId"],
                source=item["source"],
                source_path=item["sourcePath"],
                cwd=item.get("cwd", ""),
                repo=item.get("repo", "unknown"),
                started_at=item.get("startedAt"),
                ended_at=item.get("endedAt"),
                user_goal=item.get("userGoal", ""),
                task_type=item.get("taskType", "unknown"),
                scope=item.get("scope", "unknown"),
                risk_class=item.get("riskClass", "medium"),
                steps=steps,
                outcome=Outcome(
                    status=outcome_data.get("status", "unknown"),
                    tests_run=outcome_data.get("testsRun", []),
                    tests_passed=outcome_data.get("testsPassed"),
                    committed=outcome_data.get("committed"),
                    residual_risk=outcome_data.get("residualRisk", []),
                ),
                labels=Labels(
                    should_imitate=bool(label_data.get("shouldImitate", False)),
                    useful_skill_candidates=label_data.get("usefulSkillCandidates", []),
                    failure_modes=label_data.get("failureModes", []),
                    user_preference_signals=label_data.get("userPreferenceSignals", []),
                    process_score=float(label_data.get("processScore", 0.0)),
                    cost_efficiency_score=float(label_data.get("costEfficiencyScore", 0.0)),
                ),
            )
        )
    return episodes


def _emit(args: argparse.Namespace, payload: dict[str, object]) -> int:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
