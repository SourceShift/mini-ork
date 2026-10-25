"""Probes must never report "no gaps" when they failed to look.

The two bugs this file exists to prevent:

* a crashed/timed-out probe collapsing into ``ok`` (so a broken environment reads
  as a healthy repo), and
* pytest's "no tests collected" (rc 5) being interpreted as "tests failed".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mini_ork.rsi import probes
from mini_ork.rsi.probes import (
    Finding,
    ProbeResult,
    ProbeSpec,
    default_specs,
    normalize_message,
    parse_garden,
    parse_pytest,
    parse_validate,
    run_probe,
    run_probes,
)


def _runner(rc: int, stdout: str = "", stderr: str = ""):
    calls: list[dict] = []

    def run(argv, *, timeout_s, cwd):
        calls.append({"argv": list(argv), "timeout_s": timeout_s, "cwd": cwd})
        return rc, stdout, stderr

    return run, calls


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

GARDEN_TEXT = """\
[error]   output collision: 'judge-minimax-audit.md' used by a,b
          Fix: use recipe-specific output paths
[warning] recipe workflow exceeds 16 KiB: /abs/root/recipes/x/workflow.yaml (20 KiB)
[info]    run directory older than 30 days: /abs/root/.mini-ork/runs/run-1
"""


def test_garden_parser_keeps_errors_and_warnings_but_drops_info():
    findings = parse_garden(GARDEN_TEXT)
    assert [f.level for f in findings] == ["error", "warning"]
    # The info line is a stale run directory: it says nothing about the commit
    # under test, so admitting it would make every differential gate noisy.
    assert not any("run directory" in f.message for f in findings)


def test_garden_parser_extracts_an_embedded_path():
    findings = parse_garden(GARDEN_TEXT)
    assert findings[1].path == "recipes/x/workflow.yaml"


def test_validate_parser_ignores_the_summary_line():
    text = "[error]   lane 'kimi' is unhealthy: 401\nvalidate: 1 error(s), 0 warning(s)\n"
    findings = parse_validate(text)
    assert len(findings) == 1
    assert findings[0].level == "error"


def test_pytest_parser_reads_failed_and_error_lines():
    text = (
        "FAILED tests/unit/test_a.py::test_x - AssertionError: nope\n"
        "ERROR tests/unit/test_b.py::test_y\n"
        "======= 1 failed, 1 error in 0.10s =======\n"
    )
    findings = parse_pytest(text)
    assert [(f.path, f.level) for f in findings] == [
        ("tests/unit/test_a.py", "failure"),
        ("tests/unit/test_b.py", "error"),
    ]
    assert findings[0].message == "AssertionError: nope"


def test_finding_signature_is_root_independent():
    a = Finding("error", "collision in /Volumes/one/repo/recipes/x.md")
    b = Finding("error", "collision in /Volumes/two/repo/recipes/x.md")
    assert a.signature() == b.signature()


def test_finding_signature_changes_with_level_message_or_path():
    base = Finding("error", "boom", "a.py")
    assert base.signature() != Finding("warning", "boom", "a.py").signature()
    assert base.signature() != Finding("error", "other", "a.py").signature()
    assert base.signature() != Finding("error", "boom", "b.py").signature()


def test_normalize_message_compresses_whitespace_and_paths():
    assert normalize_message("a   /x/y/run-1\n b") == "a <path> b"


# --------------------------------------------------------------------------
# Spec validation
# --------------------------------------------------------------------------

def test_spec_requires_argv_or_fn():
    with pytest.raises(ValueError, match="neither argv nor fn"):
        ProbeSpec("empty")


def test_spec_rejects_unknown_policy():
    with pytest.raises(ValueError, match="unknown probe policy"):
        ProbeSpec("p", argv=("x",), policy="yolo")


def test_result_rejects_unknown_status():
    with pytest.raises(ValueError, match="unknown probe status"):
        ProbeResult("p", "cmd", "fine", 0, 1)


# --------------------------------------------------------------------------
# Classification — the load-bearing part
# --------------------------------------------------------------------------

def test_rc_zero_with_no_findings_is_ok(tmp_path):
    run, _ = _runner(0)
    spec = ProbeSpec("p", argv=("x",), policy="reported")
    result = run_probe(spec, root=tmp_path, runner=run)
    assert result.status == "ok" and result.rc == 0


def test_reported_nonzero_with_parsed_findings_is_findings(tmp_path):
    run, _ = _runner(1, "[error]   boom\n")
    spec = ProbeSpec("p", argv=("x",), policy="reported", parser="garden")
    assert run_probe(spec, root=tmp_path, runner=run).status == "findings"


def test_reported_nonzero_without_findings_is_error_not_ok(tmp_path):
    # The tool said something went wrong and we could not read why. Calling
    # that "ok" is exactly the silent-success bug.
    run, _ = _runner(2, "")
    spec = ProbeSpec("p", argv=("x",), policy="reported", parser="garden")
    result = run_probe(spec, root=tmp_path, runner=run)
    assert result.status == "error" and result.rc == 2


def test_ok_only_nonzero_is_error(tmp_path):
    run, _ = _runner(1, "whatever")
    spec = ProbeSpec("p", argv=("x",), policy="ok_only")
    assert run_probe(spec, root=tmp_path, runner=run).status == "error"


def test_pytest_no_tests_collected_is_error_not_findings(tmp_path):
    run, _ = _runner(5, "no tests ran\n")
    spec = ProbeSpec("p", argv=("pytest",), policy="pytest", parser="pytest")
    assert run_probe(spec, root=tmp_path, runner=run).status == "error"


def test_pytest_failures_are_findings(tmp_path):
    run, _ = _runner(1, "FAILED tests/unit/test_a.py::test_x - boom\n")
    spec = ProbeSpec("p", argv=("pytest",), policy="pytest", parser="pytest")
    result = run_probe(spec, root=tmp_path, runner=run)
    assert result.status == "findings"
    assert result.findings[0].path == "tests/unit/test_a.py"


def test_timeout_is_its_own_status_and_preserves_required(tmp_path):
    def boom(argv, *, timeout_s, cwd):
        raise subprocess.TimeoutExpired(cmd="x", timeout=timeout_s)

    spec = ProbeSpec("p", argv=("x",), required=True)
    result = run_probe(spec, root=tmp_path, runner=boom)
    assert (result.status, result.rc, result.required) == ("timeout", None, True)


def test_oserror_is_an_error_status(tmp_path):
    def boom(argv, *, timeout_s, cwd):
        raise FileNotFoundError("no such binary")

    result = run_probe(ProbeSpec("p", argv=("x",)), root=tmp_path, runner=boom)
    assert result.status == "error"
    assert "no such binary" in result.payload["error"]


def test_stderr_findings_are_parsed_too(tmp_path):
    run, _ = _runner(1, "", "[error]   on stderr\n")
    spec = ProbeSpec("p", argv=("x",), policy="reported", parser="garden")
    assert len(run_probe(spec, root=tmp_path, runner=run).findings) == 1


def test_runner_receives_root_as_cwd_and_the_spec_timeout(tmp_path):
    run, calls = _runner(0)
    run_probe(ProbeSpec("p", argv=("x", "y"), timeout_s=42), root=tmp_path, runner=run)
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["timeout_s"] == 42
    assert calls[0]["argv"] == ["x", "y"]


def test_duration_is_recorded_as_an_integer(tmp_path):
    run, _ = _runner(0)
    assert isinstance(run_probe(ProbeSpec("p", argv=("x",)), root=tmp_path, runner=run).duration_ms, int)


# --------------------------------------------------------------------------
# In-process probes
# --------------------------------------------------------------------------

def test_fn_probe_maps_flags_to_findings(tmp_path):
    spec = ProbeSpec("verifier-audit", fn=lambda: {"flags": ["gate_hackable", {"message": "collapse"}]})
    result = run_probe(spec, root=tmp_path)
    assert result.status == "findings"
    assert [f.message for f in result.findings] == ["gate_hackable", "collapse"]


def test_fn_probe_crash_is_an_error_not_an_exception(tmp_path):
    def boom():
        raise RuntimeError("detector exploded")

    result = run_probe(ProbeSpec("verifier-audit", fn=boom), root=tmp_path)
    assert result.status == "error"
    assert "detector exploded" in result.payload["error"]


# --------------------------------------------------------------------------
# Serial run + defaults
# --------------------------------------------------------------------------

def test_run_probes_preserves_declaration_order(tmp_path):
    run, _ = _runner(0)
    specs = [ProbeSpec(f"p{i}", argv=("x",)) for i in range(3)]
    assert [r.probe_id for r in run_probes(specs, root=tmp_path, runner=run)] == ["p0", "p1", "p2"]


def test_default_specs_marks_the_red_baseline_probes_required(tmp_path):
    specs = {s.probe_id: s for s in default_specs(root=tmp_path)}
    assert specs["garden"].required and specs["validate"].required
    assert not specs["metrics"].required


def test_default_specs_omit_tests_without_paths(tmp_path):
    assert "tests" not in {s.probe_id for s in default_specs(root=tmp_path)}
    with_paths = {s.probe_id: s for s in default_specs(root=tmp_path, pytest_paths=["tests/unit/x.py"])}
    assert with_paths["tests"].required
    assert "tests/unit/x.py" in with_paths["tests"].argv


def test_result_json_round_trip_preserves_required(tmp_path):
    run, _ = _runner(1, "[error]   boom\n")
    spec = ProbeSpec("garden", argv=("x",), policy="reported", parser="garden", required=True)
    original = run_probe(spec, root=tmp_path, runner=run)
    again = ProbeResult.from_dict(original.to_dict())
    assert again == original


# --------------------------------------------------------------------------
# verifier-audit — the one probe with no CLI verb
# --------------------------------------------------------------------------

def test_verifier_audit_is_built_in_process_and_optional(tmp_path):
    spec = {s.probe_id: s for s in default_specs(root=tmp_path)}["verifier-audit"]
    assert spec.fn is not None and not spec.argv
    assert not spec.required


def test_verifier_audit_turns_audit_flags_into_findings(tmp_path, monkeypatch):
    from mini_ork.learning import verifier_audit

    seen: dict[str, str] = {}

    def fake_audit(task_class, db_path, **kw):
        seen["db"] = str(db_path)
        return {"ok": False, "flags": ["gate_hackable:suite_shrunk"],
                "evidence": {"detectors": 3}}

    monkeypatch.setattr(verifier_audit, "audit", fake_audit)
    db = tmp_path / "state.db"
    spec = {s.probe_id: s for s in default_specs(root=tmp_path, db=db)}["verifier-audit"]
    result = run_probe(spec, root=tmp_path)
    assert result.status == "findings"
    assert [f.message for f in result.findings] == ["verifier-audit: gate_hackable:suite_shrunk"]
    assert result.payload["ok"] is False
    # The cycle's DB wins over whatever the probe process would resolve.
    assert seen["db"] == str(db)


def test_verifier_audit_is_ok_when_the_audit_finds_nothing(tmp_path, monkeypatch):
    from mini_ork.learning import verifier_audit

    monkeypatch.setattr(verifier_audit, "audit",
                        lambda *a, **k: {"ok": True, "flags": [], "evidence": {}})
    spec = {s.probe_id: s for s in default_specs(root=tmp_path)}["verifier-audit"]
    assert run_probe(spec, root=tmp_path).status == "ok"


def test_verifier_audit_resolves_the_db_from_the_environment(tmp_path, monkeypatch):
    from mini_ork.learning import verifier_audit

    seen: dict[str, str] = {}

    def fake_audit(task_class, db_path, **kw):
        seen["db"] = str(db_path)
        return {"ok": True, "flags": [], "evidence": {}}

    monkeypatch.setattr(verifier_audit, "audit", fake_audit)
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "env.db"))
    spec = {s.probe_id: s for s in default_specs(root=tmp_path)}["verifier-audit"]
    run_probe(spec, root=tmp_path)
    assert seen["db"] == str(tmp_path / "env.db")


REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "rsi-cycle.example.yaml"


def test_every_probe_the_shipped_config_names_has_a_runnable_spec():
    """The example config must be runnable *as shipped*.

    Regression guard for a real defect: ``probes.run`` named two ids no runner
    built, and the shipped config failed at startup — after HARVEST, before any
    SUGGEST — with "has no runnable spec". Config parsing accepted both, so
    nothing caught it until a live invocation.
    """
    from mini_ork.rsi.config import load_config

    cfg = load_config(EXAMPLE, env={}, strict=True)
    built = {s.probe_id for s in default_specs(
        root=REPO, pytest_paths=cfg.probes.pytest_paths, db=REPO / ".mini-ork" / "state.db")}
    unbuilt = [p for p in cfg.probes.run
               if not p.startswith(probes.SYNTHETIC_PREFIX) and p not in built]
    assert unbuilt == []


def test_module_exports_the_registry_it_documents():
    assert set(probes.PARSERS) == {"none", "garden", "validate", "pytest"}
    assert probes.STATUSES == ("ok", "findings", "error", "timeout", "skipped")
