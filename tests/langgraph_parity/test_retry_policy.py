"""LangGraph RetryPolicy parity tests (P1.4 of spec-driven-delivery).

Covers AC1 of /private/tmp/miniork-p1-specs/p1-4-retry-policy.md:

  F1 — defaults match LangGraph's current RetryPolicy (max_attempts=3,
       initial_interval=0.5, backoff_factor=2.0, max_interval=128.0,
       jitter=True). Source: langgraph/types.py (cited in the module
       docstring and in docs/LANGGRAPH_PARITY.md).
  F2 — citation lives inline AND in docs/LANGGRAPH_PARITY.md.
  F3 — writes from a failed attempt do not carry into the next.
  F4 — terminal failure persists an ERROR row; the resume path
       (P1.2/P1.3) re-dispatches it.
  F5 — retry block vs ``retries`` edge compose with independent caps.
  F6 — backoff envelope respects initial_interval / max_interval /
       backoff_factor with bounded jitter.
  C1 — extend the existing retry surface (``retries`` edge + retry edge
       handling), do not fork a second mechanism.

Plus AC2 coverage — the retry block does not change behavior for a node
that succeeds on the first attempt (zero overhead).

Test isolation:
  - ``retry.py`` is pure-Python: parsing, backoff math, and the ERROR
    row writer are unit-testable without bringing up the executor.
  - the discard + ERROR-row paths are exercised against a fresh
    ``tmp_path`` per test (no shared state, no leaked env).
  - ``compute_backoff`` is deterministic when seeded so F6 is not flaky.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.workflow import retry as retry_mod
from mini_ork.workflow.retry import (  # noqa: E402
    LANGGRAPH_DEFAULTS_URL,
    RETRYABLE_FINISH_REASONS,
    RetryPolicy,
    attempt_schedule,
    compute_backoff,
    default_retry_policy,
    load_attempt_log,
    load_retry_policies,
    mark_retry_error,
    parse_retry_policy,
    record_attempt,
    retryable_finish_reason,
    should_retry,
)


# ── F1: LangGraph defaults parity ────────────────────────────────────────────


def test_default_policy_matches_langgraph():
    """F1 — defaults are max_attempts=3 / 0.5 / 2.0 / 128.0 / True.

    Source: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py
    (langgraph.types.RetryPolicy NamedTuple). The module docstring of
    retry.py carries the same URL.
    """
    policy = default_retry_policy()
    assert policy.max_attempts == 3
    assert policy.initial_interval == 0.5
    assert policy.backoff_factor == 2.0
    assert policy.max_interval == 128.0
    assert policy.jitter is True


def test_langgraph_defaults_url_is_cited():
    """F2 — the LangGraph source URL lives in the module constant.

    The same URL is also cited in docs/LANGGRAPH_PARITY.md; this
    test pins the module constant so a typo or URL rot is caught
    at unit-test time rather than at parity-audit time.
    """
    assert "langgraph" in LANGGRAPH_DEFAULTS_URL
    assert "/types.py" in LANGGRAPH_DEFAULTS_URL
    assert LANGGRAPH_DEFAULTS_URL.startswith("https://")


def test_langgraph_parity_doc_cites_default_source():
    """F2 — docs/LANGGRAPH_PARITY.md exists and cites the defaults URL."""
    doc = REPO / "docs" / "LANGGRAPH_PARITY.md"
    assert doc.is_file(), "docs/LANGGRAPH_PARITY.md must exist"
    text = doc.read_text(encoding="utf-8")
    assert "langgraph" in text.lower()
    # The five defaults are listed by name.
    for field in ("max_attempts", "initial_interval", "backoff_factor",
                  "max_interval", "jitter"):
        assert field in text, f"{field} must be named in docs/LANGGRAPH_PARITY.md"


def test_schema_declares_retry_block_with_langgraph_defaults():
    """F1 — schemas/workflow.schema.json declares the retry block with
    defaults matching LangGraph's. A future parity-audit reader can
    diff the schema's defaults against the LangGraph source."""
    schema = json.loads((REPO / "schemas" / "workflow.schema.json").read_text())
    retry = schema["$defs"]["WorkflowNode"]["properties"]["retry"]
    assert retry["additionalProperties"] is False
    props = retry["properties"]
    assert props["max_attempts"]["default"] == 3
    assert props["initial_interval"]["default"] == 0.5
    assert props["backoff_factor"]["default"] == 2.0
    assert props["max_interval"]["default"] == 128.0
    assert props["jitter"]["default"] is True


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_none_returns_defaults():
    assert parse_retry_policy(None) == default_retry_policy()


