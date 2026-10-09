"""The audit-judge-panel gate must be able to FAIL, and it must grade the
structured sidecars — not just the prose.

Two defects this pins down:

1. The original gate printed ``{"pass": false}`` and then ``sys.exit(0)``
   unconditionally. The runner reads rc, so the panel could never fail.
2. It only grepped the markdown. The two lanes' ``context-*.json`` sidecars had
   drifted to disjoint schemas and nothing noticed, because nothing looked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "audit-judge-panel" / "verifiers" / "panel-completeness.py"

SECTIONS = [
    "Discovery Evidence",
    "Finding Verdicts",
    "Fix Verdicts",
    "Recommended Fix Order",
    "Open Questions",
]


def _report() -> str:
    lines = ["# Judge report\n"]
    for i, sec in enumerate(SECTIONS, 1):
        lines.append(f"## {i}. {sec}\n")
        if sec == "Finding Verdicts":
            for f in range(1, 7):
                lines.append(f"- finding_id: F{f}")
                lines.append("  verdict: confirmed")
                lines.append("  severity: medium")
                lines.append("  evidence_checked: writeback.py:12")
                lines.append("  reasoning: re-derived from the live DB")
        elif sec == "Fix Verdicts":
            for s in range(1, 7):
                lines.append(f"- fix_id: S{s}")
                lines.append(f"  finding_id: F{s}")
                lines.append("  verdict: partially_confirmed")
                lines.append("  severity: low")
        else:
            lines.append("sqlite3 state.db 'SELECT * FROM llm_calls' → 3 rows")
    return "\n".join(lines) + "\n"


def _sidecar(lane: str) -> dict:
    return {
        "schema_version": "1",
        "panel_id": "run-test",
        "lane": lane,
        "model": "some-model",
        "findings": [
            {"finding_id": f"F{i}", "verdict": "confirmed", "severity": "medium",
             "evidence_checked": ["writeback.py:12"], "reasoning": "…",
             "corrections": []}
            for i in range(1, 7)
        ],
        "fixes": [
            {"fix_id": f"S{i}", "finding_id": f"F{i}", "verdict": "partially_confirmed",
             "severity": "low", "reasoning": "…"}
            for i in range(1, 7)
        ],
        "fix_order": [f"S{i}" for i in range(1, 7)],
        "counts": {
            "findings_confirmed": 6, "findings_partially_confirmed": 0,
            "findings_refuted": 0, "findings_unverifiable": 0,
            "fixes_confirmed": 0, "fixes_partially_confirmed": 6,
            "severity_histogram": {"critical": 0, "high": 0, "medium": 6, "low": 0},
        },
        "open_questions": [],
        "read_only_attestation": {"worktree_modified": False, "files_written": []},
    }


def _run(tmp: Path) -> tuple[int, dict]:
    env = dict(os.environ, MINI_ORK_RUN_DIR=str(tmp))
    proc = subprocess.run([sys.executable, str(VERIFIER)], env=env,
                          capture_output=True, text=True)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = {}
    return proc.returncode, out


def _seed(tmp: Path) -> None:
    for lane, node in (("opus", "opus_judge"), ("minimax", "minimax_judge")):
        (tmp / f"judge-{lane}-audit.md").write_text(_report(), encoding="utf-8")
        (tmp / f"context-{node}.json").write_text(
            json.dumps(_sidecar(lane)), encoding="utf-8")
    (tmp / "synthesis.md").write_text(
        "# Synthesis\nopus minimax F1 F2 F3 F4 F5 F6 S1 S2 S3 S4 S5 S6\n",
        encoding="utf-8")


def test_complete_panel_passes(tmp_path):
    _seed(tmp_path)
    rc, out = _run(tmp_path)
    assert rc == 0, out
    assert out["pass"] is True


def test_gate_can_fail(tmp_path):
    """The regression that mattered: a miss must be a NON-ZERO rc, because the
    runner reads rc — a JSON `pass: false` with rc 0 is recorded as a pass."""
    _seed(tmp_path)
    (tmp_path / "synthesis.md").write_text("nothing here", encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert out["pass"] is False
    assert any("synthesis" in m.lower() for m in out["missing"])


def test_divergent_sidecar_schemas_fail(tmp_path):
    _seed(tmp_path)
    obj = _sidecar("minimax")
    obj["finding_verdicts"] = obj.pop("findings")  # the drift that shipped
    (tmp_path / "context-minimax_judge.json").write_text(
        json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert any("sidecar_missing_keys" in m for m in out["missing"])


def test_fix_must_link_to_a_finding(tmp_path):
    """`fixes[*].finding_id` holding a FIX id is the exact defect observed; the
    join on finding_id then returns nothing."""
    _seed(tmp_path)
    obj = _sidecar("opus")
    obj["fixes"][0]["finding_id"] = "S1"
    (tmp_path / "context-opus_judge.json").write_text(
        json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert any("finding_id_not_a_finding" in m for m in out["missing"])


def test_report_sections_must_be_ordered(tmp_path):
    _seed(tmp_path)
    text = _report().replace("## 3. Fix Verdicts", "## 3. Open Questions")
    (tmp_path / "judge-opus-audit.md").write_text(text, encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert any("sections" in m for m in out["missing"])


def test_histogram_must_sum_to_six(tmp_path):
    _seed(tmp_path)
    obj = _sidecar("opus")
    obj["counts"]["severity_histogram"] = {"critical": 0, "high": 0, "medium": 5, "low": 0}
    (tmp_path / "context-opus_judge.json").write_text(
        json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert any("histogram" in m for m in out["missing"])


@pytest.mark.parametrize("bad", ["severe", "P0", ""])
def test_bad_severity_enum_fails(tmp_path, bad):
    _seed(tmp_path)
    obj = _sidecar("minimax")
    obj["findings"][0]["severity"] = bad
    (tmp_path / "context-minimax_judge.json").write_text(
        json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0


# ── the false negative that actually failed a live run ──────────────────────

def test_prose_containing_the_word_verdict_is_not_a_verdict_field(tmp_path):
    """run-1791584660-89421's minimax judge wrote the sentence
    ``**Magnitude verdict:** F4's dollar exposure is small (~$10.72)``.

    The unanchored field regex matched ``verdict:** F4`` inside that prose,
    produced ``invalid_verdict_enum:['F4']``, and failed the whole panel —
    on a report whose twelve real verdict fields were all valid. A field is
    only read from a line that STARTS with the field name."""
    _seed(tmp_path)
    (tmp_path / "judge-minimax-audit.md").write_text(
        _report() + "\n**Magnitude verdict:** F4's dollar exposure is small (~$10.72).\n",
        encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc == 0, out


def test_lane_may_carry_the_node_id(tmp_path):
    """A judge that names itself by node id (``opus_judge``) rather than by
    lane alias (``opus``) is naming the same panel member — the live run wrote
    the node id. Requiring one spelling exactly would fail a valid sidecar."""
    _seed(tmp_path)
    for node in ("opus_judge", "minimax_judge"):
        f = tmp_path / f"context-{node}.json"
        obj = json.loads(f.read_text(encoding="utf-8"))
        obj["lane"] = node
        f.write_text(json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc == 0, out


def test_lane_naming_the_other_panel_member_fails(tmp_path):
    _seed(tmp_path)
    f = tmp_path / "context-opus_judge.json"
    obj = json.loads(f.read_text(encoding="utf-8"))
    obj["lane"] = "minimax"
    f.write_text(json.dumps(obj), encoding="utf-8")
    rc, out = _run(tmp_path)
    assert rc != 0
    assert any("lane_mismatch" in m for m in out["missing"])
