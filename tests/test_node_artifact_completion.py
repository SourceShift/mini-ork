"""K5: an LLM node whose declared artifacts are fresh and valid is COMPLETE.

The (rc, text) a dispatch returns is the agent's self-report. SDD K2-K4 lost
whole runs to it: the implementer delivered on disk, but the engine's summary
writer clobbered the canonical implementer-summary.json, and the
contract_compiler's artifacts were discarded because its handshake timed out.
These tests pin the recompute-from-artifacts contract and, just as much, that a
missing / invalid / stale artifact or a strict_handshake node keeps failing
with its original reason.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork import execute_compat as ec  # noqa: E402
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import execute_handlers as eh  # noqa: E402
from mini_ork.workflow import ArtifactLedger  # noqa: E402
from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow  # noqa: E402
from mini_ork.workflow.store import make_artifact_store  # noqa: E402

K3_SUMMARY = {"status": "implemented", "files_changed": ["a.py"]}


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """This suite may run inside a live mini-ork node whose env points at a real
    home, recipe and target tree; none of that may leak into a handler."""
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))


def _tier1_ready(summary: dict) -> bool:
    """The gate every recursive-validate-impl tier verifier applies."""
    touched = summary.get("touched_files")
    return summary.get("ready_for_tier1") is True and isinstance(touched, list) and bool(touched)


# ── T1-T4: summary-shape normalization ──────────────────────────────────────

def test_t1_k3_shape_is_normalized_and_tier1_ready():
    s = ec.normalize_implementer_summary(K3_SUMMARY)
    assert s["ready_for_tier1"] is True
    assert s["touched_files"] == ["a.py"]
    assert s["status"] == "implemented" and s["files_changed"] == ["a.py"]
    assert K3_SUMMARY == {"status": "implemented", "files_changed": ["a.py"]}  # input not mutated


def test_t2_canonical_shape_is_idempotent():
    canonical = {"ready_for_tier1": True, "touched_files": ["x.py"], "iteration": 1}
    once = ec.normalize_implementer_summary(canonical)
    assert once == canonical
    assert ec.normalize_implementer_summary(once) == once


def test_t3_explicit_not_ready_is_preserved():
    s = ec.normalize_implementer_summary({"ready_for_tier1": False, "files_changed": ["a.py"]})
    assert s["ready_for_tier1"] is False
    assert not _tier1_ready(s)


@pytest.mark.parametrize("files_changed", [[], "a.py", None, [1, ""], {"a.py": 1}])
def test_t4_empty_or_non_list_files_changed_never_infers_ready(files_changed):
    s = ec.normalize_implementer_summary({"status": "implemented", "files_changed": files_changed})
    assert s["ready_for_tier1"] is False
    assert s["touched_files"] == []


# ── T5: on-disk write-back feeds the real tier1 gate ────────────────────────

def test_t5_file_normalization_rewrites_to_canonical_shape(tmp_path):
    path = tmp_path / "implementer-summary.json"
    path.write_text(json.dumps(K3_SUMMARY), encoding="utf-8")

    assert ec.normalize_implementer_summary_file(str(path)) is True

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert _tier1_ready(rewritten)
    assert rewritten["files_changed"] == ["a.py"]  # publisher's commit gate input kept
    assert not list(tmp_path.glob(".implementer-summary.json.*.tmp"))  # no temp debris


def test_t5_invalid_json_returns_false_and_leaves_file_untouched(tmp_path):
    path = tmp_path / "implementer-summary.json"
    path.write_text("{not json", encoding="utf-8")
    before = path.stat().st_mtime_ns

    assert ec.normalize_implementer_summary_file(str(path)) is False
    assert path.read_text(encoding="utf-8") == "{not json"
    assert path.stat().st_mtime_ns == before
    assert ec.normalize_implementer_summary_file(str(tmp_path / "absent.json")) is False


def test_t5_real_tier1_verifier_accepts_the_normalized_k3_summary(tmp_path):
    """End-to-end on the actual gate that failed SDD K3."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    summary = run_dir / "implementer-summary.json"
    summary.write_text(json.dumps(K3_SUMMARY), encoding="utf-8")
    script = REPO / "recipes" / "recursive-validate-impl" / "verifiers" / "tier1-compile-typecheck.py"
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir)}

    before = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert before.returncode == 1  # the K3 failure, reproduced

    assert ec.normalize_implementer_summary_file(str(summary))
    after = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env,
                           capture_output=True, text=True)
    assert after.returncode == 0, after.stdout + after.stderr
    assert json.loads(after.stdout)["pass"] is True


