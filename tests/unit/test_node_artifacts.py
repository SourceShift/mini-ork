"""``mini_ork.ide_pages.node_artifacts.build_artifacts_view`` — the node
artifacts view: per-node inputs / outputs with kinds, sizes, previews, and
``from`` attribution.

Mirrors the kickoff's Tests §:

* Verifier outputs (json, log, tsv, newest evidence log, node-cmd record)
  with kinds and previews.
* Reviewer inputs include its parents' outputs and ``review-diff.patch``
  named in its prompt; outputs include `review-<id>.json`.
* Declared ports win: a workflow node with ``inputs``/``outputs`` lists
  exactly those.
* ``agent_edits``: a transcript with one Edit, one MultiEdit and one Write
  → three entries with correct +/- counts; a transcript with no edits →
  the note.
* ``board node … --view artifacts`` is accepted by the CLI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages.node_artifacts import (
    EDIT_TOOLS,
    PREVIEW_LINES_CAP,
    WRITE_TOOLS,
    build_artifacts_view,
)
from mini_ork.ide_pages.node import build_node
from mini_ork.ide_pages.run import _load
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-artifacts"
T0 = 1_791_000_000

# Workflow with one node that carries explicit ``inputs``/``outputs`` ports
# (to exercise "declared ports win"). Other tests rely on the by-node-kind
# fallback. ``schema_shape_outputs_node`` and ``dict_inputs_node`` cover the
# heterogeneous shapes declared in ``workflow.schema.json``:
#   * ``outputs: [{name, kind, path}]``   (rsi-technique-review / goal-loop /
#     coord-technique-review / refactor-audit — review item #1)
#   * ``inputs:  {name: {required: true}}`` (review item #4 dict form)
# The ``escalates_to`` back-edge ``reviewer → implementer`` exercises the
# edge-type filter (review item #2): a control-flow edge must NOT contribute
# to the implementer's inputs.
WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: verifier_node, type: verifier, verifier_ref: verifiers/test.py}
  - {name: static_check_verifier, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
  - {name: declared_port_node, type: implementer, model_lane: worker,
     prompt_ref: prompts/implementer.md,
     inputs: [kickoff.md, plan.json], outputs: [out.md, out.json]}
  - {name: schema_shape_outputs_node, type: implementer, model_lane: worker,
     prompt_ref: prompts/implementer.md,
     outputs: [{name: sc_out_md, kind: markdown, path: out.md},
               {name: sc_out_json, kind: json, path: out.json}]}
  - {name: dict_inputs_node, type: implementer, model_lane: worker,
     prompt_ref: prompts/implementer.md,
     inputs: {source_corpus: {required: true}}}
edges:
  - {from: implementer, to: reviewer}
  - {from: reviewer, to: implementer, edge_type: escalates_to}
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\n")
    return h


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed_run(home: Path, *, run_id: str = RUN) -> Path:
    """One run with one session transcript per agent-type node.

    Always seeds the run with a kickoff, plan.json, context-pack.json, and
    the verifier/review/publisher artefacts the kickoff's Tests § names.
    """
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# demo kickoff\n")

    # Run-level baseline files.
    (run_dir / "plan.json").write_text(json.dumps({"objective": "demo"}))
    (run_dir / "context-pack.json").write_text(json.dumps({"paths": []}))

    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "demo-recipe", "executing", 0.10, T0, T0 + 120, T0 + 100,
         "demo", str(kickoff), "latest", "tr-demo"))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-implementer-start", run_id, "node_start",
         json.dumps({"node_id": "implementer", "node_type": "implementer", "model_lane": "worker"}),
         T0 + 10))
    con.commit()
    con.close()
    return run_dir


def _seed_implementer_session(run_dir: Path) -> None:
    """Implementer transcript: one Edit, one MultiEdit, one Write.

    Also writes ``agent-implementer.live.jsonl`` in the wire format
    ``_resolve_session_path`` expects: an outer record whose ``line`` field
    is a *stringified* inner JSON envelope with ``session_id``. Without
    this sidecar the rule-2 / rule-3 fallbacks don't fire on a tmp home
    (no llm_calls table, no running-state node), and ``build_node`` returns
    an empty ``agent_edits`` list.
    """
    session_id = "11111111-1111-1111-1111-111111111111"
    session = run_dir / "sessions" / f"{session_id}.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": _iso(T0 + 10),
         "message": {"content": "do the demo edit"}},
        {"type": "assistant", "timestamp": _iso(T0 + 70),
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                                  "input": {"file_path": "/tmp/a.py",
                                            "old_string": "old a",
                                            "new_string": "new a"}}]}},
        {"type": "assistant", "timestamp": _iso(T0 + 130),
         "message": {"content": [{"type": "tool_use", "id": "t2", "name": "MultiEdit",
                                  "input": {"file_path": "/tmp/b.py",
                                            "edits": [
                                                {"old_string": "old b1", "new_string": "new b1"},
                                                {"old_string": "old b2", "new_string": "new b2"},
                                            ]}}]}},
        {"type": "assistant", "timestamp": _iso(T0 + 190),
         "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Write",
                                  "input": {"file_path": "/tmp/c.py",
                                            "content": "line1\nline2\nline3"}}]}},
        {"type": "result", "timestamp": _iso(T0 + 250), "result": "done",
         "session_id": session_id},
    ]
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    # Live sidecar: outer record with stringified inner envelope. Review
    # item #5: a flat ``{"session_id": ...}`` line never parsed because
    # ``_resolve_session_path`` looks at ``rec["line"]`` and parses THAT as
    # JSON to find ``session_id``.
    live_inner = json.dumps({"session_id": session_id})
    live_outer = json.dumps({"seq": 0, "stream": "stdout", "t": 1.0,
                             "line": live_inner})
    (run_dir / "agent-implementer.live.jsonl").write_text(live_outer + "\n")


# ── tests ──────────────────────────────────────────────────────────────────


def test_verifier_outputs_have_kinds_and_previews(home: Path) -> None:
    run_dir = _seed_run(home)
    # Seed the verifier artefacts the kickoff's Tests § names.
    stem = "static-check"
    (run_dir / f"verifier_{stem}.json").write_text(json.dumps({
        "checks": [{"name": "lint", "pass": True}],
    }))
    (run_dir / f"verifier-{stem}.log").write_text("lint ok\n")
    (run_dir / f"verifier-{stem}.checks.tsv").write_text(
        "name\tpassed\trc\nlint\tTrue\t0\n")
    (run_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (run_dir / "evidence" / f"{stem}-001.log").write_text("evidence log\n")
    (run_dir / "node-cmd").mkdir(parents=True, exist_ok=True)
    (run_dir / "node-cmd" / f"verifier_{stem}.json").write_text("{}")

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "static_check_verifier")
    out = build_artifacts_view(run_obj, node)
    by_name = {a["name"]: a for a in out["outputs"]}

    assert f"verifier_{stem}.json" in by_name
    assert by_name[f"verifier_{stem}.json"]["kind"] == "json"
    assert by_name[f"verifier_{stem}.json"]["size"] > 0

    assert f"verifier-{stem}.log" in by_name
    assert by_name[f"verifier-{stem}.log"]["kind"] == "log"

    assert f"verifier-{stem}.checks.tsv" in by_name
    assert by_name[f"verifier-{stem}.checks.tsv"]["kind"] == "text"

    # newest evidence log
    ev_names = [n for n in by_name if n.startswith(f"{stem}") and ".log" in n and "node-cmd" not in n]
    assert any(n.startswith("static-check") for n in ev_names)


def test_reviewer_inputs_include_parent_outputs_and_review_diff(home: Path) -> None:
    run_dir = _seed_run(home)
    # Implementer output the reviewer reads.
    (run_dir / "implementer-summary.json").write_text(json.dumps({"status": "ok"}))
    (run_dir / "review-diff.patch").write_text("--- a\n+++ b\n")
    # Reviewer by-kind output must exist on disk for the view to surface it
    # (kickoff §1: "Only files that exist.").
    (run_dir / "review-reviewer.json").write_text(json.dumps({"verdict": "ok"}))

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "reviewer")
    out = build_artifacts_view(run_obj, node)
    in_names = {a["name"] for a in out["inputs"]}
    assert "implementer-summary.json" in in_names
    assert "review-diff.patch" in in_names
    assert "plan.json" in in_names  # run-level
    out_names = {a["name"] for a in out["outputs"]}
    assert "review-reviewer.json" in out_names or "review-reviewer.json.stdout.md" in out_names


def test_declared_ports_win_over_by_node_kind(home: Path) -> None:
    run_dir = _seed_run(home)
    # Pre-create the declared output files so they resolve.
    (run_dir / "out.md").write_text("# declared output\n")
    (run_dir / "out.json").write_text(json.dumps({"k": "v"}))
    # Pre-create a "by-node-kind" file the implementer would normally emit
    # — must NOT appear when declared ports are present.
    (run_dir / "implementer-summary.json").write_text(json.dumps({"status": "ok"}))

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "declared_port_node")
    out = build_artifacts_view(run_obj, node)
    out_names = {a["name"] for a in out["outputs"]}
    assert "out.md" in out_names
    assert "out.json" in out_names
    # by-node-kind fallback suppressed when declared ports are present
    assert "implementer-summary.json" not in out_names


def test_agent_edits_in_changes_view_extracts_edit_multiedit_write(home: Path) -> None:
    run_dir = _seed_run(home)
    _seed_implementer_session(run_dir)

    run_obj = _load(home, RUN)
    assert run_obj is not None
    payload = build_node(home, RUN, "implementer", view="changes")
    assert payload["ok"] is True
    edits = payload["agent_edits"]
    assert len(edits) == 3
    tools = {e["tool"] for e in edits}
    assert {"Edit", "MultiEdit", "Write"}.issubset(tools)
    # MultiEdit has two edits: 2 "- old" + 2 " + new" = 4 lines (no header)
    multiedit = next(e for e in edits if e["tool"] == "MultiEdit")
    assert multiedit["added"] == 2
    assert multiedit["removed"] == 2
    # Write has 3 lines as "+"
    write_edit = next(e for e in edits if e["tool"] == "Write")
    assert write_edit["added"] == 3
    assert write_edit["removed"] == 0
    # Edit: 1 line each
    edit_one = next(e for e in edits if e["tool"] == "Edit")
    assert edit_one["added"] == 1
    assert edit_one["removed"] == 1


def test_agent_edits_note_when_no_edits(home: Path) -> None:
    run_dir = _seed_run(home)
    sid = "22222222-2222-2222-2222-222222222222"
    # Transcript without any Edit/MultiEdit/Write calls.
    session = run_dir / "sessions" / f"{sid}.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": _iso(T0 + 10),
                    "message": {"content": "no edits here"}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 70),
                    "message": {"content": [{"type": "text", "text": "ok"}]}}),
        json.dumps({"type": "result", "timestamp": _iso(T0 + 130), "result": "done",
                    "session_id": sid}),
    ]) + "\n")
    # Live sidecar: double-JSON envelope (review item #5 — ``_resolve_session_path``
    # reads ``rec["line"]`` and parses THAT string as JSON to find ``session_id``).
    live_inner = json.dumps({"session_id": sid})
    live_outer = json.dumps({"seq": 0, "stream": "stdout", "t": 1.0,
                             "line": live_inner})
    (run_dir / "agent-implementer.live.jsonl").write_text(live_outer + "\n")
    payload = build_node(home, RUN, "implementer", view="changes")
    assert payload["ok"] is True
    assert payload["agent_edits"] == []
    assert payload["agent_edits_note"] == "This node edited no files."


def test_cli_accepts_artifacts_view() -> None:
    parser = board_cmd.build_parser()
    ns = parser.parse_args(["node", "any-run", "any-node", "--view", "artifacts"])
    assert ns.view == "artifacts"


def test_build_artifacts_view_via_build_node(home: Path) -> None:
    """End-to-end: ``build_node(..., view='artifacts')`` returns the new view."""
    run_dir = _seed_run(home)
    (run_dir / "implementer-summary.json").write_text(json.dumps({"status": "ok"}))
    out = build_node(home, RUN, "implementer", view="artifacts")
    assert out["ok"] is True
    assert out["view"] == "artifacts"
    assert "inputs" in out and "outputs" in out
    in_names = {a["name"] for a in out["inputs"]}
    out_names = {a["name"] for a in out["outputs"]}
    assert "plan.json" in in_names
    assert "implementer-summary.json" in out_names


def test_preview_capped_at_60_lines(home: Path) -> None:
    run_dir = _seed_run(home)
    big = run_dir / "plan.json"
    big.write_text(json.dumps({"lines": list(range(200))}))
    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "planner")
    out = build_artifacts_view(run_obj, node)
    plan_artifact = next(a for a in out["outputs"] if a["name"] == "plan.json")
    assert plan_artifact["preview"].count("\n") <= PREVIEW_LINES_CAP


def test_edit_tools_include_write() -> None:
    """Sanity: the new module's tool sets include ``Write`` (kickoff §2)
    while :data:`EDIT_TOOLS` mirrors the stream view's narrow set."""
    assert "Write" in WRITE_TOOLS
    assert "Write" not in EDIT_TOOLS
    assert {"Edit", "MultiEdit"}.issubset(EDIT_TOOLS)


