"""Resume a parked run from the sentinel's recorded ``waiting_node``.

Kickoff w2-review-resume-from-waiting: approving the last pending node row of
a parked run must spawn ``recover --from-node <waiting_node> --strategy
resume`` so the recovery planner re-enters at the node that was waiting
instead of restarting at the first node it cannot reuse. A sentinel without a
usable ``waiting_node`` (older engine, empty / non-JSON / non-string body)
keeps today's ``--strategy resume`` path with an explicit stderr note.

The fixtures and helpers are shared with ``test_review_resume`` — this module
adds only the waiting-node cases; the degraded-sentinel path is covered
there. ``_spawn_recover`` is monkeypatched so no real subprocess is forked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_UNIT_DIR = Path(__file__).resolve().parent
if str(_UNIT_DIR) not in sys.path:
    sys.path.insert(0, str(_UNIT_DIR))

import test_review_resume as trr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Pin the home/DB to ``tmp_path`` and scrub every key the code reads."""
    db = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", db)
    for key in trr._POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in trr._RUN_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _sentinel(home: Path, run_id: str) -> Path:
    """Path to the run's ``.review-pause`` sentinel."""
    return home / "runs" / run_id / ".review-pause"


def _parked_run(home: Path, run_id: str,
                node_id: str = "contract_compiler") -> tuple[str, list[int]]:
    """Bootstrap a parked run with one pending node row for ``node_id``."""
    return trr._setup_parked_run(home, run_id, node_ids=[node_id])


def _full_payload() -> dict:
    """The live SDD ratification-pause sentinel body from the kickoff."""
    return {
        "waiting_node": "ratification_check",
        "awaiting": ["contract_compiler"],
        "inbox_ids": [929],
        "ts": 1780000000,
    }


# ── _waiting_node unit cases ─────────────────────────────────────────────


def test_waiting_node_missing_file(tmp_path):
    """A missing sentinel yields ``None`` rather than raising."""
    from mini_ork.cli import approvals_cmd

    assert approvals_cmd._waiting_node(tmp_path, "no-such-run") is None


def test_waiting_node_valid_payload(tmp_path):
    """A sentinel with a non-empty string ``waiting_node`` returns it."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-unit-valid"
    trr._write_review_pause(tmp_path, run_id, _full_payload())
    assert approvals_cmd._waiting_node(tmp_path, run_id) == "ratification_check"


def test_waiting_node_non_dict_json(tmp_path):
    """JSON that is not an object (a list here) yields ``None``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-unit-nondict"
    sentinel = _sentinel(tmp_path, run_id)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps(["waiting_node", "ratification_check"]),
                        encoding="utf-8")
    assert approvals_cmd._waiting_node(tmp_path, run_id) is None


@pytest.mark.parametrize("node", ["--strategy", "-x", "a b", "../etc/passwd",
                                  "node;rm -rf /", "n" * 129])
def test_waiting_node_rejects_a_value_that_is_not_a_node_id(tmp_path, node):
    """The value becomes ``recover --from-node <value>``; anything that could
    read as a flag, split into two argv entries, or leave the node namespace is
    treated as absent so the caller falls back to a plain resume."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-unit-badnode"
    trr._write_review_pause(tmp_path, run_id, {"waiting_node": node})
    assert approvals_cmd._waiting_node(tmp_path, run_id) is None


def test_waiting_node_survives_a_non_utf8_sentinel(tmp_path):
    """A sentinel with a stray byte is degraded, not fatal — the docstring's
    "never raises" must cover ``UnicodeDecodeError`` too, not just ``OSError``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-unit-badbytes"
    sentinel = _sentinel(tmp_path, run_id)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b'{"waiting_node": "ratification_check", "\xff": 1}')
    assert approvals_cmd._waiting_node(tmp_path, run_id) is None


# ── approve resumes from the waiting node ────────────────────────────────


