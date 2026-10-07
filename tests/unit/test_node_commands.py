"""IDE node-stream for non-agent nodes (kickoff ide-node-commands).

Verifies the four surfaces the kickoff names:

1. ``_run_verifier_ref`` writes ``<run_dir>/node-cmd/<stem>.json`` with
   argv/cmd/cwd/rc/timing/output_path and a 6-key env whitelist — never
   any secret, never any side effect on rc/evidence bytes.
2. With the recorder sidecar present, ``build_node(..., view='stream')``
   emits the kickoff's §2a command entry (``head="$"``, the arg as
   ``cd <cwd> && <cmd>``, all output lines, an ``exit <rc> · <dur>s``
   tail).
3. Legacy runs (no record, only ``verifier_<stem>.json``) reconstruct a
   command entry plus the "predates command recording" note.
4. Researcher-shaped fixtures (cycle-gate JSON with ``gate_cmd_*`` keys,
   live-smoke JSON with ``surfaces[]`` + ``_smoke_cmd_W5-91.log``)
   surface as one ``subcmd`` entry per command.
6. Nodes that produced no artefacts keep emitting the §2e single
   "nothing was stored" note and never lose the agent-node stream path.

Mirrors ``test_ide_pages_node.py``'s tmp-home + init_db + inline WORKFLOW
pattern. No LLM calls.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.cli import execute as ex
from mini_ork.ide_pages.node import (
    _command_stream_entries, _is_command_backed, _verifier_stem, build_node,
)
from mini_ork.ide_pages.run import Node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-nc-1791000000-abc123"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: nc-demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: static_check_verifier, type: verifier, model_lane: verifier,
     verifier_ref: verifiers/static-check.py}
  - {name: test_verifier, type: verifier, model_lane: verifier,
     verifier_ref: verifiers/test.py}
  - {name: rollback_node, type: rollback, model_lane: rollback}
  - {name: publisher_node, type: publisher, model_lane: publisher}
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
    (recipe / "task_class.yaml").write_text("name: nc-demo\ndescription: node-commands demo\n")
    return h


# ── §1 — recorder sidecar ────────────────────────────────────────────────────


def test_run_verifier_ref_writes_node_cmd_record(home: Path, monkeypatch) -> None:
    """A tiny verifier writing {"pass": true} leaves a node-cmd record."""
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    recipe = home / "recipes" / "demo-recipe"
    script = recipe / "verifiers" / "static-check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('{\"pass\": true}')\n")
    ev = run_dir / "verifier_static-check.json"
    ev.write_text("")  # the production path will overwrite
    rc = ex._run_verifier_ref(
        str(script), str(ev),
        plan_path="/p/plan.json", artifact_path="/a/art.bin",
        cwd=str(home), run_dir=str(run_dir),
    )
    assert rc == 0
    record_path = run_dir / "node-cmd" / "verifier_static-check.json"
    assert record_path.is_file(), f"missing record at {record_path}"
    record = json.loads(record_path.read_text())
    assert "python" in record["argv"][0]
    assert record["argv"][-1] == str(script)
    assert "python" in record["cmd"]
    assert record["cwd"] == str(home)
    assert record["rc"] == 0
    assert record["output_path"] == str(ev)
    assert isinstance(record["started_at"], (int, float))
    assert isinstance(record["ended_at"], (int, float))
    assert record["ended_at"] >= record["started_at"]
    # Whitelisted env present.
    assert record["env"].get("MINI_ORK_PLAN_PATH") == "/p/plan.json"
    assert record["env"].get("ARTIFACT_PATH") == "/a/art.bin"
    assert record["env"].get("MINI_ORK_RUN_DIR") == str(run_dir)
    # PYTHONPATH set by execute.py
    assert "PYTHONPATH" in record["env"]
    # Secret NOT present.
    assert "SOME_API_KEY" not in record["env"]
    assert "SOME_API_KEY" not in record  # top-level either
    # Evidence untouched in shape.
    assert "pass" in ev.read_text()


def test_run_verifier_ref_rc_and_evidence_bytes_unchanged(home: Path, monkeypatch) -> None:
    """The recorder must never change the verifier's rc / evidence bytes."""
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    recipe = home / "recipes" / "demo-recipe"
    script = recipe / "verifiers" / "static-check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('OUT'); print('{\"pass\": true}')\n")
    ev = run_dir / "verifier_static-check.json"
    ev.write_text("")
    rc = ex._run_verifier_ref(
        str(script), str(ev),
        plan_path="/p/plan.json", artifact_path="/a/art.bin",
        cwd=str(home), run_dir=str(run_dir),
    )
    assert rc == 0
    text = ev.read_text()
    assert "OUT" in text and "pass" in text
    # The record was written.
    assert (run_dir / "node-cmd" / "verifier_static-check.json").is_file()


