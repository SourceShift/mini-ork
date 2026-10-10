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


def _seed_live_shape(home: Path, run_id: str = RUN, *,
                     with_node_failure: bool = True) -> Path:
    """The r2 live shape (run ``learn-memory-tab-r2-…``): a workflow node
    (``prior_art_lens`` on ``codex_lens``) dies on a 429 whose wording lives
    **only** in ``agent-prior_art_lens.live.jsonl``; a *later* reflect-time
    ``gradient-extract`` call times out on ``minimax``. The hint must pick the
    run's own node, not the newest row (the r1 bug)."""
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
        (run_id, "framework-edit", "failed", 0.5, ts, ts + 100, ts + 80,
         "framework_edit", str(kickoff), "latest"))
    # ``code_impact_lens`` started on ``minimax_lens`` but did not fail.
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-start-cil", run_id, "node_start",
         json.dumps({"node_id": "code_impact_lens", "node_type": "researcher",
                     "model_lane": "minimax_lens"}), ts + 5))
    if with_node_failure:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-start-pal", run_id, "node_start",
             json.dumps({"node_id": "prior_art_lens", "node_type": "researcher",
                         "model_lane": "codex_lens"}), ts + 10))
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-end-pal", run_id, "node_end",
             json.dumps({"node_id": "prior_art_lens",
                         "finish_reason": "error"}), ts + 30))
    con.commit()
    con.close()
    if with_node_failure:
        # NULL category; the row's error_message is unrelated stderr chatter, and
        # the 429 wording lives only in the agent live stream.
        _insert_llm_call(
            home, model_id="minimax", status="failed", run_id=run_id,
            feature_name="mini-ork:codex_lens", actor="codex_lens",
            error_message="stderr: npm warn deprecated left-pad@1.0.0",
            error_category=None, retryable=0,
        )
        (run_dir / "agent-prior_art_lens.live.jsonl").write_text(
            # The real capture shape: a ``{"seq","stream","t","line"}`` wrapper
            # whose ``line`` string holds the provider record with the result.
            json.dumps({"seq": 0, "stream": "stdout", "t": 203.956,
                        "line": json.dumps({
                            "type": "result", "is_error": True,
                            "api_error_status": 429, "result": MINIMAX_TEXT})}) + "\n",
            encoding="utf-8",
        )
    # Healthy lanes for the suggest ranking (not run-scoped).
    for _ in range(5):
        _insert_llm_call(home, model_id="deepseek", status="success")
    for _ in range(9):
        _insert_llm_call(home, model_id="glm", status="success")
    # The LATER reflect-time call — newest row (``id DESC``), NULL category.
    _insert_llm_call(
        home, model_id="minimax", status="failed", run_id=run_id,
        feature_name="mini-ork:gradient-extract", actor="gradient-extract",
        error_message="timeout after 120.0s", error_category=None, retryable=0,
    )
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


# ── 5. run-1791619207-22518: codex turn.failed quota, no 429, category unknown ──

CODEX_QUOTA_TEXT = (
    "You've hit your usage limit. Upgrade to Plus to continue using Codex "
    "(https://chatgpt.com/explore/plus), or try again at Nov 8th, 2026 10:19 AM."
)


def test_record_mark_codex_terminal_event_shapes() -> None:
    """The codex app-server carries the provider's words in ``message`` /
    ``error.message`` on terminal events — never in ``result``, and with no
    429. Those shapes must mark; a non-terminal record quoting the wording
    (a tool_result) must not."""
    wrapper = json.loads(json.dumps({"seq": 10, "stream": "stderr", "t": 6.076,
                                     "line": json.dumps(
                                         {"type": "turn.failed",
                                          "error": {"message": CODEX_QUOTA_TEXT}})}))
    marked, detail = retry_hint._record_mark_and_result(wrapper)
    assert marked
    assert "usage limit" in detail
    marked, detail = retry_hint._record_mark_and_result(
        {"type": "error", "message": CODEX_QUOTA_TEXT})
    assert marked
    assert "usage limit" in detail
    # The silent-death result record (empty result, not an error) never marks…
    marked, _ = retry_hint._record_mark_and_result(
        {"type": "result", "is_error": False, "api_error_status": None,
         "result": "", "subtype": "success"})
    assert not marked
    # …and neither does a transcript that merely quotes the wording.
    marked, _ = retry_hint._record_mark_and_result(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "kickoff says: " + CODEX_QUOTA_TEXT}]}})
    assert not marked


