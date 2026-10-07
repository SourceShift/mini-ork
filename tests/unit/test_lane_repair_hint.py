"""``lane-repair-hint`` — classify a dead-lane failure, suggest a working lane,
tell the owner what to do.

Covers the four touch-points from ``kickoffs/auto/lane-repair-hint.md``:

  * ``classify_error`` quota wording (MiniMax "Token Plan usage limit" text),
  * the ``llm_dispatch`` failed-call tail capturing the stdout ``result`` 429,
  * ``lane_suggest.suggest`` ranking (code vs analysis aliases, glm exclusion),
  * the new ``retry_hint`` case ``lane`` + ``retry_notify.fix_steps`` lane branch
    and the ``notify`` inbox enqueue.

Hermetic: ``tmp_path`` homes, ``mig.init_db`` for the schema, no LLM, no
network, no live home.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.dispatch import llm_dispatch as ld  # noqa: E402
from mini_ork.recovery import lane_suggest, retry_hint, retry_notify  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.web.db import db_for  # noqa: E402

RUN = "run-lane-repair-20261007142114"


# Real framework-edit lane wiring: ``prior_art_lens`` and ``implementer`` share
# the ``codex_lens`` alias (that is the alias the kickoff wants switched).
WORKFLOW = """\
version: 1
task_class: framework_edit
nodes:
  - {name: planner, type: planner, model_lane: decomposer, prompt_ref: prompts/planner.md}
  - {name: code_impact_lens, type: researcher, model_lane: minimax_lens, prompt_ref: prompts/code-impact-lens.md}
  - {name: prior_art_lens, type: researcher, model_lane: codex_lens, prompt_ref: prompts/prior-art-lens.md}
  - {name: implementer, type: implementer, model_lane: codex_lens, prompt_ref: prompts/implementer.md}
  - {name: reviewer, type: reviewer, model_lane: opus_lens, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher, model_lane: publisher}