# ── review item #1: schema-shape outputs (list[{name, kind, path}]) ────────


def test_schema_shape_outputs_resolve_without_typeerror(home: Path) -> None:
    """``outputs: [{name, kind, path}]`` must NOT raise TypeError.

    Pre-fix this hit every recipe that uses the schema-shape outputs
    (rsi-technique-review, coord-technique-review, refactor-audit,
    goal-loop, frontier-llm-research, prompt-graph-loop). It also broke
    every CHILD of such a node — the parent → child path inherits
    ``_resolved_outputs_paths`` and the TypeError propagated up through
    ``_parent_outputs``.
    """
    run_dir = _seed_run(home)
    # Create the schema-declared outputs.
    (run_dir / "out.md").write_text("# declared\n")
    (run_dir / "out.json").write_text(json.dumps({"k": "v"}))

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "schema_shape_outputs_node")
    # Must not raise TypeError.
    out = build_artifacts_view(run_obj, node)
    out_names = {a["name"] for a in out["outputs"]}
    assert "out.md" in out_names
    assert "out.json" in out_names
    # by-node-kind fallback suppressed (declared wins).
    assert "implementer-summary.json" not in out_names


# ── review item #2: edge-type filter on parent → child inputs ─────────────


def test_escalates_to_back_edge_does_not_feed_child_inputs(home: Path) -> None:
    """Back-edges / control-flow edges must NOT count as parent → child inputs.

    WORKFLOW declares ``reviewer → implementer`` with ``edge_type:
    escalates_to`` (a control-flow back-edge, not a data-flow edge).
    Pre-fix the implementer would see ``review-reviewer.json`` in its inputs
    — which is its OWN child's output, not a parent artefact.
    """
    run_dir = _seed_run(home)
    # Reviewer's by-kind output (what would leak into implementer's inputs).
    (run_dir / "review-reviewer.json").write_text(json.dumps({"verdict": "ok"}))
    # Implementer's by-kind output (a real parent artefact via the
    # ``implementer → reviewer`` edge that has no ``edge_type`` and
    # therefore defaults to ``depends_on``).
    (run_dir / "implementer-summary.json").write_text(json.dumps({"status": "ok"}))

    run_obj = _load(home, RUN)
    assert run_obj is not None
    # As a sanity check: the reviewer still reads implementer's outputs.
    reviewer = next(n for n in run_obj.nodes if n.id == "reviewer")
    rev_inputs = {a["name"] for a in build_artifacts_view(run_obj, reviewer)["inputs"]}
    assert "implementer-summary.json" in rev_inputs  # depends_on edge OK
    assert "review-reviewer.json" not in rev_inputs  # reviewer's own output

    # The real fix: implementer must NOT see the reviewer's outputs (which
    # arrive via the escalates_to back-edge in the workflow).
    implementer = next(n for n in run_obj.nodes if n.id == "implementer")
    impl_inputs = {a["name"] for a in build_artifacts_view(run_obj, implementer)["inputs"]}
    assert "review-reviewer.json" not in impl_inputs


