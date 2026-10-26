"""Unit + native-integration tests for ``mini_ork.cli.validate``.

The unit tests drive the ported checks against tmp fixtures (kickoff files,
recipe manifests, agents.yaml, providers.yaml) and assert the exact
stderr/stdout/exit-code contract of the retired ``bin/mini-ork-validate``.
The integration tests prove ``mini-ork validate`` is dispatched natively
(registered in ``_NATIVE_SUBS``, native dispatch — ``_EXEC_SUBS`` is deleted) and that
``python3 -m mini_ork.cli.validate --help`` exits 0 without any
``bin/mini-ork-*`` entrypoint.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.cli import main as cli
from mini_ork.cli import validate

REPO = Path(__file__).resolve().parents[2]


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated (root, home) wired through the env contract."""
    root = tmp_path / "root"
    home = tmp_path / "home"
    (root / "recipes").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    home.mkdir(parents=True)
    (root / "config" / "agents.yaml").write_text("lanes: {}\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MO_MAX_KICKOFF_BYTES", raising=False)
    return root, home


def _recipe(root: Path, name: str, *, workflow: str = "nodes: []\n",
            contract: str = "outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n") -> Path:
    d = root / "recipes" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task_class.yaml").write_text("name: demo\n", encoding="utf-8")
    (d / "workflow.yaml").write_text(workflow, encoding="utf-8")
    (d / "artifact_contract.yaml").write_text(contract, encoding="utf-8")
    return d


# ── usage / arg parsing ───────────────────────────────────────────────────────

def test_help_to_stdout(capsys):
    assert validate.main(["--help"]) == 0
    assert capsys.readouterr().out == validate.HELP_TEXT
    assert validate.main(["-h"]) == 0
    assert capsys.readouterr().out == validate.HELP_TEXT


def test_usage_errors(capsys):
    assert validate.main(["--nope"]) == 2
    assert capsys.readouterr().err == f"Unknown flag: --nope\n{validate.HELP_TEXT}"
    assert validate.main(["a.md", "b.md"]) == 2
    assert capsys.readouterr().err == f"Unexpected argument: b.md\n{validate.HELP_TEXT}"
    # bash aborts under set -u; the port reports usage (documented divergence).
    assert validate.main(["--recipe"]) == 2
    assert capsys.readouterr().err == validate.HELP_TEXT


# ── kickoff checks ────────────────────────────────────────────────────────────

def test_kickoff_without_goal_heading_warns(env, tmp_path, capsys):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Ship it\n\nsome prose\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert (f"[warning] kickoff {kickoff} has no recognizable Goal/Done-When heading\n"
            f"          Fix: add a '## Goal' and '## Done When' section to {kickoff}\n"
            ) in captured.err
    assert captured.err.endswith("validate: 0 error(s), 1 warning(s)\n")


@pytest.mark.parametrize("heading", ["## Goal", "# Done When", "### Done-When",
                                     "## Definition of Done", "## acceptance criteria"])
