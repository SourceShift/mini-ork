#!/usr/bin/env python3
"""Deterministic wave aggregator for the epic-runner recipe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = "pending"
TERMINAL = {STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _normalize_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_plan(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    epics = _normalize_list(plan.get("epics"))
    waves = _normalize_list(plan.get("waves"))
    if not epics or not waves:
        raise ValueError("epic-runner-plan.json must contain epics[] and waves[]")

    seen: set[str] = set()
    normalized_epics: list[dict[str, Any]] = []
    for epic in epics:
        if not isinstance(epic, dict):
            raise ValueError("every epic must be an object")
        epic_id = str(epic.get("id") or "").strip()
        if not epic_id:
            raise ValueError("every epic must have a non-empty id")
        if epic_id in seen:
            raise ValueError(f"duplicate epic id: {epic_id}")
        seen.add(epic_id)
        normalized_epics.append(epic)

    normalized_waves: list[list[str]] = []
    for wave in waves:
        if not isinstance(wave, list):
            raise ValueError("every wave must be a list of epic ids")
        ids = [str(item).strip() for item in wave if str(item).strip()]
        unknown = sorted(set(ids) - seen)
        if unknown:
            raise ValueError(f"wave references unknown epic ids: {unknown}")
        normalized_waves.append(ids)

    missing = seen - {epic_id for wave in normalized_waves for epic_id in wave}
    if missing:
        raise ValueError(f"epics missing from waves: {sorted(missing)}")

    return normalized_epics, normalized_waves


def _raw_records(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in _normalize_list(results.get("epics")):
        if not isinstance(record, dict):
            continue
        epic_id = str(record.get("id") or "").strip()
        if epic_id:
            records[epic_id] = record
    return records


def _raw_status(record: dict[str, Any] | None) -> str:
    if record is None:
        return STATUS_FAILED
    status = str(record.get("status") or "").strip().lower()
    if status in TERMINAL:
        return status
    return STATUS_FAILED


def _depends_on(epic: dict[str, Any]) -> list[str]:
    deps = epic.get("depends_on") or []
    if not isinstance(deps, list):
        return []
    return [str(dep).strip() for dep in deps if str(dep).strip()]


def _downstream_skips(epics_by_id: dict[str, dict[str, Any]], blocked_ids: set[str]) -> set[str]:
    skipped: set[str] = set()
    changed = True
    while changed:
        changed = False
        blockers = blocked_ids | skipped
        for epic_id, epic in epics_by_id.items():
            if epic_id in blockers:
                continue
            if any(dep in blockers for dep in _depends_on(epic)):
                skipped.add(epic_id)
                changed = True
    return skipped


def aggregate() -> int:
    run_dir = Path(os.environ["MINI_ORK_RUN_DIR"]).resolve()
    plan_path = run_dir / "epic-runner-plan.json"
    results_path = run_dir / "epic-results.json"
    aggregate_path = run_dir / "wave-aggregate.json"

    plan = _load_json(plan_path)
    results = _load_json(results_path)
    epics, waves = _normalize_plan(plan)

    epics_by_id = {str(epic["id"]): epic for epic in epics}
    records = _raw_records(results)
    planned_ids = [str(epic["id"]) for epic in epics]
    missing_ids = [epic_id for epic_id in planned_ids if epic_id not in records]

    counted_status: dict[str, str] = {}
    dependency_respected = True
    dependency_blocked: set[str] = set()

    for epic in epics:
        epic_id = str(epic["id"])
        status = _raw_status(records.get(epic_id))
        deps = _depends_on(epic)
        deps_passed = all(counted_status.get(dep) == STATUS_PASSED for dep in deps)
        if status == STATUS_PASSED and not deps_passed:
            dependency_respected = False
            status = STATUS_FAILED
            dependency_blocked.add(epic_id)
        counted_status[epic_id] = status

    failed_ids = {epic_id for epic_id, status in counted_status.items() if status == STATUS_FAILED}
    skipped_downstream = _downstream_skips(epics_by_id, failed_ids | dependency_blocked)
    for epic_id in skipped_downstream:
        if counted_status.get(epic_id) == STATUS_PASSED:
            dependency_respected = False
        if counted_status.get(epic_id) not in {STATUS_FAILED, STATUS_SKIPPED}:
            counted_status[epic_id] = STATUS_SKIPPED

    per_wave: list[dict[str, Any]] = []
    waves_completed = 0
    first_incomplete_seen = False
    for wave_index, wave in enumerate(waves):
        first_failure = ""
        all_passed = True
        for epic_id in wave:
            status = counted_status.get(epic_id, STATUS_FAILED)
            if status != STATUS_PASSED:
                all_passed = False
                if not first_failure:
                    first_failure = epic_id
        if all_passed and not first_incomplete_seen:
            waves_completed += 1
        else:
            first_incomplete_seen = True
        per_wave.append(
            {
                "wave": wave_index,
                "epics": wave,
                "all_passed": all_passed,
                "first_failure": first_failure,
            }
        )

    findings: list[dict[str, Any]] = []
    for epic_id in planned_ids:
        record = records.get(epic_id) or {}
        findings.append(
            {
                "epic_id": epic_id,
                "status": counted_status.get(epic_id, STATUS_FAILED),
                "artifact_ref": str(record.get("final_artifact_ref") or record.get("artifact_ref") or ""),
                "files_written": [str(item) for item in _normalize_list(record.get("files_written"))],
            }
        )

    for epic_id in missing_ids:
        if not any(finding["epic_id"] == epic_id for finding in findings):
            findings.append(
                {
                    "epic_id": epic_id,
                    "status": STATUS_FAILED,
                    "artifact_ref": "",
                    "files_written": [],
                }
            )

    payload = {
        "verdict": "in_progress",
        "aggregate": {
            "epics_total": len(planned_ids),
            "epics_passed": sum(1 for status in counted_status.values() if status == STATUS_PASSED),
            "epics_failed": sum(1 for status in counted_status.values() if status == STATUS_FAILED),
            "epics_skipped": sum(1 for status in counted_status.values() if status == STATUS_SKIPPED),
            "waves_total": len(waves),
            "waves_completed": waves_completed,
            "dependency_respected": dependency_respected,
        },
        "per_wave": per_wave,
        "findings": findings,
    }
    _write_json(aggregate_path, payload)
    return 0 if dependency_respected and not missing_ids else 1


if __name__ == "__main__":
    try:
        raise SystemExit(aggregate())
    except Exception as exc:
        print(f"wave aggregator failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
