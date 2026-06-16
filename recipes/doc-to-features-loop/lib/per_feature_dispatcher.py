#!/usr/bin/env python3
"""Deterministic P0 child dispatcher for doc-to-features-loop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _feature_priority(feature: dict[str, Any]) -> str:
    return str(feature.get("priority") or feature.get("priority_band") or "").strip().upper()


def _p0_features(index: dict[str, Any]) -> list[dict[str, Any]]:
    raw = index.get("features")
    if not isinstance(raw, list):
        raise ValueError("feature-index.json must contain features[]")
    features = [item for item in raw if isinstance(item, dict) and _feature_priority(item) == "P0"]
    for feature in features:
        if not str(feature.get("id") or "").strip():
            raise ValueError("every P0 feature must have a non-empty id")
    return features


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _dod_probes(feature: dict[str, Any]) -> list[Any]:
    for key in ("dod_probes", "definition_of_done", "success_criteria", "verification"):
        probes = feature.get(key)
        if isinstance(probes, list) and probes:
            return probes
        if isinstance(probes, str) and probes.strip():
            return [probes.strip()]
    return [
        f"Implement only feature {feature.get('id')}.",
        "Run the nearest scoped lint, typecheck, or unit-test command for touched files.",
        "Record touched files and verification evidence in implementer-summary.json.",
    ]


def _feature_dependencies(feature: dict[str, Any]) -> list[Any]:
    deps = feature.get("dependencies", feature.get("depends_on", []))
    return _as_list(deps)


def _write_kickoff(path: Path, feature: dict[str, Any], source_kickoff: str) -> None:
    title = str(feature.get("title") or feature.get("id") or "").strip()
    fid = str(feature.get("id") or "").strip()
    source_evidence = feature.get("source_evidence", feature.get("source_section", []))
    modern_refs = _as_list(feature.get("modern_techniques_refs"))
    dependencies = _feature_dependencies(feature)
    request = str(feature.get("implementation_request") or feature.get("rationale") or "").strip()

    content = f"""# recursive-validate-impl child kickoff: {fid}

## Feature

- ID: `{fid}`
- Title: {title}
- Source kickoff: `{source_kickoff}`
- Priority: {_feature_priority(feature)}
- Dependencies: `{", ".join(str(dep) for dep in dependencies) if dependencies else "none"}`

## Source Evidence

```json
{_json_block(source_evidence)}
```

## Scoped Implementation Request

Implement only this feature:

{request or title}

Preserve unrelated user changes. Keep the patch scoped to this feature and do
not broaden the child run into neighboring feature work.

## Definition Of Done Probes

```json
{_json_block(_dod_probes(feature))}
```

## arxiv-search-tool Modern Technique References

```json
{_json_block(modern_refs)}
```

## Feature Record

