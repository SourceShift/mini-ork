"""framework-edit kickoff-contract verifier.

A framework-edit run must fail when it does not deliver what its kickoff
declared. `overlay_plan` replaces the plan's outputs with the recipe's own
contract, so a kickoff's "Required artifacts" never reach plan.json, and
nothing runs its "Self-application measurement" — the implementer is trusted
to. Four SDE runs (2026-10-13/14) ended `verdict: pass` with none of their
declared artifacts produced.

Every case here is hermetic: a temp run dir with a `context-pack.v2.json`
snapshot + `run_profile.json`, a temp target dir, and the verifier run as a
subprocess exactly as the executor runs it (cf. test_framework_edit_static_check).
`MINI_ORK_*` / `MO_*` are scrubbed from the child env so a leaked harness
variable cannot make the snapshot-wins or sentinel-absence cases pass spuriously.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "kickoff-contract.py"
WORKFLOW = REPO / "recipes" / "framework-edit" / "workflow.yaml"

AUTO_TRIAGE_MARKER = "<!-- mini-ork:origin=auto-triage -->"


# ── harness ─────────────────────────────────────────────────────────────────

def _env(run_dir: Path, target: Path | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env["MINI_ORK_RUN_DIR"] = str(run_dir)
    if target is not None:
        env["MO_TARGET_CWD"] = str(target)
    if extra:
        env.update(extra)
    return env


def _provision(
    tmp_path: Path,
    *,
    snapshot: str | None = None,
    pack: bool = True,
    kickoff_text: str | None = None,
    kickoff_path: Path | None = None,
) -> Path:
    """Build a run dir. ``snapshot`` goes in the pack; a ``kickoff_path`` file is
    written when ``kickoff_text`` is given (the live kickoff the run may edit)."""
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    if pack:
        (run / "context-pack.v2.json").write_text(
            json.dumps({"kickoff_snapshot": snapshot if snapshot is not None else ""}))
    profile: dict[str, str] = {}
    if kickoff_text is not None:
        path = kickoff_path or (tmp_path / "kickoff.md")
        path.write_text(kickoff_text)
        profile["kickoff_path"] = str(path)
    (run / "run_profile.json").write_text(json.dumps(profile))
    return run


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    return target


def _invoke(run_dir: Path, target: Path, *, args=(), extra=None, timeout: int = 90):
    return subprocess.run(
        [sys.executable, str(VERIFIER), *args],
        cwd=str(target), env=_env(run_dir, target, extra),
        capture_output=True, text=True, timeout=timeout)


def _payload(run_dir: Path) -> dict:
    return json.loads((run_dir / "verifier_kickoff-contract.json").read_text())


def _measure(commands: str) -> str:
    return "## Self-application measurement\n\nprose\n\n```measure\n" + commands + "\n```\n"


def _artifacts(*paths: str) -> str:
    bullets = "\n".join(f"- `${{MINI_ORK_RUN_DIR}}/{p}`" for p in paths)
    return "## Required artifacts\n\n" + bullets + "\n"


# ── required artifacts ──────────────────────────────────────────────────────

def test_present_non_empty_artifact_passes(tmp_path) -> None:
    run = _provision(tmp_path, snapshot=_artifacts("out.json"))
    (run / "out.json").write_text("done\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["missing"] == []
    assert "note" not in payload


def test_missing_artifact_fails_and_names_path(tmp_path) -> None:
    run = _provision(tmp_path, snapshot=_artifacts("diagnosis-replay.json"))
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["missing"] == [str(run / "diagnosis-replay.json")]


def test_zero_byte_artifact_fails(tmp_path) -> None:
    run = _provision(tmp_path, snapshot=_artifacts("out.json"))
    (run / "out.json").write_text("")  # exists, but empty
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    assert _payload(run)["missing"] == [str(run / "out.json")]


def test_non_run_local_token_is_ignored(tmp_path) -> None:
    """Only ``${MINI_ORK_RUN_DIR}/``-prefixed tokens are collected."""
    snapshot = "## Required artifacts\n\n- `docs/plan.md`\n- `/etc/hosts`\n"
    run = _provision(tmp_path, snapshot=snapshot)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert _payload(run)["artifacts"] == {}


def test_artifact_escaping_the_run_dir_is_refused(tmp_path) -> None:
    """``${MINI_ORK_RUN_DIR}/../x`` leaves the run; the verifier's visibility
    limit is run-local, so the declaration FAILS — it is never silently ignored,
    even when the escaped file happens to exist."""
    run = _provision(tmp_path, snapshot=_artifacts("../escaped.json"))
    (tmp_path / "escaped.json").write_text("real file, wrong side of the fence\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["escaped"] == ["${MINI_ORK_RUN_DIR}/../escaped.json"]
    assert payload["missing"] == ["${MINI_ORK_RUN_DIR}/../escaped.json"]


# ── measure blocks ──────────────────────────────────────────────────────────

def test_measure_writes_required_artifact_passes(tmp_path) -> None:
    snapshot = (_measure('echo hello > "${MINI_ORK_RUN_DIR}/out.json"')
                + "\n" + _artifacts("out.json"))
    run = _provision(tmp_path, snapshot=snapshot)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (run / "out.json").read_text().strip() == "hello"


def test_measure_nonzero_rc_fails(tmp_path) -> None:
    run = _provision(tmp_path, snapshot=_measure("exit 1"))
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["measures"][0]["rc"] == 1


def test_measure_timeout_fails(tmp_path) -> None:
    run = _provision(tmp_path, snapshot=_measure("sleep 5; echo late"))
    proc = _invoke(run, _target(tmp_path), extra={"MO_KICKOFF_MEASURE_TIMEOUT_S": "2"})
    assert proc.returncode == 1
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["measures"][0]["timed_out"] is True


def test_measure_runs_with_run_dir_in_env(tmp_path) -> None:
    """The command expands ``${MINI_ORK_RUN_DIR}`` from the env it is handed."""
    run = _provision(tmp_path, snapshot=_measure('echo "$MINI_ORK_RUN_DIR" > "${MINI_ORK_RUN_DIR}/out.txt"'))
    _invoke(run, _target(tmp_path))
    assert (run / "out.txt").read_text().strip() == str(run)


def test_measure_gets_a_scrubbed_env(tmp_path) -> None:
    """A measure block is operator prose the run EXECUTES: provider credentials
    must not reach it (and so cannot reach the evidence log), while the two keys
    a measure legitimately needs are put back."""
    run = _provision(tmp_path, snapshot=_measure('env > "${MINI_ORK_RUN_DIR}/env.txt"'))
    _invoke(run, _target(tmp_path), extra={"OPENAI_API_KEY": "sk-must-not-leak",
                                           "MINI_ORK_SECRETS": "/secrets.local.sh",
                                           "MINI_ORK_HOME": str(tmp_path / "home")})
    text = (run / "env.txt").read_text()
    assert "sk-must-not-leak" not in text
    assert "OPENAI_API_KEY" not in text
    assert "MINI_ORK_SECRETS" not in text
    assert f"MINI_ORK_RUN_DIR={run}" in text
    assert f"MINI_ORK_HOME={tmp_path / 'home'}" in text


def test_malformed_measure_timeout_fails_closed(tmp_path) -> None:
    """A knob that is PRESENT but not a positive integer is a broken knob, not
    permission to run the command under some default."""
    run = _provision(tmp_path, snapshot=_measure('touch "${MINI_ORK_RUN_DIR}/sentinel"'))
    proc = _invoke(run, _target(tmp_path), extra={"MO_KICKOFF_MEASURE_TIMEOUT_S": "soon"})
    assert proc.returncode == 1
    assert not (run / "sentinel").exists()  # never ran under the broken knob
    payload = _payload(run)
    assert payload["pass"] is False
    assert payload["measures"][0]["pass"] is False
    assert "not an integer" in payload["measures"][0]["error"]


# ── opt-out paths ───────────────────────────────────────────────────────────

def test_auto_triage_marker_suppresses_measure(tmp_path) -> None:
    snapshot = (AUTO_TRIAGE_MARKER + "\n"
                + "# raw log excerpt\n\n"
                + _measure('touch "${MINI_ORK_RUN_DIR}/sentinel"'))
    run = _provision(tmp_path, snapshot=snapshot)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert not (run / "sentinel").exists()
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["note"] == "auto-triage kickoff: no operator contract"
    assert payload["measures"] == []


def test_inline_backtick_in_prose_is_not_run(tmp_path) -> None:
    snapshot = ("## Self-application measurement\n\n"
                'By hand: `touch "${MINI_ORK_RUN_DIR}/sentinel"`.\n')
    run = _provision(tmp_path, snapshot=snapshot)
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert not (run / "sentinel").exists()
    assert _payload(run)["note"] == "no kickoff contract declared"


def test_snapshot_wins_over_edited_live_kickoff(tmp_path) -> None:
    """The run received a contract-less snapshot; editing the live kickoff file
    afterwards to add a measure block must not make it execute."""
    run = _provision(
        tmp_path,
        snapshot="# Goal\n\nsnapshot declares no contract\n",
        kickoff_text=_measure('touch "${MINI_ORK_RUN_DIR}/sentinel"'))
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert not (run / "sentinel").exists()
    payload = _payload(run)
    assert payload["kickoff_source"] == "snapshot"
    assert payload["note"] == "no kickoff contract declared"


def test_no_contract_sections_passes_with_note(tmp_path) -> None:
    run = _provision(tmp_path, snapshot="# Goal\n\nAdd a badge.\n\n## Done When\n\n- it works\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    payload = _payload(run)
    assert payload["pass"] is True
    assert payload["note"] == "no kickoff contract declared"
    assert payload["measures"] == []


def test_falls_back_to_profile_kickoff_when_no_pack(tmp_path) -> None:
    run = _provision(tmp_path, pack=False, kickoff_text=_artifacts("out.json"))
    (run / "out.json").write_text("x\n")
    proc = _invoke(run, _target(tmp_path))
    assert proc.returncode == 0
    assert _payload(run)["kickoff_source"] == "profile-file"


# ── --no-measure ────────────────────────────────────────────────────────────

def test_no_measure_skips_commands_but_still_fails_on_missing_artifact(tmp_path) -> None:
    snapshot = (_measure('echo hi > "${MINI_ORK_RUN_DIR}/out.json"')
                + "\n" + _artifacts("out.json"))
    run = _provision(tmp_path, snapshot=snapshot)
    proc = _invoke(run, _target(tmp_path), args=("--no-measure",))
    assert proc.returncode == 1
    assert not (run / "out.json").exists()  # the measure never ran
    payload = _payload(run)
    assert payload["missing"] == [str(run / "out.json")]
    assert payload["measures"][0]["skipped"] is True


# ── engine contract: the payload must read as verifier evidence ─────────────

def test_payload_is_a_parseable_verifier_envelope(tmp_path) -> None:
    """`levels._contract_from_recipe_verifiers` needs a bool `pass` plus a list
    `checks`; a payload without `checks` reads as `unparsable` and flips every
    framework-edit run's contract level to UNVERIFIED. Passing run → PROVEN,
    failing run → REFUTED."""
    from mini_ork.verify import levels

    ok_run = _provision(tmp_path / "ok", snapshot=_artifacts("out.json"))
    (ok_run / "out.json").write_text("done\n")
    assert _invoke(ok_run, _target(tmp_path)).returncode == 0
    assert isinstance(_payload(ok_run)["checks"], list)
    assert levels._contract_from_recipe_verifiers(str(ok_run))[0] == levels.PROVEN

    bad_run = _provision(tmp_path / "bad", snapshot=_artifacts("out.json"))
    assert _invoke(bad_run, _target(tmp_path)).returncode == 1
    assert levels._contract_from_recipe_verifiers(str(bad_run))[0] == levels.REFUTED


def test_no_measure_checks_do_not_read_as_failures(tmp_path) -> None:
    """A skipped measure is judged by nothing: its check row must not fail."""
    run = _provision(tmp_path, snapshot=_measure("echo hi"))
    assert _invoke(run, _target(tmp_path), args=("--no-measure",)).returncode == 0
    assert all(c["pass"] for c in _payload(run)["checks"])


def test_failing_payload_carries_a_single_reason_string(tmp_path) -> None:
    """`diagnose.py:446-451` reads a single `reason`/`summary` string for the
    failure detail; a `reasons` list alone would leave a diagnosis without one."""
    run = _provision(tmp_path, snapshot=_artifacts("out.json"))
    assert _invoke(run, _target(tmp_path)).returncode == 1
    payload = _payload(run)
    assert isinstance(payload["reason"], str)
    assert "required-artifacts failed:" in payload["reason"]


# ── workflow wiring ─────────────────────────────────────────────────────────

def test_workflow_compiles_with_kickoff_contract_node() -> None:
    from mini_ork.workflow.compiler import compile_workflow

    compiled = compile_workflow(WORKFLOW)
    node = compiled.nodes["kickoff_contract_verifier"]
    assert node.type == "verifier"
    assert node.verifier_ref == "verifiers/kickoff-contract.py"
    assert node.model_lane == "verifier"
    assert node.prompt_ref == ""
    assert node.dispatch_mode == "serial"

    # verifies: implementer -> kickoff_contract_verifier
    assert "implementer" in compiled.control_parents["kickoff_contract_verifier"]
    # depends_on: kickoff_contract_verifier -> reviewer
    assert "kickoff_contract_verifier" in compiled.control_parents["reviewer"]
    # retries: kickoff_contract_verifier -> implementer, max_rounds 2
    retry = compiled.retry_edges["kickoff_contract_verifier"]
    assert retry.target == "implementer"
    assert retry.max_rounds == 2

    # escalates_to -> rollback. framework-edit declares no `recursion:` block, so
    # the compiler keeps escalation edges out of the compiled graph; assert the
    # declared edge in the YAML itself.
    import yaml

    edges = yaml.safe_load(WORKFLOW.read_text())["edges"]
    assert {"from": "kickoff_contract_verifier", "to": "rollback",
            "edge_type": "escalates_to"} in edges


def test_recipe_shape_verifier_still_matches_the_recipe(tmp_path) -> None:
    """`framework-edit-shape.py` asserts the recipe's own node count and lane
    map; a node added here without updating it breaks every framework-edit run's
    success gate on the tree that ships."""
    verifier = REPO / "recipes" / "framework-edit" / "verifiers" / "framework-edit-shape.py"
    run = tmp_path / "run"
    run.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run), MINI_ORK_ROOT=str(REPO))
    proc = subprocess.run([sys.executable, str(verifier)], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=120)
    payload = json.loads(proc.stdout)
    assert payload["pass"] is True, payload["failed_checks"]


# ── --replay-check ──────────────────────────────────────────────────────────

_REPLAY = (
    ("sde-1-diagnose-20261013-232707", "diagnosis-replay.json", False),
    ("sde-2-kernel-20261013-232708", "kernel-refusal.json", False),
    ("sde-2-kernel-20261014-000931", "kernel-refusal.json", False),
    ("sde-1-diagnose-20261014-000929", "diagnosis-replay.json", True),
)


def _replay_tree(tmp_path: Path, *, omit: str | None = None) -> Path:
    runs = tmp_path / "runs"
    for run_id, artifact, present in _REPLAY:
        if run_id == omit:
            continue
        run = runs / run_id
        run.mkdir(parents=True)
        (run / "context-pack.v2.json").write_text(
            json.dumps({"kickoff_snapshot": _artifacts(artifact)}))
        (run / "run_profile.json").write_text("{}")
        if present:
            (run / artifact).write_text("produced\n")
    return runs


def test_replay_check_matches_expected_matrix(tmp_path) -> None:
    runs = _replay_tree(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(VERIFIER), "--replay-check", str(runs)],
        cwd=str(REPO), env={k: v for k, v in os.environ.items()
                            if not k.startswith(("MINI_ORK_", "MO_"))},
        capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    matrix = json.loads(proc.stdout)
    assert set(matrix) == {rid for rid, _, _ in _REPLAY}
    for run_id, artifact, present in _REPLAY:
        assert matrix[run_id]["pass"] is present, run_id
        if not present:
            assert matrix[run_id]["missing"] == [str(runs / run_id / artifact)]


def test_replay_check_fails_loudly_on_absent_run_dir(tmp_path) -> None:
    runs = _replay_tree(tmp_path, omit="sde-2-kernel-20261014-000931")
    proc = subprocess.run(
        [sys.executable, str(VERIFIER), "--replay-check", str(runs)],
        cwd=str(REPO), env={k: v for k, v in os.environ.items()
                            if not k.startswith(("MINI_ORK_", "MO_"))},
        capture_output=True, text=True, timeout=90)
    assert proc.returncode != 0
    assert "expected run dir absent" in proc.stderr


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
