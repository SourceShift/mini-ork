"""Typed failure classes + the one action table (kickoff B1).

Hermetic: no provider credentials, no network. The walk tests stub
``providers.dispatch_model`` per lane; the retry/ledger tests drive the real
``llm_dispatch`` loop with a 5-arg-plus-observer fake and a fresh migrated DB.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from mini_ork.dispatch import failure_class, providers
from mini_ork.dispatch.models import DispatchRequest, DispatchResult

REPO = Path(__file__).resolve().parents[2]

QUOTA_TEXT = ("HTTP 429: API Error: Request rejected (429) · Token Plan usage "
              "limit reached: Upgrade your Token Plan …")
RATE_LIMIT_TEXT = ("[openai_chat] HTTP 429 from https://openrouter.ai/api/v1: … "
                   "temporarily rate-limited upstream")
CODEX_HEAD = "[cl_codex] codex exec failed with rc=1 — see stderr for cause"
MODEL_NOT_FOUND = ('"status":400,…"message":"The \'gpt-5.5\' model is not supported '
                   'when using Codex with a ChatGPT account.","code":"model_not_found"')
SPAWN_FAIL = "spawn failed: [Errno 2] No such file or directory: '/x'"
# core.spawn_local names a missing cwd; a missing binary keeps the bare errno.
SPAWN_NO_CWD = ("spawn failed: cwd does not exist: /x "
                "([Errno 2] No such file or directory: '/x')")
SPAWN_NO_BINARY = "spawn failed: [Errno 2] No such file or directory: 'claude'"
MISSING_KEY = "[cl_minimax] $MINIMAX_API_KEY is empty"


# ── 1. classification table ────────────────────────────────────────────────

@pytest.mark.parametrize("text, rc, expected", [
    ("timeout after 120.0s (spawned→kill 120s, kill→reaped 0s)", 124,
     ("timeout", "network")),
    (SPAWN_FAIL, 127, ("environment", "config")),
    (SPAWN_NO_CWD, 127, ("environment", "config")),
    ("… likely cwd-confusion (a target-repo lane landing in mini-ork) …", 1,
     ("environment", "config")),
    ("dispatch result failed shape check (looks_like_json)", 1,
     ("shape", "request")),
    (MODEL_NOT_FOUND, 4, ("model_access", "config")),
    (QUOTA_TEXT, 1, ("quota", "quota")),
    (RATE_LIMIT_TEXT, 4, ("rate_limit", "capacity")),
    ("lane preflight failed: kimi: API key rejected (HTTP 401) …", 2,
     ("credential", "auth")),
    ("HTTP 500 Internal Server Error", 1, ("transient", "provider")),
    ("400 Bad Request: invalid request", 1, ("caller_fault", "request")),
    ("something odd", 1, ("unknown", "unknown")),
    ("empty output", 0, ("transient", "provider")),
])
def test_classification_table(text, rc, expected):
    assert failure_class.classify(text, rc) == expected


def test_classify_checks_the_full_text():
    text = CODEX_HEAD + " " + "unclassifiable codex chatter. " * 15 + QUOTA_TEXT
    assert "Token Plan" not in text[:200]
    assert failure_class.classify(text, 1) == ("quota", "quota")
    assert failure_class.classify(text + " then TIMEOUT AFTER 12.0s", 1) == (
        "timeout", "network")


def test_model_access_phrase_is_line_bounded():
    # A "model" on one line and a missing *file* on another is not a model
    # access rejection (kickoff defect 2).
    assert failure_class.classify(
        "model: glm\nspawn helper: file does not exist", 1)[0] != "model_access"
    assert failure_class.classify(
        "using model registry from config\n"
        "trace: /var/db/models.json does not exist", 1)[0] != "model_access"


def test_single_line_model_does_not_exist_is_model_access():
    assert failure_class.classify(
        "The model `gpt-x` does not exist", 1)[0] == "model_access"
    assert failure_class.classify(
        "deployment glm-prod does not exist", 1)[0] == "model_access"


def test_db_category_always_within_the_check_constraint():
    allowed = {"auth", "quota", "capacity", "request", "safety", "network",
               "stream", "provider", "config", "unknown"}
    for text, rc in [("timeout after 1s", 124), (SPAWN_FAIL, 127),
                     (MODEL_NOT_FOUND, 4), (QUOTA_TEXT, 1), ("odd", 1)]:
        cls, category = failure_class.classify(text, rc)
        assert category in allowed
        assert cls in failure_class.CLASSES


# ── 2. wait_hint ───────────────────────────────────────────────────────────

def test_wait_hint_variants():
    assert failure_class.wait_hint("Retry-After: 30") == 30
    assert failure_class.wait_hint("try again in 2 minutes") == 120
    assert failure_class.wait_hint("try again at 10:50 AM",
                                   now=datetime(2026, 3, 14, 10, 40)) == 600
    assert failure_class.wait_hint("no hint here") is None
    assert failure_class.wait_hint("") is None


# ── 3. action ──────────────────────────────────────────────────────────────

def test_action_table():
    # Only a run-wide environment fault stops the walk; a fault of one lane
    # (its binary, its key) walks on to a lane that may work.
    assert failure_class.action("environment", SPAWN_NO_CWD).stop_walk is True
    assert failure_class.action(
        "environment", "… likely cwd-confusion …").stop_walk is True
    for lane_local in (SPAWN_NO_BINARY, MISSING_KEY, ""):
        act = failure_class.action("environment", lane_local)
        assert act.stop_walk is False and act.next_lane is True, lane_local
    assert failure_class.action("caller_fault").next_lane is True
    assert failure_class.action(
        "caller_fault", "", {"MO_FALLBACK_STOP_ON_CALLER_FAULT": "1"}).stop_walk
    assert failure_class.action("rate_limit", "Retry-After: 30").wait_hint_s == 30
    assert failure_class.action("timeout") == failure_class.Action(
        False, True, False, None)


def test_transient_action_carries_the_wait_hint():
    # Kickoff defect 4: a transient failure must not drop the provider's hint.
    assert failure_class.action("transient", "Retry-After: 7").wait_hint_s == 7
    assert failure_class.action("transient", "").wait_hint_s is None
    assert failure_class.action("transient", "Retry-After: 9") == failure_class.Action(
        True, True, False, 9.0)


# ── 4/5. the fallback walk ─────────────────────────────────────────────────

def _fake_walk(monkeypatch, lane_specs):
    calls = []

    def fake_dispatch_model(req, root=None):
        calls.append(req.model)
        spec = lane_specs[req.model]
        return DispatchResult(ok=spec["ok"], rc=spec["rc"], text=spec.get("text", ""),
                              error=spec.get("error", ""), model=req.model)

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)
    return calls


def test_walk_stops_on_spawn_failure(monkeypatch, tmp_path, capsys):
    calls = _fake_walk(monkeypatch, {
        "deadlane": {"ok": False, "rc": 127, "error": SPAWN_NO_CWD},
        "goodlane": {"ok": True, "rc": 0, "text": "served by lane two"},
    })
    result = providers.dispatch_with_fallback(
        DispatchRequest(model="deadlane", prompt="p"), ["deadlane", "goodlane"],
        str(tmp_path))
    assert calls == ["deadlane"]
    assert result.ok is False and result.rc == 127
    err = capsys.readouterr().err
    assert "no other lane can help" in err and "environment" in err


@pytest.mark.parametrize("error", [SPAWN_NO_BINARY, MISSING_KEY])
def test_walk_continues_on_a_lane_local_environment_fault(monkeypatch, tmp_path, error):
    """A lane whose CLI is not installed, or whose key is unset, is one lane's
    fault: the laptop with only a glm key must still reach the glm lane."""
    calls = _fake_walk(monkeypatch, {
        "lanea": {"ok": False, "rc": 127, "error": error},
        "laneb": {"ok": True, "rc": 0, "text": "served by lane two"},
    })
    result = providers.dispatch_with_fallback(
        DispatchRequest(model="lanea", prompt="p"), ["lanea", "laneb"], str(tmp_path))
    assert calls == ["lanea", "laneb"]
    assert result.ok is True


def test_spawn_local_names_a_missing_cwd_but_not_a_missing_binary(tmp_path):
    from mini_ork.dispatch.core import spawn_local

    gone = str(tmp_path / "deleted-worktree")
    rc, _out, err = spawn_local(["true"], stdin="", timeout=10.0, env={}, cwd=gone)
    assert rc == 127 and f"cwd does not exist: {gone}" in err
    assert failure_class.action(*_cls_and_text(err, rc)).stop_walk is True

    rc, _out, err = spawn_local(["no-such-cli-b1"], stdin="", timeout=10.0, env={},
                                cwd=str(tmp_path))
    assert rc == 127 and "cwd does not exist" not in err
    assert failure_class.action(*_cls_and_text(err, rc)).stop_walk is False


def _cls_and_text(err, rc):
    return failure_class.classify(err, rc)[0], err


def test_walk_continues_on_model_not_found(monkeypatch, tmp_path):
    calls = _fake_walk(monkeypatch, {
        "lanea": {"ok": False, "rc": 4, "error": MODEL_NOT_FOUND},
        "laneb": {"ok": True, "rc": 0, "text": "served by lane two"},
    })
    result = providers.dispatch_with_fallback(
        DispatchRequest(model="lanea", prompt="p"), ["lanea", "laneb"], str(tmp_path))
    assert calls == ["lanea", "laneb"]
    assert result.ok is True


# ── integration: the real llm_dispatch loop / ledger ───────────────────────

@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    from mini_ork.stores.migrate import init_db

    db = str(tmp_path / "state.db")
    rc, _out, err = init_db(db=db, root=str(REPO))
    assert rc == 0, err
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for var in ("MO_TARGET_CWD", "MO_FUSE_TRANSIENT", "MO_FUSE_ENABLED",
                "MO_SHAPE_CHECK", "MO_NODE_ID", "MO_PLACEMENT",
                "MO_DAILY_BUDGET_USD", "MINI_ORK_RUN_ID", "MO_LANE_ACTOR",
                "MO_RESUME_SESSION_ID", "MO_FALLBACK_STOP_ON_CALLER_FAULT",
                "MINI_ORK_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MO_DISPATCH_RETRY_BASE_S", "0")
    monkeypatch.setenv("MO_DISPATCH_RETRY_MAX_SLEEP_S", "45")
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "3")
    return db


def _make_fake(script):
    calls = []

    def fake(model, prompt_text, out_file, timeout_s, max_turns,
             on_attempt=None, accept=None):
        calls.append(str(model))
        step = script[min(len(calls) - 1, len(script) - 1)]
        rc, error, out = step
        Path(out_file).write_text(out)
        if error:
            Path(out_file + ".err.log").write_text(error)
        if rc != 0 and on_attempt is not None:
            lane = str(model).split(",")[0].strip()
            on_attempt(lane, DispatchResult(ok=False, rc=rc, text="", error=error,
                                            model=lane))
        return rc

    return fake, calls


def _make_fake_stdout(stdout, rc=1):
    """A failing fake whose per-attempt observer carries model *stdout*.

    The stdout tail is the only input mechanism 1 can be red on, and the
    historic ``_make_fake`` always hands ``text=""`` to ``on_attempt``.
    """
    calls = []

    def fake(model, prompt_text, out_file, timeout_s, max_turns,
             on_attempt=None, accept=None):
        calls.append(str(model))
        Path(out_file).write_text("")
        if on_attempt is not None:
            lane = str(model).split(",")[0].strip()
            on_attempt(lane, DispatchResult(ok=False, rc=rc, text=stdout,
                                            error="", model=lane))
        return rc

    return fake, calls


def _dispatch(fake, tmp_path, model="codex"):
    from mini_ork.dispatch import llm_dispatch as mod
    return mod.llm_dispatch(
        ["--node-type", "researcher", "--prompt-text", "p",
         "--out", str(tmp_path / "out.txt"), "--model", model,
         "--timeout", "30", "--max-turns", "4"],
        root=str(tmp_path), dispatch_fn=fake)


def _rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT status, error_category, retryable, error_message, metadata_json"
            " FROM llm_calls ORDER BY id").fetchall()]
    finally:
        con.close()


# 6. classified on the full text
def test_per_attempt_row_classified_on_the_full_text(hermetic, tmp_path):
    long_error = CODEX_HEAD + " " + "unclassifiable codex chatter. " * 15 + QUOTA_TEXT
    fake, _calls = _make_fake([(1, long_error, "")])
    assert _dispatch(fake, tmp_path) != 0
    attempts = [r for r in _rows(hermetic)
                if "dispatch_attempt" in (r["metadata_json"] or "")]
    assert attempts
    for row in attempts:
        assert row["error_category"] == "quota"
        assert json.loads(row["metadata_json"])["failure_class"] == "quota"
        assert len(row["error_message"] or "") <= 200
        assert "Token Plan" not in (row["error_message"] or "")
        assert str(row["retryable"]) == "0"


# 7. rate-limit hint under the cap → sleep the hint, retry
def test_rate_limit_hint_under_cap_sleeps_and_retries(hermetic, tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("mini_ork.dispatch.llm_dispatch.time.sleep", sleeps.append)
    fake, calls = _make_fake([
        (4, RATE_LIMIT_TEXT + " — Retry-After: 2", ""),
        (0, "", "ok"),
    ])
    assert _dispatch(fake, tmp_path) == 0
    assert len(calls) == 2
    assert any(abs(float(s) - 2.0) <= 0.5 for s in sleeps), sleeps


# 8. hint above the cap → no retry of the chain, log line
def test_hint_above_cap_does_not_retry(hermetic, tmp_path, capsys):
    fake, calls = _make_fake([
        (4, RATE_LIMIT_TEXT + " — try again in 10 minutes", ""),
    ])
    assert _dispatch(fake, tmp_path) != 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "asks to wait" in err and "not retrying" in err
    row = [r for r in _rows(hermetic) if r["status"] == "failed"][-1]
    assert row["error_category"] == "capacity"
    meta = json.loads(row["metadata_json"])
    assert meta["failure_class"] == "rate_limit"
    assert float(meta["wait_hint_s"]) == 600.0


# 9. model prose on stdout may not fuse a lane (RB-10)
def test_stdout_prose_never_classifies_auth(hermetic, tmp_path):
    fake, _calls = _make_fake_stdout(
        "I checked: the API returned 401 unauthorized.")
    assert _dispatch(fake, tmp_path) != 0
    rows = _rows(hermetic)
    assert rows
    assert all(r["error_category"] != "auth" for r in rows)
    attempts = [r for r in rows
                if "dispatch_attempt" in (r["metadata_json"] or "")]
    assert attempts
    for row in attempts:
        assert json.loads(row["metadata_json"])["failure_class"] == "unknown"


# 10. a structured provider rejection on stdout may still promote
def test_stdout_structured_model_not_found_promotes(hermetic, tmp_path):
    fake, _calls = _make_fake_stdout(
        '{"error":{"code":"model_not_found",'
        '"message":"The model `gpt-x` does not exist"}}')
    assert _dispatch(fake, tmp_path) != 0
    attempts = [r for r in _rows(hermetic)
                if "dispatch_attempt" in (r["metadata_json"] or "")]
    assert attempts
    for row in attempts:
        assert json.loads(row["metadata_json"])["failure_class"] == "model_access"


# 11. a non-retryable class does not re-run the chain
def test_non_retryable_timeout_calls_the_chain_once(hermetic, tmp_path):
    fake, calls = _make_fake([(124, "timeout after 5.0s", "")])
    assert _dispatch(fake, tmp_path) != 0
    assert len(calls) == 1