```json
{_json_block(feature)}
```
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _parse_key_values(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _normalise_child_verdict(child_dir: Path) -> tuple[dict[str, Any] | None, Path | None, str]:
    verdict_path = child_dir / "verdict.json"
    if verdict_path.exists():
        data = _load_json(verdict_path)
        if not isinstance(data.get("pass"), bool):
            raise ValueError(f"{verdict_path} missing boolean pass")
        return data, verdict_path, str(verdict_path)

    panel_path = child_dir / "panel-verdict.json"
    if panel_path.exists():
        panel = _load_json(panel_path)
        verdict = str(panel.get("verdict") or "").strip().upper()
        return {
            "pass": verdict == "APPROVE",
            "source_verdict_path": str(panel_path),
            "source_verdict": panel,
        }, panel_path, str(panel_path)

    return None, None, ""


def _files_written(child_dir: Path, verdict: dict[str, Any] | None) -> list[str]:
    if isinstance(verdict, dict):
        files = verdict.get("files_written") or verdict.get("files_changed")
        if isinstance(files, list):
            return [str(item) for item in files]
        source = verdict.get("source_verdict")
        if isinstance(source, dict):
            files = source.get("files_written") or source.get("files_changed")
            if isinstance(files, list):
                return [str(item) for item in files]

    summary = child_dir / "implementer-summary.json"
    if summary.exists():
        try:
            data = _load_json(summary)
            files = data.get("touched_files") or data.get("files_written")
            if isinstance(files, list):
                return [str(item) for item in files]
        except Exception:
            pass
    return []


def _final_artifact_ref(child_dir: Path) -> str:
    for name in ("implementer-summary.json", "panel-verdict.json", "replan.json"):
        candidate = child_dir / name
        if candidate.exists():
            return str(candidate)
    return ""


def _child_command(root: Path, kickoff_path: Path) -> list[str]:
    override = os.environ.get("MINI_ORK_PER_FEATURE_MINI_ORK_BIN")
    mini_ork = Path(override) if override else root / "bin" / "mini-ork"
    return [str(mini_ork), "run", "recursive-validate-impl", str(kickoff_path)]


def _dispatch_feature(
    feature: dict[str, Any],
    run_dir: Path,
    root: Path,
    home: Path,
    parent_run_id: str,
    source_kickoff: str,
) -> dict[str, Any]:
    fid = str(feature["id"]).strip()
    child_run_id = f"{parent_run_id}-{fid}"
    kickoff_path = run_dir / "child-kickoffs" / f"{fid}.md"
    _write_kickoff(kickoff_path, feature, source_kickoff)

    child_dir = home / "runs" / child_run_id
    log_path = run_dir / "child-runs" / f"{fid}.log"
    result: dict[str, Any] = {
        "feature_id": fid,
        "child_kickoff": str(kickoff_path),
        "child_run_id": child_run_id,
        "child_run_dir": str(child_dir),
        "status": STATUS_PENDING,
        "verdict_path": None,
        "final_artifact_ref": None,
        "files_written": [],
    }

    env = os.environ.copy()
    env["MINI_ORK_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(root)
    env.setdefault("MINI_ORK_DB", str(home / "state.db"))
    env["MINI_ORK_RUN_ID"] = child_run_id
    env["MINI_ORK_PARENT_RUN_ID"] = parent_run_id
    env.pop("MINI_ORK_RUN_DIR", None)
    env.pop("MINI_ORK_PLAN_PATH", None)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            _child_command(root, kickoff_path),
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )

    output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    parsed = _parse_key_values(output)
    if parsed.get("child_run_id"):
        result["child_run_id"] = parsed["child_run_id"]
        child_dir = Path(parsed.get("child_run_dir") or home / "runs" / parsed["child_run_id"])
        result["child_run_dir"] = str(child_dir)

    try:
        verdict, source_verdict_path, _ = _normalise_child_verdict(child_dir)
        if verdict is None:
            result["status"] = STATUS_FAILED if proc.returncode else STATUS_PENDING
            result["error"] = "missing child verdict"
        else:
            normalized_verdict_path = run_dir / "child-runs" / f"{fid}.verdict.json"
            _write_json(normalized_verdict_path, verdict)
            result["verdict_path"] = str(normalized_verdict_path)
            result["source_verdict_path"] = str(source_verdict_path) if source_verdict_path else None
            result["files_written"] = _files_written(child_dir, verdict)
            result["final_artifact_ref"] = _final_artifact_ref(child_dir) or None
            result["status"] = STATUS_PASSED if proc.returncode == 0 and verdict.get("pass") is True else STATUS_FAILED
            if result["status"] == STATUS_FAILED:
                result["error"] = f"child exited {proc.returncode}; pass={verdict.get('pass')}"
    except Exception as exc:
        result["status"] = STATUS_FAILED
        result["error"] = f"malformed child verdict: {exc}"

    result["log_path"] = str(log_path)
    return result


def dispatch() -> int:
    run_dir = Path(os.environ["MINI_ORK_RUN_DIR"]).resolve()
    root = Path(os.environ.get("MINI_ORK_ROOT", Path.cwd())).resolve()
    home = Path(os.environ.get("MINI_ORK_HOME", Path.cwd() / ".mini-ork")).resolve()
    parent_run_id = os.environ.get("MINI_ORK_RUN_ID") or os.environ.get("MINI_ORK_TASK_RUN_ID") or run_dir.name
    feature_index_path = run_dir / "feature-index.json"
    child_records_dir = run_dir / "child-runs"
    child_records_dir.mkdir(parents=True, exist_ok=True)

    try:
        index = _load_json(feature_index_path)
        features = _p0_features(index)
    except Exception as exc:
        _write_json(
            child_records_dir / "_dispatcher-error.json",
            {
                "feature_id": "_dispatcher-error",
                "child_kickoff": None,
                "child_run_id": None,
                "child_run_dir": None,
                "status": STATUS_FAILED,
                "verdict_path": None,
                "final_artifact_ref": None,
                "files_written": [],
                "error": str(exc),
            },
        )
        print(f"per-feature dispatcher failed to load feature index: {exc}", file=sys.stderr)
        return 1

    source_kickoff = str(index.get("source_kickoff") or os.environ.get("MINI_ORK_KICKOFF_PATH") or "")
    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parent_run_id": parent_run_id,
        "total": len(features),
        "features": [],
    }

    exit_code = 0
    for feature in features:
        record = _dispatch_feature(feature, run_dir, root, home, parent_run_id, source_kickoff)
        _write_json(child_records_dir / f"{feature['id']}.json", record)
        summary["features"].append(record)
        if record["status"] != STATUS_PASSED:
            exit_code = 1

    summary["passed"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_PASSED)
    summary["failed"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_FAILED)
    summary["pending"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_PENDING)
    _write_json(child_records_dir / "_summary.json", summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(dispatch())