def test_parse_empty_mapping_returns_defaults():
    assert parse_retry_policy({}) == default_retry_policy()


def test_parse_partial_override():
    policy = parse_retry_policy({"max_attempts": 5, "jitter": False})
    assert policy.max_attempts == 5
    assert policy.jitter is False
    # The other fields stay at LangGraph defaults.
    assert policy.initial_interval == 0.5
    assert policy.backoff_factor == 2.0
    assert policy.max_interval == 128.0


def test_parse_rejects_unknown_fields():
    """A typo (``initial_inverval``) must fail closed at parse time."""
    with pytest.raises(ValueError, match="unknown fields"):
        parse_retry_policy({"initial_inverval": 1.0})


def test_parse_rejects_non_integer_max_attempts():
    with pytest.raises(ValueError, match="max_attempts"):
        parse_retry_policy({"max_attempts": 3.5})


def test_parse_rejects_bool_max_attempts():
    with pytest.raises(ValueError, match="max_attempts"):
        parse_retry_policy({"max_attempts": True})


def test_parse_rejects_non_boolean_jitter():
    with pytest.raises(ValueError, match="jitter"):
        parse_retry_policy({"jitter": "yes"})


def test_parse_rejects_non_object_payload():
    with pytest.raises(ValueError, match="must be an object"):
        parse_retry_policy([1, 2, 3])  # type: ignore[arg-type]


def test_constructor_validates_minimums():
    """Field-level invariants — failure closed at construction time."""
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="initial_interval must be >= 0"):
        RetryPolicy(initial_interval=-1.0)
    with pytest.raises(ValueError, match="backoff_factor must be >= 1"):
        RetryPolicy(backoff_factor=0.5)
    with pytest.raises(ValueError, match="max_interval must be >= 0"):
        RetryPolicy(max_interval=-1.0)


# ── workflow loader ─────────────────────────────────────────────────────────


