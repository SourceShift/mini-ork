"""tier2 must execute a crate behind an off-by-default feature, not just compile it.

``cargo test -p crate`` runs with default features. A module behind
``#[cfg(feature = "otel")]`` is compiled out, its tests compiled out with it,
and tier2 reports green on code it never ran. The OTel span work shipped that
way; three tier4 lenses caught what tier2 had already certified.

The rule under test: when a touched ``.rs`` names a feature in a ``cfg``
attribute AND that file's own manifest declares a feature of the same name,
tier2 runs the crate a second time with those features enabled. Declared-only
intersection matters — a ``cfg`` borrowed from a dependency must not turn into
``cargo: feature not found``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier2-scoped-unit.py"

_CRATE = "crates/otel"
_SOURCE = f"{_CRATE}/src/spans.rs"

_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$CARGO_LOG"
exit "${CARGO_EXIT:-0}"
"""


def _crate_manifest(features: str) -> str:
    body = (
        "[package]\n"
        'name = "otel-core"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
    )
    if features:
        body += f"\n[features]\n{features}\n"
    return body


def _fixture(tmp_path: Path, *, source: str, features: str = "otel = []") -> tuple[Path, Path]:
    """Workspace with one crate; returns (run_dir, cargo_log)."""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/otel"]\nresolver = "2"\n', encoding="utf-8"
    )
    crate = tmp_path / _CRATE
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(_crate_manifest(features), encoding="utf-8")
    (tmp_path / _SOURCE).write_text(source, encoding="utf-8")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    cargo = stubs / "cargo"
    cargo.write_text(_STUB, encoding="utf-8")
    cargo.chmod(0o755)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"ready_for_tier1": True, "touched_files": [_SOURCE]}), encoding="utf-8"
    )
    return run_dir, tmp_path / "cargo-argv.log"


def _run(tmp_path: Path, run_dir: Path, cargo_log: Path, *, rc: int = 0):
    env = {
        **os.environ,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "CARGO_LOG": str(cargo_log),
        "CARGO_EXIT": str(rc),
        "PATH": f"{tmp_path / 'stubs'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    calls = []
    if cargo_log.exists():
        calls = [line for line in cargo_log.read_text(encoding="utf-8").splitlines() if line]
    return result, calls


def test_a_declared_feature_the_source_uses_is_executed(tmp_path):
    """Second cargo run carries --features; without it the suite is vacuous."""
    run_dir, log = _fixture(
        tmp_path,
        source='#[cfg(feature = "otel")]\nmod spans;\n',
    )

    result, calls = _run(tmp_path, run_dir, log)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pass"] is True
    assert payload["rust_feature_rc"] == 0
    assert len(calls) == 2, calls
    assert calls[0].startswith("test --quiet -p otel-core")
    assert "--features" not in calls[0]
    assert "--features otel" in calls[1]


def test_a_feature_the_manifest_does_not_declare_is_not_enabled(tmp_path):
    """A cfg borrowed from a dependency is not this crate's feature to turn on."""
    run_dir, log = _fixture(
        tmp_path,
        source='#[cfg(feature = "serde")]\nfn enrich() {}\n',
        features="otel = []",
    )

    result, calls = _run(tmp_path, run_dir, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1, calls
    assert json.loads(result.stdout)["rust_feature_rc"] == 0


def test_a_red_default_run_does_not_get_a_feature_retry(tmp_path):
    """tier2 already fails; a second red run adds noise, not signal."""
    run_dir, log = _fixture(
        tmp_path,
        source='#[cfg(feature = "otel")]\nmod spans;\n',
    )

    result, calls = _run(tmp_path, run_dir, log, rc=101)

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["pass"] is False
    assert payload["rust_rc"] == 101
    assert len(calls) == 1, calls


def test_a_red_feature_run_fails_the_tier(tmp_path):
    """The point of the extra run is that its failure counts."""
    run_dir, log = _fixture(
        tmp_path,
        source='#[cfg(feature = "otel")]\nmod spans;\n',
    )
    env = {
        **os.environ,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "CARGO_LOG": str(log),
        "PATH": f"{tmp_path / 'stubs'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    # Succeed the first invocation, fail the second.
    stubs = tmp_path / "stubs"
    (stubs / "cargo").write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CARGO_LOG"\n'
        'if [ "$(wc -l < "$CARGO_LOG")" -gt 1 ]; then exit 101; fi\nexit 0\n',
        encoding="utf-8",
    )
    (stubs / "cargo").chmod(0o755)

    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)

    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert payload["pass"] is False
    assert payload["rust_feature_rc"] == 101
    assert payload["rust_rc"] == 0
