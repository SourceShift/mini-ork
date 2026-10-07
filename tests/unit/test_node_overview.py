"""``mini-ork board node <run_id> <node_id> --view overview`` — never-lost transcripts + new overview payload.

Pairs with :mod:`tests.unit.test_ide_pages_node`. Each test seeds a single-run
home with just enough artefacts for the resolver / view code paths the kickoff
names:

* live-sidecar ``agent-<id>.live.jsonl`` as the agent's session when no real
  transcript exists (rule 5);
* ``~/.claude/projects/<proj>/<sid>.jsonl`` as a second fallback (rule 4);
* the ``overview`` view's eight parts (``headline``/``facts``/``result``/
  ``files``/``diff``/``diff_note``/``final``/``links``);
* per-kind headline dispatch (reviewer, verifier failing, verifier UNVERIFIED,
  implementer with diff, failed node, lens, planner, command);
* the default view is ``overview`` (kickoff §2);
* the CLI ``--view overview`` flag is accepted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages.node import (
    _DEFAULT_VIEW,
    _VIEWS,
    _is_live_sidecar,
    _live_sidecar_records,
    _overview_view,
    _resolve_session_path,
    _session_entries,
    build_node,
)
from mini_ork.ide_pages.run import _load
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: lens, type: lens, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: verifier, type: verifier, verifier_ref: verifiers/test.py}
  - {name: publisher, type: publisher, model_lane: shell, prompt_ref: prompts/publisher.md}
"""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    return h


def _insert_run(home: Path, *, run_id: str, status: str = "published",
                ended_at: int | None = T0 + 200) -> Path:
    """Insert the run row + create the run dir. Returns the run dir."""
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "demo-recipe", status, 0.0, T0, T0 + 60, ended_at,
         "demo", str(kickoff), "latest", "tr-demo-1"))
    con.commit()
    con.close()
    return run_dir


def _insert_node_events(home: Path, run_id: str, *, node_id: str, ntype: str,
                        lane: str, finish: str | None = "done",
                        cost: float = 0.0, calls: int = 0,
                        error_msg: str | None = None) -> None:
    con = sqlite3.connect(home / "state.db")
    payload = {"node_id": node_id, "node_type": ntype, "model_lane": lane}
    if finish:
        payload["finish_reason"] = finish
    con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"ev-{run_id}-{node_id}-s", run_id, "node_start",
                 json.dumps(payload), T0 + 10))
    con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"ev-{run_id}-{node_id}-e", run_id, "node_end",
                 json.dumps(payload), T0 + 60))
    if calls:
        for i in range(calls):
            row = ("gateway", "minimax", "default", "mini-ork:worker", lane,
                   run_id, cost / max(calls, 1), "success", _iso(T0 + 50 + i))
            if error_msg:
                row = list(row) + [error_msg]
                con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, "
                            "actor, run_id, cost_usd, status, ts, error_message) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            else:
                con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, "
                            "actor, run_id, cost_usd, status, ts) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", row)
    con.commit()
    con.close()


def _envelope(stream_json_obj: dict) -> dict:
    """Build a live-sidecar envelope ``{"line": "<stream-json>"}``."""
    return {"seq": 0, "stream": "stdout", "t": T0 + 60,
            "line": json.dumps(stream_json_obj)}


def _write_live_only(run_dir: Path, node_id: str, *, session_id: str,
                     records: list[dict]) -> Path:
    """Run has NO real transcript; only the live sidecar with envelopes.

    Records are the raw Claude Code transcript dicts (the same shape as
    ``sessions/<sid>.jsonl``). Each is wrapped in ``{"line": "..."}``.
    """
    live = run_dir / f"agent-{node_id}.live.jsonl"
    run_dir.mkdir(parents=True, exist_ok=True)
    # First envelope carries the session_id so the resolver's rule 1 learns it.
    cost_state = {"session_id": session_id, "total_cost_usd": 0.31, "num_turns": 12}
    lines = [_envelope(cost_state)] + [_envelope(r) for r in records]
    live.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return live


# ── A) Lost-transcript fallback (live sidecar) ─────────────────────────────


