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


def test_fixture_exemption_is_value_scoped_and_anchored() -> None:
    """The exemption must be the fixture VALUE, never the directory it is in.

    gitleaks matches an allowlist entry when ANY of its conditions match, so a
    `paths = ['^tests/.*']` entry exempts every secret under tests/ — a real
    credential included. (`condition = "AND"` exists in some releases but is
    not honoured by all of them, so it cannot be relied on.) Each pattern here
    is anchored, so it can only match the whole fixture value.
    """
    rule = next(
        a for a in _config()["allowlists"] if "synthetic" in a["description"]
    )
    assert rule["regexTarget"] == "secret"
    assert "paths" not in rule, (
        "a path scope on the fixture allowlist exempts every secret in that "
        "tree, not just the synthetic ones"
    )
    for pattern in rule["regexes"]:
        assert pattern.startswith("^") and pattern.endswith("$"), (
            f"unanchored pattern {pattern!r} would allow any secret containing "
            "the marker"
        )


def test_every_allowlist_is_scoped() -> None:
    """An allowlist with neither a path scope nor an anchored value pattern
    would exempt findings it was never meant to cover."""
    for rule in _config()["allowlists"]:
        patterns = rule.get("regexes") or []
        assert rule.get("paths") or patterns, (
            f"unscoped allowlist: {rule.get('description')}"
        )
        if not rule.get("paths"):
            assert rule.get("regexTarget") == "secret", (
                f"{rule.get('description')} allowlists by value but not on the "
                "secret; a line-scoped match allows far more than the value"
            )
            assert all(p.startswith("^") and p.endswith("$") for p in patterns), (
                f"unanchored pattern in {rule.get('description')}"
            )


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


@pytest.mark.skipif(
    shutil.which("gitleaks") is None, reason="gitleaks binary not installed"
)
def test_a_real_credential_in_a_test_file_is_still_reported(tmp_path: Path) -> None:
    """The exemption is the fixture's shape, not the tests/ directory.

    This is the half that a path-scoped allowlist would silently break: the
    scanner would report "no leaks found" for a genuine credential pasted into
    a test, which reads exactly like a clean tree.
    """
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    token = "ghp_" + "Zx4Qm8Tp2Vr6Kb9Nc3Wd7Yf1Hs5Lj0Pg4Rd6S"
    (tmp_path / "tests" / "unit" / "planted.py").write_text(
        f'K = "{token}"\n', encoding="utf-8"
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
        "a credential in a test file was exempted by path, not by value:\n"
        + result.stdout + result.stderr
    )


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
