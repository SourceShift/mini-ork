"""spec-driven-delivery e2e dry-run (SDD K4): the deterministic spine on the real
on-disk fixture under tests/fixtures/sdd_e2e, with no LLM lane.

A tmp copy of the fixture target repo goes through the real CLI (`bin/mini-ork
specs ingest|lint`) and every deterministic verifier in workflow order:
specdir-ingest -> ratification-check -> spec-lint -> test-validity ->
dispatch-aggregator -> smoke-live -> ui-craft-gate -> ledger-writer. The LLM
nodes (contract_compiler, test_author, per_spec_dispatcher) are replaced by the
artifacts they would write: the golden SpecCard, an empty ratification record,
gates derived from the card, and a synthetic child run whose commit is a real
git sha in the tmp target.

Probes are real `python3 -m pytest` runs inside the tmp target. A PATH shim
pins `python3` to this interpreter (bare python3 may be an older system
interpreter; a symlink would drop a venv, so the shim is an exec wrapper),
PYTHONSAFEPATH=1 mirrors the engine's child env, and git runs with no
global/system config or hooks. Verifiers are asserted against the executor
contract: one JSON line on stdout, nothing on stderr, exit 0 iff pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.specdir import validate_card

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "sdd_e2e"
SPEC = FIXTURE / "specs" / "multiply-feature.md"
GOLDEN = FIXTURE / "golden" / "multiply-feature.spec-card.json"
BIN = REPO / "bin" / "mini-ork"
VERIFIERS = REPO / "recipes" / "spec-driven-delivery" / "verifiers"
SPEC_ID = "multiply-feature"
PRECONDITIONS = frozenset({"AC3"})
MULTIPLY = "\n\ndef multiply(a, b):\n    return a * b\n"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache")
GIT = ("git", "-c", "user.name=sdd-e2e", "-c", "user.email=sdd-e2e@example.invalid",
       "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main")


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def gates_for(card: dict) -> dict:
    """test_author's output, derived 1:1 from the card's acceptance gates."""
    probes = [{"gate_id": a["id"], "acceptance_ref": a["id"],
               "deliverable_refs": [d["id"] for d in card["deliverables"] if a["id"] in d["acceptance_refs"]],
               "kind": a["gate"]["kind"], "probe": a["gate"]["probe"], "expect": a["gate"]["expect"],
               "tags": ["precondition"] if a["id"] in PRECONDITIONS else [],
               "fails_today_because": "precondition: passes before work starts" if a["id"] in PRECONDITIONS
               else "calc.multiply does not exist yet"}
              for a in card["acceptance"]]
    return {"spec_id": card["spec_id"], "source_hash": card["source_hash"], "probes": probes,
            "unprobeable": []}


class Spine:
    """tmp copies of the fixture target repo + spec dir, a run dir, and the
    hermetic env every subprocess (CLI, verifier, probe, git) runs with."""

    def __init__(self, tmp: Path):
        self.target, self.specs, self.run = tmp / "target", tmp / "specs", tmp / "run"
        shutil.copytree(FIXTURE / "target", self.target, ignore=IGNORE)
        shutil.copytree(FIXTURE / "specs", self.specs, ignore=IGNORE)
        self.run.mkdir()
        shim = tmp / "shim"
        shim.mkdir()
        python3 = shim / "python3"
        python3.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MO_SDD_", "PYTEST_", "GIT_"))
               and k not in ("MINI_ORK_KICKOFF", "MINI_ORK_KICKOFF_PATH")}
        env.update({
            "PATH": f"{shim}{os.pathsep}{env.get('PATH', '')}",
            "MINI_ORK_ROOT": str(REPO), "MINI_ORK_ENGINE_ROOT": str(REPO), "MINI_ORK_USE_VENV": "0",
            "MINI_ORK_RUN_DIR": str(self.run), "MO_SDD_SMOKE_TIMEOUT_S": "60",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })
        self.env = env
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "baseline: add() only")

    # subprocesses ---------------------------------------------------------
    def git(self, *args: str) -> str:
        proc = subprocess.run([*GIT, *args], cwd=self.target, env=self.env, capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN), *args], cwd=self.target, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def verify(self, node: str, **extra: str) -> tuple[int, dict]:
        """Run one verifier like the executor does: cwd = target repo."""
        proc = subprocess.run([sys.executable, str(VERIFIERS / f"{node}.py")], cwd=self.target,
                              env={**self.env, **extra}, capture_output=True, text=True, timeout=300)
        assert proc.stderr == "", f"{node} wrote to stderr: {proc.stderr}"
        lines = proc.stdout.splitlines()
        assert len(lines) == 1, f"{node} must print exactly one JSON line, got: {proc.stdout!r}"
        payload = json.loads(lines[0])
        assert isinstance(payload["pass"], bool) and isinstance(payload["reason"], str), payload
        assert proc.returncode in ((0,) if payload["pass"] else (1, 2)), (proc.returncode, payload)
        return proc.returncode, payload

    # stage artifacts ------------------------------------------------------
    def ingest(self) -> dict:
        """`specs ingest` via the real CLI; returns the one index entry."""
        proc = self.cli("specs", "ingest", str(self.specs), "--out", str(self.run / "spec-index.json"))
        assert proc.returncode == 0, proc.stderr
        index = json.loads((self.run / "spec-index.json").read_text(encoding="utf-8"))
        assert sorted(index["specs"]) == [SPEC_ID], index
        return index["specs"][SPEC_ID]

    def kickoff(self) -> Path:
        path = self.run / "kickoff.md"
        path.write_text(f"# Deliver the multiply spec\n\n## Spec dir:\n\n`{self.specs}`\n", encoding="utf-8")
        return path

    def contract(self, entry: dict, card: dict | None = None, gates: dict | None = None) -> dict:
        """contract_compiler + test_author stand-ins: card, ratification, gates."""
        card = card or golden()
        assert card["source_hash"] == entry["source_hash"], "golden card is stale for the fixture spec"
        card["source_path"] = entry["source_path"]  # from the index: survives /var -> /private/var
        dump(self.run / "spec-cards" / f"{SPEC_ID}.json", card)
        dump(self.run / "ratification" / f"{SPEC_ID}.json",
             {"spec_id": SPEC_ID, "source_hash": card["source_hash"], "ratification": []})
        dump(self.run / "gates" / f"{SPEC_ID}.json", gates or gates_for(card))
        return card

    def implement(self, body: str = MULTIPLY) -> str:
        """The child run's work: multiply() lands as a real commit; returns its sha."""
        calc = self.target / "calc.py"
        calc.write_text(calc.read_text(encoding="utf-8") + body, encoding="utf-8")
        self.git("commit", "-q", "-am", "feat: calc.multiply")
        return self.git("rev-parse", "HEAD")

    def dispatch(self, commit: str) -> Path:
        """per_spec_dispatcher stand-in: one delivered child run for D1."""
        child = self.run.parent / "children" / "run-child-D1"
        verdict = dump(child / "panel-verdict.json",
                       {"verifier": "tier4-panel-quorum", "pass": True, "verdict": "pass"})
        dump(child / "implementer-summary.json", {"commit": commit, "touched_files": ["calc.py"]})
        return dump(self.run / "dispatch-results.json", {"deliverables": [{
            "deliverable_id": "D1", "spec_id": SPEC_ID,
            "child_kickoff": str(self.run / "child-kickoffs" / f"{SPEC_ID}--D1.md"),
            "child_run_id": child.name, "child_run_dir": str(child), "status": "delivered",
            "verdict_path": str(verdict), "ask_path": None}]})