def test_run_verifier_ref_record_landed_in_run_dir_not_tmp(home: Path, monkeypatch) -> None:
    """When the handler passes run_dir=ctx.run_dir_eff, the record lands under it."""
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    run_dir = home / "runs" / RUN / "deep"
    run_dir.mkdir(parents=True)
    recipe = home / "recipes" / "demo-recipe"
    script = recipe / "verifiers" / "static-check.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('{\"pass\": true}')\n")
    # Place evidence one level deep; the record must follow run_dir.
    ev = run_dir / "evidence" / "static-check.log"
    ev.parent.mkdir(parents=True)
    rc = ex._run_verifier_ref(
        str(script), str(ev),
        plan_path="/p/plan.json", artifact_path="/a/art.bin",
        cwd=str(home), run_dir=str(run_dir),
    )
    assert rc == 0
    record_path = run_dir / "node-cmd" / "verifier_static-check.json"
    assert record_path.is_file()


# ── §2 — non-agent stream entries ────────────────────────────────────────────


def _seed_run(home: Path, *, status: str = "executing",
              run_id: str = RUN, recipe: str = "demo-recipe") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / "nc-demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# node-commands\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.0, T0, T0 + 120, T0 + 100,
         "nc-demo", str(kickoff), "latest", "tr-nc-1"))
    events = [
        ("node_start", "static_check_verifier", "verifier", "verifier", T0 + 30, None),
        ("node_end", "static_check_verifier", "verifier", "verifier", T0 + 60, "done"),
        ("node_start", "rollback_node", "rollback", "rollback", T0 + 60, None),
        ("node_end", "rollback_node", "rollback", "rollback", T0 + 90, "done"),
        ("node_start", "publisher_node", "publisher", "publisher", T0 + 90, None),
        ("node_end", "publisher_node", "publisher", "publisher", T0 + 120, "done"),
    ]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (f"ev-nc-{i}", run_id, kind, json.dumps(payload), ts))
    con.commit()
    con.close()
    return run_dir


def test_stream_recorded_verifier_emits_command_entry(home: Path) -> None:
    """With a node-cmd/<stem>.json record, the first stream entry is the kickoff §2a command."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    record = {
        "script": "verifiers/static-check.py",
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {"MINI_ORK_RUN_DIR": str(run_dir)},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.5,
        "rc": 0,
        "output_path": str(run_dir / "evidence" / "static-check.log"),
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["ok"] is True
    entries = out["entries"]
    # First entry is the $ command.
    assert entries, out
    first = entries[0]
    assert first["k"] == "tool"
    assert first["head"] == "$"
    assert first["arg"].startswith("cd ") and "&& python3" in first["arg"]
    lines = first["lines"]
    # Tail line is the exit summary.
    tail = lines[-1]["t"]
    assert tail.startswith("exit 0")
    assert "·" in tail
    # Status pill: finished · command
    assert out["status"] == "finished · command"
    assert out["status_c"] == "green"


def test_stream_recorded_verifier_failed_rc(home: Path) -> None:
    """Non-zero rc → "failed · command" + red tail."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    record = {
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.5,
        "rc": 1,
        "output_path": str(run_dir / "evidence" / "static-check.log"),
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["status"] == "failed · command"
    assert out["status_c"] == "red"


def test_stream_legacy_run_reconstructs_command_and_note(home: Path) -> None:
    """A legacy verifier_<stem>.json (no node-cmd/) → reconstructed command + note."""
    run_dir = _seed_run(home)
    legacy = run_dir / "verifier_static-check.json"
    legacy.write_text('{"pass": false, "errors": ["a", "b"]}\n')
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["ok"] is True
    entries = out["entries"]
    assert entries[0]["k"] == "tool"
    assert entries[0]["head"] == "$"
    assert entries[0]["arg"].startswith("cd ") or "python3" in entries[0]["arg"]
    # Note entry follows.
    note_entries = [e for e in entries if e["k"] == "note"]
    assert note_entries, entries
    note = note_entries[0]
    assert "predates command recording" in note["arg"]


