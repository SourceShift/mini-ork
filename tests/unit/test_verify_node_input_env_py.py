"""`mini-ork verify` publishes the same node-input boundary `execute` does.

A recipe verifier reads its staged inputs from ``$MINI_ORK_NODE_INPUT_DIR``.
The executor publishes it when it materializes the artifact ledger, but
``verify`` re-runs the same script after the run, and used to hand it only
``ARTIFACT_PATH`` — so ``frontier-llm-research``'s verifiers died with a
KeyError no matter what the run had produced.

The boundary is recovered from what the run wrote, never re-derived, and an
unresolvable one stays empty so the verifier keeps its own fail-closed voice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import verify as ver  # noqa: E402

VERIFIERS = REPO / "recipes" / "frontier-llm-research" / "verifiers"


def _run_dir(tmp_path, recipe="frontier-llm-research"):
    run = tmp_path / "run"
    (run / "workspace" / "manifests").mkdir(parents=True)
    (run / "run_profile.json").write_text(
        json.dumps({"recipe": recipe, "roots": {"engine": str(REPO)}}), encoding="utf-8")
    return run


def _materialize(run, node):
    (run / "workspace" / "inputs" / node).mkdir(parents=True, exist_ok=True)
    manifest = run / "workspace" / "manifests" / f"{node}.inputs.json"
    manifest.write_text(json.dumps({"node_id": node, "inputs": []}), encoding="utf-8")
    return manifest


def test_the_boundary_the_run_wrote_is_recovered(tmp_path):
    run = _run_dir(tmp_path)
    manifest = _materialize(run, "research_completeness")
    script = str(VERIFIERS / "verify-aggregation.py")

    env = ver._node_input_env(str(REPO), str(run), script)

    assert env == {
        "MINI_ORK_NODE_INPUT_DIR": str(run / "workspace" / "inputs" / "research_completeness"),
        "MINI_ORK_NODE_INPUT_MANIFEST": str(manifest),
    }


def test_no_manifest_means_no_boundary(tmp_path):
    """execute never published one for this node, so verify must not invent it."""
    run = _run_dir(tmp_path)
    script = str(VERIFIERS / "verify-aggregation.py")

    assert ver._node_input_env(str(REPO), str(run), script) == {}


def test_a_script_no_node_names_gets_no_boundary(tmp_path):
    run = _run_dir(tmp_path)
    _materialize(run, "research_completeness")
    stray = tmp_path / "stray-verifier.py"
    stray.write_text("print('ok')\n", encoding="utf-8")

    assert ver._node_input_env(str(REPO), str(run), str(stray)) == {}


def test_an_ambiguous_script_is_narrowed_by_the_run_evidence(tmp_path):
    """Five nodes share search_batch.py; only the one that ran has inputs."""
    run = _run_dir(tmp_path, recipe="author-style-imitation-review")
    workflow = REPO / "recipes" / "author-style-imitation-review" / "workflow.yaml"
    yaml_text = workflow.read_text(encoding="utf-8")
    node = yaml_text.split("verifier_ref: verifiers/search_batch.py", 1)[0].rsplit("- name:", 1)[1]
    node_id = node.split("\n", 1)[0].strip()
    manifest = _materialize(run, node_id)
    script = str(REPO / "recipes" / "author-style-imitation-review" / "verifiers" / "search_batch.py")

    env = ver._node_input_env(str(REPO), str(run), script)

    assert env["MINI_ORK_NODE_INPUT_DIR"].endswith(os.path.join("inputs", node_id))
    assert env["MINI_ORK_NODE_INPUT_MANIFEST"] == str(manifest)


def test_two_materialized_candidates_stay_unresolved(tmp_path):
    run = _run_dir(tmp_path, recipe="author-style-imitation-review")
    workflow = REPO / "recipes" / "author-style-imitation-review" / "workflow.yaml"
    yaml_text = workflow.read_text(encoding="utf-8")
    names = []
    for chunk in yaml_text.split("verifier_ref: verifiers/search_batch.py")[:-1]:
        names.append(chunk.rsplit("- name:", 1)[1].split("\n", 1)[0].strip())
    assert len(names) >= 2, "fixture needs two nodes sharing one script"
    for name in names[:2]:
        _materialize(run, name)
    script = str(REPO / "recipes" / "author-style-imitation-review" / "verifiers" / "search_batch.py")

    assert ver._node_input_env(str(REPO), str(run), script) == {}


# ── the verifier's own fail-closed voice ────────────────────────────────────


def test_the_helper_fails_closed_with_a_sentence(tmp_path):
    script = tmp_path / "verify-aggregation.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(VERIFIERS)!r})\n"
        "from _inputs import input_dir\n"
        "print(input_dir())\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "MINI_ORK_NODE_INPUT_DIR"}

    proc = subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True, check=False)

    assert proc.returncode == 2
    assert "MINI_ORK_NODE_INPUT_DIR is not set" in proc.stderr
    assert "mini-ork verify" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_the_helper_returns_the_value_when_published(tmp_path):
    script = tmp_path / "verify-aggregation.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(VERIFIERS)!r})\n"
        "from _inputs import input_dir\n"
        "print(input_dir())\n",
        encoding="utf-8",
    )
    env = {**os.environ, "MINI_ORK_NODE_INPUT_DIR": str(tmp_path / "inputs")}

    proc = subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == str(tmp_path / "inputs")


# ── what the verifier actually sees in its environment ──────────────────────


def test_an_inherited_boundary_is_cleared(monkeypatch):
    """Re-run from inside a node: the parent's inputs are not this verifier's."""
    monkeypatch.setenv("MINI_ORK_NODE_INPUT_DIR", "/parent/node/inputs")
    monkeypatch.setenv("MINI_ORK_NODE_INPUT_MANIFEST", "/parent/node/inputs.json")

    env = ver._script_env("/art.md", {})

    assert env["ARTIFACT_PATH"] == "/art.md"
    assert "MINI_ORK_NODE_INPUT_DIR" not in env
    assert "MINI_ORK_NODE_INPUT_MANIFEST" not in env


def test_the_recovered_boundary_replaces_an_inherited_one(monkeypatch):
    monkeypatch.setenv("MINI_ORK_NODE_INPUT_DIR", "/parent/node/inputs")

    env = ver._script_env("/art.md", {
        "MINI_ORK_NODE_INPUT_DIR": "/run/workspace/inputs/research_completeness",
        "MINI_ORK_NODE_INPUT_MANIFEST": "/run/workspace/manifests/research_completeness.inputs.json",
    })

    assert env["MINI_ORK_NODE_INPUT_DIR"].endswith("inputs/research_completeness")


# ── the run's own record outranks the caller's environment ──────────────────


def test_the_profiles_recipe_beats_an_ambient_one(tmp_path, monkeypatch):
    run = _run_dir(tmp_path)
    _materialize(run, "research_completeness")
    monkeypatch.setenv("MINI_ORK_RECIPE", "some-other-recipe")

    env = ver._node_input_env(str(REPO), str(run), str(VERIFIERS / "verify-aggregation.py"))

    assert env["MINI_ORK_NODE_INPUT_DIR"].endswith("research_completeness")


def test_the_recipe_root_is_used_when_the_engine_lacks_the_recipe(tmp_path, monkeypatch):
    """A run dispatched by a worktree is verified against the worktree's copy."""
    run = _run_dir(tmp_path)
    (run / "run_profile.json").write_text(
        json.dumps({"recipe": "frontier-llm-research", "roots": {"engine": str(tmp_path / "gone")}}),
        encoding="utf-8")
    _materialize(run, "research_completeness")
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(REPO))

    env = ver._node_input_env(str(tmp_path / "also-gone"), str(run),
                              str(VERIFIERS / "verify-aggregation.py"))

    assert env["MINI_ORK_NODE_INPUT_DIR"].endswith("research_completeness")