edges:
  - {from: planner, to: code_impact_lens, edge_type: depends_on}
  - {from: planner, to: prior_art_lens, edge_type: depends_on}
  - {from: code_impact_lens, to: implementer, edge_type: supplies_context_to}
  - {from: prior_art_lens, to: implementer, edge_type: supplies_context_to}
  - {from: implementer, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""

MINIMAX_TEXT = (
    "API Error: Request rejected (429) · Token Plan usage limit reached: "
    "Upgrade your Token Plan or purchase Credits for more usage. (2056)"
)


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "framework-edit"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW, encoding="utf-8")
    (recipe / "task_class.yaml").write_text("name: framework_edit\ndescription: lane\n")
    (h / "config").mkdir(exist_ok=True)
    (h / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  minimax:\n    model: MiniMax-M3\n    kind: anthropic-compat\n"
        "  deepseek:\n    model: deepseek-chat\n    kind: openai-chat\n"
        "  glm:\n    model: glm-5.3\n    kind: openai-chat\n"
        "  opus:\n    model: claude-opus\n    kind: anthropic-native\n",
        encoding="utf-8",
    )
    return h


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mini-ork env leaks ``MINI_ORK_PROVIDERS`` (a scratch run's
    providers.yaml) into the test process; ``lane_suggest._providers`` honours
    it first, so it would shadow the tmp home's providers.yaml. Clear the
    knobs the lane-resolution code reads so the tmp home wins."""
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    monkeypatch.delenv("MO_CODE_LANES", raising=False)


def _insert_llm_call(home: Path, *, model_id: str, status: str,
                     run_id: str | None = None, feature_name: str = "mini-ork:dispatch",
                     actor: str | None = None, provider: str = "gateway",
                     error_message: str | None = None,
                     error_category: str | None = None,
                     retryable: int | None = None) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, status, "
        "error_message, error_category, retryable, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (provider, model_id, "default", feature_name, actor, status,
         error_message, error_category, retryable, run_id),
    )
    con.commit()
    con.close()


def _seed_health_calls(home: Path) -> None:
    """Healthy-lane history for :func:`lane_suggest.suggest`: minimax quota-failed,
    deepseek 5 ok, glm 9 ok (opus untouched → untested)."""
    _insert_llm_call(home, model_id="minimax", status="failed",
                     error_message=MINIMAX_TEXT, error_category="quota", retryable=0)
    for _ in range(5):
        _insert_llm_call(home, model_id="deepseek", status="success")
    for _ in range(9):
        _insert_llm_call(home, model_id="glm", status="success")


def _seed_run(home: Path, run_id: str = RUN, *, status: str = "failed",
              error_category: str | None = "quota") -> Path:
    """Seed a failed framework-edit run in the live shape: a dead ``minimax``
    lens call, ``prior_art_lens`` failed, ``implementer`` skipped."""
    ts = int(time.time())
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# lane-repair test\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", status, 0.5, ts, ts + 100, ts + 80,
         "framework_edit", str(kickoff), "latest"))
    con.commit()
    con.close()
    # The failed lens call (lane alias codex_lens → minimax).
    _insert_llm_call(
        home, model_id="minimax", status="failed", run_id=run_id,
        feature_name="mini-ork:codex_lens", actor="codex_lens",
        error_message=MINIMAX_TEXT, error_category=error_category, retryable=0,
    )
    # Healthy lanes for the suggest ranking (not run-scoped).
    for _ in range(5):
        _insert_llm_call(home, model_id="deepseek", status="success")
    for _ in range(9):
        _insert_llm_call(home, model_id="glm", status="success")
    # Lifecycle events: prior_art_lens failed, implementer never dispatched.
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-start-pal", run_id, "node_start",
         json.dumps({"node_id": "prior_art_lens", "node_type": "researcher",
                     "model_lane": "codex_lens"}), ts + 10))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-start-impl", run_id, "node_start",
         json.dumps({"node_id": "implementer", "node_type": "implementer",
                     "model_lane": "codex_lens"}), ts + 20))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-end-pal", run_id, "node_end",
         json.dumps({"node_id": "prior_art_lens", "finish_reason": "error"}), ts + 30))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}', encoding="utf-8")
    (run_dir / "run_profile.json").write_text(
        json.dumps({"kickoff_path": str(kickoff), "recipe": "framework-edit"}),
        encoding="utf-8",
    )
    return run_dir


# ── 1. classify_error quota wording ─────────────────────────────────────────


def test_classify_error_minimax_quota_text() -> None:
    assert ld.classify_error(MINIMAX_TEXT) == "quota"


def test_classify_error_rate_limit_stays_capacity() -> None:
    assert ld.classify_error("rate limit 429 capacity exceeded") == "capacity"


def test_classify_error_401_stays_auth() -> None:
    assert ld.classify_error("HTTP 401 unauthorized") == "auth"


# ── 2. failed-call tail captures the stdout result 429 ──────────────────────


def test_failure_path_captures_provider_result(home: Path, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    def stub(model, prompt_text, out_file, timeout_s, max_turns):
        open(out_file, "w", encoding="utf-8").write(json.dumps({
            "type": "result", "is_error": True, "api_error_status": 429,
            "result": MINIMAX_TEXT,
        }))
        return 1

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    rc = ld.llm_dispatch(
        ["--node-type", "researcher", "--prompt-text", "hi",
         "--out", str(tmp_path / "out.txt")],
        root=str(REPO), dispatch_fn=stub,
    )
    assert rc == 1
    con = sqlite3.connect(home / "state.db")
    row = con.execute(
        "SELECT error_message, error_category, retryable FROM llm_calls "
        "WHERE status = 'failed' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    con.close()
    assert row is not None
    error_message, error_category, retryable = row
    assert error_message.startswith("HTTP 429:")
    assert "Token Plan usage limit" in error_message
    assert error_category == "quota"
    assert retryable == 0


# ── 3. lane_suggest ─────────────────────────────────────────────────────────


def test_suggest_code_alias_excludes_glm_and_ranks_by_ok(home: Path) -> None:
    _seed_health_calls(home)
    db = db_for(home)
    out = lane_suggest.suggest(
        home, failed_lane="minimax", alias="codex_lens",
        node_types=["implementer"], db=db,
    )
    lanes = [s["lane"] for s in out]
    assert lanes[0] == "deepseek"
    assert "opus" in lanes
    assert "glm" not in lanes
    assert out[0]["reason"] == "5 successful calls in the last 6h"


def test_suggest_analysis_alias_ranks_glm_first(home: Path) -> None:
    _seed_health_calls(home)
    db = db_for(home)
    out = lane_suggest.suggest(
        home, failed_lane="minimax", alias="minimax_lens",
        node_types=["researcher"], db=db,
    )
    assert out[0]["lane"] == "glm"
    assert out[0]["reason"] == "9 successful calls in the last 6h"


def test_suggest_drops_quota_and_auth_lanes(home: Path) -> None:
    _seed_health_calls(home)
    _insert_llm_call(home, model_id="deepseek", status="failed",
                     error_message="401 unauthorized", error_category="auth")
    db = db_for(home)
    out = lane_suggest.suggest(
        home, failed_lane="minimax", alias="codex_lens",
        node_types=["implementer"], db=db,
    )
    assert "deepseek" not in [s["lane"] for s in out]


# ── 4. retry_hint case lane ─────────────────────────────────────────────────


def test_compute_returns_lane_hint(home: Path) -> None:
    _seed_run(home)
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["retryable"] is True
    assert hint["strategy"] == "resume"
    nc = hint["needs_change"]
    assert nc["kind"] == "lane"
    assert hint["failed_node"] == "prior_art_lens"
    assert nc["lane"] == "minimax"
    assert nc["alias"] == "codex_lens"
    assert "implementer" in nc["nodes"]
    assert nc["error_kind"] == "quota"
    assert nc["code"] is True
    assert "minimax" in nc["summary"] and "quota" in nc["summary"]
    assert hint["command"] == f"mini-ork recover {RUN} --lane codex_lens=deepseek"


def test_compute_legacy_null_category_uses_log_429(home: Path) -> None:
    run_dir = _seed_run(home, error_category=None)
    # Legacy: NULL error_category but the 429 lives in the agent live stream.
    (run_dir / "agent-prior_art_lens.live.jsonl").write_text(
        json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                    "result": MINIMAX_TEXT}) + "\n",
        encoding="utf-8",
    )
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "lane"
    assert hint["needs_change"]["lane"] == "minimax"
    assert hint["needs_change"]["alias"] == "codex_lens"


# ── 5. fix_steps + notify ───────────────────────────────────────────────────


def test_fix_steps_lane_three_lines(home: Path) -> None:
    _seed_run(home)
    hint = retry_hint.load_or_compute(home, RUN, write=False)
    assert hint is not None
    steps = retry_notify.fix_steps(hint, home=home)
    # Item 1: the failed lane line names provider, lane, alias and nodes.
    assert steps[0].startswith("gateway lane 'minimax' (used by codex_lens: ")
    assert "prior_art_lens, implementer" in steps[0]
    assert "Token Plan usage limit" in steps[0]
    # Item 2: the switch line (plus one "Or …" line per other suggestion).
    assert steps[1].startswith("Switch codex_lens to 'deepseek'")
    assert "resume from prior_art_lens" in steps[1]
    assert "--lane codex_lens=deepseek" in steps[1]
    assert any(s.startswith("Or 'opus'") for s in steps)
    # Item 3: nothing changed in the code — the run stopped pre-implementer.
    assert steps[-1] == "Nothing was changed in your code; the run stopped before the implementer."


def test_fix_steps_lane_no_suggestion_names_code_lane(home: Path) -> None:
    run_dir = _seed_run(home)
    (run_dir / "framework-edit.diff").write_text("--- a/x\n+++ b/x\n")
    hint = {
        "run_id": RUN, "from_node": "prior_art_lens", "command": "",
        "needs_change": {
            "kind": "lane", "lane": "minimax", "alias": "codex_lens",
            "provider": "gateway", "detail": "quota", "nodes": ["prior_art_lens"],
            "suggestions": [], "code": True,
        },
    }
    steps = retry_notify.fix_steps(hint, home=home)
    assert any("No other code lane looks healthy" in s for s in steps)
    # framework-edit.diff exists → the "nothing changed" line is suppressed.
    assert not any("Nothing was changed" in s for s in steps)


def test_notify_lane_enqueues_inbox_row(home: Path) -> None:
    _seed_run(home)
    out = retry_notify.notify(home, RUN)
    assert out is not None
    run_dir = home / "runs" / RUN
    assert (run_dir / retry_notify.NOTIFY_FILENAME).is_file()
    con = sqlite3.connect(home / "state.db")
    n = con.execute(
        "SELECT COUNT(*) FROM mo_inbox_gates WHERE gate_id=? AND feature=?",
        (retry_notify.GATE_ID, RUN),
    ).fetchone()[0]
    con.close()
    assert n == 1


# ── 6. _lane_from_attempt_row now reads llm_calls.model_id ──────────────────


def test_lane_from_attempt_row_reads_llm_calls(home: Path) -> None:
    run_dir = _seed_run(home)
    assert retry_notify._lane_from_attempt_row(run_dir) == "minimax"


def test_lane_from_attempt_row_empty_without_failed_call(home: Path) -> None:
    _seed_run(home, status="failed")
    # The seed run has a failed minimax row; prove the empty case with a
    # fresh run dir that has no llm_calls rows.
    empty = home / "runs" / "run-no-calls"
    empty.mkdir(parents=True)
    assert retry_notify._lane_from_attempt_row(empty) == ""
