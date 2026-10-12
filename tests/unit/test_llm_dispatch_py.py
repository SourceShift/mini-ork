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
    # Account-plan limit wording with no 429 at all (the codex CLI prints this
    # verbatim on turn.failed; run-1791619207-22518) is still a dead lane.
    ("You've hit your usage limit. Upgrade to Plus to continue using Codex, "
     "or try again at Nov 8th, 2026 10:19 AM.", "", "quota"),
    ("insufficient credits — purchase credits to continue", "", "quota"),
    # Weak quota words stay gated behind a 429.
    ("monthly billing summary attached", "", "unknown"),
    ("429 token plan", "", "quota"),
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
    # The circuit reads the llm_calls ledger (every provider call, node or
    # stage), not task_runs.cost_usd — seed the spend where it is metered.
    _sql(db_p, "INSERT INTO llm_calls (provider,model_id,tier,feature_name,cost_usd,status) "
               "VALUES ('anthropic','sonnet','default','mini-ork:worker',100.0,'success');")
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
                       "MO_FUSE_ENABLED": "1", "MO_DAILY_BUDGET_USD": "1000",
                       # capacity is a TRANSIENT category: watched only on the
                       # opt-in, because halting on one is the false positive
                       # the default set avoids.
                       "MO_FUSE_TRANSIENT": "1"})
    try:
        # A one-lane chain: the fuse refuses only when NOTHING survives the drop.
        rp = ld.llm_dispatch(["--node-type", "planner", "--model", "sonnet",
                              "--prompt-text", "hi"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 43


def test_lane_fuse_narrows_a_chain_instead_of_refusing(tmp_path):
    """A fused lane in the middle of a chain is DROPPED, and the rest run.

    Refusing only when every lane is dead is the point: a quota-walled tail lane
    would otherwise cost a full attempt-timeout on every dispatch before the walk
    abandoned it — the stall the fallback chain exists to prevent.
    """
    hp, db_p = _seed(tmp_path, "fpn")
    for _ in range(3):
        _sql(db_p, "INSERT INTO llm_calls (provider,model_id,tier,feature_name,input_tokens,"
                   "output_tokens,total_tokens,cost_usd,duration_ms,status,metadata_json,"
                   "error_category,retryable) VALUES ('gateway','dead-lane','default',"
                   "'mini-ork:planner',0,0,0,0,0,'failed','{}','capacity',1);")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_FUSE_ENABLED": "1", "MO_FUSE_TRANSIENT": "1",
                       "MO_DAILY_BUDGET_USD": "1000", "MO_DISPATCH_RETRY_BASE_S": "0",
                       "MO_DISPATCH_RETRY_MAX_SLEEP_S": "1"})
    try:
        rp = ld.llm_dispatch(["--node-type", "planner",
                              "--model", "dead-lane,no-such-lane-b",
                              "--prompt-text", "hi", "--timeout", "5"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    # The chain still ran (and failed on the lane that was actually alive), so
    # it was NOT refused for the dead lane's sake.
    assert rp != 43


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


# ── failed-attempt / refusal ledger rows (run-1791619207-22518 triage gap) ──

def test_cost_circuit_refusal_row(tmp_path):
    """rc=42 used to leave the ledger empty — triage could not tell a budget
    refusal from a crash. Now one :refused row, zero cost, retryable=0."""
    hp, db_p = _seed(tmp_path, "cpr")
    _sql(db_p, "INSERT INTO llm_calls (provider,model_id,tier,feature_name,cost_usd,status) "
               "VALUES ('anthropic','sonnet','default','mini-ork:worker',100.0,'success');")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_DAILY_BUDGET_USD": "50"})
    try:
        rp = ld.llm_dispatch(["--node-type", "planner", "--prompt-text", "hi"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 42
    rows = _sql(db_p, "SELECT feature_name, status, error_category, retryable, cost_usd, "
                      "metadata_json FROM llm_calls WHERE feature_name LIKE '%:refused';").stdout
    assert "mini-ork:planner:refused" in rows
    assert "failed" in rows and "unknown" in rows   # CHECK-constrained vocab; kind below
    assert '"refused": "cost_limit"' in rows        # refusal kind rides in metadata
    parts = rows.split("|")
    assert parts[3].strip() == "0"                  # retryable=0
    assert parts[4].strip() in ("0", "0.0")         # zero cost — nothing ran


def test_lane_fuse_refusal_row(tmp_path):
    import pytest
    hp, db_p = _seed(tmp_path, "fpr")
    cols = _sql(db_p, "PRAGMA table_info(llm_calls);").stdout
    if "error_category" not in cols or "retryable" not in cols:
        pytest.skip("llm_calls lacks error_category/retryable columns")
    for _ in range(3):
        _sql(db_p, "INSERT INTO llm_calls (provider,model_id,tier,feature_name,input_tokens,"
                   "output_tokens,total_tokens,cost_usd,duration_ms,status,metadata_json,"
                   "error_category,retryable) VALUES ('gateway','sonnet','default',"
                   "'mini-ork:planner',0,0,0,0,0,'failed','{}','capacity',1);")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_FUSE_ENABLED": "1", "MO_DAILY_BUDGET_USD": "1000",
                       "MO_FUSE_TRANSIENT": "1"})
    try:
        rp = ld.llm_dispatch(["--node-type", "planner", "--model", "sonnet",
                              "--prompt-text", "hi"], root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 43
    rows = _sql(db_p, "SELECT feature_name, error_category FROM llm_calls "
                      "WHERE feature_name LIKE '%:refused';").stdout
    assert "mini-ork:planner:refused" in rows and "capacity" in rows


def test_multilane_attempt_rows(tmp_path):
    """A fallback chain that fails on every lane writes ONE failed row per
    dead lane (attributed to THAT lane, zero cost) plus the final failure row.
    Before: only the final row — dead lanes existed solely in stream files."""
    hp, db_p = _seed(tmp_path, "ar")
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hp, "MINI_ORK_DB": db_p,
                       "MO_DAILY_BUDGET_USD": "1000", "MO_DISPATCH_RETRY_BASE_S": "0",
                       "MO_DISPATCH_RETRY_MAX_SLEEP_S": "1"})
    try:
        rp = ld.llm_dispatch(
            ["--node-type", "worker", "--prompt-text", "hi",
             "--model", "no-such-lane-a,no-such-lane-b", "--timeout", "5"],
            root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp != 0
    rows = _sql(db_p, "SELECT model_id, feature_name, status, cost_usd, metadata_json "
                      "FROM llm_calls WHERE status='failed' "
                      "AND metadata_json LIKE '%dispatch_attempt%';").stdout
    # one attempt row per dead lane, named for that lane, zero-cost
    assert "no-such-lane-a" in rows and "no-such-lane-b" in rows
    assert "mini-ork:worker" in rows
    parts = [ln.split("|") for ln in rows.strip().splitlines() if ln.strip()]
    assert len(parts) == 2
    for p in parts:
        assert float(p[3]) == 0.0          # cost pinned: spent_last_24h sums it
    # the final failure row is still there, without the attempt marker
    final = _sql(db_p, "SELECT COUNT(*) FROM llm_calls WHERE status='failed' "
                       "AND metadata_json NOT LIKE '%dispatch_attempt%' "
                       "AND feature_name='mini-ork:worker';").stdout.strip()
    assert final == "1"


def test_dispatcher_crash_writes_row(tmp_path, monkeypatch):
    """An exception inside the dispatcher escaped to the caller with NO ledger
    row (the caller maps any exception to rc=1). Now: one failed row naming the
    crash, then the exception propagates unchanged."""
    import pytest
    hp, db_p = _seed(tmp_path, "cr")
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("MINI_ORK_HOME", hp)
    monkeypatch.setenv("MINI_ORK_DB", db_p)
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "1000")

    def _boom(*a, **k):
        raise RuntimeError("kaboom mid-dispatch")

    monkeypatch.setattr(ld, "mo_llm_dispatch", _boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        ld.llm_dispatch(["--node-type", "worker", "--prompt-text", "hi",
                         "--model", "glm"], root=str(REPO))
    rows = _sql(db_p, "SELECT status, error_message FROM llm_calls "
                      "WHERE feature_name='mini-ork:worker';").stdout
    assert "failed" in rows and "dispatcher crashed: kaboom" in rows


def test_fallback_on_attempt_observer(monkeypatch):
    """dispatch_with_fallback reports every lane attempt to on_attempt (the
    winner too), and an observer exception never breaks the walk."""
    from mini_ork.dispatch import providers as prov
    from mini_ork.dispatch.models import DispatchRequest, DispatchResult

    def fake_dispatch_model(req, root=None, **kw):
        if req.model == "dead-lane":
            return DispatchResult(ok=False, rc=1, error="429 capacity", model=req.model)
        return DispatchResult(ok=True, rc=0, text="served", model=req.model)

    monkeypatch.setattr(prov, "dispatch_model", fake_dispatch_model)
    seen = []

    def observer(lane, result):
        seen.append((lane, result.ok))
        if lane == "dead-lane":
            raise ValueError("observer bug must not break dispatch")

    res = prov.dispatch_with_fallback(
        DispatchRequest(model="dead-lane", prompt="p", timeout_s=1),
        ["dead-lane", "live-lane"], on_attempt=observer)
    assert res.ok and res.text == "served"
    assert seen == [("dead-lane", False), ("live-lane", True)]