# ── the verifier script itself is found the same way ────────────────────────


def test_the_profiles_recipe_finds_the_script_without_an_ambient_one(tmp_path, monkeypatch):
    """`mini-ork verify` re-run by hand has no MINI_ORK_RECIPE in its env."""
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    run = _run_dir(tmp_path)

    found = ver._find_verifier_script(
        "verifiers/verify-aggregation.py", str(tmp_path / "root"),
        str(tmp_path / "home"), str(run))

    assert found == str(VERIFIERS / "verify-aggregation.py")


def test_the_runs_own_home_overlay_names_the_workflow(tmp_path):
    """A project overlay in the run's home outranks the engine's copy.

    The node ids the boundary is keyed on come from the workflow the run
    actually read; a lookalike under the engine naming different nodes would
    bind the verifier to someone else's inputs.
    """
    profile = json.loads((_run_dir(tmp_path) / "run_profile.json").read_text())
    home = tmp_path / "home"
    overlay = home / "recipes" / "frontier-llm-research"
    overlay.mkdir(parents=True)
    text = (REPO / "recipes" / "frontier-llm-research" / "workflow.yaml").read_text()
    (overlay / "workflow.yaml").write_text(
        text.replace("research_completeness", "overlay_node"), encoding="utf-8")
    profile["roots"]["home"] = str(home)
    run = tmp_path / "run"
    (run / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    _materialize(run, "overlay_node")

    env = ver._node_input_env(str(REPO), str(run),
                              str(VERIFIERS / "verify-aggregation.py"), str(home))

    assert env["MINI_ORK_NODE_INPUT_DIR"].endswith("overlay_node")


def test_a_gone_engine_root_falls_back_to_the_callers(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    run = _run_dir(tmp_path)
    (run / "run_profile.json").write_text(
        json.dumps({"recipe": "frontier-llm-research",
                    "roots": {"engine": str(tmp_path / "gone")}}), encoding="utf-8")

    found = ver._find_verifier_script(
        "verifiers/verify-aggregation.py", str(REPO), str(tmp_path / "home"), str(run))

    assert found == str(VERIFIERS / "verify-aggregation.py")


def test_the_ambient_recipe_still_resolves_a_run_with_no_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECIPE", "frontier-llm-research")

    found = ver._find_verifier_script(
        "verifiers/verify-aggregation.py", str(tmp_path), str(REPO), str(tmp_path / "no-run"))

    assert found == str(VERIFIERS / "verify-aggregation.py")


# ── what the dispatched process actually sees ───────────────────────────────


def test_main_hands_the_recovered_boundary_to_the_verifier(tmp_path, monkeypatch):
    """The end-to-end shape: `mini-ork verify` → a script that reads the pair.

    Every helper below could be right while ``main`` still dispatched with only
    ``ARTIFACT_PATH``; this drives the real entry point and reads the
    environment the verifier was started with.
    """
    run = _run_dir(tmp_path)
    _materialize(run, "research_completeness")
    # The probe poses as the recipe's verifier in a project overlay, so the
    # dispatcher resolves it ahead of the engine's own copy.
    home = tmp_path / "home"
    probe_dir = home / "recipes" / "frontier-llm-research" / "verifiers"
    probe_dir.mkdir(parents=True)
    profile = json.loads((run / "run_profile.json").read_text())
    profile["roots"]["home"] = str(home)
    (run / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    probe = probe_dir / "verify-aggregation.py"
    probe.write_text(
        "import json, os\n"
        "keys = ('ARTIFACT_PATH', 'MINI_ORK_NODE_INPUT_DIR', 'MINI_ORK_NODE_INPUT_MANIFEST')\n"
        "seen = {k: os.environ.get(k, '') for k in keys}\n"
        "with open(os.path.join(os.environ['MINI_ORK_RUN_DIR'], 'probe-env.json'), 'w') as fh:\n"
        "    json.dump(seen, fh)\n"
        # A verifier that exits 0 with no output is a suppressed vacuous pass,
        # so the probe has to leave evidence like a real one does.
        "print('{\"verifier\": \"probe\", \"pass\": true}')\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "task_class": "generic",
        "artifact_contract": {"success_verifiers": ["verifiers/verify-aggregation.py"]},
    }), encoding="utf-8")
    artifact = tmp_path / "aggregation.md"
    artifact.write_text("# done\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    # A boundary inherited from the node that invoked verify must not leak.
    monkeypatch.setenv("MINI_ORK_NODE_INPUT_DIR", "/parent/node/inputs")
    monkeypatch.delenv("MINI_ORK_NODE_INPUT_MANIFEST", raising=False)

    rc = ver.main([str(artifact), "--plan", str(plan)], db=str(tmp_path / "state.db"),
                  root=str(tmp_path / "root"))

    assert rc == 0
    seen = json.loads((run / "probe-env.json").read_text())
    assert seen["ARTIFACT_PATH"] == str(artifact)
    assert seen["MINI_ORK_NODE_INPUT_DIR"].endswith("inputs/research_completeness")
    assert seen["MINI_ORK_NODE_INPUT_MANIFEST"].endswith(
        "research_completeness.inputs.json")


# ── the warning follows the script, not the run ─────────────────────────────


def test_the_gap_warning_only_applies_to_a_script_that_reads_the_variable(tmp_path):
    reader = tmp_path / "reader.py"
    reader.write_text("import os\nprint(os.environ['MINI_ORK_NODE_INPUT_DIR'])\n",
                      encoding="utf-8")
    helper_reader = tmp_path / "helper_reader.py"
    helper_reader.write_text("from _inputs import input_dir\nprint(input_dir())\n",
                             encoding="utf-8")
    silent = tmp_path / "silent.py"
    silent.write_text("print('ok')\n", encoding="utf-8")
    # The other shipped helper shape: a thin wrapper over the recipe's `_stage`.
    stage_reader = tmp_path / "stage_reader.py"
    stage_reader.write_text("from _stage import input_single\nprint(input_single('x', '.json'))\n",
                            encoding="utf-8")

    assert ver._reads_input_dir(str(reader)) is True
    assert ver._reads_input_dir(str(helper_reader)) is True
    assert ver._reads_input_dir(str(stage_reader)) is True
    assert ver._reads_input_dir(str(silent)) is False
    assert ver._reads_input_dir(str(tmp_path / "missing.py")) is False


# ── a launcher that sets -P must not break the sibling import ───────────────


def test_the_helper_import_survives_pythonsafepath(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(VERIFIERS / "verify-aggregation.py")],
        env={**os.environ, "PYTHONSAFEPATH": "1",
             "MINI_ORK_NODE_INPUT_DIR": str(tmp_path / "empty"),
             "MINI_ORK_RUN_DIR": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )

    assert "_inputs" not in proc.stderr, proc.stderr
    assert "No module named" not in proc.stderr, proc.stderr


# ── no shell twin comes back ────────────────────────────────────────────────


def test_no_verifier_shell_twin_exists():
    """Guard, not a discovery: .sh verifiers were retired in the bash removal."""
    shells = sorted(p.name for p in VERIFIERS.glob("*.sh"))
    assert not shells, f"shell verifiers reintroduced: {shells}"