def test_stream_subcommand_entries_from_cycle_gate_json(home: Path) -> None:
    """gate_cmd / gate_cmd_output_tail / gate_cmd_exit → subcmd entries."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    cycle_gate = {
        "pass": True,
        "gate_cmd": "python3 -m researcher.cli cycle_gate",
        "gate_cmd_output_tail": "all checks passed",
        "gate_cmd_exit": 0,
    }
    record = {
        "argv": ["python3", "verifiers/cycle-gate.py"],
        "cmd": "python3 verifiers/cycle-gate.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.0,
        "rc": 0,
        "output_path": str(run_dir / "evidence" / "cycle-gate.log"),
    }
    (node_cmd / "verifier_cycle-gate.json").write_text(json.dumps(record))
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "cycle-gate.log").write_text(json.dumps(cycle_gate))
    build_node(home, RUN, "static_check_verifier", view="stream")
    # The recorded entry is the $ command; the cycle-gate JSON sits in
    # evidence/cycle-gate.log (the verifier's own output). Direct-test the
    # helper to prove the §2b sub-command rendering.
    from mini_ork.ide_pages.node import _subcommand_entries
    sub = _subcommand_entries(cycle_gate)
    assert len(sub) >= 1, sub
    head_args = {(e.get("head"), e.get("arg")) for e in sub}
    assert any("subcmd" == h and "cycle_gate" in (a or "") for h, a in head_args)


def test_stream_subcommand_entries_from_smoke_logs(home: Path) -> None:
    """_<run_dir>.log files split on ``$ <command>`` blocks become subcmd entries."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    # ``_<kind>_cmd*.log`` belongs to the verifier whose stem names <kind>.
    smoke_log = run_dir / "_static_cmd_W5-91.log"
    smoke_log.write_text(
        "$ python3 -m smoke.cli step_one\n"
        "all checks pass\n"
        "[rc=0]\n"
        "$ python3 -m smoke.cli step_two\n"
        "second block\n"
        "[rc=0]\n"
    )
    record = {
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.0,
        "rc": 0,
        "output_path": str(run_dir / "evidence" / "static-check.log"),
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "static-check.log").write_text("")
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    sub = [e for e in out["entries"] if e.get("_src") == "subcmd"]
    assert len(sub) >= 2, [e.get("head") for e in out["entries"]]
    args = [e["arg"] for e in sub]
    assert any("step_one" in a for a in args)
    assert any("step_two" in a for a in args)


def test_stream_rollback_node_built_in_entry(home: Path) -> None:
    """A rollback node with execute.log + rolled-back.json → built-in entry."""
    run_dir = _seed_run(home)
    (run_dir / "execute.log").write_text(
        f"[rollback] discard_worktree: {run_dir}\n"
        f"[ok] rollback complete\n"
        f"[rollback] node_id=rollback_node\n"
    )
    (run_dir / "rolled-back.json").write_text(json.dumps({"ok": True}))
    out = build_node(home, RUN, "rollback_node", view="stream")
    entries = out["entries"]
    assert entries, out
    built_in = [e for e in entries if e.get("head") == "built-in"]
    assert built_in, [e["head"] for e in entries]
    head0 = built_in[0]
    assert head0["arg"].endswith("rollback_node") or "rollback_node" in head0["arg"]
    # A rolled-back.json note is also emitted.
    notes = [e for e in entries if e.get("head") == "rollback" and e["k"] == "note"]
    assert notes, [e["head"] for e in entries]


def test_stream_no_artefacts_emits_single_note(home: Path) -> None:
    """A non-agent node with no session, no log, no record → one note entry."""
    _seed_run(home)
    # No execute.log, no node-cmd, no verifier_<stem>.json.
    out = build_node(home, RUN, "publisher_node", view="stream")
    entries = out["entries"]
    notes = [e for e in entries if e["k"] == "note"]
    assert len(notes) == 1, entries
    assert "No command or output was stored" in notes[0]["arg"]


def test_stream_agent_node_unchanged(home: Path) -> None:
    """Agent nodes (with a transcript) keep their original stream path."""
    # Plant a transcript for the planner — but planner has no LLM calls
    # in this fixture. We test the gate instead: when session_path is
    # not None, _is_command_backed must be False.
    assert home.is_dir()  # use the fixture so the param isn't flagged
    node = Node(id="implementer", type="implementer", calls=1)
    assert _is_command_backed(node, session_path=Path("/tmp/session.jsonl"),
                              has_log=False) is False