# ── review item #4: declared inputs replace the run-level fallback ─────────


def test_declared_inputs_replace_run_level_fallback(home: Path) -> None:
    """Declared inputs REPLACE the run-level fallback (parents + plan.json /
    context-pack.json). Without this, a node with ``inputs: [kickoff.md]``
    would also see every parent output and the run-level baseline files —
    the declared-port contract is meaningless if the fallback leaks through.
    """
    run_dir = _seed_run(home)
    # ``declared_port_node`` declares inputs ``[kickoff.md, plan.json]`` and
    # has NO parents in the workflow (no edges into it). So neither parents
    # nor the run-level fallback should appear.
    (run_dir / "kickoff.md").write_text("# in-run kickoff\n")
    # Pre-create the run-level files: must NOT appear in inputs because
    # declared inputs replace them.
    (run_dir / "context-pack.json").write_text(json.dumps({"paths": []}))
    (run_dir / "plan.json").write_text(json.dumps({"objective": "demo"}))

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "declared_port_node")
    in_names = {a["name"] for a in build_artifacts_view(run_obj, node)["inputs"]}
    # Declared wins: kickoff.md is in inputs.
    assert "kickoff.md" in in_names
    # Run-level fallback suppressed: context-pack.json is NOT in inputs.
    assert "context-pack.json" not in in_names


