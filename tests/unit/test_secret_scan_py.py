"""The secret-scan gate: config, CI wiring, and the positive control.

A scanner that is wired but cannot fire reports "no leaks found" forever, which
is indistinguishable from not running it. So this file checks three things:

* the config parses and keeps the two allowlist exemptions narrow (value-matched,
  not path-wide, so a real credential in a test file is still a finding);
* the CI job exists, is a dependency of ``ci result``, and is in the aggregate's
  ``expected`` set — the aggregate rejects a job that did not run, so a typo in
  ``if:`` cannot silently pass;
* the positive control — a realistic credential planted in a non-test path is
  actually reported. Skipped when the gitleaks binary is not installed, because
  the assertion is about the engine and the config together, and CI installs
  the engine.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import mini_ork

REPO = Path(mini_ork.__file__).resolve().parents[1]
CONFIG = REPO / ".gitleaks.toml"
CI = REPO / ".github" / "workflows" / "ci.yml"

tomllib = pytest.importorskip("tomllib")


def _config() -> dict:
    with CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def test_config_extends_the_default_ruleset() -> None:
    """Without ``useDefault`` the file would replace the 200-odd built-in rules
    with two allowlists and a scanner that finds nothing."""
    assert _config()["extend"]["useDefault"] is True


def test_test_fixture_exemption_matches_the_value_not_the_path() -> None:
    rule = next(
        a for a in _config()["allowlists"] if "tests/" in " ".join(a["paths"])
    )
    # `regexTarget = "secret"` is the whole point: scoping by path alone would
    # wave through a real key pasted into a test.
    assert rule["regexTarget"] == "secret"
    assert rule["paths"] == ["^tests/.*"]


def test_every_allowlist_scopes_by_path() -> None:
    """An allowlist with no path scope and no regex would exempt every finding
    in the repository."""
    for rule in _config()["allowlists"]:
        assert rule.get("paths"), f"unscoped allowlist: {rule.get('description')}"


def test_ci_job_is_required_by_the_aggregate() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "  secret-scan:" in text, "the gitleaks job is missing from ci.yml"
    needs = text.split("needs: [", 1)[1].split("]", 1)[0]
    assert "secret-scan" in needs, "ci result does not depend on secret-scan"
    expected = text.split("expected = {", 1)[1].split("}", 1)[0]
    assert "secret-scan" in expected, (
        "secret-scan is a dependency but not in `expected`; the aggregate would "
        "ignore it, and a cancelled or skipped scan would read as green"
    )


def test_config_is_a_high_risk_path() -> None:
    """Loosening an allowlist is a security change: it must need an approval at
    merge time, exactly like a workflow change."""
    text = (REPO / "config" / "risk-tiers.yaml").read_text(encoding="utf-8")
    assert "- .gitleaks.toml" in text.split("high:", 1)[1]


@pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="gitleaks binary not installed"
)
def test_a_planted_credential_is_reported(tmp_path: Path) -> None:
    """Positive control: the gate must fire on a real-shaped secret in a
    non-test path, or the green result means nothing."""
    (tmp_path / "pkg").mkdir()
    # Assembled from two pieces so the token never appears as a literal here:
    # this file is itself scanned, and a real-shaped PAT written verbatim would
    # be a finding in the repository's own tree.
    token = "ghp_" + "Q7mZ2xKp9vRt4LbN8sWcYd3FgH6jK1mL0pQa"
    (tmp_path / "pkg" / "planted.py").write_text(
        f'GH = "{token}"\n', encoding="utf-8"
    )
    result = subprocess.run(
        [
            "gitleaks", "detect", "--no-git", "--source", str(tmp_path),
            "--config", str(CONFIG), "--report-format", "json",
            "--report-path", str(tmp_path / "gl.json"),
            "--exit-code", "1", "--no-banner", "--redact",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"gitleaks did not report the planted credential:\n{result.stdout}"
        f"{result.stderr}"
    )
    assert "ghp_" not in result.stdout, "--redact must keep the secret out of the log"


def test_the_repository_is_clean_under_its_own_config() -> None:
    """Sabotage check: if this fails, either a credential is committed or an
    allowlist is too wide — read the report before widening anything."""
    if shutil.which("gitleaks") is None:
        pytest.skip("gitleaks binary not installed")
    result = subprocess.run(
        [
            "gitleaks", "detect", "--no-git", "--source", str(REPO),
            "--config", str(CONFIG), "--exit-code", "1", "--no-banner",
            "--redact",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "committed tree has secret findings:\n" + result.stdout + result.stderr
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
