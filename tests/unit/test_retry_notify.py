"""``mini_ork.recovery.retry_notify`` — owner inference, fix-steps for every
needs_change kind, dedupe on repeat notify, env-var extraction on a real
fixture, start-guard refusal + escape, board gate→retry handoff, task_state
``needs_you`` rendering for a pending gate.

Hermetic: ``tmp_path`` homes, ``mig.init_db`` for the schema, ``board_cmd``
spawn monkeypatched, no LLM, no network, no live home.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.cli import board_cmd  # noqa: E402
from mini_ork.recovery import retry_hint, retry_notify  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

WORKFLOW = """\
version: 1
task_class: framework_edit
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: live_smoke, type: verifier, verifier_ref: lib/live_smoke.py}
  - {name: static_check, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: live_smoke, edge_type: depends_on}
  - {from: live_smoke, to: static_check, edge_type: depends_on}
  - {from: static_check, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "framework-edit"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: framework_edit\ndescription: rh\n")
    return h


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(retry_notify.MO_RUN_OWNER, raising=False)
    monkeypatch.delenv(retry_notify.MO_IGNORE_PENDING_FIX, raising=False)


def _seed_run(home: Path, run_id: str, *, status: str = "failed",
              recipe: str = "framework-edit",
              now: int | None = None, kickoff_subdir: str = "kickoffs",
              kickoff_name: str = "demo.md") -> Path:
    ts = now or int(time.time())
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / kickoff_subdir / kickoff_name
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# retry-notify test\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, ts, ts + 100, ts + 80, "framework_edit",
         str(kickoff), "latest"))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-end", run_id, "node_end",
         json.dumps({"node_id": "implementer", "finish_reason": "done"}), ts + 40))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    # Minimal run_profile so notify / task_state can read the kickoff path.
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(kickoff),
        "recipe": recipe,
    }))
    return run_dir


def _write_verifier(run_dir: Path, stem: str, payload: dict[str, Any]) -> None:
    (run_dir / f"verifier_{stem}.json").write_text(
        json.dumps(payload) + "\n", encoding="utf-8",
    )


def _write_review(run_dir: Path, node_id: str, verdict: str,
                  reasons: list[str] | None = None) -> None:
    body: dict[str, Any] = {"verdict": verdict}
    if reasons is not None:
        body["reasons"] = reasons
    (run_dir / f"review-{node_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _write_impl_log(run_dir: Path, node_id: str, lines: list[str]) -> None:
    (run_dir / f"impl-{node_id}.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8",
    )


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, capture: dict[str, Any]) -> None:
    def fake_spawn(argv, *, cwd=None, env=None, stdout_path=None):
        capture["argv"] = list(argv)
        capture["cwd"] = cwd
        capture["env"] = dict(env or {})
        capture["stdout_path"] = stdout_path
        pid = 991000 + len(capture)
        capture.setdefault("calls", []).append(pid)
        return type("P", (), {"pid": pid})()

    monkeypatch.setattr(board_cmd, "_retry_spawn", fake_spawn)


# ── 1. owner ─────────────────────────────────────────────────────────────────


def test_owner_persisted_json_wins_over_mo_run_owner(home: Path) -> None:
    """owner.json is the persisted source of truth (kickoff §1).

    ``MO_RUN_OWNER`` is a runtime override that flows into the next
    ``owner.json`` write but does NOT supersede a previously-persisted
    record on read. This pins that precedence so a future refactor can't
    silently flip it back.
    """
    _seed_run(home, "run-rn-1")
    run_dir = home / "runs" / "run-rn-1"
    (run_dir / retry_notify.OWNER_FILENAME).write_text(
        json.dumps({"kind": "user", "id": "from-file@example.com",
                    "label": "user:from-file@example.com"}),
        encoding="utf-8",
    )
    os.environ[retry_notify.MO_RUN_OWNER] = "loop:demo-loop"
    rec = retry_notify.owner(home, "run-rn-1")
    assert rec == {"kind": "user", "id": "from-file@example.com",
                   "label": "user:from-file@example.com"}


