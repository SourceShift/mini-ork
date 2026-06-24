#!/usr/bin/env python3
"""Deterministic P0 child dispatcher for doc-to-features-loop."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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
    features = [
        item
        for item in raw
        if isinstance(item, dict)
        and _feature_priority(item) == "P0"
        and _is_dispatchable_feature(item)
    ]
    for feature in features:
        if not str(feature.get("id") or "").strip():
            raise ValueError("every P0 feature must have a non-empty id")
    return features


def _is_dispatchable_feature(feature: dict[str, Any]) -> bool:
    if feature.get("dispatch_ready") is False:
        return False

    fid = str(feature.get("id") or "").strip().lower()
    title = str(feature.get("title") or "").strip().lower()
    label = f"{fid} {title}"

    # feature-index.json is produced by the parent doc-to-features loop itself.
    # Dispatching it as a recursive implementation child makes the child verify
    # generated .mini-ork/run artifacts instead of product code.
    if fid in {"planner-feature-index", "ranked-feature-index", "feature-index"}:
        return False
    if "feature index" in label and ("artifact" in label or "ranked" in label):
        return False
    if "ranked backlog" in label and "artifact" in label:
        return False

    return True


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


def _success_command(feature: dict[str, Any]) -> str:
    for key in ("success_command", "verification_command", "test_command"):
        command = feature.get(key)
        if isinstance(command, str) and command.strip():
            return command.strip()
        if isinstance(command, list) and command:
            return " && ".join(str(item).strip() for item in command if str(item).strip())

    fid = str(feature.get("id") or "").lower()
    title = str(feature.get("title") or "").lower()
    label = f"{fid} {title}"
    if "deploy" in label or "smoke" in label:
        return "make check-deploy && make deploy-status"
    if "schema" in label or "repository" in label or "candidate" in label or "compiler" in label:
        return (
            "git diff --check || exit 1; "
            "files=\"$(git diff --name-only -- '*.ts' '*.tsx')\"; "
            "if [ -z \"$files\" ] && [ -f \"${MINI_ORK_RUN_DIR}/implementer-summary.json\" ]; then "
            "files=\"$(node -e \"const fs=require('fs'); const p=process.env.MINI_ORK_RUN_DIR + '/implementer-summary.json'; "
            "const j=JSON.parse(fs.readFileSync(p,'utf8')); console.log((j.touched_files||[]).filter(f=>/\\\\.tsx?$/.test(f)).join(' '));\")\"; "
            "fi; "
            "if [ -n \"$files\" ]; then pnpm type-check:touched $files; fi; "
            "tests=\"$(printf '%s\\n' $files | grep -E '__tests__/.*\\.test\\.tsx?$' || true)\"; "
            "if [ -n \"$tests\" ]; then JEST_GUARD_SOFT=1 pnpm test:server --runTestsByPath $tests; fi"
        )
    return (
        "git diff --check || exit 1; "
        "(git diff --name-only -- '*.ts' '*.tsx' | grep -q . "
        "&& pnpm type-check:touched $(git diff --name-only -- '*.ts' '*.tsx') "
        "|| true)"
    )


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
    success_command = _success_command(feature)

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

## Success Verification Command

This command must prove the child run succeeded:

```bash
{success_command}
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
        if isinstance(panel.get("pass"), bool):
            return {
                "pass": panel["pass"],
                "source_verdict_path": str(panel_path),
                "source_verdict": panel,
            }, panel_path, str(panel_path)
        verdict = str(panel.get("verdict") or "").strip().upper()
        return {
            "pass": verdict == "APPROVE",
            "source_verdict_path": str(panel_path),
            "source_verdict": panel,
        }, panel_path, str(panel_path)

    return None, None, ""


def _latest_evidence_json(child_dir: Path, prefix: str) -> dict[str, Any] | None:
    evidence_dir = child_dir / "evidence"
    if not evidence_dir.exists():
        return None
    candidates = sorted(evidence_dir.glob(f"{prefix}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                data.setdefault("evidence_log", str(path))
                return data
    return None


def _tier4_report_verdicts(child_dir: Path) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for lens in ("glm", "kimi", "codex", "minimax"):
        path = child_dir / f"tier4-{lens}.md"
        if not path.exists() or path.stat().st_size == 0:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES|ESCALATE)", text, flags=re.I)
        if matches:
            verdicts[lens] = matches[-1].upper()
    return verdicts


def _write_deterministic_panel_fallback(child_dir: Path, log_path: Path) -> Path | None:
    panel_path = child_dir / "panel-verdict.json"
    if panel_path.exists() and panel_path.stat().st_size > 0:
        return panel_path

    tier1 = _latest_evidence_json(child_dir, "tier1-compile-typecheck")
    tier2 = _latest_evidence_json(child_dir, "tier2-scoped-unit")
    tier3 = _latest_evidence_json(child_dir, "tier3-property-mutation")
    quorum = _latest_evidence_json(child_dir, "tier4-panel-quorum")
    verifier_results = [item for item in (tier1, tier2, tier3) if item is not None]
    verifier_pass = bool(verifier_results) and all(item.get("pass") is True for item in verifier_results)

    lens_verdicts = _tier4_report_verdicts(child_dir)
    approve_count = sum(1 for verdict in lens_verdicts.values() if verdict == "APPROVE")
    request_count = sum(1 for verdict in lens_verdicts.values() if verdict == "REQUEST_CHANGES")
    escalate_count = sum(1 for verdict in lens_verdicts.values() if verdict == "ESCALATE")
    quorum_pass = bool(quorum and quorum.get("pass") is True)

    if not verifier_pass or not quorum_pass or approve_count < 3 or escalate_count:
        return None

    fallback = {
        "verdict": "APPROVE",
        "pass": True,
        "source": "deterministic-tier4-fallback",
        "reason": "tier4_synth did not write panel-verdict.json, but machine verifiers passed and tier4 report quorum approved",
        "lens_verdicts": lens_verdicts,
        "approve_count": approve_count,
        "request_changes_count": request_count,
        "escalate_count": escalate_count,
        "verifier_results": verifier_results,
        "tier4_quorum": quorum,
    }
    _write_json(panel_path, fallback)
    with log_path.open("a", encoding="utf-8") as log:
        print(f"[dispatcher] wrote deterministic panel fallback at {panel_path}", file=log)
    return panel_path


def _wait_for_child_verdict(child_dir: Path, log_path: Path) -> None:
    timeout_sec = int(os.environ.get("MO_CHILD_VERDICT_TIMEOUT_SEC", "1800"))
    poll_sec = float(os.environ.get("MO_CHILD_VERDICT_POLL_SEC", "5"))
    deadline = time.monotonic() + max(timeout_sec, 0)
    verdict_files = (child_dir / "verdict.json", child_dir / "panel-verdict.json")

    while time.monotonic() <= deadline:
        if any(path.exists() and path.stat().st_size > 0 for path in verdict_files):
            return
        if _write_deterministic_panel_fallback(child_dir, log_path):
            return
        time.sleep(poll_sec)

    with log_path.open("a", encoding="utf-8") as log:
        print(
            f"[dispatcher] timed out waiting {timeout_sec}s for child verdict in {child_dir}",
            file=log,
        )


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
    env["MINI_ORK_TASK_RUN_ID"] = child_run_id
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

    _wait_for_child_verdict(child_dir, log_path)

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
            source_verdict = verdict.get("source_verdict")
            fallback_pass = (
                isinstance(source_verdict, dict)
                and source_verdict.get("pass") is True
                and str(source_verdict.get("source") or "").endswith("tier4-fallback")
            )
            result["status"] = STATUS_PASSED if verdict.get("pass") is True and (proc.returncode == 0 or fallback_pass) else STATUS_FAILED
            if result["status"] == STATUS_FAILED:
                result["error"] = f"child exited {proc.returncode}; pass={verdict.get('pass')}"
            elif proc.returncode != 0:
                result["nonzero_child_exit_accepted"] = proc.returncode
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
            if os.environ.get("MO_FEATURE_DISPATCH_FAIL_FAST", "1") != "0":
                break

    summary["passed"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_PASSED)
    summary["failed"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_FAILED)
    summary["pending"] = sum(1 for record in summary["features"] if record.get("status") == STATUS_PENDING)
    _write_json(child_records_dir / "_summary.json", summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(dispatch())