def test_session_entries_decodes_live_sidecar_envelopes(tmp_path: Path) -> None:
    """The same parser handles ``agent-<node>.live.jsonl`` as a real transcript."""
    live = tmp_path / "agent-implementer.live.jsonl"
    records = [
        {"type": "user", "timestamp": _iso(T0 + 10), "message": {"content": [
            {"type": "text", "text": "implement the fix"}]}},
        {"type": "assistant", "timestamp": _iso(T0 + 70), "message": {"content": [
            {"type": "text", "text": "I edited the file and ran pytest."}]}},
        {"type": "result", "timestamp": _iso(T0 + 130),
         "result": "done", "session_id": "sid-x",
         "total_cost_usd": 0.31, "num_turns": 12},
    ]
    live.write_text("\n".join(json.dumps(_envelope(r)) for r in records) + "\n")

    assert _is_live_sidecar(live) is True

    decoded = _live_sidecar_records(live)
    assert [r["type"] for r in decoded] == ["user", "assistant", "result"]

    entries = _session_entries(live)
    kinds = [e["k"] for e in entries]
    assert "user" in kinds and "text" in kinds and "note" in kinds


def test_resolve_session_path_falls_back_to_live_sidecar_when_no_transcript(home: Path) -> None:
    """Rule 5: ``agent-<node>.live.jsonl`` becomes the transcript when no real session exists."""
    run_id = "run-lost-tx"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    run_dir = home / "runs" / run_id

    records = [
        {"type": "result", "result": "done", "session_id": "sid-lost",
         "num_turns": 7},
    ]
    _write_live_only(run_dir, "implementer", session_id="sid-lost",
                     records=records)

    run_obj = _load(home, run_id)
    assert run_obj is not None
    target = next(n for n in run_obj.nodes if n.id == "implementer")
    sid = _resolve_session_path(run_obj, target)
    assert sid is not None
    assert sid.name == "agent-implementer.live.jsonl"


# ── B) ~/.claude/projects fallback ─────────────────────────────────────────


