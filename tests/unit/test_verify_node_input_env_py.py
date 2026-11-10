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


# ── no shell twin comes back ────────────────────────────────────────────────


def test_no_verifier_shell_twin_exists():
    """Guard, not a discovery: .sh verifiers were retired in the bash removal."""
    shells = sorted(p.name for p in VERIFIERS.glob("*.sh"))
    assert not shells, f"shell verifiers reintroduced: {shells}"