def test_owner_mo_run_owner_falls_through_when_owner_json_missing(
    home: Path,
) -> None:
    """With no owner.json, MO_RUN_OWNER is the runtime owner."""
    _seed_run(home, "run-rn-1b")
    os.environ[retry_notify.MO_RUN_OWNER] = "loop:demo-loop"
    rec = retry_notify.owner(home, "run-rn-1b")
    assert rec == {"kind": "loop", "id": "demo-loop",
                   "label": "loop:demo-loop"}


def test_owner_persisted_json_wins_over_inference(home: Path) -> None:
    _seed_run(home, "run-rn-2", kickoff_name="auto.md")
    run_dir = home / "runs" / "run-rn-2"
    (run_dir / retry_notify.OWNER_FILENAME).write_text(
        json.dumps({"kind": "automation", "id": "demo-auto",
                    "label": "automation:demo-auto"}),
        encoding="utf-8",
    )
    rec = retry_notify.owner(home, "run-rn-2")
    assert rec == {"kind": "automation", "id": "demo-auto",
                   "label": "automation:demo-auto"}


def test_owner_infers_run_parent_from_run_events(home: Path) -> None:
    _seed_run(home, "run-rn-3", kickoff_name="child.md")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
        "created_at, parent_run_id) VALUES (?,?,?,?,?,?)",
        ("ev-parent", "run-rn-3", "node_end",
         json.dumps({"node_id": "implementer"}), int(time.time()),
         "run-parent-id"),
    )
    con.commit()
    con.close()
    rec = retry_notify.owner(home, "run-rn-3")
    assert rec == {"kind": "run", "id": "run-parent-id",
                   "label": "run:run-parent-id"}


def test_owner_infers_loop_from_rsi_subdir(home: Path) -> None:
    _seed_run(home, "run-rn-4", kickoff_subdir="rsi/loop-x",
              kickoff_name="kickoff.md")
    rec = retry_notify.owner(home, "run-rn-4")
    assert rec == {"kind": "loop", "id": "loop-x", "label": "loop:loop-x"}


def test_owner_infers_automation_from_automations_subdir(home: Path) -> None:
    """Kickoff path under ``<home>/automations/<id>/`` ⇒ ``automation:<id>``.

    Regression: the previous code returned ``loop:<id>`` here because
    ``_kickoff_path_under_home`` returned the same ``(base, name)`` shape
    for both segments and the owner branch always matched the rsi string
    check (the literal ``"/rsi/" in str(base/'rsi'/'name')`` was always
    true). The fix routes the segment from the kickoff path and branches
    on it.
    """
    _seed_run(home, "run-rn-4a", kickoff_subdir="automations/nightly",
              kickoff_name="kickoff.md")
    rec = retry_notify.owner(home, "run-rn-4a")
    assert rec == {"kind": "automation", "id": "nightly",
                   "label": "automation:nightly"}


def test_owner_infers_automation_from_record(home: Path) -> None:
    _seed_run(home, "run-rn-5", kickoff_name="kickoff.md")
    (home / "automations.json").write_text(json.dumps({
        "automations": [{"id": "auto-a", "last_run_id": "run-rn-5"}],
    }))
    rec = retry_notify.owner(home, "run-rn-5")
    assert rec == {"kind": "automation", "id": "auto-a",
                   "label": "automation:auto-a"}


def test_owner_falls_back_to_user_when_nothing_matches(home: Path) -> None:
    _seed_run(home, "run-rn-6", kickoff_name="kickoff.md")
    rec = retry_notify.owner(home, "run-rn-6")
    # env var / git config user.email not injected here — falls back to
    # $USER via subprocess OR the literal "unknown" sentinel.
    assert rec is not None
    assert rec["kind"] == "user"
    assert rec["id"]


# ── 2. fix_steps ───────────────────────────────────────────────────────────