def test_compute_unknown_category_plus_codex_stream_lanes(home: Path) -> None:
    """The 2026-10-10 session-task-judge failure: the judge's minimax attempt
    died silently (success record, empty result), the codex fallback died on
    the account-plan limit (turn.failed, no 429), and the terminal row is the
    sonnet preflight with error_category 'unknown'. The hint must still name
    the node, the alias, and the quota cause — not case-5 'not classified'."""
    ts = int(time.time())
    run_id = "run-1791619207-22518"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "judge.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# judge\n", encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "framework-edit", "failed", 0.57, ts, ts + 200, ts + 192,
         "framework_edit", str(kickoff), "latest"))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-start", run_id, "node_start",
         json.dumps({"node_id": "minimax_judge", "node_type": "researcher",
                     "model_lane": "minimax_lens"}), ts + 5))
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{run_id}-end", run_id, "node_end",
         json.dumps({"node_id": "minimax_judge", "finish_reason": "error"}), ts + 192))
    con.commit()
    con.close()
    _insert_llm_call(
        home, model_id="sonnet", provider="anthropic", status="failed",
        run_id=run_id, feature_name="mini-ork:minimax_lens", actor="minimax_lens",
        error_message="lane preflight failed: unknown lane: 'sonnet'",
        error_category="unknown", retryable=0,
    )
    (run_dir / "agent-minimax_judge.live.jsonl").write_text(
        # 1) the minimax attempt's silent death: success record, empty result;
        # 2) the codex fallback's account-plan limit on turn.failed, no 429.
        json.dumps({"seq": 0, "stream": "stdout", "t": 184.8, "line": json.dumps(
            {"type": "result", "is_error": False, "api_error_status": None,
             "result": "", "num_turns": 35, "subtype": "success"})}) + "\n"
        + json.dumps({"seq": 10, "stream": "stderr", "t": 6.076, "line": json.dumps(
            {"type": "turn.failed", "error": {"message": CODEX_QUOTA_TEXT}})}) + "\n",
        encoding="utf-8",
    )
    for _ in range(5):
        _insert_llm_call(home, model_id="deepseek", status="success")
    (run_dir / "run_profile.json").write_text(
        json.dumps({"kickoff_path": str(kickoff), "recipe": "framework-edit"}),
        encoding="utf-8",
    )
    hint = retry_hint.compute(home, run_id)
    assert hint is not None
    nc = hint["needs_change"]
    assert nc["kind"] == "lane"
    assert hint["failed_node"] == "minimax_judge"
    assert nc["alias"] == "minimax_lens"
    assert nc["error_kind"] == "quota"
    assert "usage limit" in nc["detail"]
    assert hint["command"] == f"mini-ork recover {run_id} --lane minimax_lens=deepseek"


def test_lane_hint_follows_run_node_not_latest_reflect_call(home: Path) -> None:
    """The r1 live bug: the newest failed row is a reflect-time call, so the
    hint pointed at ``gradient-extract`` (a non-node alias recover rejects).
    The hint must instead name the run's own failed node and its 429."""
    _seed_live_shape(home)
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "prior_art_lens"
    assert hint["from_node"] == "prior_art_lens"
    assert hint["strategy"] == "resume"
    nc = hint["needs_change"]
    assert nc["kind"] == "lane"
    assert nc["lane"] == "minimax"
    assert nc["alias"] == "codex_lens"
    # The detail is the stream's 429, never the newer row's timeout chatter or
    # the raw capture envelope.
    assert "Token Plan usage limit" in nc["detail"]
    assert nc["detail"].startswith("API Error")
    assert "timeout after 120.0s" not in nc["detail"]
    assert {"prior_art_lens", "implementer"} <= set(nc["nodes"])
    assert nc["code"] is True
    assert all(s["lane"] != "glm" for s in nc["suggestions"])
    assert nc["suggestions"][0]["lane"] == "deepseek"
    assert hint["command"] == (
        f"mini-ork recover {RUN} --lane codex_lens={nc['suggestions'][0]['lane']}")