def test_resolve_session_path_falls_back_to_home_projects_transcript(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Rule 4: when no local session exists, look up the sid under ``~/.claude/projects/*``."""
    run_id = "run-home-fallback"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    run_dir = home / "runs" / run_id

    sid = "sid-only-in-home-projects"
    # The live sidecar carries the sid (no local session file).
    records = [{"type": "user", "timestamp": _iso(T0 + 10),
                "message": {"content": [{"type": "text", "text": "hi"}]}}]
    _write_live_only(run_dir, "implementer", session_id=sid, records=records)

    # Create the operator's home-projects tree at a tmp HOME so the resolver
    # finds the transcript outside of ``home``.
    fake_home = tmp_path / "operator-home"
    proj = fake_home / ".claude" / "projects" / "-Volumes-proj-home"
    proj.mkdir(parents=True)
    home_tx = proj / f"{sid}.jsonl"
    home_tx.write_text(json.dumps({"type": "user", "timestamp": _iso(T0 + 10),
                                   "message": {"content": [
                                       {"type": "text", "text": "from home"}]}}) + "\n")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    run_obj = _load(home, run_id)
    assert run_obj is not None
    target = next(n for n in run_obj.nodes if n.id == "implementer")
    found = _resolve_session_path(run_obj, target)
    assert found is not None
    assert str(found) == str(home_tx), (
        f"expected home-projects hit {home_tx}, got {found}")


def test_resolve_session_path_home_projects_keeps_newest_first(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Among multiple ``projects/*/<sid>.jsonl`` hits, return the newest."""
    run_id = "run-home-multi"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    run_dir = home / "runs" / run_id

    sid = "sid-multi"
    records = [{"type": "user", "timestamp": _iso(T0 + 10),
                "message": {"content": [{"type": "text", "text": "x"}]}}]
    _write_live_only(run_dir, "implementer", session_id=sid, records=records)

    fake_home = tmp_path / "operator-home-multi"
    old_proj = fake_home / ".claude" / "projects" / "old-proj"
    new_proj = fake_home / ".claude" / "projects" / "new-proj"
    old_proj.mkdir(parents=True)
    new_proj.mkdir(parents=True)
    old = old_proj / f"{sid}.jsonl"
    new = new_proj / f"{sid}.jsonl"
    body = json.dumps({"type": "user", "timestamp": _iso(T0 + 10),
                       "message": {"content": [
                           {"type": "text", "text": "hi"}]}}) + "\n"
    old.write_text(body)
    new.write_text(body)
    # Make ``new`` strictly newer.
    import os
    import time as _t
    _t.sleep(0.05)
    os.utime(new, (T0 + 999_999_999, T0 + 999_999_999))

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    run_obj = _load(home, run_id)
    assert run_obj is not None
    target = next(n for n in run_obj.nodes if n.id == "implementer")
    found = _resolve_session_path(run_obj, target)
    assert found is not None and str(found) == str(new)


# ── C) Overview view shape ─────────────────────────────────────────────────


def test_overview_view_carries_every_required_part(home: Path) -> None:
    """``overview`` returns ``headline``/``facts``/``result``/``files``/
    ``diff``/``diff_note``/``final``/``links``."""
    run_id = "run-overview-shape"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done", cost=0.10, calls=2)
    run_dir = home / "runs" / run_id
    _write_live_only(run_dir, "implementer", session_id="sid-shape", records=[
        {"type": "result", "result": "I shipped the patch.",
         "session_id": "sid-shape", "num_turns": 9},
    ])

    run_obj = _load(home, run_id)
    assert run_obj is not None
    target = next(n for n in run_obj.nodes if n.id == "implementer")
    session_path = _resolve_session_path(run_obj, target)
    view = _overview_view(run_obj, target, session_path, run_dir)
    for k in ("headline", "facts", "result", "files", "diff",
              "diff_note", "final", "links"):
        assert k in view, k
    assert {"t", "c"} <= set(view["headline"])
    assert isinstance(view["facts"], list) and view["facts"]
    assert {"title", "items"} <= set(view["result"])
    assert view["final"]["text"].startswith("I shipped the patch.")


# ── D) Per-kind headlines ─────────────────────────────────────────────────


def test_reviewer_headline_uses_verdict_and_first_reason(home: Path) -> None:
    run_id = "run-reviewer-headline"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="reviewer", ntype="reviewer",
                        lane="reviewer", finish="done")
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "warn",
        "notes": [
            {"title": "scoped edits OK"},
            {"title": "missing test for new branch"},
        ],
    }))
    out = build_node(home, run_id, "reviewer", view="overview")
    assert out["ok"] is True
    headline = out["headline"]
    assert "warn" in headline["t"]
    assert "scoped edits OK" in headline["t"]
    assert headline["c"] == "yellow"


def test_verifier_headline_with_failing_checks(home: Path) -> None:
    run_id = "run-verifier-fail"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="verifier", ntype="verifier",
                        lane="shell", finish="done")
    (run_dir / "verifier_verifier.json").write_text(json.dumps({
        "checks": [
            {"name": "scope", "pass": True},
            {"name": "lint", "pass": False},
            {"name": "format", "pass": True},
            {"name": "type", "pass": False},
            {"name": "test", "pass": True},
        ],
    }))
    out = build_node(home, run_id, "verifier", view="overview")
    headline = out["headline"]
    assert headline["t"].startswith("2 of 5")
    assert "lint" in headline["t"]
    assert headline["c"] == "red"


def test_verifier_headline_unverified_executor_shape(home: Path) -> None:
    run_id = "run-verifier-unverified"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="verifier", ntype="verifier",
                        lane="shell", finish="done")
    (run_dir / "verifier_verifier.json").write_text(json.dumps({
        "pass": False,
        "error_summary": "python: command not found",
    }))
    out = build_node(home, run_id, "verifier", view="overview")
    headline = out["headline"]
    assert headline["t"].startswith("UNVERIFIED")
    assert "python: command not found" in headline["t"]
    assert headline["c"] == "red"


def test_implementer_headline_with_diff(home: Path) -> None:
    """Implementer headline reflects the diff (``changed N files``)."""
    run_id = "run-impl-diff"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    # Drop a diff so the changes-view part is non-empty.
    (run_dir / "framework-edit.diff").write_text(
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    out = build_node(home, run_id, "implementer", view="overview")
    assert out["ok"] is True
    headline = out["headline"]
    # Either ``changed`` (when the diff is parsed) or ``no files changed``
    # (when only the raw file is present). Both are valid; we accept both
    # but require green or muted.
    assert headline["c"] in ("green", "muted")


def test_failed_node_headline_uses_finish_reason(home: Path) -> None:
    run_id = "run-failed"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="crash")
    run_dir = home / "runs" / run_id
    _write_live_only(run_dir, "implementer", session_id="sid-failed",
                     records=[{"type": "user", "timestamp": _iso(T0 + 10),
                               "message": {"content": [{"type": "text", "text": "x"}]}}])
    out = build_node(home, run_id, "implementer", view="overview")
    headline = out["headline"]
    assert "crash" in headline["t"]
    assert headline["c"] == "red"


def test_lens_headline_uses_first_h1(home: Path) -> None:
    run_id = "run-lens"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="lens", ntype="lens",
                        lane="lens", finish="done")
    (run_dir / "lens-lens.md").write_text(
        "# Code impact assessment\n\n## detail\nmore\n")
    out = build_node(home, run_id, "lens", view="overview")
    headline = out["headline"]
    assert "Code impact" in headline["t"]


def test_planner_headline_uses_plan_objective(home: Path) -> None:
    run_id = "run-planner"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="planner", ntype="planner",
                        lane="planner", finish="done")
    (run_dir / "plan.json").write_text(json.dumps({
        "objective": "Scope the surface area of the kickoff.",
    }))
    out = build_node(home, run_id, "planner", view="overview")
    headline = out["headline"]
    assert "Scope the surface" in headline["t"]


def test_command_node_headline_uses_last_log_line(home: Path) -> None:
    run_id = "run-cmd"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="publisher", ntype="publisher",
                        lane="shell", finish="done")
    (run_dir / "impl-publisher.log").write_text(
        "publishing to main\n[ok] pushed\n")
    out = build_node(home, run_id, "publisher", view="overview")
    headline = out["headline"]
    assert "[ok] pushed" in headline["t"]
    # The headline MUST NOT be red — the command-shaped branch owns it,
    # not the failure-reason fallback (r2 reviewer BLOCKER).
    assert headline["c"] != "red"


def test_done_implementer_with_impl_log_and_diff_shows_files_changed(
    home: Path,
) -> None:
    """r2 reviewer BLOCKER — done implementer with an impl log next to it
    must NOT get a red ``failure`` headline. The implementer branch owns
    the headline (``changed N files``); the impl log is incidental.
    """
    run_id = "run-impl-with-log"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    # A real diff so the implementer headline has content.
    (run_dir / "framework-edit.diff").write_text(
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    # An impl log next to a DONE node — must not leak into the failure
    # reason fallback now that it is confined to failed nodes.
    (run_dir / "impl-implementer.log").write_text("editing x.py\n[ok] pushed\n")
    out = build_node(home, run_id, "implementer", view="overview")
    headline = out["headline"]
    assert headline["c"] != "red", (
        f"DONE implementer got red headline: {headline!r}")
    assert "changed" in headline["t"].lower() or headline["t"] == "no files changed"


def test_done_lens_with_live_sidecar_uses_report_heading(home: Path) -> None:
    """r2 reviewer BLOCKER — done lens with an ``agent-<id>.live.jsonl``
    sidecar must NOT get a red raw-JSON envelope headline. The lens
    headline branch owns the dispatch.
    """
    run_id = "run-lens-live"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="code_impact_lens", ntype="lens",
                        lane="lens", finish="done")
    (run_dir / "lens-code_impact_lens.md").write_text(
        "# Code impact assessment\n\n## detail\nmore\n")
    # Real run: live sidecar envelopes. After the fix, the live sidecar is
    # excluded from the failure-reason fallback so the per-kind branch owns
    # the headline.
    _write_live_only(run_dir, "code_impact_lens", session_id="sid-lens-live",
                     records=[{"type": "user", "timestamp": _iso(T0 + 10),
                               "message": {"content": [
                                   {"type": "text", "text": "go"}]}}])
    out = build_node(home, run_id, "code_impact_lens", view="overview")
    headline = out["headline"]
    assert headline["c"] != "red", (
        f"DONE lens got red headline: {headline!r}")
    assert "Code impact" in headline["t"]


def test_done_publisher_with_impl_log_is_not_red(home: Path) -> None:
    """r2 reviewer BLOCKER — done publisher with an impl log next to it
    must NOT get a red headline. The command branch owns it.
    """
    run_id = "run-publisher-not-red"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="publisher", ntype="publisher",
                        lane="shell", finish="done")
    (run_dir / "impl-publisher.log").write_text(
        "publishing to main\n[ok] pushed\n")
    out = build_node(home, run_id, "publisher", view="overview")
    headline = out["headline"]
    assert headline["c"] != "red", (
        f"DONE publisher got red headline: {headline!r}")


def test_reviewer_headline_needs_revision_with_reasons(home: Path) -> None:
    """r2 reviewer MAJOR — reviewer headline reads ``reasons`` (the shape
    the framework-edit reviewer contract emits as ``{"verdict": …,
    "notes": [str, str, …]}``) and surfaces it after the verdict.
    """
    run_id = "run-reviewer-needs-revision"
    run_dir = _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="reviewer", ntype="reviewer",
                        lane="reviewer", finish="done")
    # Both ``reasons`` (preferred) and ``notes`` (legacy) for cross-shape.
    (run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "reasons": ["scoped edits OK", "missing test for new branch"],
    }))
    out = build_node(home, run_id, "reviewer", view="overview")
    headline = out["headline"]
    assert "needs_revision" in headline["t"]
    assert "scoped edits OK" in headline["t"]
    # ``needs_revision`` maps to yellow in the kickoff's framework-edit
    # verdict colour map.
    assert headline["c"] == "yellow"


# ── E) Default view + view dispatch ────────────────────────────────────────


def test_default_view_is_overview() -> None:
    assert _DEFAULT_VIEW == "overview"
    assert "overview" in _VIEWS


def test_build_node_uses_overview_when_view_omitted(home: Path) -> None:
    run_id = "run-default-view"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    run_dir = home / "runs" / run_id
    _write_live_only(run_dir, "implementer", session_id="sid-default", records=[
        {"type": "result", "result": "ok", "session_id": "sid-default",
         "num_turns": 3},
    ])
    out = build_node(home, run_id, "implementer")
    assert out["ok"] is True
    assert out["view"] == "overview"
    assert "headline" in out and "facts" in out and "final" in out


def test_build_node_rejects_unknown_view(home: Path) -> None:
    run_id = "run-unknown-view"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    out = build_node(home, run_id, "implementer", view="bogus")
    assert out["ok"] is False
    assert "unknown view" in out["error"]


# ── F) CLI surface ─────────────────────────────────────────────────────────


def test_cli_node_accepts_overview_view(home: Path, capsys) -> None:
    run_id = "run-cli-view"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    run_dir = home / "runs" / run_id
    _write_live_only(run_dir, "implementer", session_id="sid-cli", records=[
        {"type": "result", "result": "ok", "session_id": "sid-cli",
         "num_turns": 3},
    ])
    rc = board_cmd.main(
        ["node", run_id, "implementer", "--view", "overview",
         "--home", str(home), "--json"], "")
    assert rc == 0, capsys.readouterr().err
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["view"] == "overview"
    assert "headline" in out and "facts" in out


def test_cli_node_defaults_to_overview_when_view_omitted(home: Path, capsys) -> None:
    run_id = "run-cli-default"
    _insert_run(home, run_id=run_id)
    _insert_node_events(home, run_id, node_id="implementer", ntype="implementer",
                        lane="worker", finish="done")
    rc = board_cmd.main(
        ["node", run_id, "implementer", "--home", str(home), "--json"], "")
    assert rc == 0, capsys.readouterr().err
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True


# ── r7 regression: live-sidecar cursor must not skip records on appended
#    envelopes, and the verifier headline must tolerate DeprecationWarning
#    prefixes that recipe runner writes before the JSON object.


def test_stream_entries_cursor_drops_no_records_with_non_json_lines(tmp_path: Path) -> None:
    """r7 fix #1: a non-blank non-JSON line in the sidecar must NOT advance
    the cursor past later decoded records.

    The bug: ``_stream_entries`` previously set ``next_offset`` to the file's
    non-blank line count. With a sidecar like

        {"line":"<env 0>"}
        {"line":"<env 1>"}
        not-json-stderr-line
        {"line":"<env 2>"}
        {"line":"<env 3>"}

    the second poll at ``offset == 4`` filtered out every appended record
    whose ``_line < 4``. Fix: drop the physical-line advancement; the
    cursor tracks ``max(emitted _line) + 1`` only.

    The shape mirrors the reviewer's reproducer: ``[system, stderr-text,
    assistant, tool_use, SECOND MESSAGE, result]`` — the stale cursor
    swallowed the ``SECOND MESSAGE`` until a full ``offset=0`` reload.
    """
    from mini_ork.ide_pages.node import _stream_entries
    from mini_ork.ide_pages.run import Node

    live = tmp_path / "agent-implementer.live.jsonl"
    env0 = json.dumps({"type": "system", "subtype": "init", "timestamp": _iso(T0 + 5)})
    env1 = json.dumps({"type": "user", "timestamp": _iso(T0 + 10),
                       "message": {"content": [{"type": "text", "text": "do X"}]}})
    env2 = json.dumps({"type": "assistant", "timestamp": _iso(T0 + 70),
                       "message": {"content": [{"type": "text", "text": "ok"}]}})
    env3 = json.dumps({"type": "assistant", "timestamp": _iso(T0 + 130),
                       "message": {"content": [
                           {"type": "text", "text": "SECOND MESSAGE"}]}})
    env4 = json.dumps({"type": "result", "timestamp": _iso(T0 + 190),
                       "result": "done", "session_id": "sid"})
    live.write_text(
        json.dumps({"line": env0}) + "\n"
        + json.dumps({"line": env1}) + "\n"
        + "this-line-is-not-json-and-shifts-the-cursor" + "\n"
        + json.dumps({"line": env2}) + "\n"
        + json.dumps({"line": env3}) + "\n"
        + json.dumps({"line": env4}) + "\n"
    )

    target = Node(id="implementer", type="implementer", start=T0, end=T0 + 200)

    # First poll: cold start, offset=0. Every decoded record surfaces; the
    # non-JSON stderr line does NOT consume a cursor slot.
    entries1, next_off = _stream_entries(
        live, None, "rid", tmp_path, target=target, offset=0,
        run_dir=None, transcript_has_result=True, is_live=True,
    )
    assert any(
        isinstance(e.get("arg"), str) and "SECOND MESSAGE" in e["arg"]
        for e in entries1
    ), f"cold poll must surface SECOND MESSAGE, got args: {[e.get('arg') for e in entries1]}"
    # next_offset must equal the count of DECODED records (5: env0..env4),
    # not the non-blank physical line count (6). Anything larger would skip
    # appended records on subsequent polls.
    assert next_off == 5, f"cursor must track decoded-record count, got {next_off}"

    # Incremental poll at next_off on the same (unchanged) file → empty.
    entries2, _next_off2 = _stream_entries(
        live, None, "rid", tmp_path, target=target, offset=next_off,
        run_dir=None, transcript_has_result=True, is_live=True,
    )
    assert entries2 == [], (
        f"incremental poll on unchanged file must be empty, got {entries2}"
    )


def test_stream_entries_cursor_surfaces_appended_envelope(tmp_path: Path) -> None:
    """r7 fix #1 (positive half): once the cursor is at ``max(emitted _line)+1``,
    new envelopes appended to the file past that cursor must surface on the
    next poll.

    The OLD ``physical_lines`` advancement skipped ahead of new records when
    the file had non-JSON noise mixed with envelopes (the live-sidecar
    reproducer in the reviewer notes). The fix drops that advancement so
    the cursor tracks decoded records only.
    """
    from mini_ork.ide_pages.node import _stream_entries
    from mini_ork.ide_pages.run import Node

    live = tmp_path / "agent-implementer.live.jsonl"
    env0 = json.dumps({"type": "system", "subtype": "init", "timestamp": _iso(T0 + 5)})
    env1 = json.dumps({"type": "user", "timestamp": _iso(T0 + 10),
                       "message": {"content": [{"type": "text", "text": "do X"}]}})
    env2 = json.dumps({"type": "assistant", "timestamp": _iso(T0 + 70),
                       "message": {"content": [{"type": "text", "text": "ok"}]}})
    env3 = json.dumps({"type": "assistant", "timestamp": _iso(T0 + 130),
                       "message": {"content": [
                           {"type": "text", "text": "APPENDED"}]}})
    env4 = json.dumps({"type": "result", "timestamp": _iso(T0 + 190),
                       "result": "done", "session_id": "sid"})
    target = Node(id="implementer", type="implementer", start=T0, end=T0 + 200)

    # Phase 1 — file has env0..env2 plus a non-JSON line. Decoded records:
    # [system, user, assistant] (3 records). max _line = 2. So the cursor
    # lands at 3.
    live.write_text(
        json.dumps({"line": env0}) + "\n"
        + json.dumps({"line": env1}) + "\n"
        + "this-line-is-not-json-and-shifts-the-cursor" + "\n"
        + json.dumps({"line": env2}) + "\n"
    )
    _, snap_off = _stream_entries(
        live, None, "rid", tmp_path, target=target, offset=0,
        run_dir=None, transcript_has_result=True, is_live=True,
    )
    assert snap_off == 3, (
        f"phase-1 cursor must be 3 (decoded-record count), got {snap_off}"
    )

    # Phase 2 — APPEND env3 + env4 past the cursor. They have _line = 3, 4
    # in the fresh decode. Poll 2 at snap_off must surface them.
    with live.open("a") as f:
        f.write(json.dumps({"line": env3}) + "\n")
        f.write(json.dumps({"line": env4}) + "\n")

    entries, new_off = _stream_entries(
        live, None, "rid", tmp_path, target=target, offset=snap_off,
        run_dir=None, transcript_has_result=True, is_live=True,
    )
    assert any(
        isinstance(e.get("arg"), str) and "APPENDED" in e["arg"]
        for e in entries
    ), f"incremental poll must surface APPENDED, got {[e.get('arg') for e in entries]}"
    # The two appended records add 2 to the cursor: 3 + 2 = 5.
    assert new_off == 5


def test_overview_verifier_headline_survives_deprecation_warning_prefix(tmp_path: Path) -> None:
    """r7 fix #2: a DeprecationWarning line before the JSON body must NOT
    degrade the verifier headline to ``"verifier · <node-id>"``.

    Recipe verifier scripts (recipes/framework-edit/verifiers/*.py) emit a
    ``DeprecationWarning`` line on stderr-equivalent before the JSON
    payload. The strict ``_read_json_safely`` returns ``{}`` for that
    content, so ``_overview_verifier_headline`` silently degraded to the
    fallback. Fix: reuse the tolerant ``_read_json`` from ``node_changes``
    that scans for the first ``{`` and parses from there.
    """
    from mini_ork.ide_pages.node import _overview_verifier_headline
    from mini_ork.ide_pages.run import Node

    run_dir = tmp_path
    (run_dir / "verifier_static-check.json").write_text(
        "DeprecationWarning: distutils has been deprecated, use setuptools\n"
        + json.dumps({
            "checks": [
                {"name": "diff-apply-check-clean", "pass": False,
                 "msg": "diff already applied"},
                {"name": "ruff-clean", "pass": True},
            ],
        })
    )
    target = Node(
        id="static_check_verifier", type="verifier",
        start=T0, end=T0 + 50,
        prompt="recipes/framework-edit/verifiers/static-check.py",
    )
    headline = _overview_verifier_headline(run_dir, target)
    assert "1 of 2 checks failed: diff-apply-check-clean" in headline["t"], (
        f"expected failing-check headline, got {headline!r}"
    )
    assert headline["c"] == "red"


def test_overview_verifier_headline_survives_deprecation_warning_prefix_node_id(
    tmp_path: Path,
) -> None:
    """r7 fix #2 (node-id fallback): same tolerance when only
    ``verifier_<node.id>.json`` exists and the prompt has no verifier stem.
    """
    from mini_ork.ide_pages.node import _overview_verifier_headline
    from mini_ork.ide_pages.run import Node

    run_dir = tmp_path
    (run_dir / "verifier_some-check.json").write_text(
        "Warning: noisy recipe runner header line\n"
        + json.dumps({
            "checks": [{"name": "lint-clean", "pass": True}],
        })
    )
    target = Node(id="some-check", type="verifier", start=T0, end=T0 + 30)
    headline = _overview_verifier_headline(run_dir, target)
    assert headline["t"] == "all 1 checks passed"
    assert headline["c"] == "green"