def test_approve_spawns_resume_from_waiting_node(monkeypatch, tmp_path, capsys):
    """Full sentinel → one spawn, ``--from-node <waiting_node> --strategy resume``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-1"
    _parked_run(tmp_path, run_id)
    payload = _full_payload()
    trr._write_review_pause(tmp_path, run_id, payload)
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "contract_compiler"])
    captured = capsys.readouterr()

    assert rc == 0
    assert len(spawns) == 1, "exactly one spawn expected"
    _home_str, extra, _log = spawns[0]
    assert extra == ["--from-node", "ratification_check", "--strategy", "resume"]
    # The stderr note belongs to the degraded-sentinel path, not this one.
    assert "no waiting_node" not in captured.err

    # Renamed (not deleted) and the payload survives for the audit trail.
    assert not _sentinel(tmp_path, run_id).exists()
    resolved = _sentinel(tmp_path, run_id).with_name(".review-pause.resolved")
    assert resolved.is_file()
    assert json.loads(resolved.read_text(encoding="utf-8")) == payload


def test_empty_sentinel_resumes_without_entry_point(monkeypatch, tmp_path, capsys):
    """An empty sentinel spawns plain ``--strategy resume`` and notes why."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-2"
    _parked_run(tmp_path, run_id)
    _sentinel(tmp_path, run_id).write_text("", encoding="utf-8")
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "contract_compiler"])
    captured = capsys.readouterr()

    assert rc == 0
    assert len(spawns) == 1
    assert spawns[0][1] == ["--strategy", "resume"]
    assert "no waiting_node" in captured.err


def test_not_json_sentinel_behaves_like_empty(monkeypatch, tmp_path, capsys):
    """A sentinel holding ``not json`` spawns plain ``--strategy resume``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-3"
    _parked_run(tmp_path, run_id)
    _sentinel(tmp_path, run_id).write_text("not json", encoding="utf-8")
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "contract_compiler"])
    captured = capsys.readouterr()

    assert rc == 0
    assert len(spawns) == 1
    assert spawns[0][1] == ["--strategy", "resume"]
    assert "no waiting_node" in captured.err


def test_blank_waiting_node_behaves_like_empty(monkeypatch, tmp_path, capsys):
    """``waiting_node: ""`` is not an entry point; spawn plain resume."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-4"
    _parked_run(tmp_path, run_id)
    trr._write_review_pause(tmp_path, run_id, {
        "waiting_node": "",
        "awaiting": ["contract_compiler"],
        "inbox_ids": [929],
    })
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "contract_compiler"])
    captured = capsys.readouterr()

    assert rc == 0
    assert len(spawns) == 1
    assert spawns[0][1] == ["--strategy", "resume"]
    assert "no waiting_node" in captured.err


# ── --no-resume hint carries the entry point ─────────────────────────────


def test_no_resume_hint_includes_from_node(monkeypatch, tmp_path, capsys):
    """``--no-resume`` spawns nothing but prints ``--from-node <node>``."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-5"
    _parked_run(tmp_path, run_id)
    trr._write_review_pause(tmp_path, run_id, _full_payload())
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(
        ["approve", run_id, "--node", "contract_compiler", "--no-resume"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert spawns == [], "--no-resume must not spawn"
    assert "--from-node ratification_check" in out
    assert "--strategy resume" in out


# ── the warning is printed only when a resume actually happens ───────────


def test_no_resume_does_not_warn_about_a_missing_entry_point(
        monkeypatch, tmp_path, capsys):
    """``--no-resume`` spawns nothing, so "resuming without an entry point"
    would describe work this command did not do."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-6"
    _parked_run(tmp_path, run_id)
    _sentinel(tmp_path, run_id).write_text("", encoding="utf-8")
    trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(
        ["approve", run_id, "--node", "contract_compiler", "--no-resume"]
    )
    err = capsys.readouterr().err

    assert rc == 0
    assert "no waiting_node" not in err


def test_a_still_pending_row_does_not_warn_about_a_missing_entry_point(
        monkeypatch, tmp_path, capsys):
    """Approving one of two pending rows resumes nothing yet; the run is not
    waiting on the operator's last decision, so there is no warning to give."""
    from mini_ork.cli import approvals_cmd

    run_id = "run-waiting-7"
    trr._setup_parked_run(tmp_path, run_id,
                          node_ids=["contract_compiler", "ratification_check"])
    _sentinel(tmp_path, run_id).write_text("", encoding="utf-8")
    spawns = trr._stub_spawn(monkeypatch, approvals_cmd)

    capsys.readouterr()
    rc = approvals_cmd.main(["approve", run_id, "--node", "contract_compiler"])
    captured = capsys.readouterr()

    assert rc == 0
    assert spawns == [], "a run with rows still pending must not resume"
    assert "no waiting_node" not in captured.err