def test_is_command_backed_picks_up_deterministic_types() -> None:
    """Deterministic node types (verifier/rollback/publisher/...) are command-backed."""
    from mini_ork.ide_pages.run import _DETERMINISTIC
    for ntype in _DETERMINISTIC:
        node = Node(id="x", type=ntype, calls=0)
        assert _is_command_backed(node, session_path=None, has_log=False) is True, ntype


def test_verifier_stem_uses_prompt_path() -> None:
    """_verifier_stem parses ``<recipe>/<verifier_ref>`` and drops the extension."""
    n = Node(id="static_check_verifier", type="verifier",
             prompt="framework-edit/verifiers/static-check.py")
    assert _verifier_stem(n) == "static-check"


def test_command_stream_entries_returns_none_when_no_artefacts(home: Path) -> None:
    """No record, no legacy evidence, no execute.log → None (caller falls back)."""
    run_dir = _seed_run(home)
    # publisher_node has no execute.log lines for itself.
    from mini_ork.ide_pages.run import _load
    run_obj = _load(home, RUN)
    assert run_obj is not None
    target = next(n for n in run_obj.nodes if n.id == "publisher_node")
    out = _command_stream_entries(run_dir, target, log_path=None, recipe_dir=None)
    # Either None (no artefacts) or a built-in entry; built-in only fires
    # when execute.log has lines for this node — in _seed_run we did not
    # write such lines, so None is the expected outcome.
    assert out is None or (isinstance(out, tuple) and out[0] == [])


# ── r2 — kickoff fix #9 (pinning tests) ──────────────────────────────────────


def test_legacy_researcher_run_emits_subcmd_and_logs(home: Path) -> None:
    """Legacy researcher run → `$` + reconstructed note + ONE gate entry with
    an ``exit 1`` muted line, two command-log entries from `_static_cmd_W5-91.log`,
    and the verifier-cycle-gate.log itself (kickoff fix #1 + #2).
    """
    run_dir = _seed_run(home)
    legacy = run_dir / "verifier_static-check.json"
    legacy.write_text(json.dumps({
        "pass": False,
        "errors": ["refuted"],
        "gate_cmd": "python3 -m researcher.cli cycle_gate",
        "gate_cmd_output_tail": "all checks passed\nrefuted by verifier",
        "gate_cmd_exit": 1,
    }))
    # Two `$ <cmd>` blocks (researcher smoke shape), named for this verifier.
    (run_dir / "_static_cmd_W5-91.log").write_text(
        "$ python3 -m smoke.cli step_one\n"
        "step one output\n"
        "[rc=0]\n"
        "$ python3 -m smoke.cli step_two\n"
        "step two output\n"
        "[rc=0]\n"
    )
    # The verifier's own log (stem-keyed: ``verifier-<stem>.log``).
    (run_dir / "verifier-static-check.log").write_text("refuted: 1 issue\n")
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["ok"] is True
    entries = out["entries"]
    heads = [e.get("head") for e in entries]
    # `$` command + reconstructed note + 1 gate subcmd (no longer 3).
    assert heads[0] == "$"
    assert "reconstructed" in heads
    # 1 subcmd from the gate_cmd JSON + 2 from the smoke log = 3 total.
    subcmd_entries = [e for e in entries if e.get("_src") == "subcmd"]
    assert len(subcmd_entries) == 3, [e.get("arg") for e in subcmd_entries]
    gate = next(e for e in subcmd_entries if "cycle_gate" in (e.get("arg") or ""))
    assert gate is not None
    exit_lines = [ln for ln in gate["lines"] if ln["t"].startswith("exit ")]
    assert exit_lines, gate["lines"]
    assert exit_lines[0]["t"] == "exit 1"
    assert exit_lines[0]["c"] == "muted"
    # Two smoke entries from the log.
    smoke_args = [e.get("arg") or "" for e in subcmd_entries]
    assert any("step_one" in a for a in smoke_args)
    assert any("step_two" in a for a in smoke_args)
    # The verifier-static-check.log was added as a log entry.
    log_args = [e.get("arg") or "" for e in entries if e.get("_src") == "log"]
    assert any("verifier-static-check.log" in a for a in log_args)


