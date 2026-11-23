"""Tier 1/2/3 must check Rust and Python, not pass them vacuously.

The three deterministic tiers were written against a TypeScript+Jest repo.
On any other repo they passed *because nothing ran*: tier1 printed "no
TypeScript/JavaScript source files", tier2 "no scoped Jest tests ... passing
with verifier/local evidence only", and tier3 reported itself vacuous. The
tier-4 panel then carried the entire gate (observed on the Rust voicekit repo,
2026-11-19 — three vacuous passes ahead of an APPROVE panel).

These tests pin the added branches: a touched ``.rs`` file scopes ``cargo
check``/``cargo test`` to the crate that owns it, a touched ``.py`` module maps
to its sibling test, and a Rust property target under ``tests/`` makes tier3
non-vacuous. ``cargo`` is stubbed on PATH so the tests exercise the verifier's
decision logic without building anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIERS = REPO / "recipes" / "recursive-validate-impl" / "verifiers"
TIER1 = VERIFIERS / "tier1-compile-typecheck.py"
TIER2 = VERIFIERS / "tier2-scoped-unit.py"
TIER3 = VERIFIERS / "tier3-property-mutation.py"

CRATE = "crates/alpha"
TOUCHED_RS = f"{CRATE}/src/lib.rs"


def _rust_repo(tmp_path: Path, *, property_suite: bool = False) -> Path:
    crate = tmp_path / CRATE
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "alpha"\nversion = "0.1.0"\n', encoding="utf-8")
    (crate / "src" / "lib.rs").write_text(
        "pub fn one() -> u8 {\n    1\n}\n", encoding="utf-8")
    if property_suite:
        (crate / "tests").mkdir()
        (crate / "tests" / "properties.rs").write_text(
            "proptest! {\n    fn reads_stay_in_range(x in 0u8..2) { assert!(x < 2); }\n}\n",
            encoding="utf-8")
    return crate


def _fixture(tmp_path: Path, touched, *, ready: bool = True) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"ready_for_tier1": ready, "touched_files": touched}),
        encoding="utf-8")
    return run_dir


def _stub_cargo(tmp_path: Path, *, rc: int) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "cargo"
    stub.write_text(f"#!/bin/sh\nexit {rc}\n", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def _run(script: Path, cwd: Path, run_dir: Path, *, path_extra: Path | None = None):
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir)}
    if path_extra is not None:
        env["PATH"] = f"{path_extra}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([sys.executable, str(script)], cwd=cwd, env=env,
                          capture_output=True, text=True)


# ── helpers ─────────────────────────────────────────────────────────────────

def test_rust_packages_scope_to_the_nearest_manifest(tmp_path):
    from mini_ork.verify.langs import rust_packages

    crate = _rust_repo(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n',
                                         encoding="utf-8")

    touched = str(crate / "src" / "lib.rs")
    assert rust_packages([touched], root=str(tmp_path)) == ["alpha"]


def test_python_module_maps_to_its_sibling_test(tmp_path):
    from mini_ork.verify.langs import python_test_files

    pkg = tmp_path / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
    sibling = pkg / "tests" / "test_mod.py"
    sibling.write_text("def test_x():\n    assert True\n", encoding="utf-8")

    module = str(pkg / "mod.py")
    assert python_test_files([module]) == [str(sibling)]
    assert python_test_files([module, str(sibling)]) == [str(sibling)]


def test_property_target_detected_by_name_or_macro(tmp_path):
    from mini_ork.verify.langs import rust_property_targets

    crate = _rust_repo(tmp_path, property_suite=True)

    touched = str(crate / "src" / "lib.rs")
    assert rust_property_targets([touched], root=str(tmp_path)) == [("alpha", "properties")]


# ── tier1 ───────────────────────────────────────────────────────────────────

def test_tier1_typechecks_the_touched_crate(tmp_path):
    _rust_repo(tmp_path)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=0)

    result = _run(TIER1, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["rust_rc"] == 0
    assert payload["rust_packages"] == ["alpha"]
    evidence = (run_dir / "tier1-evidence.log").read_text(encoding="utf-8")
    assert "cargo check --quiet -p alpha" in evidence


def test_tier1_refutes_a_failing_cargo_check(tmp_path):
    _rust_repo(tmp_path)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=1)

    result = _run(TIER1, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["rust_rc"] == 1
    assert payload.get("failure_kind") == "code"


def test_tier1_rejects_python_syntax_errors(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def broken(:\n", encoding="utf-8")
    run_dir = _fixture(tmp_path, ["pkg/mod.py"])

    result = _run(TIER1, tmp_path, run_dir)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["python_rc"] == 1
    assert payload.get("failure_kind") == "code"


# ── tier2 ───────────────────────────────────────────────────────────────────

def test_tier2_runs_tests_for_the_touched_crate(tmp_path):
    _rust_repo(tmp_path)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=0)

    result = _run(TIER2, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["rust_rc"] == 0
    assert payload["rust_packages"] == ["alpha"]
    evidence = (run_dir / "tier2-evidence.log").read_text(encoding="utf-8")
    assert "cargo test --quiet -p alpha" in evidence


def test_tier2_refutes_a_failing_cargo_test(tmp_path):
    _rust_repo(tmp_path)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=1)

    result = _run(TIER2, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["rust_rc"] == 1
    assert payload.get("failure_kind") == "code"


# ── tier3 ───────────────────────────────────────────────────────────────────

def test_tier3_runs_a_rust_property_target_and_is_not_vacuous(tmp_path):
    _rust_repo(tmp_path, property_suite=True)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=0)

    result = _run(TIER3, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["vacuous"] is False
    assert payload["checks_ran"] == ["property:alpha:properties"]
    assert payload["property_rc"] == 0


def test_tier3_a_failing_rust_property_run_fails_the_node(tmp_path):
    _rust_repo(tmp_path, property_suite=True)
    run_dir = _fixture(tmp_path, [TOUCHED_RS])
    bin_dir = _stub_cargo(tmp_path, rc=1)

    result = _run(TIER3, tmp_path, run_dir, path_extra=bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["vacuous"] is False
    assert payload["property_rc"] == 1
    assert payload.get("failure_kind") == "code"


def _python_repo(tmp_path: Path, *, failing: bool = False) -> Path:
    """A touched module with a sibling tests/ suite, mirroring sdks/python."""
    pkg = tmp_path / "sdks" / "py" / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "mod.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    body = "def test_one():\n    assert {} == {}\n".format(
        "2" if failing else "1", 1)
    (pkg / "tests" / "test_mod.py").write_text(body, encoding="utf-8")
    return pkg


def test_tier3_runs_the_python_suite_and_is_not_vacuous(tmp_path):
    pkg = _python_repo(tmp_path)
    touched = str(pkg.relative_to(tmp_path) / "mod.py")
    run_dir = _fixture(tmp_path, [touched])

    result = _run(TIER3, tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["vacuous"] is False
    assert payload["checks_ran"] == ["python:pytest:sdks/py/pkg/tests"]
    assert payload["property_rc"] == 0


def test_tier3_a_failing_python_suite_fails_the_node(tmp_path):
    pkg = _python_repo(tmp_path, failing=True)
    touched = str(pkg.relative_to(tmp_path) / "mod.py")
    run_dir = _fixture(tmp_path, [touched])

    result = _run(TIER3, tmp_path, run_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["vacuous"] is False
    assert payload["property_rc"] != 0
    assert payload.get("failure_kind") == "code"