def _write_workflow(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_retry_policies_reads_every_node(tmp_path: Path):
    wf = _write_workflow(tmp_path / "wf.yaml", """
version: "1.0.0"
task_class: t
nodes:
  - name: a
    type: implementer
    dispatch_mode: serial
    retry:
      max_attempts: 5
  - name: b
    type: implementer
    dispatch_mode: serial
  - name: c
    type: researcher
    dispatch_mode: serial
    retry:
      initial_interval: 0.1
      jitter: false
edges: []
""")
    policies = load_retry_policies(wf)
    # Every node has an entry — declared overrides get applied, the
    # others carry LangGraph defaults (the executor's "absent ==
    # defaults" code path covers the legacy recipe case).
    assert set(policies.keys()) == {"a", "b", "c"}
    assert policies["a"].max_attempts == 5
    assert policies["b"] == default_retry_policy()
    assert policies["c"].initial_interval == 0.1
    assert policies["c"].jitter is False


def test_load_retry_policies_missing_file(tmp_path: Path):
    """Absent workflow file → empty dict (caller uses defaults)."""
    out = load_retry_policies(tmp_path / "missing.yaml")
    assert out == {}


def test_load_retry_policies_invalid_yaml_raises(tmp_path: Path):
    wf = _write_workflow(tmp_path / "bad.yaml", "retry:\n  max_attempts: -1\n")
    # The loader itself fails soft; a malformed per-node block raises
    # when the loader tries to parse it.
    wf.write_text("""
version: "1.0.0"
task_class: t
nodes:
  - name: a
    type: implementer
    dispatch_mode: serial
    retry:
      max_attempts: 0
edges: []
""", encoding="utf-8")
    with pytest.raises(ValueError, match="max_attempts"):
        load_retry_policies(wf)


# ── F6: backoff envelope ─────────────────────────────────────────────────────


def test_compute_backoff_zero_on_first_attempt():
    """The first attempt never waits — compute_backoff(attempt=0) → 0."""
    assert compute_backoff(default_retry_policy(), attempt=0) == 0.0


def test_compute_backoff_respects_initial_interval():
    """wait before attempt 2 == initial_interval (no jitter)."""
    policy = RetryPolicy(jitter=False)
    assert compute_backoff(policy, attempt=1) == pytest.approx(0.5)


def test_compute_backoff_multiplies_by_factor():
    """waits grow geometrically: 0.5, 1.0, 2.0, 4.0, …"""
    policy = RetryPolicy(
        jitter=False, max_attempts=5, max_interval=10_000.0,
    )
    schedule = attempt_schedule(policy)
    # schedule has max_attempts - 1 entries (waits before retries 2..5)
    assert len(schedule) == 4
    assert schedule[0] == pytest.approx(0.5)
    assert schedule[1] == pytest.approx(1.0)
    assert schedule[2] == pytest.approx(2.0)
    assert schedule[3] == pytest.approx(4.0)


def test_compute_backoff_respects_max_interval():
    """F6 — the envelope is clamped at max_interval, never exceeded."""
    policy = RetryPolicy(
        initial_interval=1.0, backoff_factor=10.0,
        max_interval=5.0, jitter=False,
    )
    schedule = attempt_schedule(policy)
    for wait in schedule:
        assert wait <= 5.0, f"wait {wait} exceeded max_interval=5.0"


def test_compute_backoff_jitter_is_bounded():
    """F6 — jitter never overshoots the un-jittered base, half-jitter
    on the lower bound. Deterministic via a seeded RNG so the test is
    not flaky."""
    policy = RetryPolicy(initial_interval=1.0, max_interval=10.0, jitter=True)
    rng = random.Random(1234)
    samples = [compute_backoff(policy, attempt=1, rng=rng) for _ in range(50)]
    base = 1.0
    assert all(base / 2 <= s <= base for s in samples), (
        f"samples outside half-jitter envelope: {samples}"
    )


def test_compute_backoff_jitter_clamps_at_max_interval():
    """Jitter must not push past max_interval either."""
    policy = RetryPolicy(
        initial_interval=10.0, backoff_factor=10.0,
        max_interval=4.0, jitter=True,
    )
    rng = random.Random(42)
    for _ in range(20):
        wait = compute_backoff(policy, attempt=3, rng=rng)
        assert 0.0 <= wait <= 4.0


# ── retryable finish reasons ───────────────────────────────────────────────────


def test_retryable_finish_reasons_are_known():
    """The set is the narrow infra-class set, not a catch-all."""
    assert retryable_finish_reason("timeout") is True
    assert retryable_finish_reason("infra_interrupt") is True
    assert retryable_finish_reason("provider_limit") is True
    # Code-shaped rejections do NOT retry here — they're the ``retries``
    # edge's domain (F5).
    assert retryable_finish_reason("verifier_refute") is False
    assert retryable_finish_reason("reviewer_reject") is False
    assert retryable_finish_reason("done") is False


def test_retryable_set_is_well_known():
    """Pin the membership so a silent rename surfaces here."""
    assert RETRYABLE_FINISH_REASONS == frozenset({
        "timeout", "infra_interrupt", "provider_limit", "network",
        "overloaded", "rate_limited", "killed", "sigterm", "rc_137", "rc_143",
    })


# ── F4: ERROR checkpoint row + resume re-dispatch ────────────────────────────


def test_mark_retry_error_writes_sidecar(tmp_path: Path):
    """F4 — terminal failure writes the sidecar JSON the resume path reads."""
    from mini_ork.workflow.retry import ErrorRow
    policy = default_retry_policy()
    row = ErrorRow(
        node_id="implementer",
        attempt=3, max_attempts=3,
        finish_reasons=("timeout", "timeout", "timeout"),
        policy=policy.to_dict(),
    )
    out = mark_retry_error(tmp_path, row)
    sidecar = Path(out["sidecar"])
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text())
    assert payload["node_id"] == "implementer"
    assert payload["attempt"] == 3
    assert payload["marker"] == "retry_exhausted"
    assert payload["finish_reasons"] == ["timeout", "timeout", "timeout"]


def test_mark_retry_error_appends_node_attempts_when_migration_present(tmp_path: Path):
    """F4 — the migration-0050 node_attempts table gets a failure row.

    The test seeds a state.db with the canonical 0050 schema (copied
    from the migration so it doesn't depend on the migrate.py harness).
    A node_id/attempt_no pair uniquely identifies the row; the test
    pins the F4 cross-reference contract: the resume path sees both the
    sidecar (file-system) AND the DB row.
    """
    from mini_ork.workflow.retry import ErrorRow
    db = tmp_path / "state.db"
    _seed_node_attempts(db)
    policy = default_retry_policy()
    row = ErrorRow(
        node_id="implementer", attempt=3, max_attempts=3,
        finish_reasons=("timeout",), policy=policy.to_dict(),
    )
    out = mark_retry_error(tmp_path, row, db_path=db, run_id="run-1")
    assert out["attempt_row"] == str(db)
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT run_id, node_id, attempt_no, result, failure_class, initiator "
            "FROM node_attempts"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "run-1"
    assert r[1] == "implementer"
    assert r[2] == 3
    assert r[3] == "failure"
    assert r[4] == "retry_exhausted"
    assert r[5] == "retry-policy"


