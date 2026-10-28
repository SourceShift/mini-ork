"""audit-judge-panel-3l completeness gate: it passes a complete three-lane
panel, reports each judge's served lane/family, and gates a harness-stamped
``served_lane`` that disagrees with ``llm_calls`` (Gap N1).

Same harness style as ``test_audit_judge_panel_4l.py``: the verifier is a
standalone script (it runs its checks at import), so each case builds a
complete panel in ``tmp_path`` and execs it as a subprocess with a scrubbed
environment and a hand-made ``llm_calls`` DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import mini_ork

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "audit-judge-panel-3l" / "verifiers" / "panel-completeness.py"

# (lane, node id, report name); laguna's sidecar is embedded in its report.
LANES = [
    ("opus", "opus_judge", "judge-opus-audit.md"),
    ("minimax", "minimax_judge", "judge-minimax-audit.md"),
    ("laguna", "laguna_lens", "lens-laguna.md"),
]
FINDING_IDS = [f"F{i}" for i in range(1, 7)]
FIX_IDS = [f"S{i}" for i in range(1, 7)]
DISTINCT_CALLS = [("opus_lens", "opus"), ("minimax_lens", "minimax"),
                  ("laguna_lens", "laguna")]


def _sidecar(lane: str, served_lane: str | None) -> dict:
    obj = {
        "schema_version": "1",
        "panel_id": "run-3l",
        "lane": lane,
        "model": lane,
        "findings": [
            {"finding_id": fid, "verdict": "confirmed", "severity": "high",
             "evidence_checked": "llm_calls", "reasoning": "r", "corrections": []}
            for fid in FINDING_IDS
        ],
        "fixes": [
            {"fix_id": sid, "finding_id": "F1", "verdict": "confirmed",
             "severity": "medium", "reasoning": "r"}
            for sid in FIX_IDS
        ],
        "fix_order": list(FIX_IDS),
        "counts": {"severity_histogram":
                   {"critical": 0, "high": 6, "medium": 0, "low": 0}},
        "open_questions": [],
        "read_only_attestation": {"worktree_modified": False},
    }
    if served_lane is not None:
        obj["served_lane"] = served_lane
    return obj


def _report(lane: str) -> list[str]:
    lines = [f"# {lane} judge audit", "", "## 1. Discovery Evidence", ""]
    lines += [f"- sqlite3 -readonly state.db \"SELECT {i} FROM llm_calls\""
              for i in range(4)]
    lines += ["", "## 2. Finding Verdicts", ""]
    for fid in FINDING_IDS:
        lines += [f"### {fid}", "- verdict: confirmed", "- severity: high", ""]
    lines += ["## 3. Fix Verdicts", ""]
    for sid in FIX_IDS:
        lines += [f"### {sid}", "- verdict: partially_confirmed",
                  "- severity: medium", ""]
    lines += ["## 4. Recommended Fix Order", ""]
    lines += [f"{i}. {sid}" for i, sid in enumerate(FIX_IDS, 1)]
    lines += ["", "## 5. Open Questions", ""]
    lines += [f"- {lane} filler note {i}, plain prose." for i in range(10)]
    return lines


def _make_panel(run_dir: Path, served_lane_by_node: dict[str, str]) -> None:
    for lane, node, report in LANES:
        lines = _report(lane)
        if report == "lens-laguna.md":
            lines += ["", "```json",
                      json.dumps(_sidecar(lane, served_lane_by_node.get(node)),
                                 indent=2),
                      "```"]
        (run_dir / report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        if report != "lens-laguna.md":
            (run_dir / f"context-{node}.json").write_text(
                json.dumps(_sidecar(lane, served_lane_by_node.get(node)), indent=2),
                encoding="utf-8")
    synth = ["# Panel synthesis", "", "## Matrix", ""]
    synth += [f"- {lane} covered every finding." for lane, _n, _r in LANES]
    synth += [f"- {fid} and {sid} agreed."
              for fid, sid in zip(FINDING_IDS, FIX_IDS)]
    (run_dir / "synthesis.md").write_text("\n".join(synth) + "\n", encoding="utf-8")


def _calls_db(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE llm_calls (run_id TEXT, actor TEXT, "
                "model_id TEXT, status TEXT)")
    con.executemany("INSERT INTO llm_calls VALUES ('run-3l', ?, ?, 'success')",
                    DISTINCT_CALLS)
    con.commit()
    con.close()
    return path


def _run(run_dir: Path, db: Path | None = None) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update({
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_RUN_ID": "run-3l",
        "MINI_ORK_DB": str(db or run_dir / "absent.db"),
        "MINI_ORK_HOME": str(run_dir / "home"),
        "PYTHONPATH": str(REPO),
    })
    proc = subprocess.run([sys.executable, str(VERIFIER)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert "{" in proc.stdout, proc.stderr[-800:]
    return proc.returncode, json.loads(proc.stdout[proc.stdout.index("{"):])


def test_complete_panel_passes(tmp_path):
    _make_panel(tmp_path, {})
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 0, out["missing"]


def test_verdict_reports_served_lanes_and_families(tmp_path):
    from mini_ork.verify.panel_heterogeneity import lane_family

    served = {node: lane for lane, node, _r in LANES}
    _make_panel(tmp_path, served)
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 0, out["missing"]
    for lane, node, _report in LANES:
        assert out["served"][node] == [lane]
        assert out["families"][node] == [lane_family(lane)]


def test_sidecar_served_lane_mismatch_fails(tmp_path):
    served = {node: lane for lane, node, _r in LANES}
    served["opus_judge"] = "minimax"  # artifact claims minimax; llm_calls: opus
    _make_panel(tmp_path, served)
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 1
    assert "sidecar_served_mismatch:opus_judge" in out["missing"]


def test_unstamped_sidecar_is_not_a_mismatch(tmp_path):
    _make_panel(tmp_path, {})
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 0, out["missing"]
    assert not any("sidecar_served_mismatch" in str(item) for item in out["missing"])


def test_one_stamped_judge_keeps_cross_lane_parity(tmp_path):
    """Only judges whose dispatch left a lane record get the harness keys, so
    parity must ignore them (review 151)."""
    served = {LANES[0][1]: LANES[0][0]}  # stamp the first judge only
    _make_panel(tmp_path, served)
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 0, out["missing"]
    assert not any("cross_lane_top_level_keys_differ" in str(m) for m in out["missing"])


def test_a_judge_written_key_difference_still_breaks_parity(tmp_path):
    _make_panel(tmp_path, {})
    node = LANES[1][1]
    path = tmp_path / f"context-{node}.json"
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj["extra_judge_key"] = 1
    path.write_text(json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path, _calls_db(tmp_path / "state.db"))
    assert rc == 1
    assert any("cross_lane_top_level_keys_differ" in str(m) for m in out["missing"])


def test_verifier_is_the_recipe_copy():
    assert mini_ork.__file__  # import sanity: the shim resolves the package
    assert VERIFIER.is_file()