def test_kickoff_recognized_headings(env, tmp_path, capsys, heading):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text(f"{heading}\n- thing\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"
    assert captured.err == ""


def test_kickoff_size_cap(env, tmp_path, capsys, monkeypatch):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("## Goal\n" + "x" * 64 + "\n", encoding="utf-8")
    monkeypatch.setenv("MO_MAX_KICKOFF_BYTES", "10")
    assert validate.main([str(kickoff), "--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    size = kickoff.stat().st_size
    assert (f"[error]   kickoff {kickoff} is {size} bytes (cap: 10)\n"
            "          Fix: split into single-deliverable kickoffs; "
            "move detail to kickoffs/references/\n") in captured.err
    assert captured.err.endswith("validate: 1 error(s), 0 warning(s)\n")


def test_missing_kickoff_file_is_silently_skipped(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    # bash: all kickoff checks are guarded by [ -f "$KICKOFF" ].
    assert validate.main(["no/such/kickoff.md", "--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


# ── recipe manifest checks ────────────────────────────────────────────────────

def test_recipe_not_found(env, capsys):
    _root, _home = env
    assert validate.main(["--recipe", "ghost"]) == 1
    captured = capsys.readouterr()
    assert ("[error]   recipe not found: ghost\n"
            "          Fix: check recipes/ or run 'mini-ork classify $KICKOFF'\n"
            ) in captured.err
    assert captured.err.endswith("validate: 1 error(s), 0 warning(s)\n")


def test_missing_manifests(env, capsys):
    root, _home = env
    d = root / "recipes" / "demo"
    d.mkdir()
    (d / "task_class.yaml").write_text("name: demo\n", encoding="utf-8")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    for req in ("workflow.yaml", "artifact_contract.yaml"):
        assert (f"[error]   recipe demo missing {req}\n"
                f"          Fix: create {d}/{req} from the recipe template\n"
                ) in captured.err
    assert captured.err.endswith("validate: 2 error(s), 0 warning(s)\n")


def test_invalid_workflow_yaml(env, capsys):
    root, _home = env
    d = _recipe(root, "demo", workflow="not: [valid\n")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert ("[error]   recipe demo workflow.yaml is not valid YAML\n"
            f"          Fix: fix YAML syntax in {d}/workflow.yaml\n") in captured.err


_FULL_RECURSION = (
    "nodes: []\n"
    "recursion:\n"
    "  max_iterations: 5\n"
    "  budget_cap_total_usd: 5.0\n"
)


def test_partial_recursion_block_is_an_error(env, capsys):
    """The schema permits a partial block; executing one would raise mid-run.

    Validate must surface it pre-run instead — that is the whole reason the
    runtime loader is stricter than the schema.
    """
    root, _home = env
    _recipe(root, "demo", workflow="nodes: []\nrecursion:\n  max_iterations: 5\n")

    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert "[error]   recipe demo has an invalid recursion block" in captured.err
    # The detail names the missing key, so the fix is obvious without reading source.
    assert "missing" in captured.err
    assert "budget_cap_total_usd" in captured.err


def test_typo_recursion_key_is_an_error(env, capsys):
    root, _home = env
    _recipe(root, "demo", workflow=(
        "nodes: []\n"
        "recursion:\n"
        "  max_iterations: 5\n"
        "  budget_cap_total: 5.0\n"  # typo: _total, not _total_usd
        "  budget_cap_total_usd: 5.0\n"
    ))

    assert validate.main(["--recipe", "demo"]) == 1
    assert "unknown recursion key" in capsys.readouterr().err


def test_a_removed_recursion_key_names_the_removal(env, capsys):
    """A recipe still carrying a dropped key must be told *why* it is wrong.

    ``convergence_check`` was removed (nothing read it) and the loader reports
    the removal before the generic unknown-key error, which would name the key
    without saying it is gone rather than mistyped.
    """
    root, _home = env
    _recipe(root, "demo", workflow=(
        "nodes: []\n"
        "recursion:\n"
        "  max_iterations: 5\n"
        "  budget_cap_total_usd: 5.0\n"
        "  convergence_check: all_done\n"
    ))

    assert validate.main(["--recipe", "demo"]) == 1
    err = capsys.readouterr().err
    assert "convergence_check was removed" in err
    assert "unknown recursion key" not in err


def test_complete_recursion_block_is_clean(env, capsys):
    root, _home = env
    _recipe(root, "demo", workflow=_FULL_RECURSION)

    assert validate.main(["--recipe", "demo"]) == 0
    assert capsys.readouterr().err == ""


def test_output_collision_warns_with_other_recipe_count(env, capsys):
    root, _home = env
    d = _recipe(root, "demo", contract="outputs:\n  - dist/app.js\n")
    _recipe(root, "other", contract="outputs:\n  - dist/app.js\n")
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] recipe demo output path 'dist/app.js' is also targeted by "
            "1 other recipe(s)\n"
            f"          Fix: use a recipe-specific output path in {d}/artifact_contract.yaml\n"
            ) in captured.err


def test_run_dir_outputs_skip_collision_check(env, capsys):
    root, _home = env
    _recipe(root, "demo", contract="outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n")
    _recipe(root, "other", contract="outputs:\n  - ${MINI_ORK_RUN_DIR}/out.md\n")
    assert validate.main(["--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


# ── agents.yaml / provider secrets ────────────────────────────────────────────

def test_missing_agents_yaml_warns(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    (root / "config" / "agents.yaml").unlink()
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] no agents.yaml found in MINI_ORK_HOME/config or "
            "ENGINE_ROOT/config\n"
            "          Fix: run 'mini-ork init' to seed default config\n") in captured.err


def test_home_agents_yaml_takes_precedence_and_invalid_yaml_errors(env, capsys):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "agents.yaml").write_text("not: [valid\n", encoding="utf-8")
    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert (f"[error]   agents.yaml is not valid YAML: {cfg}/agents.yaml\n"
            "          Fix: fix YAML syntax\n") in captured.err


def test_provider_secret_missing_warns(env, capsys, monkeypatch):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    api_key_env: GLM_TEST_MISSING_KEY_XYZ\n",
        encoding="utf-8")
    monkeypatch.delenv("GLM_TEST_MISSING_KEY_XYZ", raising=False)
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] provider secret missing: glm -> GLM_TEST_MISSING_KEY_XYZ\n"
            f"          Fix: set it in {home}/config/secrets.local.sh\n") in captured.err


def test_provider_secret_present_is_clean(env, capsys, monkeypatch):
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    api_key_env: GLM_TEST_PRESENT_KEY\n", encoding="utf-8")
    monkeypatch.setenv("GLM_TEST_PRESENT_KEY", "x")
    assert validate.main(["--recipe", "demo"]) == 0
    assert capsys.readouterr().out == "validate: OK\n"


def test_store_backed_credential_is_not_reported_missing(env, capsys, monkeypatch):
    """The dispatcher resolves credentials through `provider_environment` (the
    owner-only store overlaid by the ambient env), so a check reading only
    `os.environ` reports every store-backed lane as missing — 12 false warnings
    on the live engine home, burying the one real fault."""
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    api_key_env: GLM_STORE_ONLY_KEY\n", encoding="utf-8")
    store = cfg / "secrets.local.sh"
    store.write_text("export GLM_STORE_ONLY_KEY='from-the-store'\n", encoding="utf-8")
    store.chmod(0o600)
    monkeypatch.delenv("GLM_STORE_ONLY_KEY", raising=False)

    assert validate.main(["--recipe", "demo"]) == 0
    assert "provider secret missing" not in capsys.readouterr().err


def test_unreadable_secret_store_warns_once(env, capsys, monkeypatch):
    """Fail closed: a permissive store must not read as "credentials are fine,
    just not exported", and the two checks must not both say so."""
    root, home = env
    _recipe(root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  glm:\n    kind: anthropic-compat\n    api_key_env: GLM_KEY\n",
        encoding="utf-8")
    store = cfg / "secrets.local.sh"
    store.write_text("export GLM_KEY='x'\n", encoding="utf-8")
    store.chmod(0o644)  # world-readable: SecretStoreError
    monkeypatch.setenv("GLM_KEY", "x")

    assert validate.main(["--recipe", "demo"]) == 0
    err = capsys.readouterr().err
    assert err.count("cannot read the local secret store") == 1
    assert "owner-only, 0600" in err


# ── lane liveness (Check 5) ───────────────────────────────────────────────────
# Check 4 proves a credential is PRESENT; Check 5 proves it WORKS. The gap is
# the shipped failure: kimi sat in both dispatch_chain fallback tails with a
# 401-rejected key, so every coding/review node that fell through to it died
# with a misleading `lane preflight failed` and nothing at instantiation said
# so. Every test here stubs `providers.lane_health` — the check must never
# touch the network from a unit test, and the stub is the seam that proves the
# check consults the SAME health function the run-start gate uses.

def _gateway_lane(home: Path, *, name: str = "glm", key_env: str | None = "GLM_KEY",
                  kind: str = "anthropic-compat") -> None:
    cfg = home / "config"
    cfg.mkdir(exist_ok=True)
    key_line = f"    api_key_env: {key_env}\n" if key_env else ""
    (cfg / "providers.yaml").write_text(
        f"providers:\n  {name}:\n    kind: {kind}\n    model: m-{name}\n" + key_line,
        encoding="utf-8")


def _stub_health(monkeypatch, health):
    from mini_ork.dispatch import providers as providers_mod
    monkeypatch.setattr(providers_mod, "lane_health",
                        lambda model, root=None, **kw: health)


def test_dead_lane_is_an_error_not_a_warning(env, capsys, monkeypatch):
    """The kimi shape: credential set, credential rejected."""
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    _gateway_lane(home)
    monkeypatch.setenv("GLM_KEY", "set-but-dead")
    _stub_health(monkeypatch, LaneHealth(
        False, "API key rejected (HTTP 401) — every dispatch on this lane would fail"))

    assert validate.main(["--recipe", "demo"]) == 1
    captured = capsys.readouterr()
    assert ("[error]   lane 'glm' is unhealthy: API key rejected (HTTP 401) — "
            "every dispatch on this lane would fail\n") in captured.err
    assert "secrets.local.sh" in captured.err
    assert captured.err.endswith("validate: 1 error(s), 0 warning(s)\n")


def test_healthy_probed_lane_is_clean(env, capsys, monkeypatch):
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    _gateway_lane(home)
    monkeypatch.setenv("GLM_KEY", "live")
    _stub_health(monkeypatch, LaneHealth(True, "ok"))

    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"
    assert captured.err == ""


def test_unset_credential_is_check_4s_finding_not_a_liveness_error(env, capsys, monkeypatch):
    """An unset key must stay a warning: CI has no credentials, and a hard
    error there would make every lint-only validate run red."""
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    _gateway_lane(home, key_env="GLM_ABSENT_KEY")
    monkeypatch.delenv("GLM_ABSENT_KEY", raising=False)
    _stub_health(monkeypatch, LaneHealth(False, "should never be consulted"))

    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert "[warning] provider secret missing: glm -> GLM_ABSENT_KEY" in captured.err
    assert "unhealthy" not in captured.err


def test_unprobed_lanes_are_named_rather_than_read_as_verified(env, capsys, monkeypatch):
    """A native lane, or probes switched off: ok, but nobody checked. Silence
    here is how `validate: OK` comes to mean less than it reads."""
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    _gateway_lane(home, name="codexish", kind="anthropic-native", key_env=None)
    _stub_health(monkeypatch, LaneHealth(
        True, "ok (unprobed: native lane — auth owned by the CLI)"))

    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert ("[warning] lane liveness not verified for 1 lane(s): codexish\n") in captured.err
    assert "MO_LANE_PROBE=1" in captured.err


def test_non_probeable_kind_is_skipped(env, capsys, monkeypatch):
    """`lane_health` answers an unknown kind with "unsupported kind" — a
    providers.yaml shape problem, not a liveness verdict, and not this check's."""
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    cfg = home / "config"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        "providers:\n  odd:\n    api_key_env: ODD_KEY\n", encoding="utf-8")
    monkeypatch.setenv("ODD_KEY", "x")
    _stub_health(monkeypatch, LaneHealth(False, "provider 'odd' has unsupported kind: None"))

    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"


