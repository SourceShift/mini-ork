"""audit-judge-panel-4l: the workflow compiles, and its completeness gate both
passes a complete four-lane panel and FAILS each way a panel can be hollow —
a missing lane, a non-enum verdict, ids that are not the kickoff's, and two
judges served by the same model."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "audit-judge-panel-4l"
VERIFIER = RECIPE / "verifiers" / "panel-completeness.py"
LANES = [("opus", "opus_judge"), ("glm", "glm_judge"),
         ("deepseek", "deepseek_judge"), ("minimax", "minimax_judge")]

KICKOFF = """# Judge these

## Scope
- F9 is not a finding: this section is not a findings section.

## Findings

### F1 — first problem
Detail.

- **F2** — second problem

```
### F7 — inside a fence, never an id
```

## Proposed fixes

- S1 (F1): fix the first
- **S2** (F2): fix the second
"""


def _load_verifier(monkeypatch, run_dir: Path):
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    spec = importlib.util.spec_from_file_location("panel4l", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(lane: str) -> str:
    lines = [f"# Judge {lane}", "", "## 1. Discovery Evidence"]
    lines += [f"- sqlite3 -readonly state.db \"SELECT {i} FROM code_findings\"" for i in range(5)]
    lines += ["- mini_ork/context_v2.py:389 read", "### Cross-Finding Observations", "- shared root"]
    lines += ["", "## 2. Finding Verdicts"]
    for fid in ("F1", "F2"):
        lines += [f"### {fid}", "- verdict: confirmed", "- severity: high",
                  "- reasoning: measured", ""]
    lines += ["## 3. New Findings", "none found; searched context_assembler.py", ""]
    lines += ["## 4. Fix Verdicts"]
    for sid in ("S1", "S2"):
        lines += [f"### {sid}", "- verdict: partially_confirmed", "- severity: low", ""]
    lines += ["## 5. Proposed Solutions", "- P1 addresses F1", "",
              "## 6. Recommended Fix Order", "- S1", "- S2", "",
              "## 7. Open Questions", "- none", ""]
    lines += [f"filler {i}" for i in range(10)]
    return "\n".join(lines) + "\n"


def _sidecar(lane: str) -> dict:
    return {
        "schema_version": "1", "panel_id": "run-x", "lane": lane, "model": f"{lane}-model",
        "findings": [{"finding_id": f, "verdict": "confirmed", "severity": "high",
                      "evidence_checked": ["q"], "reasoning": "r", "corrections": []}
                     for f in ("F1", "F2")],
        "new_findings": [{"new_finding_id": "N1", "severity": "medium",
                          "evidence_checked": ["q"], "reasoning": "r"}],
        "fixes": [{"fix_id": "S1", "finding_id": "F1", "verdict": "confirmed",
                   "severity": "low", "reasoning": "r"},
                  {"fix_id": "S2", "finding_id": ["F2"], "verdict": "refuted",
                   "severity": "medium", "reasoning": "r"}],
        "solutions": [{"solution_id": "P1", "addresses": ["F1", "N1"], "summary": "s",
                       "files": [], "test": "t", "risk": "r", "effort": "S"}],
        "fix_order": ["S2", "S1"],
        "counts": {"severity_histogram": {"critical": 0, "high": 2, "medium": 0, "low": 0}},
        "open_questions": [],
        "read_only_attestation": {"worktree_modified": False, "files_written": []},
    }


SYNTHESIS = "\n".join([
    "## 1. Panel Verdict Matrix", "opus glm deepseek minimax F1 F2 S1 S2",
    "## 2. Consensus Findings", "## 3. Dissents", "## 4. Corrected Numbers",
    "## 5. New Findings", "## 6. Solution Plan", "## 7. Questions For Human Decision", ""])


def _make_panel(run_dir: Path) -> None:
    kickoff = run_dir / "kickoff.md"
    kickoff.write_text(KICKOFF, encoding="utf-8")
    (run_dir / "run_profile.json").write_text(json.dumps({"kickoff_path": str(kickoff)}))
    for lane, node in LANES:
        (run_dir / f"judge-{lane}-audit.md").write_text(_report(lane), encoding="utf-8")
        (run_dir / f"context-{node}.json").write_text(json.dumps(_sidecar(lane)))
    (run_dir / "synthesis.md").write_text(SYNTHESIS, encoding="utf-8")


def _run(run_dir: Path, db: Path | None = None) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK", "MO_"))}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    env["MINI_ORK_RUN_ID"] = "run-x"
    env["MINI_ORK_DB"] = str(db or run_dir / "absent.db")
    proc = subprocess.run([sys.executable, str(VERIFIER)], env=env,
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout)


def _calls_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE llm_calls (run_id TEXT, actor TEXT, model_id TEXT, status TEXT)")
    con.executemany("INSERT INTO llm_calls VALUES ('run-x', ?, ?, 'success')", rows)
    con.commit()
    con.close()
    return path


def test_workflow_compiles_with_four_parallel_judges():
    from mini_ork.workflow.compiler import compile_workflow

    wf = compile_workflow(str(RECIPE / "workflow.yaml"))
    judges = [n for n in wf.nodes if n.endswith("_judge")]
    assert sorted(judges) == sorted(node for _l, node in LANES)
    for _lane, node in LANES:
        assert (RECIPE / "prompts" / f"{node.split('_')[0]}-judge.md").is_file()


def test_declared_ids_reads_only_findings_and_fix_sections(monkeypatch, tmp_path):
    module = _load_verifier(monkeypatch, tmp_path)
    assert module.declared_ids(KICKOFF) == (["F1", "F2"], ["S1", "S2"])


def test_complete_panel_passes(tmp_path):
    _make_panel(tmp_path)
    rc, out = _run(tmp_path)
    assert rc == 0, out["missing"]


def test_missing_lane_fails(tmp_path):
    _make_panel(tmp_path)
    (tmp_path / "judge-glm-audit.md").unlink()
    (tmp_path / "context-glm_judge.json").unlink()
    rc, out = _run(tmp_path)
    assert rc == 1
    assert "MISSING_REPORT:judge-glm-audit.md" in out["missing"]
    assert "MISSING_SIDECAR:context-glm_judge.json" in out["missing"]


def test_non_enum_verdict_and_wrong_ids_fail(tmp_path):
    _make_panel(tmp_path)
    side = _sidecar("deepseek")
    side["findings"][0]["verdict"] = "mostly"
    side["fixes"] = side["fixes"][:1]
    (tmp_path / "context-deepseek_judge.json").write_text(json.dumps(side))
    rc, out = _run(tmp_path)
    assert rc == 1
    assert "deepseek:finding_F1_bad_verdict:mostly" in out["missing"]
    assert any(m.startswith("deepseek:fixes_not_kickoff_ids") for m in out["missing"])


def test_wrapped_prose_and_fenced_snippets_are_not_fields(tmp_path):
    # The first live run (learning-judge-4l-20261015125313) failed on glm's
    # wrapped continuation "  severity=None path for prose" read as a field.
    _make_panel(tmp_path)
    report = tmp_path / "judge-glm-audit.md"
    text = report.read_text(encoding="utf-8").replace(
        "- reasoning: measured",
        "- `evidence_checked`: `normalize_severity` `:153-168`; `_make_finding`\n"
        "  severity=None path for prose; `min_severity()` default\n"
        "```\nverdict: maybe\n```\n- reasoning: measured", 1)
    report.write_text(text, encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc == 0, out["missing"]


def test_extra_sidecar_keys_are_allowed_but_missing_ones_fail(tmp_path):
    _make_panel(tmp_path)
    side = _sidecar("deepseek")
    side.update({"head_commit": "17b89cd9", "kickoff_path": "kickoffs/auto/x.md"})
    (tmp_path / "context-deepseek_judge.json").write_text(json.dumps(side))
    rc, out = _run(tmp_path)
    assert rc == 0, out["missing"]

    del side["solutions"]
    (tmp_path / "context-deepseek_judge.json").write_text(json.dumps(side))
    rc, out = _run(tmp_path)
    assert rc == 1
    assert "deepseek:sidecar_missing_keys:['solutions']" in out["missing"]


def test_two_judges_served_by_one_model_fails(tmp_path):
    _make_panel(tmp_path)
    db = _calls_db(tmp_path / "state.db", [
        ("opus_lens", "glm"), ("glm_lens", "glm"),
        ("deepseek_lens", "deepseek_flash"), ("minimax_lens", "minimax")])
    rc, out = _run(tmp_path, db)
    assert rc == 1
    assert "served_models:opus_lens_and_glm_lens_both_served_by:glm" in out["missing"]


@pytest.mark.parametrize("rows", [[], [("opus_lens", "opus55"), ("glm_lens", "glm"),
                                       ("deepseek_lens", "deepseek_flash"),
                                       ("minimax_lens", "minimax")]])
def test_distinct_or_unlogged_models_pass(tmp_path, rows):
    _make_panel(tmp_path)
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db", rows))
    assert rc == 0, out["missing"]
