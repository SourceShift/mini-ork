"""Prompt-bearing planner/reflector nodes (RBF-08a).

A ``planner``/``reflector`` node that declares a ``prompt_ref`` is a real LLM
node. It must be dispatched through the same path as ``_handle_researcher`` —
prompt assembled from ``prompt_ref``, the node's own lane, its declared outputs,
and a normal ``node_start``/``node_end`` pair — not through the prompt-less
early handlers (the planner prints ``[skip]``; the reflector runs the global
reflect pipeline before ``node_start`` and so is invisible in ``execute.log``).
A node of either type WITHOUT ``prompt_ref`` keeps today's early behaviour.

``validate`` must warn when a repair cycle routes through a prompt-less
planner/reflector: the early handler drops the node's prompt, lane and outputs,
so the loop cannot converge.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

import pytest

from mini_ork.cli import execute_handlers as eh
from mini_ork.cli import validate as validate_mod
from mini_ork.workflow import WorkflowCompileError, compile_workflow

RECIPE = "specloop"
RUN_ID = "run-test-prompt-bearing"
DISPATCH_RESULT = "DISPATCH-RESULT-4b1f9c42"

# Ambient values the surrounding run/executor may leak into this process. Every
# test clears them so resolution never lands on the live home, DB or run dir.
_LEAKY_ENV = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_ROOT",
    "MINI_ORK_RECIPE", "MINI_ORK_RECIPE_ROOT", "MINI_ORK_PROMPT_OVERRIDE_DIR",
    "MINI_ORK_PROVIDERS", "MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_FROM",
    "MO_ROUTING_POLICY", "MO_ARTIFACT_BACKEND", "MO_TARGET_CWD",
    "MO_LANE_PROBE", "MO_MAX_KICKOFF_BYTES", "MO_EXEC_STARTED_MS",
    "MO_NODE_HEARTBEAT_SECONDS", "MO_HEARTBEAT_TIMEOUT_S",
)


@pytest.fixture()
def hermetic_env(tmp_path, monkeypatch):
    home = tmp_path / "mini-ork-home"
    home.mkdir()
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home


class _DispatchStub:
    """Stands in for the LLM lane and records what the node hands it."""

    def __init__(self):
        self.calls = 0
        self.task_class = None
        self.lane = None
        self.prompt = None

    def __call__(self, task_class, lane, prompt):
        self.calls += 1
        self.task_class = task_class
        self.lane = lane
        self.prompt = prompt
        return 0, DISPATCH_RESULT


@pytest.fixture()
def early_sentinels(monkeypatch):
    """Recording stand-ins for the early handlers, so the DISPATCH decision is
    observable independently of what the real handlers do."""
    counts = {"planner": 0, "reflector": 0}

    def planner_early(root):
        counts["planner"] += 1
        return 0, "done"

    def reflector_early(root):
        counts["reflector"] += 1
        return 0, "done"

    monkeypatch.setitem(eh.EARLY_NODE_HANDLERS, "planner", planner_early)
    monkeypatch.setitem(eh.EARLY_NODE_HANDLERS, "reflector", reflector_early)
    return counts


def _engine_root(tmp_path, prompts=None):
    root = tmp_path / "engine-root"
    prompts_dir = root / "recipes" / RECIPE / "prompts"
    prompts_dir.mkdir(parents=True)
    for name, body in (prompts or {}).items():
        (prompts_dir / name).write_text(body)
    return root


def _events_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE TABLE run_events ("
        " event_id TEXT PRIMARY KEY,"
        " run_id TEXT NOT NULL,"
        " parent_run_id TEXT,"
        " event_type TEXT NOT NULL,"
        " payload_json TEXT,"
        " created_at INTEGER,"
        " finish_reason TEXT,"
        " last_heartbeat_at INTEGER);"
    )
    con.commit()
    con.close()


def _node_event_types(db_path, run_id, node_id):
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id = ?",
            (run_id,)).fetchall()
    finally:
        con.close()
    types = set()
    for event_type, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("node_id") == node_id:
            types.add(event_type)
    return types


def _dispatch(tmp_path, root, db_path, fields, stub):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = eh.dispatch_node(
        fields,
        root=str(root),
        run_dir=str(run_dir),
        plan_path="",
        task_class=RECIPE,
        db=str(db_path),
        run_id=RUN_ID,
        dispatch_fn=stub,
        recipe=RECIPE,
    )
    return run_dir, result


def _files_containing(run_dir, needle):
    found = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            if needle in path.read_text(errors="replace"):
                found.append(path)
        except OSError:
            continue
    return found


# ── dispatch: prompt-bearing nodes take the researcher path ──────────────────

@pytest.mark.parametrize("node_type,prompt_name,sentinel", [
    ("reflector", "reflector.md", "REFLECTOR-PROMPT-7f3a1c"),
    ("planner", "replanner.md", "REPLANNER-PROMPT-0d5e2b"),
])
def test_prompt_bearing_early_node_dispatches_via_researcher(
        tmp_path, hermetic_env, early_sentinels, node_type, prompt_name, sentinel):
    root = _engine_root(tmp_path, {prompt_name: sentinel})
    db_path = tmp_path / "state.db"
    _events_db(db_path)
    stub = _DispatchStub()
    node_id = f"n_{node_type}"
    fields = [
        node_id, node_type, "Repair the loop", f"prompts/{prompt_name}",
        "", "", "spec_test_lane", "",
    ]

    run_dir, (rc, finish_reason) = _dispatch(tmp_path, root, db_path, fields, stub)

    assert early_sentinels[node_type] == 0, (
        f"the prompt-less {node_type} early handler was taken for a node with "
        f"prompt_ref={fields[3]!r}")
    assert stub.calls == 1, f"expected one LLM dispatch, got {stub.calls}"
    assert sentinel in (stub.prompt or ""), (
        f"dispatch prompt lacks the text of prompts/{prompt_name}")
    assert stub.lane == "spec_test_lane", (
        f"dispatch lane {stub.lane!r} is not the node's model_lane")
    event_types = _node_event_types(db_path, RUN_ID, node_id)
    assert "node_start" in event_types, f"no node_start; saw {sorted(event_types)}"
    assert "node_end" in event_types, f"no node_end; saw {sorted(event_types)}"
    assert (rc, finish_reason) == (0, "done")
    assert _files_containing(run_dir, DISPATCH_RESULT), (
        "no artifact under the run dir carries the dispatch result")


@pytest.mark.parametrize("node_type", ["planner", "reflector"])
def test_promptless_early_node_keeps_early_handler(
        tmp_path, hermetic_env, early_sentinels, node_type):
    root = _engine_root(tmp_path, {})
    db_path = tmp_path / "state.db"
    _events_db(db_path)
    stub = _DispatchStub()
    fields = [f"n_{node_type}", node_type, "Repair the loop", "", "", "", "", ""]

    _run_dir, (rc, finish_reason) = _dispatch(tmp_path, root, db_path, fields, stub)

    assert early_sentinels[node_type] == 1, (
        f"prompt-less {node_type} did not take its early handler")
    assert stub.calls == 0, "a prompt-less early node must not dispatch an LLM"
    assert (rc, finish_reason) == (0, "done")


# ── the prompt-less reflector stays a timed side channel ─────────────────────

class _FakeProc:
    returncode = 0
    stderr = b""
    stdout = b""


def _patch_subprocess_run(monkeypatch):
    runs = []

    def fake_run(cmd, *args, **kwargs):
        runs.append((list(cmd), kwargs))
        return _FakeProc()

    monkeypatch.setattr(eh.subprocess, "run", fake_run)
    return runs


def test_reflector_early_emits_start_and_duration(tmp_path, monkeypatch,
                                                  hermetic_env, capsys):
    _patch_subprocess_run(monkeypatch)
    root = tmp_path / "engine-root"
    root.mkdir()

    assert eh._handle_reflector_early(str(root)) == (0, "done")

    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "[reflect]" in ln]
    assert lines, f"no [reflect] line on stderr; stderr={err!r}"
    line = lines[-1]
    assert (re.search(r"\d{1,2}:\d{2}(:\d{2})?", line)
            or any(w in line.lower() for w in ("start", "began", "from", "since"))), (
        f"[reflect] line has no start time: {line!r}")
    assert (re.search(r"\d+(?:\.\d+)?\s*(?:ms|s|m|h)\b", line)
            or any(w in line.lower() for w in ("duration", "elapsed", "took"))), (
        f"[reflect] line has no duration: {line!r}")


def test_reflector_early_still_runs_the_reflect_module(tmp_path, monkeypatch,
                                                       hermetic_env, capsys):
    """Scoping reflect is a separate fix: the prompt-less handler must keep
    launching exactly today's subprocess against the node's root."""
    runs = _patch_subprocess_run(monkeypatch)
    root = tmp_path / "engine-root"
    root.mkdir()

    eh._handle_reflector_early(str(root))
    capsys.readouterr()

    assert len(runs) == 1, f"expected one reflect launch, saw {len(runs)}"
    cmd, kwargs = runs[0]
    assert cmd == [sys.executable, "-m", "mini_ork.cli.reflect"]
    assert (kwargs.get("env") or {}).get("MINI_ORK_ROOT") == str(root)


# ── validate: prompt-less repair-loop nodes are warned ───────────────────────

_CYCLE_WORKFLOW = """\
version: "0.1.0"
task_class: specloop
nodes:
  - {{ name: builder, type: implementer, prompt_ref: prompts/builder.md, dispatch_mode: serial }}
  - {{ name: checker, type: verifier, verifier_ref: verifiers/checker.py, dispatch_mode: serial }}
  - {{ name: reflector, type: reflector, {reflector_decl} dispatch_mode: serial }}
  - {{ name: replanner, type: planner, {planner_decl} dispatch_mode: serial }}