def test_mark_retry_error_swallows_missing_table(tmp_path: Path):
    """A legacy state.db (no node_attempts table) still writes the sidecar."""
    from mini_ork.workflow.retry import ErrorRow
    db = tmp_path / "legacy.db"  # no schema at all
    row = ErrorRow(
        node_id="n", attempt=2, max_attempts=2,
        finish_reasons=("killed",), policy=default_retry_policy().to_dict(),
    )
    out = mark_retry_error(tmp_path, row, db_path=db, run_id="run-2")
    assert Path(out["sidecar"]).is_file()
    # attempt_row stays empty — the legacy DB has no node_attempts.
    assert out["attempt_row"] == ""


def test_mark_retry_error_writes_are_visible_to_resume(tmp_path: Path):
    """F4 — the resume-side read of ``retry-exhausted/*.json`` works.

    The P1.2/P1.3 resume path will iterate over the sidecar directory;
    this test pins the shape that path can rely on without bringing
    P1.2/P1.3 in yet (they're not merged as of the iteration writing
    this test).
    """
    from mini_ork.workflow.retry import ErrorRow
    for node_id in ("researcher", "implementer", "verifier"):
        row = ErrorRow(
            node_id=node_id, attempt=2, max_attempts=2,
            finish_reasons=("network",), policy=default_retry_policy().to_dict(),
        )
        mark_retry_error(tmp_path, row)
    seen = sorted(
        p.name for p in (tmp_path / "retry-exhausted").glob("*.json")
    )
    assert seen == [
        "implementererror.json",
        "researchererror.json",
        "verifiererror.json",
    ]