def test_legacy_gate_cmd_exit_int_is_coerced() -> None:
    """``gate_cmd_exit`` may be int in legacy fixtures; coerce, don't str() it."""
    cycle_gate = {
        "pass": False,
        "gate_cmd": "python3 -m researcher.cli cycle_gate",
        "gate_cmd_output_tail": "tail line",
        "gate_cmd_exit": 1,  # int, not str
    }
    from mini_ork.ide_pages.node import _subcommand_entries
    subs = _subcommand_entries(cycle_gate)
    assert len(subs) == 1, subs
    exit_lines = [ln for ln in subs[0]["lines"] if ln["t"].startswith("exit ")]
    assert exit_lines[0]["t"] == "exit 1"


def test_legacy_gate_cmd_missing_falls_back_to_sentinel() -> None:
    """When ``gate_cmd`` is absent, emit a single subcmd with the sentinel."""
    cycle_gate = {
        "pass": False,
        "gate_cmd_output_tail": "tail without gate_cmd",
        "gate_cmd_exit": 0,
    }
    from mini_ork.ide_pages.node import _subcommand_entries
    subs = _subcommand_entries(cycle_gate)
    assert len(subs) == 1, subs
    assert subs[0]["arg"] == "the step's gate command"
    exit_lines = [ln for ln in subs[0]["lines"] if ln["t"].startswith("exit ")]
    assert exit_lines[0]["t"] == "exit 0"


def test_rollback_execute_log_keeps_built_in_prefix_lines(home: Path) -> None:
    """execute.log with ``[ok] rollback complete`` / ``[fail] rollback`` keeps both."""
    run_dir = _seed_run(home)
    # execute.log indents its lines with two spaces (the real shape).
    (run_dir / "execute.log").write_text(
        f"  [rollback] discard_worktree: {run_dir}\n"
        f"  [ok] rollback complete\n"
        f"  [rollback] node_id=rollback_node\n"
        f"  [fail] rollback — nothing to roll back\n"
        f"  [info] rollback\n"
        f"  [route] policy=workflow_default node=reviewer lane=opus\n"
    )
    (run_dir / "rolled-back.json").write_text(json.dumps({"ok": False}))
    out = build_node(home, RUN, "rollback_node", view="stream")
    entries = out["entries"]
    built_in = [e for e in entries if e.get("head") == "built-in"]
    assert built_in, [e["head"] for e in entries]
    line_texts = " | ".join(ln["t"] for ln in built_in[0]["lines"])
    assert "[ok] rollback complete" in line_texts
    assert "[fail] rollback" in line_texts
    assert "[info] rollback" in line_texts
    assert "node=reviewer" not in line_texts