def test_fix_steps_environment_extracts_env_var_name(home: Path) -> None:
    _seed_run(home, "run-rn-7")
    run_dir = home / "runs" / "run-rn-7"
    # Mirror the run-le-1791359434-64879-1 verifier reason + impl log line.
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED",
        "pass": False,
        "verifier": "live_smoke",
        "reason": "ONBOARDING_DEMO_BOOK_PATH is not set in the backend env",
        "evidence_path": str(run_dir / "impl-log.txt"),
    })
    _write_impl_log(run_dir, "implementer", [
        "[precondition] ONBOARDING_DEMO_BOOK_PATH must be set and the backend "
        "restarted before this verifier can run.",
    ])
    hint = retry_hint.load_or_compute(home, "run-rn-7", write=True)
    assert hint is not None
    steps = retry_notify.fix_steps(hint, home=home)
    flat = "\n".join(steps)
    assert "ONBOARDING_DEMO_BOOK_PATH" in flat
    assert "Restart the backend" in flat
    # The "Confirm: mini-ork board retry …" line is the operator hand-off.
    assert "board retry" in flat


def test_fix_steps_environment_reads_verifier_needs_list(home: Path) -> None:
    _seed_run(home, "run-rn-8")
    run_dir = home / "runs" / "run-rn-8"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED",
        "pass": False,
        "verifier": "live_smoke",
        "reason": "BACKEND_BOOK_PATH is not set; restart required",
        "evidence_path": str(run_dir / "impl-log.txt"),
        "needs": ["POSTGRES_URL", "/tmp/book.pdf"],
    })
    hint = retry_hint.load_or_compute(home, "run-rn-8", write=True)
    assert hint is not None
    steps = retry_notify.fix_steps(hint, home=home)
    flat = "\n".join(steps)
    assert "BACKEND_BOOK_PATH" in flat
    assert "POSTGRES_URL" in flat
    assert "/tmp/book.pdf" in flat


def test_fix_steps_credentials(home: Path) -> None:
    """A provider 401 must classify as ``credentials``.

    The live_smoke verifier reports ``status='PASS'`` (so the run isn't
    pinned to that verifier) and the implementer's log carries the 401 —
    case 4 (provider trouble) catches the auth token from the impl log
    walk and returns the credentials hint.
    """
    _seed_run(home, "run-rn-9")
    run_dir = home / "runs" / "run-rn-9"
    _write_verifier(run_dir, "live_smoke", {
        "status": "PASS",
        "pass": True,
        "verifier": "live_smoke",
        "reason": "ok",
    })
    _write_impl_log(run_dir, "implementer", [
        "provider rejected the call: 401 unauthorized",
    ])
    hint = retry_hint.load_or_compute(home, "run-rn-9", write=True)
    assert hint is not None
    assert hint.get("needs_change", {}).get("kind") == "credentials"
    steps = retry_notify.fix_steps(hint, home=home)
    assert any("credential" in s.lower() for s in steps)
    assert any("board retry" in s for s in steps)


def test_fix_steps_credentials_names_lane_and_secrets_file(home: Path) -> None:
    """The credentials step names the failed_node AND config/secrets.local.sh.

    Regression: previously the step said "Update the credential: …" with
    no lane or store — the operator couldn't tell which key to rotate or
    where. ``retry_hint`` populates ``hint['failed_node']`` from the most
    recent failed ``node_attempts`` row, so the step now reads it back and
    names the node alongside ``config/secrets.local.sh``.
    """
    _seed_run(home, "run-rn-9b")
    run_dir = home / "runs" / "run-rn-9b"
    _write_verifier(run_dir, "live_smoke", {
        "status": "PASS",
        "pass": True,
        "verifier": "live_smoke",
        "reason": "ok",
    })
    _write_impl_log(run_dir, "implementer", [
        "provider rejected the call: 401 unauthorized",
    ])
    # Seed a node_attempts row so retry_hint's case-4 credentials branch
    # populates hint['failed_node'] = "implementer" (the failed node).
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type, "
        "started_at, ended_at, result, failure_class) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("run-rn-9b", "implementer", 1, "implementer", int(time.time()),
         int(time.time()), "failure", "infra_interrupt"),
    )
    con.commit()
    con.close()
    hint = retry_hint.load_or_compute(home, "run-rn-9b", write=True)
    assert hint is not None
    assert hint.get("needs_change", {}).get("kind") == "credentials"
    steps = retry_notify.fix_steps(hint, home=home)
    flat = "\n".join(steps)
    assert "config/secrets.local.sh" in flat
    assert "failed node" in flat.lower()
    assert "implementer" in flat


