#!/usr/bin/env python3
"""Deterministic dispatcher for the epic-runner recipe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


STATUS_PENDING = "pending"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
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


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _record_for(epic: dict[str, Any], wave_index: int) -> dict[str, Any]:
    return {
        "id": str(epic.get("id") or ""),
        "wave": wave_index,
        "status": STATUS_PENDING,
        "child_run_id": "",
        "child_run_dir": "",
        "final_artifact_ref": "",
        "files_written": [],
        "verdict": {"pass": False},
        "error": "",
    }


def _normalize_plan(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    epics = plan.get("epics")
    waves = plan.get("waves")
    if not isinstance(epics, list) or not isinstance(waves, list):
        raise ValueError("epic-runner-plan.json must contain epics[] and waves[]")

    normalized_epics: list[dict[str, Any]] = []
    seen: set[str] = set()
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


def _downstream_ids(epics_by_id: dict[str, dict[str, Any]], failed_ids: set[str]) -> set[str]:
    downstream: set[str] = set()
    changed = True
    while changed:
        changed = False
        blocked = failed_ids | downstream
        for epic_id, epic in epics_by_id.items():
            if epic_id in blocked:
                continue
            deps = epic.get("depends_on") or []
            if not isinstance(deps, list):
                deps = []
            if any(str(dep) in blocked for dep in deps):
                downstream.add(epic_id)
                changed = True
    return downstream


def _parse_spawn_output(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _child_run_dir(child_run_id: str, parsed: dict[str, str], home: Path) -> Path:
    explicit = parsed.get("child_run_dir")
    if explicit:
        return Path(explicit)
    return home / "runs" / child_run_id


def _load_child_verdict(child_dir: Path, exit_code: int) -> dict[str, Any]:
    verdict_path = child_dir / "verdict.json"
    if verdict_path.exists():
        try:
            data = _load_json(verdict_path)
            if isinstance(data.get("pass"), bool):
                return data
            data["pass"] = exit_code == 0
            return data
        except Exception as exc:
            return {"pass": False, "error": f"invalid verdict.json: {exc}"}
    return {"pass": exit_code == 0}


def _files_written(child_dir: Path, verdict: dict[str, Any]) -> list[str]:
    files = verdict.get("files_written") or verdict.get("files_changed")
    if isinstance(files, list):
        return [str(item) for item in files]
    diff = child_dir / "framework-edit.diff"
    if diff.exists() and diff.stat().st_size > 0:
        return [str(diff)]
    return []


def _run_verifier(script: str, child_dir: Path, record: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    if not script:
        return True, {}, ""
    env = os.environ.copy()
    env["MINI_ORK_RUN_DIR"] = str(child_dir)
    if record.get("files_written"):
        env["EPIC_CHANGED_FILES"] = "\n".join(str(item) for item in record["files_written"])
    proc = subprocess.run(
        [script, str(child_dir)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    output = (proc.stdout or "").strip()
    if proc.stderr:
        output = (output + "\n" + proc.stderr.strip()).strip()
    verdict: dict[str, Any] = {"pass": proc.returncode == 0}
    try:
        if proc.stdout.strip():
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                verdict = parsed
    except json.JSONDecodeError:
        pass
    return bool(verdict.get("pass")) and proc.returncode == 0, verdict, output


def _spawn_command(
    spawn_bin: Path,
    root: Path,
    parent_run_id: str,
    child_run_id: str,
    kickoff_path: Path,
    publish_enabled: bool,
) -> list[str]:
    if spawn_bin.exists():
        cmd = [
            str(spawn_bin),
            "--parent-run",
            parent_run_id,
            "--kickoff",
            str(kickoff_path),
            "--recipe",
            "framework-edit",
            "--child-run",
            child_run_id,
        ]
        # NOTE: `mini-ork spawn` does NOT accept --smoke-shape (only the `run`
        # path below does). Appending it here made every epic-runner child
        # spawn fail with "Unknown flag: --smoke-shape". Publish gating for the
        # spawn path is handled via MINI_ORK_EPIC_PUBLISH env, so the flag is
        # unnecessary here.
    else:
        cmd = [str(root / "bin" / "mini-ork"), "run", "framework-edit", str(kickoff_path)]
        if not publish_enabled:
            cmd.append("--smoke-shape")
    return cmd


def dispatch() -> int:
    run_dir = Path(os.environ["MINI_ORK_RUN_DIR"]).resolve()
    root = Path(os.environ.get("MINI_ORK_ROOT", Path.cwd())).resolve()
    home = Path(os.environ.get("MINI_ORK_HOME", Path.cwd() / ".mini-ork")).resolve()
    parent_run_id = os.environ.get("MINI_ORK_RUN_ID") or os.environ.get("MINI_ORK_TASK_RUN_ID") or run_dir.name
    plan_path = run_dir / "epic-runner-plan.json"
    results_path = run_dir / "epic-results.json"

    try:
        plan = _load_json(plan_path)
        epics, waves = _normalize_plan(plan)
    except Exception as exc:
        payload = {"verdict": "in_progress", "epics": [], "waves_completed": 0, "waves_total": 0}
        _write_json(results_path, payload)
        print(f"epic dispatcher failed to load plan: {exc}", file=sys.stderr)
        return 1

    epics_by_id = {str(epic["id"]): epic for epic in epics}
    wave_by_id = {epic_id: idx for idx, wave in enumerate(waves) for epic_id in wave}
    records = {epic_id: _record_for(epic, wave_by_id[epic_id]) for epic_id, epic in epics_by_id.items()}
    publish_enabled = _bool_env("MINI_ORK_EPIC_PUBLISH", bool(plan.get("publish_enabled")))
    verifier_script = os.environ.get("MINI_ORK_EPIC_VERIFIER_SCRIPT") or str(plan.get("verifier_script") or "")

    def persist(waves_completed: int) -> None:
        ordered = [records[str(epic["id"])] for epic in epics]
        _write_json(
            results_path,
            {
                "verdict": "in_progress",
                "epics": ordered,
                "waves_completed": waves_completed,
                "waves_total": len(waves),
            },
        )

    persist(0)
    waves_completed = 0
    failed_ids: set[str] = set()

    for wave_index, wave in enumerate(waves):
        procs: list[tuple[str, subprocess.Popen[str], Path]] = []

        for epic_id in wave:
            epic = epics_by_id[epic_id]
            kickoff = str(epic.get("framework_edit_kickoff") or "").strip()
            if not kickoff:
                rec = records[epic_id]
                rec["status"] = STATUS_FAILED
                rec["error"] = "missing framework_edit_kickoff"
                failed_ids.add(epic_id)
                continue

            kickoff_dir = run_dir / "epic-kickoffs"
            kickoff_dir.mkdir(parents=True, exist_ok=True)
            kickoff_path = kickoff_dir / f"{epic_id}.md"
            kickoff_path.write_text(kickoff + "\n", encoding="utf-8")
            child_run_id = f"{parent_run_id}-{epic_id}"
            spawn_override = os.environ.get("MINI_ORK_EPIC_SPAWN_BIN")
            spawn_bin = Path(spawn_override) if spawn_override else root / "bin" / "mini-ork-spawn"
            cmd = _spawn_command(spawn_bin, root, parent_run_id, child_run_id, kickoff_path, publish_enabled)
            env = os.environ.copy()
            env["MINI_ORK_HOME"] = str(home)
            env["MINI_ORK_ROOT"] = str(root)
            env.setdefault("MINI_ORK_DB", str(home / "state.db"))
            env["MINI_ORK_EPIC_PUBLISH"] = "true" if publish_enabled else "false"
            log_path = run_dir / f"epic-dispatch-{epic_id}.log"
            log = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
            log.close()
            procs.append((epic_id, proc, log_path))

        for epic_id, proc, log_path in procs:
            exit_code = proc.wait()
            output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            parsed = _parse_spawn_output(output)
            rec = records[epic_id]
            child_run_id = parsed.get("child_run_id") or f"{parent_run_id}-{epic_id}"
            rec["child_run_id"] = child_run_id
            child_dir = _child_run_dir(child_run_id, parsed, home)
            rec["child_run_dir"] = str(child_dir)
            verdict = _load_child_verdict(child_dir, exit_code)
            rec["verdict"] = verdict
            rec["files_written"] = _files_written(child_dir, verdict)
            artifact = child_dir / "framework-edit.diff"
            rec["final_artifact_ref"] = str(artifact) if artifact.exists() else ""

            if exit_code != 0:
                rec["status"] = STATUS_FAILED
                rec["error"] = output.strip() or f"child exited {exit_code}"
                failed_ids.add(epic_id)
                continue

            verifier_passed, verifier_verdict, verifier_output = _run_verifier(verifier_script, child_dir, rec)
            if verifier_script:
                merged = dict(rec["verdict"])
                merged["external_verifier"] = verifier_verdict
                merged["pass"] = bool(merged.get("pass")) and verifier_passed
                rec["verdict"] = merged
            if not verifier_passed:
                rec["status"] = STATUS_FAILED
                rec["error"] = verifier_output or "external verifier failed"
                failed_ids.add(epic_id)
            elif bool(rec["verdict"].get("pass")):
                rec["status"] = STATUS_PASSED
            else:
                rec["status"] = STATUS_FAILED
                rec["error"] = str(rec["verdict"].get("error") or "child verdict pass=false")
                failed_ids.add(epic_id)

        waves_completed = wave_index + 1
        persist(waves_completed)

        wave_failed = {epic_id for epic_id in wave if records[epic_id]["status"] == STATUS_FAILED}
        if wave_failed:
            skipped = _downstream_ids(epics_by_id, failed_ids | wave_failed)
            for skipped_id in skipped:
                if records[skipped_id]["status"] == STATUS_PENDING:
                    records[skipped_id]["status"] = STATUS_SKIPPED
                    records[skipped_id]["error"] = "skipped because upstream epic failed"
            persist(waves_completed)
            return 1

    persist(waves_completed)
    return 0 if all(record["status"] in TERMINAL for record in records.values()) else 1


if __name__ == "__main__":
    raise SystemExit(dispatch())