def test_dict_shape_inputs_normalize_to_keys(home: Path) -> None:
    """``inputs: {name: {required: true}}`` (object form per
    ``workflow.schema.json``) must be normalized to its names.
    """
    run_dir = _seed_run(home)
    # ``dict_inputs_node`` declares ``inputs: {source_corpus: {required: true}}``.
    # Pre-create a file with that name so it resolves on disk.
    (run_dir / "source_corpus").write_text("the corpus\n")

    run_obj = _load(home, RUN)
    assert run_obj is not None
    node = next(n for n in run_obj.nodes if n.id == "dict_inputs_node")
    # Must not raise; declared replaces fallback so the only input is
    # ``source_corpus`` (not the run-level fallback files).
    in_names = {a["name"] for a in build_artifacts_view(run_obj, node)["inputs"]}
    assert "source_corpus" in in_names
    assert "context-pack.json" not in in_names


# ── review item #3: Write tool paths must be inside run_dir ───────────────


def test_write_tool_paths_filtered_to_run_dir(home: Path, tmp_path: Path) -> None:
    """Files written OUTSIDE ``run_dir`` must NOT appear as outputs.

    Pre-fix the implementer's Write tool calls returned every absolute
    path the agent wrote — including ``/path/to/repo/mini_ork/foo.py``,
    which is a repo source edit, not a run artefact. Those would then
    appear in the artifacts view's ``outputs`` list and leak the repo
    surface into the run's claimed artefacts.
    """
    run_dir = _seed_run(home)
    session_id = "33333333-3333-3333-3333-333333333333"
    session = run_dir / "sessions" / f"{session_id}.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    # Mix of in-run-dir Writes and out-of-run-dir Writes (repo source edits).
    inside = run_dir / "artifact.md"
    inside.write_text("in run\n")
    repo_path = tmp_path / "fake_repo" / "mini_ork" / "ide_pages" / "node_artifacts.py"
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.write_text("# source edit\n")
    lines = [
        {"type": "user", "timestamp": _iso(T0 + 10),
         "message": {"content": "do the edit"}},
        {"type": "assistant", "timestamp": _iso(T0 + 70),
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write",
                                  "input": {"file_path": str(inside),
                                            "content": "in run"}}]}},
        {"type": "assistant", "timestamp": _iso(T0 + 130),
         "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Write",
                                  "input": {"file_path": str(repo_path),
                                            "content": "# source"}}]}},
        {"type": "result", "timestamp": _iso(T0 + 250), "result": "done",
         "session_id": session_id},
    ]
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    live_inner = json.dumps({"session_id": session_id})
    live_outer = json.dumps({"seq": 0, "stream": "stdout", "t": 1.0,
                             "line": live_inner})
    (run_dir / "agent-implementer.live.jsonl").write_text(live_outer + "\n")

    run_obj = _load(home, RUN)
    assert run_obj is not None
    # Look up the implementer by id.
    implementer = next(n for n in run_obj.nodes if n.id == "implementer")
    out_view = build_artifacts_view(run_obj, implementer)
    out_paths = {a["path"] for a in out_view["outputs"]}
    assert str(inside.resolve()) in out_paths
    assert str(repo_path.resolve()) not in out_paths

# ── Opus review fixes (finished directly) ──────────────────────────────────


def test_a_planner_is_not_given_the_plan_it_writes(home: Path) -> None:
    _seed_run(home)
    out = build_node(home, RUN, "planner", view="artifacts")
    assert "plan.json" not in {a["name"] for a in out["inputs"]}, out["inputs"]


def test_files_a_prompt_names_but_the_node_writes_are_outputs_not_inputs(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mini_ork.ide_pages.node_artifacts as na

    run_dir = _seed_run(home)
    (run_dir / "implementer-summary.json").write_text("{}")
    (run_dir / "lens-notes.md").write_text("# notes\n")
    monkeypatch.setattr(
        na, "_rendered_prompt",
        lambda run, node: "Read lens-notes.md, then write implementer-summary.json.",
    )
    out = build_node(home, RUN, "implementer", view="artifacts")
    inputs = {a["name"] for a in out["inputs"]}
    outputs = {a["name"] for a in out["outputs"]}
    assert "lens-notes.md" in inputs
    assert "implementer-summary.json" not in inputs and "implementer-summary.json" in outputs