def _seed_healthy_node_quoting_429(home: Path, run_dir: Path, *,
                                   finish_reason: str | None) -> None:
    """A healthy ``code_impact_lens`` whose transcript merely *quotes* the 429
    (a ``tool_result`` reading a kickoff/log that contains it) and then reports
    success. Its file name sorts before ``prior_art_lens``, so a first-sorted
    file / raw-text heuristic picks it — the second live bug. ``finish_reason``
    stamps its ``node_end`` (``"done"`` = explicit success, ``None`` = the end
    event is absent, which counts as failed)."""
    if finish_reason is not None:
        con = sqlite3.connect(home / "state.db")
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"ev-{RUN}-end-cil", RUN, "node_end",
             json.dumps({"node_id": "code_impact_lens",
                         "finish_reason": finish_reason}), int(time.time()) + 20))
        con.commit()
        con.close()
    quoted = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "kickoff says: " + MINIMAX_TEXT}]}})
    (run_dir / "agent-code_impact_lens.live.jsonl").write_text(
        json.dumps({"seq": 0, "stream": "stdout", "t": 1.0, "line": quoted}) + "\n"
        + json.dumps({"seq": 1, "stream": "stdout", "t": 2.0, "line": json.dumps(
            {"type": "result", "is_error": False,
             "result": "lens report written"})}) + "\n",
        encoding="utf-8",
    )


def test_lane_hint_ignores_healthy_node_quoting_the_429(home: Path) -> None:
    """``code_impact_lens`` finished fine (``finish_reason: done``) but its live
    stream quotes the 429 in a ``tool_result``. The hint must still name the
    run's real failed node ``prior_art_lens`` / ``codex_lens``, and the detail
    must be the marker-carrying record's result — never the quoting node's
    benign result text nor the raw capture envelope."""
    run_dir = _seed_live_shape(home)
    _seed_healthy_node_quoting_429(home, run_dir, finish_reason="done")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "prior_art_lens"
    assert hint["from_node"] == "prior_art_lens"
    nc = hint["needs_change"]
    assert nc["kind"] == "lane"
    assert nc["alias"] == "codex_lens"
    assert nc["lane"] == "minimax"
    assert nc["detail"].startswith("API Error")
    assert "Token Plan usage limit" in nc["detail"]
    assert "lens report written" not in nc["detail"]


def test_lane_hint_uses_record_mark_not_raw_429_text(home: Path) -> None:
    """Same quote, but ``code_impact_lens`` has *no* ``node_end`` (which counts
    as failed). Only the record-level mark can exonerate it: its stream carries
    no provider record with ``is_error``/``api_error_status == 429``, so the 429
    is a quote, not its own failure. The hint must name ``prior_art_lens``."""
    run_dir = _seed_live_shape(home)
    _seed_healthy_node_quoting_429(home, run_dir, finish_reason=None)
    failures = retry_hint._legacy_lane_failures(run_dir)
    assert len(failures) == 1
    assert failures[0][0] == "prior_art_lens"
    # The detail is the marker-carrying record's ``result``, not the quoting
    # node's benign text nor the raw capture envelope.
    assert failures[0][1].startswith("API Error")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "prior_art_lens"
    assert hint["needs_change"]["alias"] == "codex_lens"