# ── T6: the engine summary writer merges instead of clobbering ──────────────

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_t6_summary_writer_keeps_the_agent_summary_and_adds_canonical_keys(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "a.py").write_text("x = 2\n")
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "ready_for_tier1": True, "touched_files": ["a.py"], "iteration": 1,
        "dod_probe_notes": [{"id": "P1", "status": "pass"}],
    }), encoding="utf-8")

    files = ex._write_implementer_summary(str(run_dir), str(repo), "impl.log")

    summary = json.loads((run_dir / "implementer-summary.json").read_text(encoding="utf-8"))
    expected_changed = [os.path.realpath(repo / "a.py")]
    assert files == expected_changed
    assert summary["files_changed"] == expected_changed       # engine-derived, git truth
    assert summary["worktree_path"] == str(repo)
    assert summary["status"] == "implemented"
    assert summary["touched_files"] == ["a.py"]               # agent's list survives
    assert summary["ready_for_tier1"] is True
    assert summary["iteration"] == 1 and summary["dod_probe_notes"][0]["id"] == "P1"


def test_t6_summary_writer_fills_canonical_keys_without_an_agent_summary(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "a.py").write_text("x = 2\n")

    ex._write_implementer_summary(str(run_dir), str(repo), "impl.log")

    summary = json.loads((run_dir / "implementer-summary.json").read_text(encoding="utf-8"))
    assert summary["touched_files"] == summary["files_changed"] == [os.path.realpath(repo / "a.py")]
    assert _tier1_ready(summary)


def test_t6_summary_writer_ignores_a_stale_summary_from_an_earlier_iteration(tmp_path):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stale = run_dir / "implementer-summary.json"
    stale.write_text(json.dumps({"touched_files": ["old.py"], "ready_for_tier1": True,
                                 "iteration": 1}), encoding="utf-8")
    past = time.time() - 120
    os.utime(stale, (past, past))
    (repo / "a.py").write_text("x = 3\n")

    ex._write_implementer_summary(str(run_dir), str(repo), "impl.log", since_mtime=time.time() - 5)

    summary = json.loads(stale.read_text(encoding="utf-8"))
    assert "iteration" not in summary
    assert summary["touched_files"] == [os.path.realpath(repo / "a.py")]


# ── T7-T11: researcher artifact completion (the contract_compiler case) ─────

_COMPILER_WORKFLOW = """\
nodes:
  - name: contract_compiler
    type: researcher
    dispatch_mode: serial
{extra}    outputs:
      - {{name: spec_index, path: spec-cards/index.json, kind: file}}
"""


