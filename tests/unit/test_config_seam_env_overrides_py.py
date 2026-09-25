"""The consumer config seam: ``MINI_ORK_PROVIDERS`` / ``MINI_ORK_AGENTS``.

A vendored mini-ork checkout is a pure mirror of upstream — re-vendoring RESETS
it — so a consumer that configures the engine by editing
``$MINI_ORK_HOME/config/*.yaml`` inside the checkout loses that configuration on
every re-vendor, silently (the files are tracked, so ``reset --hard`` restores
the upstream defaults with no error).

``MINI_ORK_PROVIDERS`` already existed as a path override in
``dispatch/providers.py`` — but three other readers of the same two files
ignored it, so the engine could resolve lanes from one file while validating,
profiling, and gating against another. ``MINI_ORK_AGENTS`` is the matching seam
for ``agents.yaml``. These tests pin all three readers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import main as cli
from mini_ork.cli import validate
from mini_ork.gates import native_gates as ng


# ── validate: the secrets check must read the file the lanes read ────────────


def test_validate_reads_the_providers_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    override = tmp_path / "vendor" / "providers.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        "providers:\n"
        "  openai:\n"
        "    api_key_env: MO_TEST_ABSENT_KEY\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(override))
    monkeypatch.delenv("MO_TEST_ABSENT_KEY", raising=False)

    findings = validate.Findings()
    validate._check_provider_secrets(str(home), findings)

    # Without the override there is no $MINI_ORK_HOME/config/providers.yaml at
    # all, so the check silently reports nothing about the file lanes use.
    assert findings.warnings == 1


def test_validate_falls_back_to_the_home_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  openai:\n"
        "    api_key_env: MO_TEST_ABSENT_KEY\n", encoding="utf-8")
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    monkeypatch.delenv("MO_TEST_ABSENT_KEY", raising=False)

    findings = validate.Findings()
    validate._check_provider_secrets(str(home), findings)

    assert findings.warnings == 1


# ── native_gates: the coalition gate must read the consumer's lane policy ────


def test_coalition_gate_reads_the_agents_override(tmp_path, monkeypatch):
    from mini_ork.gates import coalition_gate
    from mini_ork.observability import topology_metrics

    seen = {}
    monkeypatch.setattr(topology_metrics, "measure_rho", lambda *a, **k: 0.5)

    def fake_check(panel_run_id, recipe, **kwargs):
        seen.update(kwargs)
        return {"verdict": "panel_diverse"}, 0

    monkeypatch.setattr(coalition_gate, "check_panel_coalition", fake_check)
    monkeypatch.setenv("MINI_ORK_AGENTS", "/vendor/agents.yaml")

    verdict = ng._eval_coalition(
        "native:coalition",
        json.dumps({"panel_run_id": "p1", "recipe": "r"}),
        str(tmp_path / "state.db"),
        str(tmp_path),
    )

    assert verdict == "pass"
    assert seen["agents_yaml"] == "/vendor/agents.yaml"


def test_coalition_gate_defaults_to_the_root_config(tmp_path, monkeypatch):
    from mini_ork.gates import coalition_gate
    from mini_ork.observability import topology_metrics

    seen = {}
    monkeypatch.setattr(topology_metrics, "measure_rho", lambda *a, **k: 0.5)

    def fake_check(panel_run_id, recipe, **kwargs):
        seen.update(kwargs)
        return {"verdict": "panel_diverse"}, 0

    monkeypatch.setattr(coalition_gate, "check_panel_coalition", fake_check)
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    ng._eval_coalition(
        "native:coalition",
        json.dumps({"panel_run_id": "p1", "recipe": "r"}),
        str(tmp_path / "state.db"),
        str(tmp_path),
    )

    assert seen["agents_yaml"] == str(tmp_path / "config" / "agents.yaml")


# ── cli/main: the profile step must read the consumer's lane policy ──────────


def _profile_agents_path(monkeypatch, tmp_path) -> str:
    """Run the lifecycle far enough to capture gen_profile's agents_path, then
    stop before the run needs a provider."""
    home = tmp_path / "home"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-agents-seam")
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)

    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\n", encoding="utf-8")

    real_run = cli.subprocess.run

    def fake_run(argv, **kwargs):
        if "mini_ork.cli.classify" in list(argv):
            return SimpleNamespace(returncode=0, stdout="task_class=code_fix\n", stderr="")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    seen: dict[str, str] = {}

    class _Stop(Exception):
        pass

    def fake_profile(*args, **kwargs):
        seen["agents_path"] = args[5]
        raise _Stop()

    monkeypatch.setattr(cli, "gen_profile", fake_profile)
    try:
        cli.main(["run", "code-fix", str(kickoff)], root=str(REPO))
    except _Stop:
        pass
    return seen["agents_path"]


def test_profile_reads_the_agents_override(monkeypatch, tmp_path):
    override = tmp_path / "vendor" / "agents.yaml"
    monkeypatch.setenv("MINI_ORK_AGENTS", str(override))

    assert _profile_agents_path(monkeypatch, tmp_path) == str(override)


def test_profile_defaults_to_the_home_config(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)

    assert _profile_agents_path(monkeypatch, tmp_path) == str(
        tmp_path / "home" / "config" / "agents.yaml")