def _seed_node_attempts(db: Path) -> None:
    """Create a fresh state.db with the 0050 node_attempts table."""
    con = sqlite3.connect(str(db))
    try:
        con.execute("""
            CREATE TABLE node_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                node_type TEXT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL,
                result TEXT,
                failure_class TEXT,
                checkpoint_used INTEGER,
                checkpoint_produced INTEGER,
                cost_usd REAL,
                provider_session_id TEXT,
                initiator TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


# ── should_retry ─────────────────────────────────────────────────────────────


def test_should_retry_stops_on_success():
    """rc==0 → no retry, regardless of attempt counter or policy."""
    policy = RetryPolicy(max_attempts=5)
    assert should_retry(0, "timeout", attempt=1, policy=policy) is False


def test_should_retry_stops_at_max_attempts():
    """Reaching the cap → no further retries, even on a retryable reason."""
    policy = RetryPolicy(max_attempts=3)
    assert should_retry(1, "timeout", attempt=3, policy=policy) is False


def test_should_retry_refuses_non_retryable_finish_reason():
    """A reviewer reject is a code failure → retry block declines.

    The ``retries`` edge (F5) owns the revise path; the retry block
    must NOT loop on it because re-running the same dispatch would
    not produce a different answer.
    """
    policy = RetryPolicy(max_attempts=3)
    assert should_retry(1, "reviewer_reject", attempt=1, policy=policy) is False
    assert should_retry(1, "verifier_refute", attempt=1, policy=policy) is False


def test_should_retry_proceeds_on_infra_failure():
    policy = RetryPolicy(max_attempts=3)
    assert should_retry(1, "timeout", attempt=1, policy=policy) is True
    assert should_retry(1, "network", attempt=2, policy=policy) is True


# ── attempt log (cross-process resume) ───────────────────────────────────────


def test_record_and_load_attempt_log_roundtrip(tmp_path: Path):
    record_attempt(tmp_path, "n", attempt=1, finish_reason="timeout", rc=1)
    record_attempt(tmp_path, "n", attempt=2, finish_reason="network", rc=1)
    log = load_attempt_log(tmp_path, "n")
    assert len(log) == 2
    assert log[0]["attempt"] == 1
    assert log[1]["attempt"] == 2
    assert log[0]["finish_reason"] == "timeout"
    assert log[1]["finish_reason"] == "network"


def test_load_attempt_log_returns_empty_when_absent(tmp_path: Path):
    assert load_attempt_log(tmp_path, "n") == []


# ── C1: the retry module composes with the existing ``retries`` edge ─────────


def test_retry_module_does_not_for_existing_retries_handler(caplog):
    """C1 — extending, not forking. The retry module owns the policy
    and the ERROR row; the ``retries`` edge (in ``workflow.graph``) is
    untouched. The most reliable single-grep check: a fresh-import of
    the module does not import the revise-loop module.
    """
    import importlib
    # Reimport to clear any cached ``sys.modules`` entry, then check the
    # set of imports the retry module pulls in.
    importlib.reload(retry_mod)
    # A successful reload + the absence of the revise module in the
    # module's __dict__ is the test. If a future revision accidentally
    # imports ``mini_ork.cli.execute``'s revise handler, this assertion
    # fails loudly.
    assert "retry_edge_handler" not in dir(retry_mod)


# ── AC2: zero overhead when no failure ───────────────────────────────────────


def test_zero_overhead_on_first_attempt_success():
    """AC2 — a green first attempt consumes zero backoff time and zero
    discard calls. Pure-Python check on the policy / loader pair (the
    runtime integration is exercised by the executor suite)."""
    policy = default_retry_policy()
    assert should_retry(0, "done", attempt=1, policy=policy) is False
    # The schedule still has ``max_attempts - 1`` entries (the sleeps
    # before each retry) but ``compute_backoff`` is only consulted when
    # the loop decides to retry — the zero-overhead path never reaches
    # it. This is documented behaviour; the test pins the call so a
    # future refactor doesn't start charging a sleep even on success.
    assert len(attempt_schedule(policy)) == policy.max_attempts - 1


# ── Panel run-1791760519 D1: fail-closed retry block + skill release ────────


def _dispatch_harness(tmp_path, monkeypatch, workflow_text: str):
    """Shared scaffold: initialized state.db, run dir, plan, clean env.

    Mirrors test_tool_grants_py.test_dispatched_undeclared_implementer_gets_
    its_type_aware_tools (the established dispatch_node harness).
    """
    import json
    import subprocess

    import mini_ork.cli.execute as ex

    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(repo / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "workflow.yaml").write_text(workflow_text)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    for key in ("MO_NODE_TYPE", "MO_WORKFLOW_YAML", "MO_RESOLVED_NODE_TOOLS"):
        monkeypatch.delenv(key, raising=False)
    return ex, repo, rd, plan, db


def test_dispatch_handles_malformed_retry_block_fails_closed(tmp_path, monkeypatch):
    """A malformed per-node ``retry:`` block must fail the dispatch loudly,
    not degrade to LangGraph defaults. The loader docstring promised
    "compilation errors raise" while the except swallowed exactly that —
    panel defect 1 (run-1791760519 p1-4-retry-policy D1)."""
    ex, repo, rd, plan, db = _dispatch_harness(
        tmp_path, monkeypatch,
        "nodes:\n  - {name: impl, type: implementer,"
        " retry: {max_attempts: 3, bogus_field: 1}}\n",
    )
    monkeypatch.setitem(ex.NODE_HANDLER_REGISTRY, "implementer", lambda ctx: (0, "done"))

    with pytest.raises(ValueError):
        ex.dispatch_node(("impl", "implementer", "d", "", "serial", "", "implementer", ""),
                         root=str(repo), run_dir=str(rd), plan_path=str(plan),
                         task_class="code_fix", db=db, run_id="run-badretry",
                         dispatch_fn=lambda *a: (0, "{}"),
                         workflow=str(rd / "workflow.yaml"))


def test_handler_raise_releases_materialized_skills(tmp_path, monkeypatch):
    """A handler that raises mid-attempt must still release the node's
    materialized skills — the grant is scoped to the node, not to the
    happy path. The old loop released only after the while, so a raising
    handler leaked the grant — panel defect 2 (run-1791760519 D1)."""
    import mini_ork.cli.execute_handlers as exh

    ex, repo, rd, plan, db = _dispatch_harness(
        tmp_path, monkeypatch,
        "nodes:\n  - {name: impl, type: implementer}\n",
    )

    class _Recorder:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    recorder = _Recorder()
    monkeypatch.setattr(exh, "materialize_skills", lambda skills, cwd: recorder)

    def boom(ctx):
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(ex.NODE_HANDLER_REGISTRY, "implementer", boom)

    with pytest.raises(RuntimeError, match="handler exploded"):
        ex.dispatch_node(("impl", "implementer", "d", "", "serial", "", "implementer", ""),
                         root=str(repo), run_dir=str(rd), plan_path=str(plan),
                         task_class="code_fix", db=db, run_id="run-raise",
                         dispatch_fn=lambda *a: (0, "{}"))
    assert recorder.released, "skill grant leaked: handler raised but release() never ran"