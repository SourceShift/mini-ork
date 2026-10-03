#!/usr/bin/env python3
"""specdir-ingest — pipeline step 1: deterministic spec-directory ingestion.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (specdir_ingest).
Reads the spec dir from the kickoff's `## Spec dir:` line (MINI_ORK_KICKOFF
or the run's kickoff copy) and shells
`bin/mini-ork specs ingest <spec_dir> --out ${MINI_ORK_RUN_DIR}/spec-index.json`.
MO_SDD_SPEC_GLOB (default *.md) selects spec files. Pass iff the command
exits 0, spec-index.json validates against schemas/spec-index.schema.json,
and it lists at least MO_SDD_MIN_SPECS (default 1) specs.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Kickoff lookup, first hit wins: MINI_ORK_KICKOFF, MINI_ORK_KICKOFF_PATH (a
set variable must name an existing file), ${MINI_ORK_RUN_DIR}/kickoff.md, then
run_profile.json's kickoff_path. The spec dir is the inline text after the
`## Spec dir:` heading's colon, else the first non-empty line below it
(backticks, quotes and a bullet marker stripped); a relative dir resolves
against the cwd. No kickoff, no spec-dir line, or a missing dir exits 2. Any
stale spec-index.json is removed before the ingest so only this run's output
is validated. The ingest runs on this interpreter with
MO_SDD_INGEST_TIMEOUT_S (default 300) and a process-group kill on timeout.
MO_SDD_MIN_SPECS below 1 still requires one spec: zero specs never passes.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    OUTPUT_TAIL_CHARS,
    Malformed,
    engine_root,
    load_json,
    positive_float_env,
    run_cmd,
    run_dir,
    specdir_module,
)

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*spec dir\s*:?\s*(.*?)\s*#*\s*$", re.I)
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


def _kickoff_path(rd: Path) -> tuple[Path, str]:
    for var in ("MINI_ORK_KICKOFF", "MINI_ORK_KICKOFF_PATH"):
        raw = os.environ.get(var, "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_file():
                raise Malformed(f"{var} names a missing file: {path}")
            return path, var
    if (rd / "kickoff.md").is_file():
        return rd / "kickoff.md", "run_dir/kickoff.md"
    profile = load_json(rd / "run_profile.json", required=False)
    if isinstance(profile, dict) and isinstance(profile.get("kickoff_path"), str):
        path = Path(profile["kickoff_path"]).expanduser()
        if path.is_file():
            return path, "run_profile.json:kickoff_path"
    raise Malformed("no kickoff found (MINI_ORK_KICKOFF, MINI_ORK_KICKOFF_PATH, "
                    "run_dir/kickoff.md, run_profile.json kickoff_path)")


def _clean(value: str) -> str:
    value = re.sub(r"^[-*+]\s+", "", value.strip())
    return value.strip().strip("`'\"").strip()


def parse_spec_dir(text: str) -> str | None:
    lines = text.splitlines()
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if not m:
            continue
        inline = _clean(m.group(1))
        if inline:
            return inline
        for below in lines[i + 1:]:
            if _ANY_HEADING_RE.match(below):
                return None
            if below.strip() and not below.lstrip().startswith(("```", "~~~")):
                return _clean(below) or None
        return None
    return None


def _min_specs() -> int:
    try:
        return max(1, int(os.environ.get("MO_SDD_MIN_SPECS", "").strip()))
    except ValueError:
        return 1


def body():
    rd = run_dir()
    kickoff, source = _kickoff_path(rd)
    raw = parse_spec_dir(kickoff.read_text(encoding="utf-8", errors="replace"))
    if not raw:
        raise Malformed(f"no '## Spec dir:' line in {kickoff}", kickoff=str(kickoff))
    spec_dir = Path(raw).expanduser()
    if not spec_dir.is_absolute():
        spec_dir = Path.cwd() / spec_dir
    spec_dir = spec_dir.resolve()
    if not spec_dir.is_dir():
        raise Malformed(f"spec dir is not a directory: {spec_dir}", kickoff=str(kickoff))

    root = engine_root()
    launcher = root / "bin" / "mini-ork"
    if not launcher.is_file():
        raise Malformed(f"launcher not found: {launcher}")
    out = rd / "spec-index.json"
    if out.exists():
        out.unlink()
    timeout = positive_float_env("MO_SDD_INGEST_TIMEOUT_S", 300.0)
    res = run_cmd([sys.executable, str(launcher), "specs", "ingest", str(spec_dir), "--out", str(out)],
                  timeout=timeout, env=dict(os.environ), cwd=os.getcwd())
    detail = {"kickoff": str(kickoff), "kickoff_source": source, "spec_dir": str(spec_dir),
              "rc": res["exit_code"], "output_tail": res["output"][-OUTPUT_TAIL_CHARS:],
              "min_specs": _min_specs()}
    if res["error"]:
        return False, f"specs ingest could not start: {res['error']}", detail
    if res["timed_out"]:
        return False, f"specs ingest timed out after {timeout:g}s", detail
    if res["exit_code"] != 0:
        return False, f"specs ingest exited {res['exit_code']}", detail
    if not out.is_file():
        return False, "specs ingest exited 0 but wrote no spec-index.json", detail

    index = load_json(out)
    errors = specdir_module("index").validate_index(index)
    if not errors and not isinstance(index, dict):
        errors = ["<root>: not a JSON object"]
    if errors:
        detail["schema_errors"] = errors[:20]
        return False, "spec-index.json violates spec-index.schema.json", detail
    assert isinstance(index, dict)
    spec_ids = sorted(index["specs"])
    detail.update(spec_count=len(spec_ids), spec_ids=spec_ids)
    if len(spec_ids) < detail["min_specs"]:
        return False, f"{len(spec_ids)} spec(s) indexed, need at least {detail['min_specs']}", detail
    return True, f"{len(spec_ids)} spec(s) indexed from {spec_dir}", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