def test_fix_steps_unknown_returns_last_log_lines(home: Path) -> None:
    """``unknown`` steps read the run dir's most recent impl log, not the
    400-char truncated detail string the old ``_step_unknown`` produced.
    """
    _seed_run(home, "run-rn-unknown")
    run_dir = home / "runs" / "run-rn-unknown"
    _write_impl_log(run_dir, "implementer", [
        "line one: trivial startup",
        "line two: preflight ok",
        "line three: called LLM",
        "line four: LLM returned ok",
        "line five: dispatching",
        "line six: ERROR something exploded",
        "line seven: cleanup",
    ])
    hint = {
        "needs_change": {
            "kind": "unknown",
            "summary": "Failed at ?",
            "detail": "line one\nline two\nline three",
            "evidence": "",
        },
        "run_id": "run-rn-unknown",
    }
    steps = retry_notify.fix_steps(hint, home=home)
    flat = "\n".join(steps)
    assert "last 5 log lines" in flat
    # The most-recent 5 log lines should be in the output.
    assert "dispatching" in flat
    assert "ERROR something exploded" in flat
    assert "cleanup" in flat


def test_fix_steps_environment_reads_spec_needs(home: Path) -> None:
    """Spec-pinned ``spec.needs`` list surfaces even when the top-level
    ``needs`` is absent. Mirrors the run-le-1791359434-64879-1 verifier
    shape that migrated to ``spec.needs`` between revisions.
    """
    _seed_run(home, "run-rn-8b")
    run_dir = home / "runs" / "run-rn-8b"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED",
        "pass": False,
        "verifier": "live_smoke",
        "reason": "ONBOARDING_DEMO_BOOK_PATH is not set; restart required",
        "evidence_path": str(run_dir / "impl-log.txt"),
        "spec": {"needs": ["ONBOARDING_DEMO_BOOK_PATH",
                           "/tmp/book.pdf"]},
    })
    hint = retry_hint.load_or_compute(home, "run-rn-8b", write=True)
    assert hint is not None
    steps = retry_notify.fix_steps(hint, home=home)
    flat = "\n".join(steps)
    assert "ONBOARDING_DEMO_BOOK_PATH" in flat
    assert "/tmp/book.pdf" in flat


def test_fix_steps_budget(home: Path) -> None:
    _seed_run(home, "run-rn-10")
    run_dir = home / "runs" / "run-rn-10"
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")
    hint = retry_hint.load_or_compute(home, "run-rn-10", write=True)
    assert hint is not None
    steps = retry_notify.fix_steps(hint)
    assert any("mini-ork resume" in s for s in steps)


def test_fix_steps_code_surfaces_reviewer_reasons(home: Path) -> None:
    _seed_run(home, "run-rn-11")
    run_dir = home / "runs" / "run-rn-11"
    _write_verifier(run_dir, "static_check", {
        "status": "PASS",
        "verifier": "static_check",
        "reason": "",
    })
    _write_review(run_dir, "reviewer", "needs_revision", [
        "didn't strip the unused arg",
        "missing newline at end of file",
    ])
    hint = retry_hint.load_or_compute(home, "run-rn-11", write=True)
    assert hint is not None
    steps = retry_notify.fix_steps(hint)
    flat = "\n".join(steps)
    assert "revision" in flat.lower()
    assert "didn't strip the unused arg" in flat
    assert "missing newline" in flat