def _compiler_ctx(tmp_path: Path, dispatch_fn, *, strict: bool | None = None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    extra = "" if strict is None else f"    strict_handshake: {str(strict).lower()}\n"
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(_COMPILER_WORKFLOW.format(extra=extra), encoding="utf-8")
    compiled = compile_workflow(workflow)
    ledger = ArtifactLedger(store=make_artifact_store("r", base_dir=run_dir))
    traces: list[tuple] = []
    ctx = eh.NodeDispatch(
        node_id="contract_compiler", node_type="researcher", node_desc="compile the contract",
        prompt_ref="", verifier_ref="", model_lane="researcher", node_requires_capabilities="",
        root=str(REPO), run_dir=str(run_dir), plan_path="", task_class="spec_driven_delivery",
        db="", run_id="r", recipe="spec-driven-delivery", workflow=str(workflow),
        lane="researcher", run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="plan", learned="", dispatch_fn=dispatch_fn,
        trace=lambda *a, **k: traces.append(a), charge=lambda: None,
        artifact_ledger=ledger, compiled_workflow=compiled,
    )
    artifact = Path(ledger.output_path(compiled, "contract_compiler", "spec_index"))
    return ctx, artifact, traces


def _writes_then_times_out(artifact_getter, content):
    def dispatch(task_class, lane, prompt):
        artifact = artifact_getter()
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            artifact.write_text(content, encoding="utf-8")
        return 124, ""
    return dispatch


def test_t7_compiler_artifacts_present_handshake_absent_completes(tmp_path, capsys):
    box = {}
    ctx, artifact, traces = _compiler_ctx(
        tmp_path, _writes_then_times_out(lambda: box["a"], '{"cards": 35}'))
    box["a"] = artifact

    assert eh._handle_researcher(ctx) == (0, "done")

    assert ec.ARTIFACT_COMPLETION_LOG in capsys.readouterr().err
    assert traces and traces[-1][1] == "success"
    assert json.loads(artifact.read_text(encoding="utf-8")) == {"cards": 35}  # not overwritten
    manifest = Path(ctx.run_dir) / "workspace" / "manifests" / "contract_compiler.outputs.json"
    assert manifest.is_file()  # the declared output was published to the ledger


def test_t7_legacy_researcher_without_declared_outputs_uses_its_output_file(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_file = Path(ex._researcher_output_file(str(run_dir), "spec-driven-delivery", "contract_compiler"))
    traces: list[tuple] = []
    ctx = eh.NodeDispatch(
        node_id="contract_compiler", node_type="researcher", node_desc="d", prompt_ref="",
        verifier_ref="", model_lane="researcher", node_requires_capabilities="", root=str(REPO),
        run_dir=str(run_dir), plan_path="", task_class="t", db="", run_id="r",
        recipe="spec-driven-delivery", workflow="", lane="researcher", run_dir_eff=str(run_dir),
        recipe_dir="", prompt_file="", plan_content="", learned="",
        dispatch_fn=_writes_then_times_out(lambda: out_file, '{"ok": true}'),
        trace=lambda *a, **k: traces.append(a), charge=lambda: None,
    )

    assert eh._handle_researcher(ctx) == (0, "done")
    assert ec.ARTIFACT_COMPLETION_LOG in capsys.readouterr().err


def test_t8_no_artifact_keeps_the_original_failure(tmp_path, capsys):
    box = {}
    ctx, artifact, traces = _compiler_ctx(tmp_path, _writes_then_times_out(lambda: box["a"], None))
    box["a"] = artifact

    assert eh._handle_researcher(ctx) == (1, "timeout")
    assert traces[-1][1] == "failure" and traces[-1][-1] == "timeout"
    assert ec.ARTIFACT_COMPLETION_LOG not in capsys.readouterr().err


def test_t9_invalid_json_artifact_keeps_the_original_failure(tmp_path):
    box = {}
    ctx, artifact, traces = _compiler_ctx(
        tmp_path, _writes_then_times_out(lambda: box["a"], "{truncated"))
    box["a"] = artifact

    assert eh._handle_researcher(ctx) == (1, "timeout")
    assert traces[-1][-1] == "timeout"


def test_t10_stale_artifact_from_a_previous_iteration_keeps_the_failure(tmp_path):
    def dispatch(task_class, lane, prompt):
        return 124, ""   # writes nothing this iteration

    ctx, artifact, traces = _compiler_ctx(tmp_path, dispatch)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{"cards": 35}', encoding="utf-8")
    past = time.time() - 600
    os.utime(artifact, (past, past))

    assert eh._handle_researcher(ctx) == (1, "timeout")
    assert traces[-1][-1] == "timeout"


def test_t11_strict_handshake_node_keeps_the_failure(tmp_path, capsys):
    box = {}
    ctx, artifact, traces = _compiler_ctx(
        tmp_path, _writes_then_times_out(lambda: box["a"], '{"cards": 35}'), strict=True)
    box["a"] = artifact

    assert eh._handle_researcher(ctx) == (1, "timeout")
    assert ec.ARTIFACT_COMPLETION_LOG not in capsys.readouterr().err


def test_t11_cost_limit_failure_without_fresh_artifacts_keeps_its_reason(tmp_path):
    def dispatch(task_class, lane, prompt):
        return 1, "cost_circuit_open"

    ctx, _artifact, _traces = _compiler_ctx(tmp_path, dispatch)
    assert eh._handle_researcher(ctx) == (1, "cost_limit")


def test_declared_artifacts_ok_reports_each_violation(tmp_path):
    now = time.time() - 1
    good = tmp_path / "good.json"
    good.write_text("{}", encoding="utf-8")
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")

    assert ec.declared_artifacts_ok([str(good)], since_mtime=now) == (True, "ok")
    assert ec.declared_artifacts_ok([], since_mtime=now) == (False, "no_declared_artifacts")
    ok, why = ec.declared_artifacts_ok([str(good), str(tmp_path / "missing.json")], since_mtime=now)
    assert not ok and why.endswith("missing")
    ok, why = ec.declared_artifacts_ok([str(empty)], since_mtime=now)
    assert not ok and why.endswith("empty")
    ok, why = ec.declared_artifacts_ok([str(tmp_path)], since_mtime=now)
    assert not ok and "not a regular file" in why


# ── T14: per-kind completion contracts — the fabrication gate ───────────────
#
# The generic checks (regular / non-empty / fresh / JSON-parseable) pass for a
# well-formed fabrication. These pin the per-kind contract layer that closes
# that gap, and just as much that an unknown basename keeps exactly today's
# checks (no regression on the default path).

def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def _summary_path(run_dir: Path, payload) -> str:
    path = run_dir / "implementer-summary.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return str(path)


def test_t14_empty_files_changed_is_rejected(tmp_path):
    path = _summary_path(_run_dir(tmp_path), {"status": "implemented", "files_changed": []})

    ok, why = ec.declared_artifacts_ok([path], since_mtime=time.time() - 1)

    assert not ok
    assert "implementer-summary.json contract" in why and "empty" in why


def test_t14_missing_claimed_relative_file_is_rejected(tmp_path):
    run_dir = _run_dir(tmp_path)
    repo = tmp_path / "target"
    repo.mkdir()
    path = _summary_path(run_dir, {"files_changed": ["a.py"]})  # a.py absent under repo

    ok, why = ec.declared_artifacts_ok([path], since_mtime=time.time() - 1, repo_root=str(repo))

    assert not ok
    assert "implementer-summary.json contract" in why and "missing" in why


def test_t14_stale_claimed_file_is_rejected(tmp_path):
    run_dir = _run_dir(tmp_path)
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    past = time.time() - 600
    os.utime(repo / "a.py", (past, past))  # older than the dispatch-start marker
    path = _summary_path(run_dir, {"files_changed": ["a.py"]})  # summary itself is fresh

    ok, why = ec.declared_artifacts_ok([path], since_mtime=time.time() - 1, repo_root=str(repo))

    assert not ok
    assert "implementer-summary.json contract" in why and "stale" in why


def test_t14_fresh_claimed_file_passes(tmp_path):
    run_dir = _run_dir(tmp_path)
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    path = _summary_path(run_dir, {"files_changed": ["a.py"]})

    assert ec.declared_artifacts_ok(
        [path], since_mtime=time.time() - 1, repo_root=str(repo)) == (True, "ok")


def test_t14_honest_abstention_passes(tmp_path):
    """A "no repro" report with an explicit not-ready + reason is a deliverable."""
    path = _summary_path(_run_dir(tmp_path), {"ready_for_tier1": False, "block_reason": "no repro"})

    assert ec.declared_artifacts_ok([path], since_mtime=time.time() - 1) == (True, "ok")


def test_t14_abstention_without_a_block_reason_is_rejected(tmp_path):
    path = _summary_path(_run_dir(tmp_path), {"ready_for_tier1": False, "files_changed": []})

    ok, why = ec.declared_artifacts_ok([path], since_mtime=time.time() - 1)
    assert not ok and "implementer-summary.json contract" in why


def test_t14_plan_json_step_shape(tmp_path):
    run_dir = _run_dir(tmp_path)
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"steps": []}), encoding="utf-8")

    ok, why = ec.declared_artifacts_ok([str(plan)], since_mtime=time.time() - 1)
    assert not ok and "plan.json contract" in why and "empty" in why

    plan.write_text(json.dumps({"steps": [{"id": "s1"}]}), encoding="utf-8")
    assert ec.declared_artifacts_ok([str(plan)], since_mtime=time.time() - 1) == (True, "ok")


