"""Shared models for production scenario validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    recipe: str
    kickoff: Path
    seed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    mode: str
    provider_policy: str
    md_only: bool
    timeout_seconds: int
    keep: bool


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    ok: bool
    skipped: bool = False
    returncode: int | None = None
    expected_task_class: str = ""
    actual_task_class: str = ""
    output: str = ""
    output_log: Path | None = None
    tmp_project: Path | None = None
    plan_path: Path | None = None
    error: str = ""

