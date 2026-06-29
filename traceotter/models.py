"""Core data contracts for TraceOtter.

The episode shape is intentionally close to Agent Data Protocol concepts:
messages/actions/observations are normalized into ordered steps, while outcome
and provenance stay explicit so later SFT examples do not learn from lucky or
unverified traces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


JsonDict = dict[str, Any]


@dataclass(slots=True)
class Step:
    index: int
    actor: str
    action_kind: str
    summary: str
    command: str | None = None
    files: list[str] = field(default_factory=list)
    output_digest: str | None = None
    grounded_in_evidence: bool = False
    reversible: bool = True
    noisy: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "index": self.index,
            "actor": self.actor,
            "action_kind": self.action_kind,
            "summary": self.summary,
            "command": self.command,
            "files": self.files,
            "output_digest": self.output_digest,
            "groundedInEvidence": self.grounded_in_evidence,
            "reversible": self.reversible,
            "noisy": self.noisy,
        }


@dataclass(slots=True)
class Outcome:
    status: str = "unknown"
    tests_run: list[str] = field(default_factory=list)
    tests_passed: bool | None = None
    committed: bool | None = None
    residual_risk: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "status": self.status,
            "testsRun": self.tests_run,
            "testsPassed": self.tests_passed,
            "committed": self.committed,
            "residualRisk": self.residual_risk,
        }


@dataclass(slots=True)
class Labels:
    should_imitate: bool = False
    useful_skill_candidates: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    user_preference_signals: list[str] = field(default_factory=list)
    process_score: float = 0.0
    cost_efficiency_score: float = 0.0

    def to_dict(self) -> JsonDict:
        return {
            "shouldImitate": self.should_imitate,
            "usefulSkillCandidates": self.useful_skill_candidates,
            "failureModes": self.failure_modes,
            "userPreferenceSignals": self.user_preference_signals,
            "processScore": self.process_score,
            "costEfficiencyScore": self.cost_efficiency_score,
        }


@dataclass(slots=True)
class Episode:
    episode_id: str
    source: str
    source_path: str
    cwd: str = ""
    repo: str = "unknown"
    started_at: str | None = None
    ended_at: str | None = None
    user_goal: str = ""
    task_type: str = "unknown"
    scope: str = "unknown"
    risk_class: str = "medium"
    steps: list[Step] = field(default_factory=list)
    artifacts: list[JsonDict] = field(default_factory=list)
    outcome: Outcome = field(default_factory=Outcome)
    labels: Labels = field(default_factory=Labels)

    def to_dict(self) -> JsonDict:
        return {
            "episodeId": self.episode_id,
            "source": self.source,
            "sourcePath": self.source_path,
            "cwd": self.cwd,
            "repo": self.repo,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "userGoal": self.user_goal,
            "taskType": self.task_type,
            "scope": self.scope,
            "riskClass": self.risk_class,
            "steps": [step.to_dict() for step in self.steps],
            "artifacts": self.artifacts,
            "outcome": self.outcome.to_dict(),
            "labels": self.labels.to_dict(),
        }


@dataclass(slots=True)
class Skill:
    skill_id: str
    title: str
    trigger: str
    procedure: list[str]
    verification: list[str] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)
    confidence: float = 0.2
    status: str = "candidate"

    def to_dict(self) -> JsonDict:
        return {
            "skillId": self.skill_id,
            "title": self.title,
            "trigger": self.trigger,
            "procedure": self.procedure,
            "verification": self.verification,
            "sourceEpisodeIds": self.source_episode_ids,
            "confidence": self.confidence,
            "status": self.status,
        }