def test_t14_patch_requires_a_header_pair(tmp_path):
    patch = _run_dir(tmp_path) / "framework-edit.patch"
    patch.write_text("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n", encoding="utf-8")
    assert ec.declared_artifacts_ok([str(patch)], since_mtime=time.time() - 1) == (True, "ok")

    patch.write_text("prose with no diff headers at all\n", encoding="utf-8")
    ok, why = ec.declared_artifacts_ok([str(patch)], since_mtime=time.time() - 1)
    assert not ok and "patch contract" in why and "header pair" in why


def test_t14_diff_extension_matches_the_patch_contract(tmp_path):
    diff = _run_dir(tmp_path) / "changes.diff"
    diff.write_text("just prose\n", encoding="utf-8")

    ok, why = ec.declared_artifacts_ok([str(diff)], since_mtime=time.time() - 1)
    assert not ok and "patch contract" in why


def test_t14_unknown_basename_keeps_todays_checks(tmp_path):
    notes = _run_dir(tmp_path) / "notes.md"
    notes.write_text("free-form notes\n", encoding="utf-8")

    assert ec.declared_artifacts_ok([str(notes)], since_mtime=time.time() - 1) == (True, "ok")


def test_t14_custom_contract_is_honored_for_its_basename_only(tmp_path):
    run_dir = _run_dir(tmp_path)
    seen: list[tuple] = []

    def contract(path, since_mtime):
        seen.append((path, since_mtime))
        return "custom-notes contract: nope"

    ec.register_completion_contract("custom-notes.txt", contract)
    custom = run_dir / "custom-notes.txt"
    custom.write_text("anything\n", encoding="utf-8")
    try:
        ok, why = ec.declared_artifacts_ok([str(custom)], since_mtime=time.time() - 1)
        assert not ok and why.endswith("custom-notes contract: nope") and len(seen) == 1

        other = run_dir / "other-notes.txt"
        other.write_text("fine\n", encoding="utf-8")
        assert ec.declared_artifacts_ok([str(other)], since_mtime=time.time() - 1) == (True, "ok")
    finally:
        # Drop the test registration so later tests see the built-in registry.
        ec._COMPLETION_CONTRACTS[:] = [
            entry for entry in ec._COMPLETION_CONTRACTS if entry[0] != "custom-notes.txt"
        ]


