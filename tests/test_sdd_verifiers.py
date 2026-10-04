"""spec-driven-delivery verifiers (SDD K3): the eight deterministic gates under
recipes/spec-driven-delivery/verifiers/, exercised as real subprocesses against
synthetic ${MINI_ORK_RUN_DIR} trees built in tmp_path.

Every run asserts the executor contract: exactly one JSON line on stdout,
nothing on stderr, exit 0/1/2 matching the verdict. Probes are sleep-based or
trivial (no CPU-bound loops). Nothing here touches tests/fixtures or the real
.mini-ork state.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_ork.specdir import build_index, scan_specdir, validate_card, write_index

REPO = Path(__file__).resolve().parents[1]
VERIFIERS = REPO / "recipes" / "spec-driven-delivery" / "verifiers"
NODES = ("specdir-ingest", "spec-lint", "ratification-check", "test-validity",
         "dispatch-aggregator", "smoke-live", "ui-craft-gate", "ledger-writer")

SPEC_TEXT = """# Feature export

Depends on: none

## Inputs

A feature flag file in the repo root.

## Acceptance criteria

- AC1: `cat feature.txt` prints `feature ready`.
- AC2: the specs directory exists before work starts.

```bash
cat feature.txt
```
"""

FEATURE_PROBE = "test -f feature.txt && cat feature.txt"


# ── harness ───────────────────────────────────────────────────────────────


def run_verifier(node: str, run_dir: Path, cwd: Path, **env_overrides: str):
    """Run one verifier like the executor does; return (exit_code, payload, seconds)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MO_SDD_") and k not in ("MINI_ORK_KICKOFF", "MINI_ORK_KICKOFF_PATH")}
    env.update({
        "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_ENGINE_ROOT": str(REPO),
        # Keep the launcher (specdir-ingest's `bin/mini-ork specs ingest`) on
        # THIS interpreter, as tests/test_specdir_ingest.py does.
        "MINI_ORK_USE_VENV": "0",
    })
    env.update(env_overrides)
    start = time.monotonic()
    proc = subprocess.run([sys.executable, str(VERIFIERS / f"{node}.py")], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - start
    assert proc.stderr == "", f"{node} wrote to stderr: {proc.stderr}"
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, f"{node} must print exactly one line, got: {proc.stdout!r}"
    payload = json.loads(lines[0])
    assert isinstance(payload["pass"], bool) and isinstance(payload["reason"], str)
    assert proc.returncode in ((0,) if payload["pass"] else (1, 2)), (proc.returncode, payload)
    return proc.returncode, payload, elapsed


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


class Project:
    """A target repo with one spec, plus a run dir carrying the artifacts each
    pipeline stage would have written by the time its verifier runs."""

    SPEC_ID = "feature-export"

    def __init__(self, tmp: Path, *, ui_craft: bool = False, ui_gate: bool = False):
        self.target = tmp / "target"
        self.specs = self.target / "specs"
        self.run = tmp / "run"
        self.run.mkdir(parents=True)
        self.specs.mkdir(parents=True)
        self.spec = self.specs / f"{self.SPEC_ID}.md"
        self.spec.write_text(SPEC_TEXT, encoding="utf-8")
        self.ui_craft = ui_craft
        self.ui_gate = ui_gate

    # stage artifacts -----------------------------------------------------
    def kickoff(self, spec_dir_line: str | None = None) -> Path:
        line = spec_dir_line if spec_dir_line is not None else f"`{self.specs}`"
        path = self.run / "kickoff.md"
        path.write_text(f"# Kickoff\n\n## Spec dir:\n\n{line}\n\n## Hard rules\n\n- none\n", encoding="utf-8")
        return path

    def index(self) -> Path:
        return write_index(build_index(scan_specdir(self.specs), self.specs), self.run / "spec-index.json")

    def card(self, **overrides) -> dict:
        acceptance = [
            {"id": "AC1", "clause_refs": ["F1"], "text": "cat feature.txt prints feature ready",
             "gate": {"kind": "cmd", "probe": FEATURE_PROBE, "expect": "^feature ready$"}},
            {"id": "AC2", "clause_refs": ["C1"], "text": "the specs directory exists",
             "gate": {"kind": "cmd", "probe": "test -d specs", "expect": "exit 0"}},
        ]
        deliverables = [{"id": "D1", "title": "write the feature file", "acceptance_refs": ["AC1", "AC2"],
                         "depends_on": []}]
        if self.ui_gate:
            acceptance.append({"id": "AC3", "clause_refs": ["F1", "C1"], "text": "the page renders",
                               "gate": {"kind": "ui", "probe": "/feature h1", "expect": "Feature"}})
            deliverables.append({"id": "D2", "title": "render the page", "acceptance_refs": ["AC3"],
                                 "depends_on": ["D1"]})
        card = {
            "spec_id": self.SPEC_ID, "source_path": str(self.spec.resolve()),
            "source_hash": sha(self.spec), "title": "Feature export",
            "clauses": {"functional": [{"id": "F1", "text": "prints feature ready"}], "quality": [],
                        "constitutional": [{"id": "C1", "text": "specs dir exists"}], "architectural": []},
            "acceptance": acceptance, "deliverables": deliverables,
            "ui_craft": {"required": self.ui_craft,
                         "design_sources": ["design/feature.html"] if self.ui_craft else []},
            "status": "draft",
        }
        card.update(overrides)
        assert validate_card(card) == [] or overrides, validate_card(card)
        dump(self.run / "spec-cards" / f"{self.SPEC_ID}.json", card)
        return card

    def ratification(self, entries=()) -> Path:
        return dump(self.run / "ratification" / f"{self.SPEC_ID}.json",
                    {"spec_id": self.SPEC_ID, "source_hash": sha(self.spec), "ratification": list(entries)})

    def gates(self, probes: list[dict] | None = None, unprobeable=()) -> Path:
        if probes is None:
            probes = [probe("AC1", FEATURE_PROBE, "^feature ready$", deliverables=["D1"]),
                      probe("AC2", "test -d specs", "exit 0", tags=["precondition"], deliverables=["D1"])]
            if self.ui_gate:
                probes.append(probe("AC3", "/feature h1", "Feature", kind="ui", deliverables=["D2"]))
        return dump(self.run / "gates" / f"{self.SPEC_ID}.json",
                    {"spec_id": self.SPEC_ID, "source_hash": sha(self.spec), "probes": probes,
                     "unprobeable": list(unprobeable)})

    def implement(self, content: str = "feature ready\n") -> None:
        (self.target / "feature.txt").write_text(content, encoding="utf-8")

    def child(self, deliverable_id: str = "D1", *, verdict=None, status: str = "delivered",
              commit: str = "abc1234", **record_overrides) -> dict:
        child_dir = self.run.parent / "children" / f"run-child-{deliverable_id}"
        child_dir.mkdir(parents=True, exist_ok=True)
        verdict_path = child_dir / "panel-verdict.json"
        if verdict is None:
            verdict = {"verifier": "tier4-panel-quorum", "pass": True, "verdict": "pass"}
        verdict_path.write_text(verdict if isinstance(verdict, str) else json.dumps(verdict), encoding="utf-8")
        dump(child_dir / "implementer-summary.json", {"commit": commit, "touched_files": ["feature.txt"]})
        record = {"deliverable_id": deliverable_id, "spec_id": self.SPEC_ID,
                  "child_kickoff": str(self.run / "child-kickoffs" / f"{self.SPEC_ID}--{deliverable_id}.md"),
                  "child_run_id": child_dir.name, "child_run_dir": str(child_dir), "status": status,
                  "verdict_path": str(verdict_path), "ask_path": None}
        record.update(record_overrides)
        return record

    def dispatch(self, *records: dict) -> Path:
        return dump(self.run / "dispatch-results.json", {"deliverables": list(records)})

    def verify(self, node: str, **env: str):
        return run_verifier(node, self.run, self.target, **env)


def probe(ref: str, command: str, expect: str, *, kind: str = "cmd", tags=(), deliverables=("D1",)) -> dict:
    return {"gate_id": ref, "acceptance_ref": ref, "deliverable_refs": list(deliverables), "kind": kind,
            "probe": command, "expect": expect, "tags": list(tags), "fails_today_because": "not built"}


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(tmp_path)


def ratified(project: Project) -> Project:
    project.index()
    project.card()
    project.ratification()
    project.gates()
    return project


# ── 1. end-to-end pass path ──────────────────────────────────────────────


def test_end_to_end_pass_path(project):
    project.kickoff()
    code, out, _ = project.verify("specdir-ingest")
    assert (code, out["pass"], out["spec_ids"]) == (0, True, ["feature-export"]), out
    assert out["kickoff_source"] == "run_dir/kickoff.md"

    code, out, _ = project.verify("spec-lint")
    assert code == 0 and out["error_count"] == 0, out
    assert isinstance(json.loads((project.run / "spec-lint.json").read_text()), list)

    project.card()
    project.ratification()
    project.gates()
    code, out, _ = project.verify("ratification-check")
    assert code == 0 and out["specs"]["feature-export"]["ok"], out

    code, out, _ = project.verify("test-validity")
    assert code == 0, out
    statuses = {p["acceptance_ref"]: p["status"]
                for p in json.loads((project.run / "test-validity.json").read_text())["probes"]}
    assert statuses == {"AC1": "FAILS_TODAY", "AC2": "PRECONDITION_OK"}

    project.implement()
    project.dispatch(project.child("D1"))
    code, out, _ = project.verify("dispatch-aggregator")
    assert code == 0 and (out["total"], out["delivered"], out["pass_rate"]) == (1, 1, 1.0), out
    agg = json.loads((project.run / "aggregate-verdict.json").read_text())
    assert set(agg) == {"total", "delivered", "failed", "blocked", "pending", "pass_rate", "deliverables"}
    assert agg["deliverables"][0]["commit"] == "abc1234"

    code, out, _ = project.verify("smoke-live")
    assert code == 0 and out["counts"] == {"total": 2, "passed": 2, "failed": 0, "waived": 0}, out

    code, out, _ = project.verify("ui-craft-gate")
    assert code == 0 and out["reason"] == "no ui_craft cards", out

    code, out, _ = project.verify("ledger-writer")
    assert code == 0, out
    rows = [json.loads(line) for line in (project.run / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == out["rows_written"] == 2
    assert {(r["gate_id"], r["clause_id"], r["deliverable_id"], r["verdict"]) for r in rows} == {
        ("AC1", "F1", "D1", "PASSED"), ("AC2", "C1", "D1", "PASSED")}
    assert all(r["child_run_id"] == "run-child-D1" and r["commit"] == "abc1234" for r in rows)
    assert out["gauges"]["waived_gates"] == out["gauges"]["failed_gates"] == 0


# ── 2. spec drift ────────────────────────────────────────────────────────


def test_spec_drift_fails_ratification(project):
    ratified(project)
    assert project.verify("ratification-check")[0] == 0
    data = bytearray(project.spec.read_bytes())
    data[2] ^= 0x01  # flip one byte of the source after carding
    project.spec.write_bytes(bytes(data))
    code, out, _ = project.verify("ratification-check")
    assert code == 1
    assert any("spec drift" in v for v in out["specs"]["feature-export"]["violations"]), out


# ── 3. unacknowledged ratification ───────────────────────────────────────


@pytest.mark.parametrize("acknowledged, expected", [(False, 1), (True, 0)])
def test_unacknowledged_ratification(project, acknowledged, expected):
    ratified(project)
    project.ratification([{"source_excerpt": "must be fast", "reason": "untestable",
                           "acknowledged": acknowledged}])
    code, out, _ = project.verify("ratification-check")
    assert code == expected, out
    if not acknowledged:
        assert "unacknowledged uncovered requirement" in out["reason"]


def test_ratification_coverage_and_missing_record(project):
    ratified(project)
    card = project.card()
    card["deliverables"][0]["acceptance_refs"] = ["AC1", "AC9"]
    dump(project.run / "spec-cards" / "feature-export.json", card)
    (project.run / "ratification" / "feature-export.json").unlink()
    code, out, _ = project.verify("ratification-check")
    violations = out["specs"]["feature-export"]["violations"]
    assert code == 1
    assert any("acceptance_ref 'AC9' does not resolve" in v for v in violations)
    assert any("'AC2' is not referenced by any deliverable" in v for v in violations)
    assert any("ratification record: missing" in v for v in violations)


def test_ratification_noise_covered_by_clause_is_reclassified(project):
    ratified(project)
    project.ratification([{"source_excerpt": "Specs  dir exists.", "reason": "untestable",
                           "acknowledged": False}])
    code, out, _ = project.verify("ratification-check")
    assert code == 0, out
    spec = out["specs"]["feature-export"]
    assert spec["ok"] and spec["violations"] == [], out
    assert spec["reclassified_covered"] == [
        {"index": 0, "source_excerpt": "Specs  dir exists.", "clause_id": "C1"}]
    assert out["reclassified_covered_count"] == 1 and "stderr_tail" not in out
    record = json.loads((project.run / "ratification" / "feature-export.json").read_text())
    assert record["ratification"][0]["acknowledged"] is False  # never written back


def test_ratification_genuine_gap_still_fails(project):
    ratified(project)
    project.ratification([{"source_excerpt": "Exports finish within two seconds.",
                           "reason": "untestable", "acknowledged": False}])
    code, out, _ = project.verify("ratification-check")
    assert code == 1, out
    assert "unacknowledged uncovered requirement" in out["reason"]
    assert out["reclassified_covered_count"] == 0


@pytest.mark.parametrize("entry", [
    {"source_excerpt": "", "reason": "ambiguous", "acknowledged": False},
    {"source_excerpt": "specs", "reason": "ambiguous", "acknowledged": False},
    {"source_excerpt": "dir exist", "reason": "ambiguous", "acknowledged": False},
    {"source_excerpt": "specs dir exists and is writable", "reason": "ambiguous", "acknowledged": False},
    {"source_excerpt": "specs dir exists", "reason": "conflicts_with:C1 — C1 says otherwise",
     "acknowledged": False},
    "specs dir exists",
], ids=["empty", "too-short", "split-word", "clause-inside-excerpt", "conflict", "non-dict"])
def test_ratification_backstop_guards_still_fail(project, entry):
    ratified(project)
    project.ratification([entry])
    code, out, _ = project.verify("ratification-check")
    assert code == 1 and "unacknowledged uncovered requirement" in out["reason"], out
    assert "reclassified_covered" not in out["specs"]["feature-export"]
    assert out["reclassified_covered_count"] == 0


def test_ratification_mixed_record_fails_only_on_the_gap(project):
    ratified(project)
    project.ratification([
        {"source_excerpt": "`prints feature ready`", "reason": "untestable", "acknowledged": False},
        {"source_excerpt": "Exports finish within two seconds.", "reason": "untestable",
         "acknowledged": False},
    ])
    code, out, _ = project.verify("ratification-check")
    spec = out["specs"]["feature-export"]
    assert code == 1, out
    assert spec["violations"] == [
        "unacknowledged uncovered requirement [1]: 'Exports finish within two seconds.'"]
    assert [(r["index"], r["clause_id"]) for r in spec["reclassified_covered"]] == [(0, "F1")]


# ── 4. vacuous probes ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_probe, needle", [
    (probe("AC1", "test -d specs", "exit 0"), "passes on the untouched tree"),
    (probe("AC1", "true", "^feature ready$"), "vacuous probe"),
    (probe("AC1", "  exit 0 ; ", "^feature ready$"), "vacuous probe"),
    (probe("AC1", FEATURE_PROBE, ".*"), "satisfied by any output"),
])
def test_vacuous_probe_fails_test_validity(project, bad_probe, needle):
    ratified(project)
    project.gates([bad_probe, probe("AC2", "test -d specs", "exit 0", tags=["precondition"])])
    code, out, _ = project.verify("test-validity")
    assert code == 1, out
    assert needle in json.dumps(out["specs"]), out


def test_precondition_probe_failing_now_fails(project):
    ratified(project)
    project.gates([probe("AC1", FEATURE_PROBE, "^feature ready$"),
                   probe("AC2", "test -d no-such-dir", "exit 0", tags=["precondition"])])
    code, out, _ = project.verify("test-validity")
    assert code == 1 and "precondition probe does not pass now" in out["reason"], out


def test_test_validity_coverage_unprobeable_and_kind(project):
    ratified(project)
    project.gates([probe("AC1", FEATURE_PROBE, "^feature ready$", kind="contract")],
                  unprobeable=[{"acceptance_ref": "AC2", "reason": "no surface"}])
    code, out, _ = project.verify("test-validity")
    text = json.dumps(out["specs"])
    assert code == 1
    assert "kind 'contract' != card gate kind 'cmd'" in text
    assert "'AC2' is unprobeable" in text and "'AC2' has 0 probes" in text


# ── 5. aggregator never trusts the dispatcher ────────────────────────────


@pytest.mark.parametrize("case", ["no_child_dir", "missing_child_dir", "empty_verdict",
                                  "verdict_outside_child", "child_says_fail", "omitted"])
def test_aggregator_catches_claimed_delivered_without_child_evidence(project, tmp_path, case):
    ratified(project)
    if case == "no_child_dir":
        rec = project.child("D1", child_run_dir=None, verdict_path=None)
    elif case == "missing_child_dir":
        rec = project.child("D1", child_run_dir=str(tmp_path / "nope"))
    elif case == "empty_verdict":
        rec = project.child("D1", verdict="")
    elif case == "verdict_outside_child":
        forged = dump(tmp_path / "forged.json", {"pass": True})
        rec = project.child("D1", verdict_path=str(forged))
    elif case == "child_says_fail":
        rec = project.child("D1", verdict={"verdict": "REQUEST_CHANGES"})
    else:
        rec = None
    project.dispatch(*([rec] if rec else []))
    code, out, _ = project.verify("dispatch-aggregator")
    assert code == 1 and out["delivered"] == 0, out
    row = json.loads((project.run / "aggregate-verdict.json").read_text())["deliverables"][0]
    assert row["status"] != "delivered"
    if rec:
        assert row["claimed_status"] == "delivered" and row["mismatch"] is True
        assert out["mismatches"], out


def test_aggregator_accepts_log_prefixed_verdict(project):
    ratified(project)
    verdict = "[tier4] quorum check\n" + json.dumps({"verifier": "tier4-panel-quorum", "pass": True}) + "\n"
    project.dispatch(project.child("D1", verdict=verdict))
    code, out, _ = project.verify("dispatch-aggregator")
    assert code == 0 and out["delivered"] == 1, out


@pytest.mark.parametrize("with_ask, expected_status", [(False, "failed"), (True, "blocked")])
def test_aggregator_blocked_needs_ask(project, with_ask, expected_status):
    ratified(project)
    ask = project.run / "asks" / "feature-export--D1.json"
    if with_ask:
        dump(ask, {"spec_id": "feature-export", "deliverable_id": "D1", "question": "which base URL?"})
    project.dispatch({"deliverable_id": "D1", "spec_id": "feature-export", "child_kickoff": "k.md",
                      "child_run_id": None, "child_run_dir": None, "status": "blocked",
                      "verdict_path": None, "ask_path": str(ask)})
    code, out, _ = project.verify("dispatch-aggregator")
    assert code == 1, out
    assert out[expected_status] == 1
    assert out["mismatches"] if not with_ask else not out["mismatches"]


# ── 6. smoke-live ────────────────────────────────────────────────────────


def test_smoke_expect_mismatch_fails(project):
    ratified(project)
    project.implement("feature broken\n")
    code, out, _ = project.verify("smoke-live")
    assert code == 1 and out["counts"]["failed"] == 1, out
    gate = next(g for g in out["gates"] if g["gate_id"] == "AC1")
    assert gate["status"] == "FAILED" and "does not satisfy expect" in gate["reason"]


def test_smoke_timeout_kills_process_group(project):
    ratified(project)
    project.gates([probe("AC1", "(sleep 30 &); sleep 30", "never"),
                   probe("AC2", "test -d specs", "exit 0", tags=["precondition"])])
    code, out, elapsed = project.verify("smoke-live", MO_SDD_SMOKE_TIMEOUT_S="1")
    assert code == 1, out
    gate = next(g for g in out["gates"] if g["gate_id"] == "AC1")
    assert (gate["status"], gate["reason"]) == ("FAILED", "timeout")
    assert elapsed < 4.5, f"timeout did not kill the probe's process group ({elapsed:.1f}s)"


def test_smoke_missing_probe_fails_and_surface_env_loads(project, tmp_path):
    ratified(project)
    env_file = tmp_path / "surface.env"
    env_file.write_text("# surface\nexport SDD_BASE='ready'\n", encoding="utf-8")
    project.gates([probe("AC1", 'printf "feature %s\\n" "$SDD_BASE"', "^feature ready$")])
    code, out, _ = project.verify("smoke-live", MO_SDD_SURFACE_ENV=str(env_file))
    statuses = {g["gate_id"]: (g["status"], g["reason"]) for g in out["gates"]}
    assert code == 1
    assert statuses["AC1"][0] == "PASSED"
    assert statuses["AC2"] == ("FAILED", "no probe for this acceptance id")


def test_smoke_ui_probe_waived_without_template(tmp_path):
    project = ratified_ui(tmp_path)
    project.implement()
    code, out, _ = project.verify("smoke-live")
    assert code == 0 and out["counts"]["waived"] == 1 and out["counts"]["passed"] == 2, out
    assert next(g for g in out["gates"] if g["gate_id"] == "AC3")["status"] == "WAIVED"

    code, out, _ = project.verify("smoke-live", MO_SDD_UI_PROBE_CMD="printf 'Feature at %s\\n' {probe}")
    assert code == 0 and out["counts"] == {"total": 3, "passed": 3, "failed": 0, "waived": 0}, out


def ratified_ui(tmp_path: Path, *, ui_craft: bool = False) -> Project:
    project = Project(tmp_path, ui_craft=ui_craft, ui_gate=True)
    return ratified(project)


# ── 7. ui-craft gate ─────────────────────────────────────────────────────


def test_ui_craft_waived_path_counts_in_ledger(tmp_path):
    project = Project(tmp_path, ui_craft=True)
    ratified(project)
    project.implement()
    project.dispatch(project.child("D1"))
    assert project.verify("dispatch-aggregator")[0] == 0
    assert project.verify("smoke-live")[0] == 0
    code, out, _ = project.verify("ui-craft-gate", MO_SDD_UI_GATE_CMD="")
    assert code == 0 and out["pass"] is True and out["waived"] is True, out
    code, out, _ = project.verify("ledger-writer")
    assert code == 0 and out["gauges"]["waived_gates"] == 1, out
    rows = [json.loads(line) for line in (project.run / "ledger.jsonl").read_text().splitlines()]
    assert {"gate_id": "ui_craft", "verdict": "WAIVED", "waived": True}.items() <= \
        next(r for r in rows if r["gate_id"] == "ui_craft").items()


@pytest.mark.parametrize("cmd, expected", [
    ("test {spec_id} = feature-export && test {design_sources} = design/feature.html", 0),
    ("exit 3", 1),
])
def test_ui_craft_runs_template(tmp_path, cmd, expected):
    project = Project(tmp_path, ui_craft=True)
    ratified(project)
    code, out, _ = project.verify("ui-craft-gate", MO_SDD_UI_GATE_CMD=cmd)
    assert code == expected, out
    assert out["waived"] is False


# ── 8. ledger gauges ─────────────────────────────────────────────────────


def test_ledger_gauge_counts_and_append(tmp_path):
    project = Project(tmp_path, ui_craft=True, ui_gate=True)
    ratified(project)
    dump(project.run / "smoke-live.json", {"gates": [
        {"spec_id": "feature-export", "gate_id": "AC1", "acceptance_ref": "AC1", "status": "PASSED"},
        {"spec_id": "feature-export", "gate_id": "AC2", "acceptance_ref": "AC2", "status": "FAILED"},
        {"spec_id": "feature-export", "gate_id": "AC3", "acceptance_ref": "AC3", "status": "WAIVED"},
    ]})
    dump(project.run / "aggregate-verdict.json", {"total": 2, "deliverables": [
        {"spec_id": "feature-export", "deliverable_id": "D1", "child_run_id": "run-a", "commit": "abc1234"},
        {"spec_id": "feature-export", "deliverable_id": "D2", "child_run_id": None, "commit": None}]})
    dump(project.run / "ui-craft.json", {"specs": [{"spec_id": "feature-export", "status": "WAIVED"}]})
    dump(project.run / "asks" / "feature-export--D2.json", {"question": "which route?"})
    dump(project.run / "asks" / "feature-export--D9.json", {"question": "old", "resolved": True})
    (project.run / "asks" / "broken.json").write_text("{not json", encoding="utf-8")
    dump(project.run / "vetoes" / "v1.json", {"spec_id": "feature-export", "deliverable_id": "D2",
                                               "reason": "operator veto"})

    code, out, _ = project.verify("ledger-writer")
    assert code == 0, out  # non-zero gauges still pass
    gauges = out["gauges"]
    assert (gauges["waived_gates"], gauges["failed_gates"], gauges["asks_open"], gauges["vetoes"]) == (2, 1, 2, 1)
    assert gauges["spec_edit_ratio"] is None
    # AC1 x F1 x D1, AC2 x C1 x D1, AC3 x {F1, C1} x D2, ui_craft, veto: the
    # waived AC3 spans two rows but counts as one gate
    assert out["rows_written"] == 6
    slide = json.loads((project.run / "slide-back.json").read_text())
    assert slide["waived_gates"] == 2 and slide["asks_open"] == 2

    project.verify("ledger-writer")
    lines = (project.run / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 12  # appended, never truncated
    first = [json.loads(line) for line in lines[:6]]
    assert {"spec_id", "clause_id", "deliverable_id", "child_run_id", "commit", "gate_id", "verdict",
            "waived", "ts"} == set(first[0])
    ac1 = next(r for r in first if r["gate_id"] == "AC1")
    assert (ac1["child_run_id"], ac1["commit"], ac1["waived"]) == ("run-a", "abc1234", False)


def test_ledger_corrupt_input_is_malformed(project):
    ratified(project)
    (project.run / "smoke-live.json").write_text("{", encoding="utf-8")
    dump(project.run / "aggregate-verdict.json", {"deliverables": []})
    code, out, _ = project.verify("ledger-writer")
    assert code == 2 and out["reason"].startswith("malformed input"), out


# ── fail closed: missing inputs and empty sets ───────────────────────────


@pytest.mark.parametrize("node", NODES)
def test_missing_run_dir_is_malformed(tmp_path, node):
    code, out, _ = run_verifier(node, tmp_path / "absent", tmp_path)
    assert code == 2 and "MINI_ORK_RUN_DIR" in out["reason"], out


@pytest.mark.parametrize("node", ["ratification-check", "test-validity", "dispatch-aggregator",
                                  "smoke-live", "ui-craft-gate", "ledger-writer"])
def test_empty_card_set_never_passes(project, node):
    project.index()
    (project.run / "spec-cards").mkdir()
    dump(project.run / "dispatch-results.json", {"deliverables": []})
    code, out, _ = project.verify(node)
    assert code == 2 and out["pass"] is False and "no SpecCards" in out["reason"], out


@pytest.mark.parametrize("node, artifact", [
    ("spec-lint", "spec-index.json"),
    ("dispatch-aggregator", "dispatch-results.json"),
    ("smoke-live", "gates/feature-export.json"),
    ("ledger-writer", "smoke-live.json"),
])
def test_missing_required_artifact_is_malformed(project, node, artifact):
    ratified(project)
    project.dispatch(project.child("D1"))
    (project.run / artifact).unlink(missing_ok=True)
    assert not (project.run / artifact).exists()
    code, out, _ = project.verify(node)
    assert code == 2 and out["reason"].startswith("malformed input"), out


def test_specdir_ingest_without_spec_dir_line_is_malformed(project):
    project.kickoff(spec_dir_line="")
    code, out, _ = project.verify("specdir-ingest")
    assert code == 2 and "Spec dir" in out["reason"], out


def test_specdir_ingest_env_kickoff_and_lint_error(project, tmp_path):
    (project.specs / "broken.md").write_text("# Broken\n\nNo criteria here.\n", encoding="utf-8")
    kickoff = tmp_path / "k.md"
    kickoff.write_text(f"# K\n\n## Spec dir: `{project.specs}`\n", encoding="utf-8")
    code, out, _ = project.verify("specdir-ingest", MINI_ORK_KICKOFF=str(kickoff))
    assert code == 1 and out["rc"] == 1 and out["kickoff_source"] == "MINI_ORK_KICKOFF", out
    # the ingest still wrote an index; spec-lint pins the error-severity finding
    code, out, _ = project.verify("spec-lint")
    assert code == 1 and out["error_codes"] == ["NO_ACCEPTANCE"], out


# ── ratification: acceptance-coverage reclassification (2026-10-04) ──────


def test_ratification_ac_label_prefix_reclassified(project):
    """An 'AC1: <the AC's own text>' entry is compiler noise, not a gap."""
    project.index()
    card = project.card()
    ac1 = card["acceptance"][0]["text"]
    project.ratification([{"source_excerpt": f"AC1: {ac1}.", "reason": "listed by compiler"}])
    code, out, _ = run_verifier("ratification-check", project.run, project.target)
    assert code == 0 and out["pass"], out
    assert out["reclassified_covered_count"] == 1, out
    entry = out["specs"][project.SPEC_ID]["reclassified_covered"][0]
    assert entry["acceptance_id"] == "AC1", entry


def test_ratification_ac_reference_tokens_reclassified(project):
    """An entry that names which ACs enforce it (all resolving) is covered."""
    project.index()
    project.card()
    project.ratification([{"source_excerpt":
                           "FE test asserting the flip (AC1) and the exclusivity rule (AC2).",
                           "reason": "outputs line"}])
    code, out, _ = run_verifier("ratification-check", project.run, project.target)
    assert code == 0 and out["pass"], out
    assert out["reclassified_covered_count"] == 1, out


def test_ratification_conflict_never_reclassified_by_acceptance(project):
    """A conflicts_with entry fails even when its text IS a card AC."""
    project.index()
    card = project.card()
    ac1 = card["acceptance"][0]["text"]
    project.ratification([{"source_excerpt": f"AC1: {ac1}.",
                           "reason": "conflicts_with:C1 — contradictory condition"}])
    code, out, _ = run_verifier("ratification-check", project.run, project.target)
    assert code == 1 and not out["pass"], out
