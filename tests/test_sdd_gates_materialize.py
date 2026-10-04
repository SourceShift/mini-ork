"""gates-materialize.py — deterministic gate extraction from spec bash fences.

Run as a real subprocess against synthetic ${MINI_ORK_RUN_DIR} trees, like
tests/test_sdd_verifiers.py does for its siblings.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERIFIER = REPO / "recipes" / "spec-driven-delivery" / "verifiers" / "gates-materialize.py"


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def run_materialize(run_dir: Path, cwd: Path):
    env = dict(os.environ)
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    proc = subprocess.run([sys.executable, str(VERIFIER)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout
    return proc.returncode, json.loads(lines[0])


def make_spec(tmp: Path, sid: str, text: str, acceptance: list[dict]) -> Path:
    spec = tmp / "specs" / f"{sid}.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(text, encoding="utf-8")
    run = tmp / "run"
    card = {
        "spec_id": sid, "source_path": str(spec.resolve()), "source_hash": sha(spec),
        "title": sid, "clauses": {"functional": [{"id": "F1", "text": "t"}], "quality": [],
                                  "constitutional": [], "architectural": []},
        "acceptance": acceptance,
        "deliverables": [{"id": "D1", "title": "d", "depends_on": [],
                          "acceptance_refs": [a["id"] for a in acceptance]}],
        "ui_craft": {"required": False, "design_sources": []}, "status": "draft",
    }
    dump(run / "spec-cards" / f"{sid}.json", card)
    return run


def ac(aid: str, kind: str = "cmd", text: str = "does the thing", probe: str = "p", expect: str = "e") -> dict:
    return {"id": aid, "clause_refs": ["F1"], "text": text,
            "gate": {"kind": kind, "probe": probe, "expect": expect}}


def gates_of(run: Path, sid: str) -> dict:
    return json.loads((run / "gates" / f"{sid}.json").read_text())


def test_labeled_fences_map_per_ac_with_prelude(tmp_path):
    text = """# S

## Acceptance criteria

- AC1: lists
- AC2: counts

```bash
BASE=http://x
# AC1 — list
curl $BASE/list && echo "AC1 pass"
# AC2 — count
curl $BASE/count && echo "AC2 pass"
```
"""
    run = make_spec(tmp_path, "s-lab", text, [ac("AC1"), ac("AC2")])
    code, out = run_materialize(run, tmp_path)
    assert code == 0 and out["pass"], out
    g = gates_of(run, "s-lab")
    by = {p["acceptance_ref"]: p for p in g["probes"]}
    assert "curl $BASE/list" in by["AC1"]["probe"] and "curl $BASE/count" not in by["AC1"]["probe"]
    assert by["AC1"]["probe"].startswith("set -euo pipefail")  # fail-fast header first
    assert "BASE=http://x" in by["AC1"]["probe"]              # then the prelude
    assert by["AC1"]["expect"] == "AC1\\ pass" or "AC1" in by["AC1"]["expect"]
    assert g["unprobeable"] == []


def test_labeled_segment_is_failfast(tmp_path):
    """A mid-segment failure must not fall through to the trailing pass echo.

    Regression for the live 2026-10-04 s11-lead-outcome-marks-api defect: the
    labeled-segment path carried no `set -e`, so a PATCH that 404'd (route not
    yet implemented) did not stop the segment — AC1's undo assertion
    (`not booked_at`) is satisfied by the 404 body, the trailing
    `echo "AC1 pass"` fired, and the gate reported PASS against a route that
    did not exist. Any gate green on vacuous evidence silences the whole
    campaign's verification, so this is asserted behaviourally, not
    structurally.
    """
    text = """# S

## Acceptance criteria

- AC1: patches