def test_fix_steps_unknown(home: Path) -> None:
    """``unknown`` with no impl log yields the no-log captured message."""
    hint = {
        "needs_change": {
            "kind": "unknown",
            "summary": "Failed at ?",
            "detail": "line one\nline two\nline three",
            "evidence": "",
        },
        "run_id": "run-no-such-run",
    }
    steps = retry_notify.fix_steps(hint)
    assert steps
    # No impl log captured → graceful degradation message.
    assert "no impl log" in steps[0].lower() or "last 5" in steps[0].lower()


# ── 3. notify ──────────────────────────────────────────────────────────────


def test_notify_writes_md_and_enqueues_exactly_one_gate(home: Path,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    _seed_run(home, "run-rn-12")
    run_dir = home / "runs" / "run-rn-12"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED",
        "pass": False,
        "verifier": "live_smoke",
        "reason": "ONBOARDING_DEMO_BOOK_PATH is not set",
        "evidence_path": str(run_dir / "impl-log.txt"),
    })
    _write_impl_log(run_dir, "implementer", [
        "[precondition] ONBOARDING_DEMO_BOOK_PATH must be exported.",
    ])
    out1 = retry_notify.notify(home, "run-rn-12")
    captured = capsys.readouterr()
    assert out1 is not None
    assert (run_dir / retry_notify.OWNER_FILENAME).is_file()
    assert (run_dir / retry_notify.NOTIFY_FILENAME).is_file()
    md = (run_dir / retry_notify.NOTIFY_FILENAME).read_text(encoding="utf-8")
    assert "ONBOARDING_DEMO_BOOK_PATH" in md
    assert retry_notify.NOTIFY_BANNER in captured.out
    assert "needs_change=environment" in captured.out
    assert "retry_inbox_id=" in captured.out

    # Repeat notify → same inbox_id, no new row.
    out2 = retry_notify.notify(home, "run-rn-12")
    assert out2 is not None
    assert out2["inbox_id"] == out1["inbox_id"]
    con = sqlite3.connect(home / "state.db")
    n = con.execute(
        "SELECT COUNT(*) FROM mo_inbox_gates WHERE gate_id=? AND feature=?",
        (retry_notify.GATE_ID, "run-rn-12"),
    ).fetchone()[0]
    con.close()
    assert n == 1


def test_notify_no_needs_change_returns_none(home: Path) -> None:
    _seed_run(home, "run-rn-13")
    # No verifier/review files → hint is `unknown` → still notifies because
    # needs_change is set. To exercise the None branch, fake a published run.
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET status='published' WHERE id='run-rn-13'")
    con.commit()
    con.close()
    out = retry_notify.notify(home, "run-rn-13")
    assert out is None