def test_lane_case_ignores_reflect_only_failure(home: Path) -> None:
    """With ONLY the reflect-time ``gradient-extract`` failure (no workflow node
    failed), the lane case declines and the existing cases decide."""
    run_dir = _seed_live_shape(home, with_node_failure=False)
    assert retry_hint._case_lane_unavailable(
        home, RUN, run_dir, "framework-edit") is None
    hint = retry_hint.compute(home, RUN)
    if hint is not None:
        assert hint["needs_change"]["kind"] != "lane"


@pytest.mark.parametrize("category,record", [
    ("capacity", {"type": "result", "is_error": True, "api_error_status": 529,
                  "result": "API Error: 529 Overloaded. Please retry."}),
    ("network", {"type": "result", "is_error": True,
                 "result": "API Error: Connection error."}),
])
def test_lane_case_declines_transient_not_dead_lane(
        home: Path, category: str, record: dict) -> None:
    """The r2 regression: a transient provider failure — a 529 overload or a
    connection error — must NOT be reported as a dead lane. Both carry
    ``is_error`` (the 529 a non-429 ``api_error_status``), so only the quota/auth
    *wording* can tell them from a wall. The row is also categorised
    (``capacity``/``network``), which keeps it out of the legacy NULL-category
    branch — a categorised row is never a dead-lane row."""
    run_dir = _seed_live_shape(home)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "UPDATE llm_calls SET error_category = ? "
        "WHERE run_id = ? AND actor = ?",
        (category, RUN, "codex_lens"),
    )
    con.commit()
    con.close()
    (run_dir / "agent-prior_art_lens.live.jsonl").write_text(
        json.dumps({"seq": 0, "stream": "stdout", "t": 1.0,
                    "line": json.dumps(record)}) + "\n",
        encoding="utf-8",
    )
    assert retry_hint._case_lane_unavailable(
        home, RUN, run_dir, "framework-edit") is None
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] != "lane"


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


def _set_end(home: Path, node_id: str, finish_reason: str) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (f"ev-{RUN}-end-{node_id}-{finish_reason}", RUN, "node_end",
         json.dumps({"node_id": node_id, "finish_reason": finish_reason}),
         int(time.time()) + 60))
    con.commit()
    con.close()


def test_quota_row_never_names_a_node_that_finished(home: Path) -> None:
    """Opus r2 review: a quota row on an alias whose nodes all ended ``done``
    (the lane recovered — retry or fallback) must not produce a lane hint
    naming a finished node."""
    run_dir = _seed_run(home)              # quota row on codex_lens
    _set_end(home, "prior_art_lens", "done")   # later end wins: recovered
    _set_end(home, "implementer", "done")
    assert retry_hint._case_lane_unavailable(
        home, RUN, run_dir, "framework-edit") is None


def test_quota_row_names_the_node_that_did_not_finish(home: Path) -> None:
    """The lens recovered but the implementer (same alias) never ended: the
    run died mid-node there, so that is the failed node."""
    run_dir = _seed_run(home)
    _set_end(home, "prior_art_lens", "done")
    hint = retry_hint._case_lane_unavailable(home, RUN, run_dir, "framework-edit")
    assert hint is not None
    assert hint["failed_node"] == "implementer"


def test_suggest_code_lanes_follow_policy_order_over_success_count(home: Path) -> None:
    """For code, MO_CODE_LANES order (opus, the reviewer family, last) outranks
    raw success counts among healthy lanes — live 2026-10-07 opus had the most
    successes and was suggested for the implementer first."""
    for _ in range(3):
        _insert_llm_call(home, model_id="deepseek", status="success")
    for _ in range(30):
        _insert_llm_call(home, model_id="opus", status="success")
    out = lane_suggest.suggest(
        home, failed_lane="minimax", alias="codex_lens",
        node_types=["implementer"], db=db_for(home),
    )
    lanes = [s["lane"] for s in out]
    assert "deepseek" in lanes and "opus" in lanes
    assert lanes.index("deepseek") < lanes.index("opus")
    assert "glm" not in lanes