edges:
  - {{ from: builder, to: checker, edge_type: depends_on }}
  - {{ from: checker, to: reflector, edge_type: escalates_to, condition: fail }}
  - {{ from: reflector, to: replanner, edge_type: depends_on }}
  - {{ from: replanner, to: builder, edge_type: depends_on, recursive: true }}
"""

# Same node shape with the recursive back edge removed: acyclic full graph.
_DAG_WORKFLOW = """\
version: "0.1.0"
task_class: specloop
nodes:
  - { name: builder, type: implementer, prompt_ref: prompts/builder.md, dispatch_mode: serial }
  - { name: checker, type: verifier, verifier_ref: verifiers/checker.py, dispatch_mode: serial }
  - { name: reflector, type: reflector, dispatch_mode: serial }
  - { name: replanner, type: planner, prompt_ref: prompts/replanner.md, dispatch_mode: serial }
edges:
  - { from: builder, to: checker, edge_type: depends_on }
  - { from: checker, to: reflector, edge_type: escalates_to, condition: fail }
  - { from: reflector, to: replanner, edge_type: depends_on }
"""


def _validate_root(tmp_path, workflow_text):
    root = tmp_path / "engine-root"
    recipe_dir = root / "recipes" / RECIPE
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "workflow.yaml").write_text(workflow_text)
    (recipe_dir / "task_class.yaml").write_text("task_class: specloop\n")
    (recipe_dir / "artifact_contract.yaml").write_text("outputs: []\n")
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "agents.yaml").write_text("{}\n")
    return root


def _run_validate(monkeypatch, root):
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    return validate_mod.main(["--recipe", RECIPE])


def _warnings(err):
    return [ln for ln in err.splitlines() if ln.startswith("[warning]")]


def _assert_compiles(root):
    try:
        compile_workflow(root / "recipes" / RECIPE / "workflow.yaml")
    except WorkflowCompileError as exc:  # pragma: no cover - fixture guard
        pytest.fail(f"fixture workflow must compile: {exc}")


def test_validate_warns_for_promptless_cycle_nodes(tmp_path, monkeypatch,
                                                   hermetic_env, capsys):
    root = _validate_root(tmp_path, _CYCLE_WORKFLOW.format(
        reflector_decl="", planner_decl="prompt_ref: prompts/replanner.md, "))
    _assert_compiles(root)

    _run_validate(monkeypatch, root)

    warnings = _warnings(capsys.readouterr().err)
    assert [ln for ln in warnings if "reflector" in ln], (
        f"no warning named the prompt-less cycle node 'reflector': {warnings}")
    assert not [ln for ln in warnings if "replanner" in ln], (
        f"a prompt-bearing planner was warned anyway: {warnings}")
    assert not [ln for ln in warnings if "builder" in ln or "checker" in ln], (
        f"non-early nodes were warned: {warnings}")


def test_validate_warns_for_promptless_cycle_planner(tmp_path, monkeypatch,
                                                     hermetic_env, capsys):
    root = _validate_root(tmp_path, _CYCLE_WORKFLOW.format(
        reflector_decl="prompt_ref: prompts/reflector.md, ", planner_decl=""))
    _assert_compiles(root)

    _run_validate(monkeypatch, root)

    warnings = _warnings(capsys.readouterr().err)
    assert [ln for ln in warnings if "replanner" in ln], (
        f"no warning named the prompt-less cycle planner 'replanner': {warnings}")


def test_validate_no_warning_when_cycle_nodes_have_prompt_ref(
        tmp_path, monkeypatch, hermetic_env, capsys):
    root = _validate_root(tmp_path, _CYCLE_WORKFLOW.format(
        reflector_decl="prompt_ref: prompts/reflector.md, ",
        planner_decl="prompt_ref: prompts/replanner.md, "))
    _assert_compiles(root)

    _run_validate(monkeypatch, root)

    warnings = _warnings(capsys.readouterr().err)
    assert not [ln for ln in warnings if "reflector" in ln or "replanner" in ln], (
        f"prompt-bearing cycle nodes were warned: {warnings}")


def test_validate_no_warning_for_promptless_node_off_any_cycle(
        tmp_path, monkeypatch, hermetic_env, capsys):
    root = _validate_root(tmp_path, _DAG_WORKFLOW)
    _assert_compiles(root)

    _run_validate(monkeypatch, root)

    warnings = _warnings(capsys.readouterr().err)
    assert not [ln for ln in warnings if "reflector" in ln], (
        f"a prompt-less reflector off any cycle was warned: {warnings}")