def test_strict_mode_escalates_the_unprobed_warning(env, capsys, monkeypatch):
    from mini_ork.dispatch.providers import LaneHealth
    _root, home = env
    _recipe(_root, "demo")
    _gateway_lane(home, name="codexish", kind="anthropic-native", key_env=None)
    _stub_health(monkeypatch, LaneHealth(True, "ok (unprobed: native lane)"))

    assert validate.main(["--recipe", "demo", "--strict"]) == 1
    assert capsys.readouterr().err.endswith(
        "validate: 0 error(s), 1 warning(s) (strict mode)\n")


# ── strict mode / summary ─────────────────────────────────────────────────────

def test_strict_turns_warnings_into_exit_1(env, tmp_path, capsys):
    root, _home = env
    _recipe(root, "demo")
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# no headings\n", encoding="utf-8")
    assert validate.main([str(kickoff), "--recipe", "demo", "--strict"]) == 1
    captured = capsys.readouterr()
    assert captured.err.endswith("validate: 0 error(s), 1 warning(s) (strict mode)\n")


def test_clean_validate_ok(env, capsys):
    root, _home = env
    _recipe(root, "demo")
    assert validate.main(["--recipe", "demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "validate: OK\n"
    assert captured.err == ""


# ── recipe inference via classify ─────────────────────────────────────────────

def test_infer_recipe_maps_task_class_underscores_to_dashes(tmp_path):
    """bash: classify | grep ^task_class= | cut -d= -f2 | tr '_' '-'."""
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# Fix the bug in the parser\n", encoding="utf-8")
    recipe = validate._infer_recipe(str(kickoff), str(REPO))
    assert recipe  # classify must produce some task class in dry-run
    assert "_" not in recipe
    assert validate._infer_recipe(str(tmp_path / "missing.md"), str(REPO)) == ""


# ── native integration ────────────────────────────────────────────────────────

def test_validate_is_registered_natively():
    assert "validate" in cli._NATIVE_SUBS
    assert not hasattr(cli, "_EXEC_SUBS"), "validate must dispatch natively — the bash trampoline set is gone"
    assert "validate" in cli.SUBCOMMAND_REGISTRY


def test_validate_dispatches_via_python_m(monkeypatch):
    calls = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: (calls.append(argv), _Done())[1])
    assert cli.main(["validate", "k.md"], root=str(REPO)) == 0
    assert calls == [[sys.executable, "-m", "mini_ork.cli.validate", "k.md"]]


def test_module_help_exits_zero_without_bin_entrypoint(tmp_path):
    """``python3 -m mini_ork.cli.validate --help`` runs the module directly —
    no bin/mini-ork-validate involved (cwd is an empty tmp dir)."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("MINI_ORK_", "MO_"))}
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else "")
    run = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.validate", "--help"],
        capture_output=True, text=True, cwd=tmp_path, env=env, check=False)
    assert run.returncode == 0
    assert run.stdout == validate.HELP_TEXT
    assert run.stderr == ""
