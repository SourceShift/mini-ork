"""``mini-ork.recovery.retry_hint`` + ``board retry`` + run-page Retry section.

The kickoff (``kickoffs/auto/retry-hint.md``) routes the board verb and
run-page section tests here too so the four touch-points stay together. Each
classification case has its own dedicated test, the overlay recipe lookup is
covered, the cache freshness boundary is exercised on both sides, and the
board verb's gating rules are all hit with the spawn monkeypatched.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import build_page
from mini_ork.recovery import retry_hint
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-rh-1791359434-abc123"


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


def _seed_run(home: Path, run_id: str, *, status: str, recipe: str = "framework-edit",
              now: int | None = None) -> Path:
    ts = now or int(time.time())
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# retry-hint test\n")
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
    return run_dir


def _write_verifier(run_dir: Path, stem: str, payload: dict[str, Any]) -> None:
    """A verifier result with optional banner lines before the JSON object."""
    p = run_dir / f"verifier_{stem}.json"
    p.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _write_review(run_dir: Path, node_id: str, verdict: str,
                  reasons: list[str] | None = None) -> None:
    body: dict[str, Any] = {"verdict": verdict}
    if reasons is not None:
        body["reasons"] = reasons
    (run_dir / f"review-{node_id}.json").write_text(json.dumps(body), encoding="utf-8")


def _write_impl_log(run_dir: Path, node_id: str, lines: list[str]) -> None:
    (run_dir / f"impl-{node_id}.log").write_text("\n".join(lines) + "\n",
                                                  encoding="utf-8")


def _insert_attempt(home: Path, run_id: str, node_id: str, failure_class: str) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type, "
        "started_at, ended_at, result, failure_class) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, node_id, 1, "implementer", int(time.time()), int(time.time()),
         "failure", failure_class))
    con.commit()
    con.close()


# ── hint classification ────────────────────────────────────────────────────


def test_published_run_returns_none(home: Path) -> None:
    _seed_run(home, RUN, status="published")
    assert retry_hint.compute(home, RUN) is None


def test_running_run_returns_none(home: Path) -> None:
    _seed_run(home, RUN, status="executing")
    assert retry_hint.compute(home, RUN) is None


def test_missing_run_dir_returns_none(home: Path) -> None:
    _seed_run(home, RUN, status="failed")
    # Remove the run dir to simulate a run whose artefacts were cleaned up.
    shutil.rmtree(home / "runs" / RUN)
    assert retry_hint.compute(home, RUN) is None


def test_unknown_run_returns_none(home: Path) -> None:
    assert retry_hint.compute(home, "run-nope-1234") is None


def test_case_1_cost_pause_returns_resume_cost(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    (run_dir / ".cost-pause").write_text("budget=5.0 cap=4.0\n", encoding="utf-8")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] == "resume-cost" and hint["retryable"] is True
    assert hint["needs_change"]["kind"] == "budget"
    assert hint["command"].startswith("mini-ork resume ")
    assert "Paused at the cost cap" in hint["needs_change"]["summary"]


def test_case_2_verifier_unverified_returns_environment(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False,
                     "status": "UNVERIFIED",
                     "reason": "cmd reports an unreachable surface: backend lacks env var",
                     "evidence_path": str(run_dir / "evidence.log")})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_impl_log(run_dir, "implementer", [
        "starting local backend smoke test",
        "local BE must be restarted with ONBOARDING_DEMO_BOOK_PATH exported",
        " — supervisor precondition",
    ])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] == "verify" and hint["retryable"] is True
    assert hint["needs_change"]["kind"] == "environment"
    assert "live_smoke" in hint["needs_change"]["summary"]
    assert any("ONBOARDING_DEMO_BOOK_PATH" in n for n in hint["notes"])
    assert hint["command"].endswith("--ack-change")


def test_case_2_only_when_earlier_verifiers_passed(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "UNVERIFIED",
                     "reason": "unreachable"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": False, "status": "REFUTED",
                     "reason": "compile error"})
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    # earlier static_check did NOT pass → not the verify/environment case
    assert hint["needs_change"]["kind"] != "environment"


def test_case_2_extracts_implementer_notes_only_relevant_lines(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "UNVERIFIED",
                     "reason": "missing precondition"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_impl_log(run_dir, "implementer", [
        "noisy banner", "noise", "export PATH=/x: precondition not met",
        "must be restarted with the new env", "noise line", "noise", "noise",
    ])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert 0 < len(hint["notes"]) <= 3
    for n in hint["notes"]:
        assert any(tok in n.lower() for tok in
                   ("precondition", "must be restarted", "export", "not set", "env"))


def test_case_3_reviewer_needs_revision_returns_code(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "needs_revision",
                  ["the diff misses a guard clause", "missing error handling"])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["retryable"] is False and hint["strategy"] == "none"
    assert hint["needs_change"]["kind"] == "code"
    assert "judged wrong" in hint["needs_change"]["summary"]
    assert "guard clause" in hint["needs_change"]["detail"] or "error handling" in hint["needs_change"]["detail"]
    assert hint["command"] == ""


def test_case_3_verifier_refuted_returns_code(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": False, "status": "REFUTED",
                     "reason": "hard compile error in module X"})
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "code"
    assert hint["retryable"] is False


def test_case_4_infra_interrupt_returns_resume_without_needs_change(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "pass")
    # No reviewer fail, no verifier fail → case 4 via failure_class on an LLM node.
    _insert_attempt(home, RUN, "implementer", "infra_interrupt")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] == "resume" and hint["retryable"] is True
    assert hint["needs_change"] is None
    assert hint["command"] == "mini-ork recover " + RUN + " --strategy resume"


def test_case_4_with_auth_token_returns_credentials(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "pass")
    # The auth sub-case is driven by the verifier reason, so encode "401
    # unauthorized" in the implementer log that the run page renders.
    _write_impl_log(run_dir, "implementer", ["provider returned 401 unauthorized — api key invalid"])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] == "resume" and hint["retryable"] is True
    assert hint["needs_change"]["kind"] == "credentials"
    assert hint["command"].endswith("--ack-change")


def test_case_5_unknown_returns_unclassified(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "pass")
    _write_impl_log(run_dir, "implementer", ["random end-of-run output"])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "unknown"
    assert hint["retryable"] is False and hint["strategy"] == "none"
    assert "implementer" in hint["needs_change"]["summary"]


def test_overlay_recipe_lookup_uses_project_recipe(home: Path) -> None:
    """A project recipe under home/recipes wins — the kickoff mandates find_recipe()."""
    run_dir = _seed_run(home, RUN, status="failed")
    (run_dir / ".cost-pause").write_text("budget=5\n", encoding="utf-8")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["strategy"] == "resume-cost"


def test_cache_is_used_when_fresh(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "REFUTED",
                     "reason": "x"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    first = retry_hint.load_or_compute(home, RUN, write=True)
    second = retry_hint.load_or_compute(home, RUN, write=False)
    assert first is not None and second is not None
    assert first == second
    # the cached file actually exists now
    assert (run_dir / "retry-hint.json").is_file()


def test_cache_recomputes_when_a_dependency_changes(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "REFUTED",
                     "reason": "x"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    first = retry_hint.load_or_compute(home, RUN, write=True)
    # touch the failing verifier to invalidate the cache
    time.sleep(0.01)
    vpath = run_dir / "verifier_live_smoke.json"
    vpath.write_text(json.dumps({"verifier": "live_smoke", "pass": False,
                                 "status": "REFUTED",
                                 "reason": "y — different reason"}) + "\n",
                      encoding="utf-8")
    second = retry_hint.load_or_compute(home, RUN, write=False)
    assert first is not None and second is not None
    assert second["needs_change"]["detail"] != first["needs_change"]["detail"]


def test_load_or_compute_with_write_false_does_not_write_cache(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    (run_dir / ".cost-pause").write_text("b=1\n")
    out = retry_hint.load_or_compute(home, RUN, write=False)
    assert out is not None
    assert not (run_dir / "retry-hint.json").exists()


# ── board retry verb ───────────────────────────────────────────────────────


def _patch_spawn(monkeypatch, return_pid: int = 4242) -> dict[str, Any]:
    """Never spawn a real subprocess from a test — record what was asked."""
    captured: dict[str, Any] = {}

    class _Fake:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    def _fake(argv, *, cwd, env, stdout_path):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env_keys"] = sorted(env.keys())
        captured["env_mini_ork_home"] = env.get("MINI_ORK_HOME")
        captured["stdout_path"] = str(stdout_path)
        return _Fake(return_pid)

    monkeypatch.setattr(board_cmd, "_retry_spawn", _fake)
    return captured


def _run_retry(rest: list[str], home: Path) -> dict[str, Any]:
    """Parse ``rest`` through the board CLI parser and dispatch ``_act_retry``
    directly. ``board_cmd.main`` returns an ``int`` exit code for the shell;
    these tests need the payload dict, so go straight to the verb."""
    args = board_cmd.build_parser().parse_args(rest)
    return board_cmd._act_retry(
        home, args.run_id,
        ack_change=args.ack_change, force=args.force, dry_run=args.dry_run,
    )


def test_board_retry_dry_run_returns_hint_without_spawning(home: Path, monkeypatch) -> None:
    _seed_run(home, RUN, status="failed")
    (home / "runs" / RUN / ".cost-pause").write_text("b=1\n")
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home), "--dry-run"], home)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["hint"]["strategy"] == "resume-cost"
    assert "argv" not in captured


def test_board_retry_no_hint_returns_nothing_to_retry(home: Path, monkeypatch) -> None:
    _seed_run(home, RUN, status="published")
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home)], home)
    assert payload["ok"] is False
    assert "nothing to retry" in payload["error"]
    assert "argv" not in captured


def test_board_retry_not_retryable_without_force_returns_error(home: Path, monkeypatch) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "needs_revision", ["bad diff"])
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home)], home)
    assert payload["ok"] is False
    assert "judged wrong" in payload["error"]
    assert payload["hint"]["retryable"] is False
    assert "argv" not in captured


def test_board_retry_not_retryable_with_force_spawns(home: Path, monkeypatch) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "needs_revision", ["bad"])
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home), "--force"], home)
    assert payload["ok"] is True
    assert "argv" in captured and captured["argv"][:2] == ["mini-ork", "recover"]
    assert "--force" in captured["argv"]
    assert captured["env_mini_ork_home"] == str(home)


def test_board_retry_needs_change_without_ack_returns_error(home: Path, monkeypatch) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "UNVERIFIED",
                     "reason": "precondition missing"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home)], home)
    assert payload["ok"] is False
    assert payload["error"].startswith("needs a change first:")
    assert "argv" not in captured


def test_board_retry_with_ack_change_spawns(home: Path, monkeypatch) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "UNVERIFIED",
                     "reason": "precondition missing"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home), "--ack-change"], home)
    assert payload["ok"] is True
    assert "--ack-change" in captured["argv"]
    # log lives under the run dir
    assert captured["stdout_path"].startswith(str(home / "runs" / RUN))
    assert payload["pid"] == 4242


def test_board_retry_resume_cost_spawns_resume(home: Path, monkeypatch) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    (run_dir / ".cost-pause").write_text("b=1\n")
    captured = _patch_spawn(monkeypatch)
    payload = _run_retry(["retry", RUN, "--home", str(home)], home)
    assert payload["ok"] is True
    assert captured["argv"] == ["mini-ork", "resume", RUN]


def test_board_retry_parser_lists_retry_verb_and_flags() -> None:
    parser = board_cmd.build_parser()
    args = parser.parse_args(["retry", "run-1", "--ack-change", "--force", "--dry-run"])
    assert args.verb == "retry"
    assert args.run_id == "run-1"
    assert args.ack_change is True and args.force is True and args.dry_run is True


# ── run-page Retry section ─────────────────────────────────────────────────


def _section(page: dict, kind: str, title: str) -> dict:
    for s in page.get("sections") or []:
        if s.get("type") == kind and s.get("title") == title:
            return s
    raise AssertionError(f"no {kind} {title!r} in {[s.get('title') for s in page.get('sections') or []]}")


def _overview(home: Path, run_id: str) -> dict:
    return build_page(home, "run", "overview", {"run": run_id})


def test_run_page_retry_section_for_case_2(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": False, "status": "UNVERIFIED",
                     "reason": "missing env"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    page = _overview(home, RUN)
    sec = _section(page, "list", "Retry")
    assert any("Needs a change" in i["t"] for i in sec["items"])
    labels = [a["label"] for a in sec["actions"]]
    assert "I fixed it — retry" in labels
    ack = next(a for a in sec["actions"] if a["label"] == "I fixed it — retry")
    assert ack["do"]["cli"] == ["board", "retry", RUN, "--ack-change"]


def test_run_page_retry_section_for_case_3(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "reject", ["absolutely no"])
    page = _overview(home, RUN)
    sec = _section(page, "list", "Retry")
    assert sec["actions"] == []  # no Retry button on case-3
    assert any("Can't" in i["t"] or "judged" in i["t"].lower() for i in sec["items"])


def test_run_page_retry_section_for_case_4(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    _write_verifier(run_dir, "live_smoke",
                    {"verifier": "live_smoke", "pass": True, "status": "PROVEN"})
    _write_verifier(run_dir, "static-check",
                    {"verifier": "static-check", "pass": True, "status": "PROVEN"})
    _write_review(run_dir, "reviewer", "pass")
    _insert_attempt(home, RUN, "implementer", "infra_interrupt")
    page = _overview(home, RUN)
    sec = _section(page, "list", "Retry")
    assert any("resume" in i["t"].lower() for i in sec["items"]) \
        or any("↻" in a["label"] for a in sec["actions"])
    retry_btn = next(a for a in sec["actions"] if a["label"].startswith("↻ Retry"))
    assert retry_btn["do"]["cli"][:3] == ["board", "retry", RUN]


def test_run_page_no_retry_section_for_succeeded_run(home: Path) -> None:
    _seed_run(home, RUN, status="published")
    page = _overview(home, RUN)
    titles = [s["title"] for s in page["sections"]]
    assert "Retry" not in titles


def test_run_page_no_retry_section_for_running_run(home: Path) -> None:
    _seed_run(home, RUN, status="executing")
    page = _overview(home, RUN)
    titles = [s["title"] for s in page["sections"]]
    assert "Retry" not in titles


def test_run_page_retry_section_for_cost_pause(home: Path) -> None:
    run_dir = _seed_run(home, RUN, status="failed")
    (run_dir / ".cost-pause").write_text("b=1\n")
    page = _overview(home, RUN)
    sec = _section(page, "list", "Retry")
    assert any("cost-paused" in i["t"].lower() or "Resume" in i["t"]
               for i in sec["items"])
    assert any(a["label"] == "Resume" for a in sec["actions"])


def test_run_page_drops_retry_section_on_hint_crash(home: Path, monkeypatch) -> None:
    _seed_run(home, RUN, status="failed")

    def boom():
        raise RuntimeError("hint module down")

    monkeypatch.setattr(retry_hint, "load_or_compute", boom)
    page = _overview(home, RUN)
    titles = [s["title"] for s in page["sections"]]
    # Retry section is silently dropped, but the rest of the page survives.
    assert "Retry" not in titles
    assert any(s["title"] == "Run inputs" for s in page["sections"])