```bash
# AC1
false
echo "AC1 pass"
```
"""
    run = make_spec(tmp_path, "s-vac", text, [ac("AC1")])
    code, out = run_materialize(run, tmp_path)
    assert code == 0 and out["pass"], out
    probe = gates_of(run, "s-vac")["probes"][0]["probe"]
    assert probe.startswith("set -euo pipefail")
    r = subprocess.run(["bash", "-c", probe], capture_output=True, text=True)
    assert r.returncode != 0, r.stdout
    assert "AC1 pass" not in r.stdout


def test_unlabeled_fence_serves_every_cmd_ac(tmp_path):
    text = "# S\n\n```bash\n./verify.sh && echo \"all pass\"\n```\n"
    run = make_spec(tmp_path, "s-unlab", text, [ac("AC1"), ac("AC2")])
    code, out = run_materialize(run, tmp_path)
    assert code == 0 and out["pass"], out
    g = gates_of(run, "s-unlab")
    assert len(g["probes"]) == 2
    assert all(p["probe"] == 'set -euo pipefail\n./verify.sh && echo "all pass"' for p in g["probes"])
    assert all("all" in p["expect"] for p in g["probes"])


def test_ui_ac_uses_agent_browser_fence(tmp_path):
    text = "# S\n\n```bash\nagent-browser open \"$SDD_FE_BASE/en/x\" && agent-browser snapshot -i | grep -q 'x-y-z'\n```\n"
    run = make_spec(tmp_path, "s-uifence", text, [ac("AC1", kind="ui")])
    code, out = run_materialize(run, tmp_path)
    assert code == 0 and out["pass"], out
    g = gates_of(run, "s-uifence")
    assert "agent-browser open" in g["probes"][0]["probe"]
    assert g["probes"][0]["probe"].startswith("set -euo pipefail")


def test_ui_template_from_author_literals(tmp_path):
    text = '# S\n\nThe page at `/en/audience` renders it.\n'
    run = make_spec(tmp_path, "s-uitpl", text,
                    [ac("AC2", kind="ui", text='renders data-testid="audience-share-card"')])
    code, out = run_materialize(run, tmp_path)
    assert code == 0 and out["pass"], out
    g = gates_of(run, "s-uitpl")
    ui = [p for p in g["probes"] if p["kind"] == "ui"][0]
    assert "audience-share-card" in ui["probe"] and "/en/audience" in ui["probe"]
    assert ui["provenance"] == "spec-literal-template"


def test_ui_without_tokens_is_unprobeable(tmp_path):
    text = "# S\n\nNothing executable here.\n"
    run = make_spec(tmp_path, "s-uibad", text, [ac("AC1", kind="ui", text="looks nice")])
    code, out = run_materialize(run, tmp_path)
    assert code == 1 and not out["pass"], out
    assert "UI_TOKENS_MISSING" in out["reason"]
    g = gates_of(run, "s-uibad")
    assert g["unprobeable"][0]["acceptance_ref"] == "AC1"


def test_source_drift_fails(tmp_path):
    text = "# S\n\n```bash\necho x\n```\n"
    run = make_spec(tmp_path, "s-drift", text, [ac("AC1")])
    spec = tmp_path / "specs" / "s-drift.md"
    spec.write_text(text + "\nmore\n", encoding="utf-8")
    code, out = run_materialize(run, tmp_path)
    assert code == 1 and "drifted" in out["reason"], out


def test_precondition_tag_from_ac_text(tmp_path):
    text = "# S\n\n```bash\necho ok\n```\n"
    run = make_spec(tmp_path, "s-pre", text,
                    [ac("AC1", text="precondition: the add tests already pass")])
    code, out = run_materialize(run, tmp_path)
    assert code == 0, out
    g = gates_of(run, "s-pre")
    assert g["probes"][0]["tags"] == ["precondition"]


def test_no_cards_is_malformed(tmp_path):
    run = tmp_path / "run"
    (run / "spec-cards").mkdir(parents=True)
    code, out = run_materialize(run, tmp_path)
    assert code == 2 and not out["pass"], out