# ── implementer artifact completion (recursive-validate-impl) ───────────────

def _dispatch_implementer(tmp_path, monkeypatch, *, recipe, rc, summary):
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}), encoding="utf-8")
    applied: list[tuple] = []
    monkeypatch.setattr(ex, "apply_impl_output", lambda *a, **k: applied.append(a))

    def fake_dispatch(task_class, lane, prompt):
        (repo / "a.py").write_text("x = 2\n")
        if summary is not None:
            (run_dir / "implementer-summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return rc, "--- a/a.py\n+++ b/a.py\n@@ truncated"

    result = ex.dispatch_node(
        ("implementer", "implementer", "implement", "", "serial", "", "worker", ""),
        root=str(tmp_path), run_dir=str(run_dir), plan_path=str(plan),
        task_class="recursive_validate_impl", db="", run_id="r", dispatch_fn=fake_dispatch,
        recipe=recipe, workflow="")
    return result, run_dir, applied


def test_implementer_timeout_with_fresh_k3_summary_completes_without_applying(tmp_path, monkeypatch, capsys):
    (rc, fr), run_dir, applied = _dispatch_implementer(
        tmp_path, monkeypatch, recipe="recursive-validate-impl", rc=124, summary=K3_SUMMARY)

    assert (rc, fr) == (0, "done")
    assert applied == []   # a truncated diff from an aborted dispatch is never applied
    assert ec.ARTIFACT_COMPLETION_LOG in capsys.readouterr().err
    summary = json.loads((run_dir / "implementer-summary.json").read_text(encoding="utf-8"))
    assert _tier1_ready(summary)
    assert not list(run_dir.glob(".dispatch-marker-*"))


def test_implementer_timeout_without_summary_keeps_the_timeout(tmp_path, monkeypatch):
    (rc, fr), _run_dir, applied = _dispatch_implementer(
        tmp_path, monkeypatch, recipe="recursive-validate-impl", rc=124, summary=None)

    assert (rc, fr) == (1, "timeout")
    assert applied == []


def test_implementer_timeout_in_a_recipe_without_declared_artifacts_keeps_the_timeout(tmp_path, monkeypatch):
    (rc, fr), _run_dir, _applied = _dispatch_implementer(
        tmp_path, monkeypatch, recipe="code-fix", rc=124, summary=K3_SUMMARY)

    assert (rc, fr) == (1, "timeout")


def test_implementer_clean_handshake_still_applies_and_normalizes(tmp_path, monkeypatch):
    (rc, fr), run_dir, applied = _dispatch_implementer(
        tmp_path, monkeypatch, recipe="recursive-validate-impl", rc=0, summary=K3_SUMMARY)

    assert (rc, fr) == (0, "done")
    assert len(applied) == 1
    assert _tier1_ready(json.loads((run_dir / "implementer-summary.json").read_text(encoding="utf-8")))


def test_implementer_artifact_completion_emits_additive_contracts_field(tmp_path, monkeypatch):
    """The completion event gains a per-contract pass/fail field; the pre-existing
    payload keys (rc, suppressed_finish_reason, artifacts) are unchanged."""
    events: list[tuple] = []
    monkeypatch.setattr(eh, "mo_node_emit", lambda *a, **k: events.append(a))

    (rc, fr), _run_dir, _applied = _dispatch_implementer(
        tmp_path, monkeypatch, recipe="recursive-validate-impl", rc=124, summary=K3_SUMMARY)
    assert (rc, fr) == (0, "done")

    payloads = [json.loads(a[4]) for a in events if a[3] == "node.artifact_completion"]
    assert payloads, "expected a node.artifact_completion run_event"
    payload = payloads[-1]
    assert payload["contracts"] == {"implementer-summary.json": "pass"}
    assert payload["rc"] == 124 and "suppressed_finish_reason" in payload and payload["artifacts"]


# ── T12: strict_handshake in the compiler and the schema ────────────────────

def _workflow(tmp_path: Path, strict_line: str) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text(
        "nodes:\n"
        "  - {name: a, type: researcher, dispatch_mode: serial" + strict_line + "}\n"
        "  - {name: b, type: implementer, dispatch_mode: serial}\n",
        encoding="utf-8")
    return path


def test_t12_compiler_parses_strict_handshake_and_defaults_false(tmp_path):
    compiled = compile_workflow(_workflow(tmp_path, ", strict_handshake: true"))
    assert compiled.nodes["a"].strict_handshake is True
    assert compiled.nodes["b"].strict_handshake is False
    assert ec.node_strict_handshake(compiled, "", "a") is True
    assert ec.node_strict_handshake(None, str(tmp_path / "wf.yaml"), "a") is True  # YAML fallback
    assert ec.node_strict_handshake(None, str(tmp_path / "wf.yaml"), "b") is False
    assert ec.node_strict_handshake(None, "", "a") is False


def test_t12_non_bool_strict_handshake_is_a_compile_error(tmp_path):
    with pytest.raises(WorkflowCompileError, match="strict_handshake"):
        compile_workflow(_workflow(tmp_path, ", strict_handshake: 'yes'"))


def test_t12_schema_accepts_strict_handshake_bool_only():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schemas" / "workflow.schema.json").read_text(encoding="utf-8"))
    node_schema = {**schema["$defs"]["WorkflowNode"], "$defs": schema["$defs"]}
    node = {"name": "contract_compiler", "type": "researcher", "dispatch_mode": "serial"}

    jsonschema.validate({**node, "strict_handshake": True}, node_schema)
    jsonschema.validate(node, node_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**node, "strict_handshake": "yes"}, node_schema)