def test_notify_uses_retry_hint_status_gate(home: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-running run must NOT notify (load_or_compute short-circuits)."""
    _seed_run(home, "run-rn-14")
    run_dir = home / "runs" / "run-rn-14"
    _write_verifier(run_dir, "live_smoke", {
        "status": "REFUTED",
        "verifier": "live_smoke",
        "reason": "X is not set",
    })
    # Re-running = non-terminal status. load_or_compute returns None.
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET status='executing' WHERE id='run-rn-14'")
    con.commit()
    con.close()
    out = retry_notify.notify(home, "run-rn-14")
    assert out is None


# ── 4. start guard ─────────────────────────────────────────────────────────


def test_pending_fix_for_kickoff_matches_realpath(home: Path) -> None:
    _seed_run(home, "run-rn-15", kickoff_name="kickoff.md")
    # Trigger a notify so a row exists for the kickoff realpath.
    run_dir = home / "runs" / "run-rn-15"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    retry_notify.notify(home, "run-rn-15")

    kp = str(run_dir.parent.parent / "kickoffs" / "kickoff.md")
    pending = retry_notify.pending_fix_for_kickoff(home, kp)
    assert pending is not None
    assert pending["gate_id"] == retry_notify.GATE_ID


def test_pending_fix_for_kickoff_no_row_returns_none(tmp_path: Path) -> None:
    h = tmp_path / "home"
    h.mkdir()
    assert retry_notify.pending_fix_for_kickoff(h, "/anywhere") is None


def test_pending_fix_for_kickoff_ignore_env_bypasses(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(home, "run-rn-16", kickoff_name="kickoff.md")
    run_dir = home / "runs" / "run-rn-16"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    retry_notify.notify(home, "run-rn-16")
    kp = str(run_dir.parent.parent / "kickoffs" / "kickoff.md")
    monkeypatch.setenv(retry_notify.MO_IGNORE_PENDING_FIX, "1")
    assert retry_notify.pending_fix_for_kickoff(home, kp) is None


def test_pending_fix_for_run_reads_gate_pointer(home: Path) -> None:
    _seed_run(home, "run-rn-17")
    run_dir = home / "runs" / "run-rn-17"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    retry_notify.notify(home, "run-rn-17")
    pending = retry_notify.pending_fix_for_run(home, run_dir)
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["gate_id"] == retry_notify.GATE_ID


def test_pending_fix_for_run_no_pointer_returns_none(home: Path) -> None:
    _seed_run(home, "run-rn-18")
    run_dir = home / "runs" / "run-rn-18"
    assert retry_notify.pending_fix_for_run(home, run_dir) is None


# ── 5. board gate → retry handoff ──────────────────────────────────────────


def test_board_gate_approve_retry_precondition_dispatches_retry(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(home, "run-rn-19", kickoff_name="kickoff.md")
    run_dir = home / "runs" / "run-rn-19"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    # Seed a node attempts row so the recover strategy can pick a command.
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type, "
        "started_at, ended_at, result, failure_class) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("run-rn-19", "live_smoke", 1, "verifier", int(time.time()),
         int(time.time()), "failure", "infra_interrupt"),
    )
    con.commit()
    con.close()
    payload = retry_notify.notify(home, "run-rn-19")
    assert payload is not None
    iid = payload["inbox_id"]
    assert iid is not None

    capture: dict[str, Any] = {}
    _patch_spawn(monkeypatch, capture)
    rc = board_cmd.main([
        "gate", "approve", str(iid),
        "--note", "user confirmed",
        "--home", str(home),
    ], "")
    assert rc == 0
    argv = capture.get("argv") or []
    assert "--ack-change" in argv
    assert "recover" in argv
    assert any(a.endswith("run-rn-19") for a in argv)

    # The gate is now resolved.
    con = sqlite3.connect(home / "state.db")
    row = con.execute(
        "SELECT status, review_note FROM mo_inbox_gates WHERE inbox_id=?",
        (iid,),
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "approved"
    assert row[1] == "user confirmed"


def test_board_gate_reject_writes_abandoned(home: Path) -> None:
    _seed_run(home, "run-rn-20")
    run_dir = home / "runs" / "run-rn-20"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    payload = retry_notify.notify(home, "run-rn-20")
    assert payload is not None
    iid = payload["inbox_id"]
    rc = board_cmd.main([
        "gate", "reject", str(iid),
        "--note", "abandoned",
        "--home", str(home),
    ], "")
    assert rc == 0
    # retry-gate.json should now carry the abandoned marker (only when the
    # reject path lands; the kickoff says ``{"abandoned": true}``).
    # The hook in board_cmd (kickoff §4) writes it; assert it exists.
    gate_pointer = json.loads(
        (run_dir / retry_notify.GATE_POINTER_FILENAME).read_text(encoding="utf-8"),
    )
    assert gate_pointer.get("inbox_id") == iid
    assert gate_pointer.get("abandoned") is True
    # The reject note lands in mo_inbox_gates.
    con = sqlite3.connect(home / "state.db")
    row = con.execute(
        "SELECT status, review_note FROM mo_inbox_gates WHERE inbox_id=?",
        (iid,),
    ).fetchone()
    con.close()
    assert row[0] == "rejected"
    assert row[1] == "abandoned"


def test_board_retry_ack_change_resolves_gate(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_run(home, "run-rn-21")
    run_dir = home / "runs" / "run-rn-21"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type, "
        "started_at, ended_at, result, failure_class) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("run-rn-21", "live_smoke", 1, "verifier", int(time.time()),
         int(time.time()), "failure", "infra_interrupt"),
    )
    con.commit()
    con.close()
    payload = retry_notify.notify(home, "run-rn-21")
    assert payload is not None
    iid = payload["inbox_id"]

    capture: dict[str, Any] = {}
    _patch_spawn(monkeypatch, capture)
    rc = board_cmd.main([
        "retry", "run-rn-21", "--ack-change", "--home", str(home),
    ], "")
    assert rc == 0
    argv = capture.get("argv") or []
    assert "--ack-change" in argv

    # Gate is resolved with the "retried via board retry" note.
    con = sqlite3.connect(home / "state.db")
    row = con.execute(
        "SELECT status, review_note FROM mo_inbox_gates WHERE inbox_id=?",
        (iid,),
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "approved"
    assert "retried via board retry" in (row[1] or "")


# ── 6. task_state rule 0 ───────────────────────────────────────────────────


def test_task_state_pending_gate_yields_needs_you(home: Path) -> None:
    from mini_ork.acp import task_state

    _seed_run(home, "run-rn-22")
    run_dir = home / "runs" / "run-rn-22"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    retry_notify.notify(home, "run-rn-22")
    snap = {"status": "failed", "events": [], "llm_calls": []}
    ts = task_state.task_state(run_dir, snap)
    assert ts.state == "needs_you"
    assert "needs a fix" in ts.detail


def test_task_state_no_gate_pointer_returns_failed(
    home: Path,
) -> None:
    from mini_ork.acp import task_state

    _seed_run(home, "run-rn-23")
    run_dir = home / "runs" / "run-rn-23"
    snap = {"status": "failed", "events": [], "llm_calls": []}
    ts = task_state.task_state(run_dir, snap)
    assert ts.state == "failed"


def test_task_state_resolved_gate_returns_failed(home: Path) -> None:
    from mini_ork.acp import task_state

    _seed_run(home, "run-rn-24")
    run_dir = home / "runs" / "run-rn-24"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED", "pass": False, "verifier": "live_smoke",
        "reason": "X is not set",
    })
    retry_notify.notify(home, "run-rn-24")
    # Resolve the gate.
    iid = json.loads(
        (run_dir / retry_notify.GATE_POINTER_FILENAME).read_text(encoding="utf-8"),
    )["inbox_id"]
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "UPDATE mo_inbox_gates SET status='approved', resolved_at=?, "
        "review_note='fixed' WHERE inbox_id=?",
        (int(time.time()), iid),
    )
    con.commit()
    con.close()
    snap = {"status": "failed", "events": [], "llm_calls": []}
    ts = task_state.task_state(run_dir, snap)
    assert ts.state == "failed"


# ── read-only proof: ONBOARDING_DEMO_BOOK_PATH fixture ────────────────────


def test_read_only_proof_names_env_var_and_restart(home: Path) -> None:
    """The kickoff's read-only proof: ``fix_steps(load_or_compute(...))`` on
    the real ``run-le-1791359434-64879-1`` shape must name the env var
    AND the backend restart step. This test seeds the same shape; the
    installer's job is to copy it into a fixture (kickoff §"Tests" L86).
    """
    _seed_run(home, "run-le-1791359434-64879-1")
    run_dir = home / "runs" / "run-le-1791359434-64879-1"
    _write_verifier(run_dir, "live_smoke", {
        "status": "UNVERIFIED",
        "pass": False,
        "verifier": "live_smoke",
        "reason": "ONBOARDING_DEMO_BOOK_PATH is not set in the backend env",
        "evidence_path": str(run_dir / "impl-log.txt"),
    })
    _write_impl_log(run_dir, "implementer", [
        "verify read above: ONBOARDING_DEMO_BOOK_PATH must be exported and the "
        "backend must be restarted before this verifier can run.",
    ])
    hint = retry_hint.load_or_compute(home, "run-le-1791359434-64879-1",
                                       write=False)
    assert hint is not None
    steps = retry_notify.fix_steps(hint)
    flat = "\n".join(steps)
    assert "ONBOARDING_DEMO_BOOK_PATH" in flat
    assert "Restart the backend" in flat

# ── review fixes (Opus r2): env names, owner timing, the start guard ───────


def test_env_var_names_are_never_cut_at_the_context_window_edge() -> None:
    text = ("The variable ONBOARDING_DEMO_BOOK_PATH_FOR_BACKEND, read by the onboarding "
            "smoke, is missing")
    assert retry_notify._env_var_names(text) == ["ONBOARDING_DEMO_BOOK_PATH_FOR_BACKEND"]


def test_the_real_w5_91_error_names_the_variable() -> None:
    err = "ONBOARDING_DEMO_BOOK_PATH is not set — refusing to email a guessed demo-book link"
    assert retry_notify._env_var_names(err) == ["ONBOARDING_DEMO_BOOK_PATH"]


def test_owner_is_written_at_start_only_when_the_launcher_names_one(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert retry_notify.record_owner_at_start(home, "run-start-a") is None
    assert not (home / "runs" / "run-start-a" / retry_notify.OWNER_FILENAME).exists()
    monkeypatch.setenv(retry_notify.MO_RUN_OWNER, "thread:sess-42")
    rec = retry_notify.record_owner_at_start(home, "run-start-b")
    assert rec is not None and (rec["kind"], rec["id"]) == ("thread", "sess-42")
    saved = json.loads((home / "runs" / "run-start-b" / retry_notify.OWNER_FILENAME).read_text())
    assert (saved["kind"], saved["id"]) == ("thread", "sess-42")


def test_a_loop_kickoff_is_inferred_as_the_owner_when_none_was_named(home: Path) -> None:
    _seed_run(home, "run-loop-1", kickoff_subdir="rsi/acq-wave5-rsi", kickoff_name="kickoff.md")
    rec = retry_notify.owner(home, "run-loop-1")
    assert rec is not None and (rec["kind"], rec["id"]) == ("loop", "acq-wave5-rsi")


def _cli_home(tmp_path: Path) -> Path:
    import subprocess

    home = tmp_path / "cli-home"
    home.mkdir()
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(home / "state.db")},
                   capture_output=True, text=True, check=True)
    return home


def _cli_run(tmp_path: Path, home: Path, kickoff: Path, **extra: str):
    import subprocess

    env = {**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(home / "state.db"),
           "MINI_ORK_ROOT": str(REPO), "MINI_ORK_RUN_ID": "guard-probe",
           "MINI_ORK_PROFILE_GATE": "0", "MINI_ORK_NONINTERACTIVE": "1", **extra}
    env.pop(retry_notify.MO_IGNORE_PENDING_FIX, None)
    env.update(extra)
    return subprocess.run([str(REPO / "bin" / "mini-ork"), "run", "--json", "code-fix", str(kickoff)],
                          capture_output=True, text=True, env=env, timeout=180)


def test_mini_ork_run_refuses_a_kickoff_with_a_pending_fix(tmp_path: Path) -> None:
    from mini_ork.cli import main as cli_main
    from mini_ork.gates import oversight_inbox

    home = _cli_home(tmp_path)
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# fix\n\n## Files in scope\n\n- a.py\n")
    oversight_inbox.enqueue(
        retry_notify.GATE_ID, "run-earlier", "retry",
        {"hint": {"needs_change": {"kind": "environment", "summary": "set FOO_BAR"}}},
        blocks_dispatch_for=os.path.realpath(kickoff), db_path=str(home / "state.db"))

    blocked = _cli_run(tmp_path, home, kickoff, MINI_ORK_DRY_RUN="1")
    assert blocked.returncode == cli_main.RC_BLOCKED, blocked.stderr[-400:]
    assert "run-earlier" in blocked.stderr and "set FOO_BAR" in blocked.stderr

    bypass = _cli_run(tmp_path, home, kickoff, MINI_ORK_DRY_RUN="1", MO_IGNORE_PENDING_FIX="1")
    assert "needs a change before this run can continue" not in bypass.stderr
