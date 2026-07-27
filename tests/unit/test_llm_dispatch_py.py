"""Unit tests: mini_ork.dispatch.llm_dispatch (bash parity halves removed; formerly vs lib/llm-dispatch.sh).

The provider invocation is already ported+tested in mini_ork.dispatch; here we
test the WRAPPER + pure helpers that remained in bash. The deterministic gates
in the llm_dispatch shim — cost circuit (rc 42), lane fuse (rc 43), lane
resolution — are driven through the port. The retry/backoff loop is exercised
with an injected stub dispatch (no real LLM).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch import llm_dispatch as ld


# ── pure helpers ──

_CLASSIFY_CASES = [
    ("HTTP 401 unauthorized", "", "auth"),
    ("rate limit 429 capacity exceeded", "", "capacity"),
    ("429 monthly quota exceeded", "", "quota"),
    ("400 invalid request prompt too long", "", "request"),
    ("422 content filter", "", "safety"),
    ("connection refused", "", "network"),
    ("partial stream closed", "", "stream"),
    ("500 internal server error", "", "provider"),
    ("something weird", "", "unknown"),
    ("boom", "7", "network"),       # curl rc 7 = failed connect
    ("boom", "28", "network"),      # curl rc 28 = timeout
    ("503 overload", "", "capacity"),
    ("bearer token", "", "unknown"),
    ("no providers.yaml entry", "", "config"),
]


def test_classify_error():
    for msg, rc, expect in _CLASSIFY_CASES:
        assert ld.classify_error(msg, rc) == expect, f"classify({msg!r},{rc!r})"


def test_error_retryable():
    for cat in ("capacity", "network", "stream", "provider"):
        assert ld.error_retryable(cat) == "1", cat
    for cat in ("quota", "auth", "unknown"):
        assert ld.error_retryable(cat) == "0", cat


def test_provider_for_model():
    expect = {
        "codex": "openai", "gpt-4o": "openai", "o1-mini": "openai",
        "gemini": "google", "x-gemini-2": "google",
        "minimax": "gateway", "glm": "gateway", "kimi": "gateway",
        "deepseek": "gateway",
        "opus": "anthropic", "sonnet": "anthropic", "weird-lane": "anthropic",
    }
    for m, provider in expect.items():
        assert ld.provider_for_model(m) == provider, m


def test_redact_secrets():
    cases = [
        ("ANTHROPIC_API_KEY=abc123secretvalue here", "ANTHROPIC_API_KEY=[REDACTED] here"),
        ("Bearer aXbYcZ12345678 done", "Bearer [REDACTED] done"),
        ("sk-ant-abc12345678901234567890 tail", "[REDACTED_KEY] tail"),
        ("hash deadbeefdeadbeefdeadbeefdeadbeef end", "hash [REDACTED_HEX] end"),
        ("nothing to redact", "nothing to redact"),
    ]
    for s, expect in cases:
        assert ld.redact_secrets(s) == expect, s


def test_strip_protocol_blocks(tmp_path):
    cases = [
        ('body text\n<z-insight>{"a":1}</z-insight>\ntail\n', 'body text\n\ntail\n'),
        ('clean output no insight\n', 'clean output no insight\n'),
        ('truncated <z-insight>{"unterminated": ', 'truncated\n'),
    ]
    for content, expect in cases:
        fp = tmp_path / "p.txt"
        fp.write_text(content)
        ld.strip_protocol_blocks(str(fp))
        assert fp.read_text() == expect, repr(content)


def test_retryable_predicates():
    # throttle_retryable: capacity is retryable while attempt < max
    assert ld.throttle_retryable("sonnet", "429 capacity overload", "", 1, 3) is True
    # exhausted attempts → not retryable
    assert ld.throttle_retryable("sonnet", "429 capacity overload", "", 3, 3) is False
    # glm fair-usage
    assert ld.glm_fair_usage_retryable("glm", "1313 fair usage policy", 1, 3) is True
    assert ld.glm_fair_usage_retryable("sonnet", "1313 fair usage policy", 1, 3) is False


def test_backoff_bounds():
    # jitter is random; assert the base+cap+floor envelope.
    for attempt in (1, 2, 3, 5, 20):
        # fixed jitter=0 gives the base; jitter 0..3, capped at 45
        base = ld.backoff_seconds_raw(attempt, 45, 5, _jitter=0)
        assert 1 <= base <= 45
        capped = ld.backoff_seconds_raw(attempt, 45, 5, _jitter=3)
        assert capped <= 45
    assert ld.backoff_seconds_raw(1, 45, 5, _jitter=0) == 5     # 2^0*5
    assert ld.backoff_seconds_raw(3, 45, 5, _jitter=0) == 20    # 2^2*5
    assert ld.backoff_seconds_raw(20, 45, 5, _jitter=0) == 45   # clamped attempt→12, capped


# ── shim gates: cost circuit + fuse ──

def _seed(tmp, name):
    home = tmp / name / ".mini-ork"; home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return str(home), db


def _sql(db, s):
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def test_cost_circuit(tmp_path):
    hp, db_p = _seed(tmp_path, "cp")
    _sql(db_p, "INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
               "cost_usd,created_at,updated_at) VALUES ('r1','x','v1','k.md','published',100.0,"
               "strftime('%s','now'),strftime('%s','now'));")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_DAILY_BUDGET_USD": "50"})
    try:
        rp = ld.llm_dispatch(["--node-type", "planner", "--prompt-text", "hi"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 42


def test_lane_fuse(tmp_path):
    hp, db_p = _seed(tmp_path, "fp")
    cols = _sql(db_p, "PRAGMA table_info(llm_calls);").stdout
    if "error_category" not in cols or "retryable" not in cols:
        import pytest
        pytest.skip("llm_calls lacks error_category/retryable columns")
    for _ in range(3):
        _sql(db_p, "INSERT INTO llm_calls (provider,model_id,tier,feature_name,input_tokens,"
                   "output_tokens,total_tokens,cost_usd,duration_ms,status,metadata_json,"
                   "error_category,retryable) VALUES ('gateway','sonnet','default',"
                   "'mini-ork:planner',0,0,0,0,0,'failed','{}','capacity',1);")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_FUSE_ENABLED": "1", "MO_DAILY_BUDGET_USD": "1000"})
    try:
        rp = ld.llm_dispatch(["--node-type", "planner", "--prompt-text", "hi"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 43


def test_lane_resolution(tmp_path):
    home = tmp_path / "lr" / ".mini-ork" / "config"; home.mkdir(parents=True)
    (home / "agents.yaml").write_text("lanes:\n  planner: opus\n  worker: sonnet\n")
    root = str(tmp_path / "lr"); hm = str(tmp_path / "lr" / ".mini-ork")
    for nt, expect in [("planner", "opus"), ("implementer", "sonnet"), ("worker", "sonnet")]:
        assert ld.resolve_lane_model(nt, root, hm) == expect, nt


# ── retry loop with injected stub ──

def test_retry_loop_stub(tmp_path, monkeypatch):
    home, db = _seed(tmp_path, "rl")
    calls = {"n": 0}

    def stub(model, prompt, out_file, timeout_s, max_turns):
        calls["n"] += 1
        if calls["n"] < 3:
            open(out_file + ".err.log", "w").write("429 capacity overload temporarily unavailable")
            return 1
        open(out_file, "w").write("OK RESPONSE")
        open(out_file + ".cost", "w").write("0.010000")
        return 0

    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("MO_DISPATCH_RETRY_BASE_S", "0")   # near-zero backoff for test speed
    monkeypatch.setenv("MO_DISPATCH_RETRY_MAX_SLEEP_S", "1")
    rc = ld.llm_dispatch(["--node-type", "planner", "--prompt-text", "hi",
                          "--out", str(tmp_path / "out.txt")], root=str(REPO), dispatch_fn=stub)
    assert rc == 0 and calls["n"] == 3            # failed twice (retried), succeeded on 3rd
    assert "OK RESPONSE" in (tmp_path / "out.txt").read_text()
    # a success llm_calls row was written
    n = _sql(db, "SELECT COUNT(*) FROM llm_calls WHERE status='success';").stdout.strip()
    assert n == "1"