# ── T13: no behavior change for verifier / publisher / rollback ─────────────

def test_t13_non_llm_handlers_are_untouched():
    assert ex.NODE_HANDLER_REGISTRY["verifier"] is eh._handle_verifier
    assert ex.NODE_HANDLER_REGISTRY["publisher"] is eh._handle_publisher
    assert ex.NODE_HANDLER_REGISTRY["rollback"] is eh._handle_rollback
    assert ex.EARLY_NODE_HANDLERS == {
        "planner": eh._handle_planner_early,
        "reflector": eh._handle_reflector_early,
    }


# ── property check (tier3): runs when hypothesis is installed ───────────────

def test_normalization_properties():
    hypothesis = pytest.importorskip("hypothesis")
    st = hypothesis.strategies
    files = st.one_of(st.none(), st.text(max_size=3),
                      st.lists(st.one_of(st.text(max_size=4), st.integers()), max_size=3))
    summaries = st.fixed_dictionaries({}, optional={
        "files_changed": files,
        "touched_files": files,
        "ready_for_tier1": st.one_of(st.booleans(), st.none(), st.text(max_size=2)),
        "status": st.text(max_size=5),
    })

    @hypothesis.settings(max_examples=300, deadline=None)
    @hypothesis.given(summaries)
    def check(summary):
        once = ec.normalize_implementer_summary(summary)
        assert ec.normalize_implementer_summary(once) == once
        if summary.get("ready_for_tier1") is False:
            assert once["ready_for_tier1"] is False
        if once["ready_for_tier1"] is True and summary.get("ready_for_tier1") is not True:
            assert _tier1_ready(once)

    check()
