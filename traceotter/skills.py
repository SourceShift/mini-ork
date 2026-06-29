"""MemP-inspired procedural memory consolidation."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from .models import Episode, Skill


def consolidate_skills(episodes: list[Episode], min_support: int = 1) -> list[Skill]:
    grouped: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        for candidate in episode.labels.useful_skill_candidates:
            grouped[_normalize(candidate)].append(episode)

    skills: list[Skill] = []
    for title, source_episodes in sorted(grouped.items()):
        if len(source_episodes) < min_support:
            continue
        source_ids = [ep.episode_id for ep in source_episodes]
        skill_id = "skill_" + hashlib.sha256((title + ",".join(source_ids)).encode("utf-8")).hexdigest()[:12]
        confidence = min(0.95, 0.25 + 0.15 * len(source_episodes))
        skills.append(
            Skill(
                skill_id=skill_id,
                title=title,
                trigger=_trigger_for(title),
                procedure=_procedure_for(title),
                verification=_verification_for(title),
                source_episode_ids=source_ids,
                confidence=confidence,
            )
        )
    return skills


def _normalize(text: str) -> str:
    return " ".join(text.strip().split())


def _trigger_for(title: str) -> str:
    low = title.lower()
    if "worktree" in low:
        return "Non-trivial repository implementation or multi-file change."
    if "verification" in low:
        return "After editing code, migrations, prompts, or tests."
    if "dirty-tree" in low:
        return "Before staging, committing, or moving changes between worktrees."
    return "When the current task resembles the source episodes."


def _procedure_for(title: str) -> list[str]:
    low = title.lower()
    if "worktree" in low:
        return [
            "Inspect git status in the primary checkout.",
            "Create or use a task-specific worktree for implementation.",
            "Keep unrelated user changes out of the task diff.",
            "Merge back only after focused verification passes.",
        ]
    if "verification" in low:
        return [
            "Identify the smallest relevant verification command.",
            "Run the focused test or static check.",
            "Record exact command, result, and any skipped checks.",
        ]
    if "dirty-tree" in low:
        return [
            "List modified and untracked files.",
            "Classify files as owned by this task or unrelated.",
            "Stage and commit only owned files.",
        ]
    return ["Apply the procedure demonstrated by the supporting episodes.", "Verify before promotion."]


def _verification_for(title: str) -> list[str]:
    if "verification" in title.lower():
        return ["A concrete test/check command is present in the final episode outcome."]
    return ["At least one source episode completed successfully.", "No high-risk failure mode contradicts the skill."]