def test_offset_poll_returns_nothing_new(home: Path) -> None:
    """Polling at the returned offset yields an empty list and the same offset."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    record = {
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.0,
        "rc": 0,
        "output_path": str(run_dir / "evidence" / "static-check.log"),
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "static-check.log").write_text("OK\n")
    # First poll — full entries.
    first = build_node(home, RUN, "static_check_verifier", view="stream", offset=0)
    assert first["ok"] is True
    first_entries = first["entries"]
    assert first_entries, first
    next_offset = int(first["offset"])
    assert next_offset == len(first_entries), (next_offset, len(first_entries))
    # Second poll — at the returned offset, no duplicates.
    second = build_node(home, RUN, "static_check_verifier", view="stream",
                        offset=next_offset)
    assert second["ok"] is True
    assert second["entries"] == [], second["entries"]
    assert int(second["offset"]) == next_offset
    # The pill describes the whole stream, not the (empty) slice.
    assert (second["status"], second["status_c"]) == (first["status"], first["status_c"])
    assert first["status"] == "finished · command"


def test_pill_red_when_rc_zero_but_pass_false(home: Path) -> None:
    """``rc=0`` BUT verifier evidence ``pass: false`` → red ``failed · command``.

    r2 kickoff fix #5 widens the pill beyond just the recorded rc: when
    the recorded rc is 0 but the verifier wrote ``{"pass": false}`` in its
    evidence log, the pill must still colour red so the operator sees the
    truth instead of a green "finished · command" sticker over a refuted
    verdict.
    """
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    record = {
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.0,
        "rc": 0,
        "output_path": str(run_dir / "evidence" / "static-check.log"),
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "static-check.log").write_text(
        json.dumps({"pass": False, "errors": ["broken"]}),
    )
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["status"] == "failed · command", out["status"]
    assert out["status_c"] == "red"


def test_pill_red_for_legacy_pass_false(home: Path) -> None:
    """Legacy run with ``pass: false`` evidence → red ``failed · command``."""
    run_dir = _seed_run(home)
    (run_dir / "verifier_static-check.json").write_text(
        json.dumps({"pass": False, "errors": ["legacy failed"]}),
    )
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    assert out["ok"] is True
    assert out["status"] == "failed · command", out["status"]
    assert out["status_c"] == "red"


def test_no_duplicate_log_when_record_output_path_matches(home: Path) -> None:
    """A log whose absolute path equals ``output_path`` must not appear twice."""
    run_dir = _seed_run(home)
    node_cmd = run_dir / "node-cmd"
    node_cmd.mkdir()
    log_path = run_dir / "evidence" / "static-check.log"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text("OUT\n")
    record = {
        "argv": ["python3", "verifiers/static-check.py"],
        "cmd": "python3 verifiers/static-check.py",
        "cwd": str(home),
        "env": {},
        "started_at": T0 + 0.0,
        "ended_at": T0 + 1.0,
        "rc": 0,
        "output_path": str(log_path),  # == the log path
    }
    (node_cmd / "verifier_static-check.json").write_text(json.dumps(record))
    out = build_node(home, RUN, "static_check_verifier", view="stream")
    log_entries = [e for e in out["entries"] if e.get("_src") == "log"]
    assert len(log_entries) == 0, [e.get("arg") for e in log_entries]


def test_reconstructed_path_keeps_the_verifier_ref_subdirectory(home: Path) -> None:
    """``node.prompt`` is ``<recipe dir>/<verifier_ref>``: the reconstructed
    command runs ``<recipe dir path>/verifiers/static-check.py``."""
    run_dir = _seed_run(home)
    (run_dir / "verifier_static-check.json").write_text(json.dumps({"pass": True}))
    node = Node(id="static_check_verifier", type="verifier",
                prompt="demo-recipe/verifiers/static-check.py")
    entries, _offset, _src = _command_stream_entries(run_dir, node, None, None, home=home)
    expected = home / "recipes" / "demo-recipe" / "verifiers" / "static-check.py"
    assert entries[0]["arg"].endswith(f"python3 {expected}"), entries[0]["arg"]


def test_a_verifier_with_only_its_own_logs_still_shows_them(home: Path) -> None:
    """No JSON, no record, only ``verifier-<stem>.log`` + ``evidence/<stem>-*.log``
    (the researcher scope_guard shape): command + note + both logs."""
    run_dir = _seed_run(home)
    (run_dir / "verifier-static-check.log").write_text("step W5-91: 7 changed path(s)\nscope_clean\n")
    (run_dir / "evidence").mkdir(exist_ok=True)
    (run_dir / "evidence" / "static-check-1791-1-ab.log").write_text("scope_clean\n")
    node = Node(id="static_check_verifier", type="verifier",
                prompt="demo-recipe/verifiers/static-check.py")
    entries, total, _src = _command_stream_entries(run_dir, node, None, None, home=home)
    heads = [e["head"] for e in entries]
    assert heads == ["$", "reconstructed", "log", "log"], heads
    assert total == 4
    assert any("scope_clean" in ln["t"] for ln in entries[2]["lines"])


def test_smoke_command_logs_belong_to_the_smoke_verifier_only(home: Path) -> None:
    run_dir = _seed_run(home)
    (run_dir / "_smoke_cmd_W5-91.log").write_text("$ python3 smoke.py w5_91\nok\n[rc=0]\n")
    (run_dir / "verifier_static-check.json").write_text(json.dumps({"pass": True}))
    gate = Node(id="static_check_verifier", type="verifier",
                prompt="demo-recipe/verifiers/static-check.py")
    entries, _o, _s = _command_stream_entries(run_dir, gate, None, None, home=home)
    assert not any("smoke.py" in (e.get("arg") or "") for e in entries)
    (run_dir / "verifier_live-smoke.json").write_text(json.dumps({"pass": False, "status": "UNVERIFIED"}))
    smoke = Node(id="live_smoke", type="verifier", prompt="demo-recipe/verifiers/live-smoke.py")
    entries, _o, _s = _command_stream_entries(run_dir, smoke, None, None, home=home)
    assert any("smoke.py w5_91" in (e.get("arg") or "") for e in entries)