@pytest.fixture
def spine(tmp_path) -> Spine:
    return Spine(tmp_path)


def statuses(rows: list[dict], key: str = "acceptance_ref") -> dict[str, str]:
    return {r[key]: r["status"] for r in rows}


# ── fixture guards ────────────────────────────────────────────────────────


def test_golden_card_matches_fixture_spec():
    card = golden()
    assert card["source_hash"] == sha(SPEC), "fixture spec changed: regenerate the golden source_hash"
    assert card["spec_id"] == SPEC.stem == SPEC_ID
    assert validate_card({**card, "source_path": str(SPEC)}) == []
    assert "def multiply" not in (FIXTURE / "target" / "calc.py").read_text(encoding="utf-8")


def test_fixture_stays_tiny():
    files = [p for p in FIXTURE.rglob("*") if p.is_file()
             and not {"__pycache__", ".pytest_cache"} & set(p.relative_to(FIXTURE).parts)]
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)
    assert lines < 200, f"sdd_e2e fixture is {lines} lines; keep it under 200"


# ── the spine, end to end ─────────────────────────────────────────────────


def test_dryrun_spine_end_to_end(spine):
    entry = spine.ingest()
    proc = spine.cli("specs", "lint", str(spine.specs), "--json")
    assert proc.returncode == 0 and json.loads(proc.stdout) == [], proc.stdout

    # specdir_ingest: the kickoff's `## Spec dir:` line drives the same ingest
    spine.kickoff()
    code, out = spine.verify("specdir-ingest")
    assert (code, out["spec_ids"], out["kickoff_source"]) == (0, [SPEC_ID], "run_dir/kickoff.md"), out
    entry = json.loads((spine.run / "spec-index.json").read_text(encoding="utf-8"))["specs"][SPEC_ID]

    card = spine.contract(entry)
    code, out = spine.verify("ratification-check")
    assert code == 0 and out["specs"][SPEC_ID] == {"ok": True, "violations": []}, out

    code, out = spine.verify("spec-lint")
    assert code == 0 and (out["error_count"], out["warning_count"]) == (0, 0), out

    # test_validity: AC1/AC2 fail on the untouched tree, the AC3 precondition passes
    code, out = spine.verify("test-validity")
    assert code == 0, out
    rows = json.loads((spine.run / "test-validity.json").read_text(encoding="utf-8"))["probes"]
    assert statuses(rows) == {"AC1": "FAILS_TODAY", "AC2": "FAILS_TODAY", "AC3": "PRECONDITION_OK"}

    # negative arm: the live surface is red before implementation
    code, out = spine.verify("smoke-live")
    assert code == 1 and out["counts"] == {"total": 3, "passed": 1, "failed": 2, "waived": 0}, out
    assert statuses(out["gates"], "gate_id") == {"AC1": "FAILED", "AC2": "FAILED", "AC3": "PASSED"}

    commit = spine.implement()
    spine.dispatch(commit)
    code, out = spine.verify("dispatch-aggregator")
    assert code == 0 and (out["total"], out["delivered"], out["pass_rate"]) == (1, 1, 1.0), out
    agg = json.loads((spine.run / "aggregate-verdict.json").read_text(encoding="utf-8"))
    assert (agg["deliverables"][0]["commit"], agg["deliverables"][0]["mismatch"]) == (commit, False)

    code, out = spine.verify("smoke-live")
    assert code == 0 and out["counts"] == {"total": 3, "passed": 3, "failed": 0, "waived": 0}, out

    code, out = spine.verify("ui-craft-gate")
    assert code == 0 and out["reason"] == "no ui_craft cards", out

    code, out = spine.verify("ledger-writer")
    assert code == 0 and out["rows_written"] == 3, out
    ledger = [json.loads(line) for line in (spine.run / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(r["gate_id"], r["clause_id"], r["deliverable_id"], r["verdict"]) for r in ledger} == {
        ("AC1", "F1", "D1", "PASSED"), ("AC2", "F2", "D1", "PASSED"), ("AC3", "C1", "D1", "PASSED")}
    assert len(ledger) == 3
    assert all((r["child_run_id"], r["commit"], r["waived"]) == ("run-child-D1", commit, False) for r in ledger)
    slide = json.loads((spine.run / "slide-back.json").read_text(encoding="utf-8"))
    assert (slide["waived_gates"], slide["failed_gates"], slide["asks_open"], slide["vetoes"],
            slide["rows_written"], slide["spec_edit_ratio"]) == (0, 0, 0, 0, 3, None)
    assert {a["id"] for a in card["acceptance"]} == {r["gate_id"] for r in ledger}


# ── mutants: each one must turn exactly the guarding gate red ─────────────


def _drop_precondition_tag(card: dict) -> dict:
    gates = gates_for(card)
    for probe in gates["probes"]:
        probe["tags"] = []
    return gates


def _unreference_ac3(card: dict) -> dict:
    card["deliverables"][0]["acceptance_refs"] = ["AC1", "AC2"]
    return card


def test_mutant_wrong_multiply_fails_smoke(spine):
    spine.contract(spine.ingest())
    spine.implement("\n\ndef multiply(a, b):\n    return a + b\n")
    code, out = spine.verify("smoke-live")
    assert code == 1 and statuses(out["gates"], "gate_id") == {"AC1": "FAILED", "AC2": "FAILED",
                                                               "AC3": "PASSED"}, out


def test_mutant_spec_drift_fails_ratification(spine):
    spine.contract(spine.ingest())
    spec = spine.specs / f"{SPEC_ID}.md"
    data = bytearray(spec.read_bytes())
    data[2] ^= 0x01
    spec.write_bytes(bytes(data))
    code, out = spine.verify("ratification-check")
    assert code == 1 and any("spec drift" in v for v in out["specs"][SPEC_ID]["violations"]), out


def test_mutant_dropped_precondition_tag_fails_test_validity(spine):
    entry = spine.ingest()
    spine.contract(entry, gates=_drop_precondition_tag(golden()))
    code, out = spine.verify("test-validity")
    rows = json.loads((spine.run / "test-validity.json").read_text(encoding="utf-8"))["probes"]
    assert code == 1 and statuses(rows)["AC3"] == "VACUOUS", out


def test_mutant_unreferenced_acceptance_fails_ratification(spine):
    entry = spine.ingest()
    spine.contract(entry, card=_unreference_ac3(golden()))
    code, out = spine.verify("ratification-check")
    assert code == 1, out
    assert "acceptance 'AC3' is not referenced by any deliverable" in out["specs"][SPEC_ID]["violations"]
